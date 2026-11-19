import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "docker"
    / "40-rewrite-download-urls.sh"
)
BAKED = "https://eda.cn/kicad-addons/"
MANIFESTS = (
    "repository.v1.json",
    "repository.v2.json",
    "packages-v1.json",
    "packages.json",
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("sh") is None,
    reason="需要 POSIX sh 才能运行启动脚本",
)


@pytest.fixture
def html_root(tmp_path):
    root = tmp_path / "kicad-addons"
    root.mkdir()
    for name in MANIFESTS:
        doc = {"packages": {"url": BAKED + name}}
        (root / name).write_text(json.dumps(doc), encoding="utf-8")
    return root


def run_script(root: Path, base: str | None):
    env = dict(os.environ)
    env["DOWNLOAD_HTML_ROOT"] = str(root)
    env.pop("DOWNLOAD_BASE_URL", None)
    if base is not None:
        env["DOWNLOAD_BASE_URL"] = base
    return subprocess.run(
        ["sh", str(SCRIPT)], env=env, capture_output=True, text=True
    )


def urls(root: Path) -> dict[str, str]:
    return {
        name: json.loads((root / name).read_text(encoding="utf-8"))["packages"][
            "url"
        ]
        for name in MANIFESTS
    }


def test_host_only_base_is_completed(html_root):
    result = run_script(html_root, "http://192.168.1.10:8080")

    assert result.returncode == 0
    for name, url in urls(html_root).items():
        assert url == f"http://192.168.1.10:8080/kicad-addons/{name}"


def test_explicit_path_without_trailing_slash(html_root):
    result = run_script(html_root, "http://mirror.example.com:8080/kicad-addons")

    assert result.returncode == 0
    for name, url in urls(html_root).items():
        assert url == f"http://mirror.example.com:8080/kicad-addons/{name}"


def test_unset_base_keeps_baked_urls(html_root):
    result = run_script(html_root, None)

    assert result.returncode == 0
    for name, url in urls(html_root).items():
        assert url == BAKED + name


def test_invalid_base_aborts_without_modifying(html_root):
    result = run_script(html_root, "192.168.1.10:8080")

    assert result.returncode == 1
    assert "错误" in result.stderr
    for name, url in urls(html_root).items():
        assert url == BAKED + name
