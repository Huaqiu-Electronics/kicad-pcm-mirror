"""KiCad PCM 仓库元数据解析与重写。

设计约束（见规格书 §3.3）：
- 只重写允许变化的 URL 字段，其余字段（含未知字段、键序）必须原样保留；
- 不使用强类型模型，避免 ``$schema`` / ``platforms`` / ``kicad_version_max``
  等字段被静默丢弃；
- 插件文件名必须确定、可重复，并检测同名冲突。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterator

UPSTREAM_REPOSITORY_URL = "https://repository.kicad.org/repository.json"

# 上游按 Accept 返回不同 schema（见 dev-docs.kicad.org/en/addons/）
ACCEPT_V1 = "application/json"
ACCEPT_V2 = "application/vnd.kicad.pcm.v2+json"

DEFAULT_BASE_URL = "https://eda.cn/kicad-addons/"

_MISSING = object()

# 文件名只允许这些字符，防止上游数据里出现路径分隔符
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._+-]+$")


class ManifestError(RuntimeError):
    """元数据结构异常，例如缺少必需字段或文件名冲突。"""


@dataclass(frozen=True)
class Variant:
    key: str
    accept: str
    repository_file: str
    packages_file: str


VARIANTS: tuple[Variant, ...] = (
    Variant("v1", ACCEPT_V1, "repository.v1.json", "packages-v1.json"),
    Variant("v2", ACCEPT_V2, "repository.v2.json", "packages.json"),
)


def normalize_base_url(base_url: str) -> str:
    """统一为以 ``/`` 结尾的形式，保证拼接出的 URL 稳定。"""
    base = base_url.strip()
    if not base:
        raise ManifestError("base url 不能为空")
    if not base.endswith("/"):
        base += "/"
    return base


def loads(raw: bytes | str) -> dict:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    doc = json.loads(raw)
    if not isinstance(doc, dict):
        raise ManifestError("元数据根节点必须是 JSON 对象")
    return doc


def dump_json(doc: dict) -> str:
    """确定性序列化：保留键序，统一缩进与结尾换行。"""
    return json.dumps(doc, indent=4, ensure_ascii=False) + "\n"


def dumps_bytes(doc: dict) -> bytes:
    return dump_json(doc).encode("utf-8")


def artifact_name(identifier: str, version: str) -> str:
    """插件 ZIP 的确定性文件名：``{identifier}-{version}.zip``。"""
    name = f"{identifier}-{version}.zip"
    if not _SAFE_NAME.match(name) or ".." in name:
        raise ManifestError(f"不安全的插件文件名: {name!r}")
    return name


def iter_versions(packages_doc: dict) -> Iterator[tuple[dict, dict]]:
    packages = packages_doc.get("packages")
    if not isinstance(packages, list):
        raise ManifestError("packages.json 缺少 packages 数组")
    for pkg in packages:
        versions = pkg.get("versions")
        if not isinstance(versions, list):
            raise ManifestError(f"插件 {pkg.get('identifier')!r} 缺少 versions 数组")
        for version in versions:
            yield pkg, version


def collect_artifacts(packages_doc: dict) -> dict[str, dict]:
    """汇总所有被引用的 ZIP：文件名 -> 来源、sha256、大小。

    同名但内容不同视为冲突，直接失败，避免互相覆盖。
    """
    artifacts: dict[str, dict] = {}
    for pkg, version in iter_versions(packages_doc):
        name = artifact_name(pkg["identifier"], version["version"])
        sha256 = (version.get("download_sha256") or "").lower() or None
        size = version.get("download_size")
        item = artifacts.get(name)
        if item is None:
            artifacts[name] = {
                "name": name,
                "source_url": version["download_url"],
                "sha256": sha256,
                "size": size,
            }
            continue
        if item["sha256"] != sha256:
            raise ManifestError(
                f"插件文件名冲突且内容不同: {name} "
                f"({item['sha256']} != {sha256})"
            )
        if item["source_url"] != version["download_url"]:
            raise ManifestError(
                f"插件文件名冲突且来源不同: {name} "
                f"({item['source_url']} != {version['download_url']})"
            )
    return artifacts


def drop_versions(packages_doc: dict, names: set[str]) -> int:
    """删除指向失效产物的 version 条目，返回删除条数。

    某个插件若已无任何可用版本，则整个插件条目一并移除，
    避免产出 ``versions: []`` 这种不被 PCM 接受的结构。
    """
    packages = packages_doc.get("packages")
    if not isinstance(packages, list):
        raise ManifestError("packages.json 缺少 packages 数组")
    removed = 0
    kept_packages = []
    for pkg in packages:
        versions = pkg.get("versions")
        if not isinstance(versions, list):
            raise ManifestError(f"插件 {pkg.get('identifier')!r} 缺少 versions 数组")
        kept = [
            version
            for version in versions
            if artifact_name(pkg["identifier"], version["version"]) not in names
        ]
        removed += len(versions) - len(kept)
        if kept:
            pkg["versions"] = kept
            kept_packages.append(pkg)
    packages[:] = kept_packages
    return removed


def rewrite_package_urls(packages_doc: dict, base_url: str) -> int:
    """把每个版本的 download_url 指向镜像地址，返回重写条数。"""
    base = normalize_base_url(base_url)
    count = 0
    for pkg, version in iter_versions(packages_doc):
        version["download_url"] = base + artifact_name(
            pkg["identifier"], version["version"]
        )
        count += 1
    return count


def rewrite_repository(
    repo: dict,
    base_url: str,
    packages_file: str,
    packages_sha256: str,
    resources_file: str,
) -> dict:
    """重写 repository.json 中的元数据引用。

    只改 ``packages.url`` / ``packages.sha256`` / ``resources.url``；
    ``resources.sha256`` 因文件字节未变而保持官方值。
    """
    base = normalize_base_url(base_url)
    for key in ("packages", "resources"):
        if not isinstance(repo.get(key), dict):
            raise ManifestError(f"repository.json 缺少 {key} 节点")
    repo["packages"]["url"] = base + packages_file
    repo["packages"]["sha256"] = packages_sha256
    repo["resources"]["url"] = base + resources_file
    return repo


def diff_paths(
    original: Any, current: Any, allowed: set[tuple], path: tuple = ()
) -> list[tuple[tuple, Any, Any]]:
    """比较两份 JSON，返回不在 ``allowed`` 白名单内的差异。"""
    diffs: list[tuple[tuple, Any, Any]] = []

    if isinstance(original, dict) and isinstance(current, dict):
        for key, value in original.items():
            if key not in current:
                diffs.append((path + (key,), value, _MISSING))
            else:
                diffs.extend(diff_paths(value, current[key], allowed, path + (key,)))
        for key, value in current.items():
            if key not in original:
                diffs.append((path + (key,), _MISSING, value))
    elif isinstance(original, list) and isinstance(current, list):
        if len(original) != len(current):
            diffs.append((path, f"len={len(original)}", f"len={len(current)}"))
        else:
            for index, (left, right) in enumerate(zip(original, current)):
                diffs.extend(diff_paths(left, right, allowed, path + (index,)))
    elif original != current:
        diffs.append((path, original, current))

    return [item for item in diffs if item[0] not in allowed]
