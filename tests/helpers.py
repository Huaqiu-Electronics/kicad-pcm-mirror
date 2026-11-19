"""测试公共设施：脚本路径、假上游 HTTP 服务、最小可用上游数据。"""

from __future__ import annotations

import io
import sys
import threading
import zipfile
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from manifest import ACCEPT_V1, ACCEPT_V2  # noqa: E402
from verify import sha256_bytes  # noqa: E402

BASE_URL = "https://www.eda.cn/kicad-addons/"


class UpstreamServer:
    """按 (路径, Accept) 返回内容的极简 HTTP 服务。"""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str | None], bytes] = {}
        self.failures: dict[str, int] = {}
        self.hits: Counter[str] = Counter()
        handler = self._handler()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_port}"

    def add(self, path: str, body: bytes, accept: str | None = None) -> None:
        self.routes[(path, accept)] = body

    def remove(self, path: str, accept: str | None = None) -> None:
        self.routes.pop((path, accept), None)

    def fail_next(self, path: str, times: int) -> None:
        self.failures[path] = times

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def _handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server 接口
                path = self.path.split("?")[0]
                outer.hits[path] += 1
                remaining = outer.failures.get(path, 0)
                if remaining > 0:
                    outer.failures[path] = remaining - 1
                    self.send_response(500)
                    self.end_headers()
                    return
                accept = self.headers.get("Accept")
                body = outer.routes.get((path, accept))
                if body is None:
                    body = outer.routes.get((path, None))
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # noqa: D401 - 静默日志
                return

        return Handler


def make_zip(entries: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in (entries or {"metadata.json": b"{}"}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


def make_args(upstream: UpstreamServer, tmp_path: Path, **overrides) -> SimpleNamespace:
    values = {
        "out": str(tmp_path / "dist"),
        "base_url": BASE_URL,
        "upstream": f"{upstream.base}/repository.json",
        "workers": 2,
        "retries": 1,
        "timeout": 10.0,
        "skip_existing": False,
        "limit": 0,
        "image_tag": "test-tag",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def build_upstream(server: UpstreamServer, size_delta: int = 0) -> dict:
    """注册最小上游（2 个插件、1 个 resources），返回构造结果供断言使用。

    ``size_delta`` 用于模拟上游 ``download_size`` 填写错误（内容不变）。
    """

    zip_alpha = make_zip({"metadata.json": b'{"identifier":"com.example.alpha"}'})
    zip_beta = make_zip({"metadata.json": b'{"identifier":"com.example.beta"}'})
    resources = make_zip({"com.example.alpha/icon.png": b"png-bytes"})

    packages_doc = {
        "packages": [
            {
                "$schema": "https://go.kicad.org/pcm/schemas/v1",
                "identifier": "com.example.alpha",
                "name": "Alpha Plugin",
                "description": "alpha",
                "description_full": "alpha full",
                "license": "MIT",
                "type": "plugin",
                "author": {"name": "Alpha Author"},
                "maintainer": {"name": "Alpha Maintainer"},
                "resources": {"Github": "https://github.com/example/alpha"},
                "versions": [
                    {
                        "version": "1.0",
                        "download_url": f"{server.base}/alpha.zip",
                        "download_sha256": sha256_bytes(zip_alpha),
                        "download_size": len(zip_alpha) + size_delta,
                        "install_size": 4096,
                        "kicad_version": "8.0",
                        "kicad_version_max": "10",
                        "platforms": ["linux", "windows"],
                        "status": "stable",
                    }
                ],
            },
            {
                "identifier": "com.example.beta",
                "name": "Beta Theme",
                "description": "beta",
                "description_full": "beta full",
                "license": "CC0-1.0",
                "type": "colortheme",
                "author": {"name": "Beta Author"},
                "maintainer": {"name": "Beta Maintainer"},
                "resources": {},
                "versions": [
                    {
                        "version": "2.0",
                        "download_url": f"{server.base}/beta.zip",
                        "download_sha256": sha256_bytes(zip_beta),
                        "download_size": len(zip_beta) + size_delta,
                        "install_size": 1024,
                        "kicad_version": "5.99",
                        "status": "stable",
                    }
                ],
            },
        ]
    }
    packages_bytes = json_dumps(packages_doc)

    repository_v1 = make_repository(
        server,
        packages_file="/packages-v1.json",
        packages_bytes=packages_bytes,
        resources_bytes=resources,
        schema_version=None,
    )
    repository_v2 = make_repository(
        server,
        packages_file="/packages.json",
        packages_bytes=packages_bytes,
        resources_bytes=resources,
        schema_version=2,
    )

    server.add("/repository.json", json_dumps(repository_v1), ACCEPT_V1)
    server.add("/repository.json", json_dumps(repository_v2), ACCEPT_V2)
    server.add("/packages-v1.json", packages_bytes)
    server.add("/packages.json", packages_bytes)
    server.add("/resources.zip", resources)
    server.add("/alpha.zip", zip_alpha)
    server.add("/beta.zip", zip_beta)

    return {
        "packages_doc": packages_doc,
        "packages_bytes": packages_bytes,
        "resources": resources,
        "zip_alpha": zip_alpha,
        "zip_beta": zip_beta,
        "repository_v1": repository_v1,
        "repository_v2": repository_v2,
        "alpha_name": "com.example.alpha-1.0.zip",
        "beta_name": "com.example.beta-2.0.zip",
    }


def make_repository(
    server: UpstreamServer,
    packages_file: str,
    packages_bytes: bytes,
    resources_bytes: bytes,
    schema_version: int | None,
) -> dict:
    doc = {
        "$schema": "https://go.kicad.org/pcm/schemas/v1#/definitions/Repository",
        "name": "KiCad official repository",
        "maintainer": {
            "name": "KiCad team",
            "contact": {"web": "https://www.kicad.org/about/contact-us/"},
        },
        "packages": {
            "url": server.base + packages_file,
            "sha256": sha256_bytes(packages_bytes),
            "update_time_utc": "2026-11-16 11:02:17",
            "update_timestamp": 1794826937,
        },
        "resources": {
            "url": f"{server.base}/resources.zip",
            "sha256": sha256_bytes(resources_bytes),
            "update_time_utc": "2026-11-16 11:02:17",
            "update_timestamp": 1794826937,
        },
    }
    if schema_version is not None:
        doc["$schema"] = "https://go.kicad.org/pcm/schemas/v2#/definitions/Repository"
        doc["schema_version"] = schema_version
    return doc


def json_dumps(doc: dict) -> bytes:
    import json

    return json.dumps(doc, indent=4).encode("utf-8")
