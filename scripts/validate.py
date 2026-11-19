"""快照级校验：Manifest 与 ZIP 必须来自同一次同步且互相自洽。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from manifest import (
    DEFAULT_BASE_URL,
    VARIANTS,
    ManifestError,
    artifact_name,
    diff_paths,
    iter_versions,
    normalize_base_url,
)
from verify import VerifyError, sha256_file, zip_error

REPOSITORY_ALLOWED_PATHS = {
    ("packages", "url"),
    ("packages", "sha256"),
    ("resources", "url"),
}


class SnapshotError(RuntimeError):
    """快照不完整或前后不一致。"""


def _load(path: Path, errors: list[str]) -> dict | None:
    if not path.is_file():
        errors.append(f"缺少文件: {path.name}")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        errors.append(f"无法解析 {path.name}: {exc}")
        return None


def _validate_variant(
    dist: Path,
    base_url: str,
    variant,
    spec: dict | None,
    errors: list[str],
) -> dict[str, dict]:
    """校验单个 variant 的 repository/packages，返回其引用的产物。"""
    repository = _load(dist / variant.repository_file, errors)
    packages = _load(dist / variant.packages_file, errors)
    if repository is None or packages is None:
        return {}

    if spec:
        for path, original, current in diff_paths(
            spec["original_repository"], repository, REPOSITORY_ALLOWED_PATHS
        ):
            errors.append(f"{variant.repository_file} 出现非预期变更: {_fmt(path)}")
        allowed = {
            ("packages", index, "versions", position, "download_url")
            for index, package in enumerate(spec["original_packages"].get("packages", []))
            for position, _ in enumerate(package.get("versions", []))
        }
        for path, original, current in diff_paths(
            spec["original_packages"], packages, allowed
        ):
            errors.append(f"{variant.packages_file} 出现非预期变更: {_fmt(path)}")

    packages_ref = repository.get("packages", {})
    resources_ref = repository.get("resources", {})

    if packages_ref.get("url") != base_url + variant.packages_file:
        errors.append(
            f"{variant.repository_file}: packages.url 未指向镜像 "
            f"({packages_ref.get('url')!r})"
        )
    expected_packages_sha = sha256_file(dist / variant.packages_file)
    if (packages_ref.get("sha256") or "").lower() != expected_packages_sha:
        errors.append(f"{variant.repository_file}: packages.sha256 与文件不一致")

    resources_url = resources_ref.get("url") or ""
    if not resources_url.startswith(base_url):
        errors.append(f"{variant.repository_file}: resources.url 未指向镜像")
    resources_name = resources_url.rsplit("/", 1)[-1]
    resources_path = dist / resources_name
    referenced: dict[str, dict] = {}
    if not resources_name or not resources_path.is_file():
        errors.append(f"{variant.repository_file}: 缺少 resources 文件 {resources_name!r}")
    else:
        actual = sha256_file(resources_path)
        if (resources_ref.get("sha256") or "").lower() != actual:
            errors.append(f"{variant.repository_file}: resources.sha256 与文件不一致")
        problem = zip_error(resources_path)
        if problem:
            errors.append(f"{resources_name}: {problem}")
        referenced[resources_name] = {"sha256": actual}

    for pkg, version in iter_versions(packages):
        try:
            name = artifact_name(pkg["identifier"], version["version"])
        except ManifestError as exc:
            errors.append(str(exc))
            continue
        expected_url = base_url + name
        if version.get("download_url") != expected_url:
            errors.append(f"{variant.packages_file}: {name} download_url 未指向镜像")
        referenced.setdefault(
            name,
            {
                "sha256": (version.get("download_sha256") or "").lower() or None,
                "size": version.get("download_size"),
            },
        )

    return referenced


def _validate_artifacts(
    dist: Path,
    referenced: dict[str, dict],
    errors: list[str],
    complete: bool,
    check_zip: bool,
) -> dict:
    total_bytes = 0
    for name, expected in sorted(referenced.items()):
        if name.endswith(".json"):
            continue
        path = dist / name
        if not path.is_file():
            if complete:
                errors.append(f"缺少插件文件: {name}")
            continue
        size = path.stat().st_size
        total_bytes += size
        if expected.get("size") is not None and size != expected["size"]:
            errors.append(f"大小不符: {name} 期望 {expected['size']} 实际 {size}")
        if expected.get("sha256"):
            actual = sha256_file(path)
            if actual != expected["sha256"]:
                errors.append(f"sha256 不符: {name}")
        if check_zip and name.endswith(".zip"):
            problem = zip_error(path)
            if problem:
                errors.append(f"{name}: {problem}")
    return {"artifacts": len(referenced), "artifact_bytes": total_bytes}


def validate_snapshot(
    dist_dir: str | Path,
    base_url: str,
    variants: dict | None = None,
    complete: bool = True,
    check_zip: bool = False,
) -> dict:
    """校验整个快照，任何问题抛 ``SnapshotError``。

    ``variants`` 为原始上游文档（用于零漂移比对）；为空时只做自洽校验。
    """
    dist = Path(dist_dir)
    base = normalize_base_url(base_url)
    errors: list[str] = []

    if not dist.is_dir():
        raise SnapshotError(f"快照目录不存在: {dist}")

    referenced: dict[str, dict] = {}
    for variant in VARIANTS:
        spec = (variants or {}).get(variant.key)
        referenced.update(
            _validate_variant(dist, base, variant, spec, errors)
        )

    stats = _validate_artifacts(dist, referenced, errors, complete, check_zip)

    for path in dist.glob("*.zip"):
        if path.name not in referenced:
            errors.append(f"存在未被引用的文件: {path.name}")

    if errors:
        raise SnapshotError("快照校验失败:\n  - " + "\n  - ".join(errors))

    return {
        "base_url": base,
        "artifact_count": stats["artifacts"],
        "artifact_bytes": stats["artifact_bytes"],
    }


def _fmt(path: tuple) -> str:
    return "/".join(str(part) for part in path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验已生成的 KiCad 镜像快照")
    parser.add_argument("dist", help="快照目录（sync.py --out 的输出）")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--check-zip",
        action="store_true",
        help="逐个大文件重新校验 ZIP 结构（较慢）",
    )
    args = parser.parse_args(argv)

    try:
        summary = validate_snapshot(
            args.dist, args.base_url, check_zip=args.check_zip
        )
    except (SnapshotError, VerifyError, ManifestError) as exc:
        print(f"[kicad-mirror] {exc}", file=sys.stderr)
        return 1

    print(
        f"[kicad-mirror] 快照校验通过: {summary['artifact_count']} 个引用文件, "
        f"{summary['artifact_bytes']} 字节"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
