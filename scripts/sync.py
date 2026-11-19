"""KiCad 官方插件仓库中国镜像 —— 同步入口。

流程（规格书 §3.2 / §9）：
1. 分别以 v1、v2 的 Accept 抓取官方 repository.json；
2. 抓取其引用的 packages 与 resources，并按上游声明校验 sha256；
3. 下载全部被引用的插件 ZIP（原始字节，不重新打包）；
4. 逐项校验 sha256 / 大小 / ZIP 结构，任一失败即终止；
5. 仅重写 download_url 与元数据引用地址；
6. 校验完整快照，并输出 mirror-manifest.json。
"""

from __future__ import annotations

import argparse
import copy
import io
import os
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from download import ChecksumMismatch, MissingUpstream, download_many, fetch_bytes
from manifest import (
    DEFAULT_BASE_URL,
    UPSTREAM_REPOSITORY_URL,
    VARIANTS,
    ManifestError,
    collect_artifacts,
    drop_versions,
    dumps_bytes,
    iter_versions,
    loads,
    normalize_base_url,
    rewrite_package_urls,
    rewrite_repository,
)
from validate import SnapshotError, validate_snapshot
from verify import sha256_bytes

LOG = "[kicad-mirror]"

# repository.kicad.org 是 Cloudflare 前置入口，偶发 504；GitLab 仓库里有同样内容的原始文件
GITLAB_REPOSITORY = {
    "v1": "https://gitlab.com/kicad/addons/repository/-/raw/main/repository-v1.json",
    "v2": "https://gitlab.com/kicad/addons/repository/-/raw/main/repository.json",
}


class SyncError(RuntimeError):
    """同步失败，快照不可发布。"""


def log(message: str) -> None:
    print(f"{LOG} {message}", flush=True)


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _zip_problem(data: bytes) -> str | None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            bad = archive.testzip()
    except (zipfile.BadZipFile, OSError) as exc:
        return f"不是有效的 ZIP: {exc}"
    return f"ZIP 内文件损坏: {bad}" if bad else None


def _fetch_metadata(
    url: str, accept: str | None, args, retries: int | None = None
) -> bytes:
    try:
        return fetch_bytes(
            url,
            accept=accept,
            timeout=args.timeout,
            retries=args.retries if retries is None else retries,
        )
    except Exception as exc:  # noqa: BLE001 - 统一转换为发布阻断错误
        raise SyncError(f"获取元数据失败: {url}: {exc}") from exc


def _parse_metadata(raw: bytes, label: str, url: str) -> dict:
    try:
        return loads(raw)
    except (ValueError, ManifestError) as exc:
        raise SyncError(
            f"{label} 不是有效 JSON（{len(raw)} 字节）: {url}: {exc}"
        ) from exc


def build_snapshot(args) -> dict:
    upstream = args.upstream
    base = normalize_base_url(args.base_url)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # 元数据按 URL 缓存：v1/v2 的 packages 与 resources 常常指向同一文件
    fetched_urls: dict[str, bytes] = {}

    def fetch_once(url: str, accept: str | None) -> tuple[bytes, bool]:
        cached = fetched_urls.get(url)
        if cached is not None:
            return cached, True
        raw = _fetch_metadata(url, accept, args)
        fetched_urls[url] = raw
        return raw, False

    # 1) 上游两个变体的 repository.json；官方入口不可用时回退到 GitLab 原始文件
    repositories: dict[str, dict] = {}
    for variant in VARIANTS:
        fallback = (
            GITLAB_REPOSITORY.get(variant.key)
            if upstream == UPSTREAM_REPOSITORY_URL
            else None
        )
        try:
            # 走官方入口时少重试几次，尽快判断是否需要回退
            raw = _fetch_metadata(
                upstream,
                variant.accept,
                args,
                retries=min(args.retries, 2) if fallback else None,
            )
            source = upstream
        except SyncError:
            if not fallback:
                raise
            log(f"{variant.key} 官方入口不可用，回退到 GitLab 原始地址")
            raw = _fetch_metadata(fallback, None, args)
            source = fallback
        repositories[variant.key] = {
            "raw_sha256": sha256_bytes(raw),
            "original": _parse_metadata(
                raw, f"{variant.key} repository.json", source
            ),
        }
        log(f"已获取 {variant.key} repository.json（{len(raw)} 字节，来自 {source}）")

    # 2) packages 文档 + resources 字节（按上游声明的 sha256 校验）
    packages_state: dict[str, dict] = {}
    variant_resources: dict[str, str] = {}
    resources_by_sha: dict[str, bytes] = {}
    for variant in VARIANTS:
        repo = repositories[variant.key]["original"]

        packages_url = repo["packages"]["url"]
        raw, reused = fetch_once(packages_url, None)
        actual = sha256_bytes(raw)
        declared = (repo["packages"].get("sha256") or "").lower()
        if declared and declared != actual:
            raise SyncError(
                f"{variant.key} packages.json 的 sha256 与上游声明不一致"
            )
        packages_state[variant.key] = {
            "original": _parse_metadata(
                raw, f"{variant.key} packages.json", packages_url
            ),
            "sha256": actual,
        }
        log(
            f"已获取 {variant.key} packages.json（{len(raw)} 字节"
            f"{'，复用同一 URL' if reused else ''}）"
        )

        resources_url = repo["resources"]["url"]
        resources_raw, reused = fetch_once(resources_url, None)
        resources_sha = sha256_bytes(resources_raw)
        declared = (repo["resources"].get("sha256") or "").lower()
        if declared and declared != resources_sha:
            raise SyncError(
                f"{variant.key} resources 的 sha256 与上游声明不一致"
            )
        problem = _zip_problem(resources_raw)
        if problem:
            raise SyncError(f"resources 校验失败: {problem}")
        resources_by_sha.setdefault(resources_sha, resources_raw)
        variant_resources[variant.key] = resources_sha
        log(
            f"已获取 {variant.key} resources（{len(resources_raw)} 字节"
            f"{'，复用同一 URL' if reused else ''}）"
        )

    # 资源文件命名：内容相同则共用一个 resources.zip，不同则按 sha 前缀区分
    resource_names: dict[str, str] = {}
    for index, sha in enumerate(sorted(resources_by_sha)):
        resource_names[sha] = (
            "resources.zip" if index == 0 else f"resources-{sha[:8]}.zip"
        )

    # 3) 汇总所有被引用的插件 ZIP（检测同名冲突）
    artifacts: dict[str, dict] = {}
    for variant in VARIANTS:
        for name, item in collect_artifacts(
            packages_state[variant.key]["original"]
        ).items():
            existing = artifacts.get(name)
            if existing is None:
                artifacts[name] = item
            elif existing["sha256"] != item["sha256"] or existing["size"] != item["size"]:
                raise SyncError(
                    f"插件文件名冲突且内容不同: {name}"
                )

    selected = list(artifacts.items())
    incomplete = bool(args.limit and args.limit > 0)
    if incomplete:
        selected = selected[: args.limit]
        log(
            f"警告: --limit={args.limit} 仅用于冒烟，产物不完整，禁止发布"
        )

    # 4) 并发下载 + 逐项校验
    tasks = [
        {
            "name": name,
            "url": item["source_url"],
            "dest": str(out / name),
            "sha256": item["sha256"],
            "size": item["size"],
        }
        for name, item in selected
    ]
    log(f"开始下载 {len(tasks)} 个插件 ZIP（并发 {args.workers}）...")
    done = {"count": 0}

    def on_result(name: str, result, error) -> None:
        done["count"] += 1
        if done["count"] % 25 == 0 or done["count"] == len(tasks):
            log(f"下载进度: 已完成 {done['count']}/{len(tasks)}")

    results, errors = download_many(
        tasks,
        workers=args.workers,
        retries=args.retries,
        timeout=args.timeout,
        skip_existing=args.skip_existing,
        on_result=on_result,
    )
    # 上游死链（404/410）与内容漂移（sha256 不符）剔除对应版本；其余失败仍阻断发布
    excluded = {}
    for name, exc in errors:
        if isinstance(exc, MissingUpstream):
            excluded[name] = exc
            log(f"上游链接已失效，从镜像索引剔除: {name} ({exc})")
        elif isinstance(exc, ChecksumMismatch):
            excluded[name] = exc
            log(f"内容与上游声明 sha256 不符，从镜像索引剔除: {name} ({exc})")
    fatal = [
        (name, exc)
        for name, exc in errors
        if name not in excluded
    ]
    for name, exc in fatal:
        print(f"{LOG} 下载失败: {name}: {exc}", file=sys.stderr)
    if fatal:
        raise SyncError(f"{len(fatal)} 个产物下载失败，快照构建终止")

    skipped = sum(1 for item in results.values() if item.get("skipped"))
    dead_links = sum(
        1 for exc in excluded.values() if isinstance(exc, MissingUpstream)
    )
    drifted = sum(
        1 for exc in excluded.values() if isinstance(exc, ChecksumMismatch)
    )
    log(
        f"下载完成: {len(tasks)} = {len(results)} 发布 + {len(excluded)} 剔除"
        f"（死链 {dead_links} / 校验不符 {drifted}）"
        + (f"，复用本地 {skipped}" if skipped else "")
    )

    size_warnings = [
        {
            "name": name,
            "declared_size": artifacts[name]["size"],
            "actual_size": result["size"],
        }
        for name, result in sorted(results.items())
        if result.get("size_mismatch")
    ]
    if size_warnings:
        log(
            f"上游 download_size 与真实字节不一致: {len(size_warnings)} 个"
            f"（以 sha256 为准，详见 manifest）"
        )

    # 5) 清理已不被上游引用的旧产物
    referenced = (set(artifacts) - set(excluded)) | set(resource_names.values())
    for path in out.glob("*.zip"):
        if path.name not in referenced:
            path.unlink()
            log(f"清理过期产物: {path.name}")

    # 6) 写入 resources
    for sha, name in resource_names.items():
        _write_atomic(out / name, resources_by_sha[sha])

    # 7) 剔除失效版本后重写 packages 与 repository
    variant_summary: dict[str, dict] = {}
    pruned_packages: dict[str, dict] = {}
    for variant in VARIANTS:
        packages = copy.deepcopy(packages_state[variant.key]["original"])
        dropped = drop_versions(packages, set(excluded))
        if dropped:
            log(f"{variant.key}: 剔除 {dropped} 个失效版本条目")
        pruned_packages[variant.key] = copy.deepcopy(packages)
        rewritten = rewrite_package_urls(packages, base)
        packages_bytes = dumps_bytes(packages)
        _write_atomic(out / variant.packages_file, packages_bytes)
        packages_sha = sha256_bytes(packages_bytes)

        repo = copy.deepcopy(repositories[variant.key]["original"])
        resources_sha = variant_resources[variant.key]
        rewrite_repository(
            repo,
            base,
            variant.packages_file,
            packages_sha,
            resource_names[resources_sha],
        )
        _write_atomic(out / variant.repository_file, dumps_bytes(repo))

        variant_summary[variant.key] = {
            "repository": variant.repository_file,
            "packages": variant.packages_file,
            "packages_sha256": packages_sha,
            "schema_version": repo.get("schema_version"),
            "resources": resource_names[resources_sha],
            "resources_sha256": resources_sha,
            "versions": rewritten,
        }
        log(f"已生成 {variant.packages_file}（重写 {rewritten} 个下载地址）")
        log(f"已生成 {variant.repository_file}（packages 与 resources 地址）")

    # 8) 构建追踪信息
    sync_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    artifact_list = []
    published_bytes = 0
    for name in sorted(artifacts):
        path = out / name
        size = path.stat().st_size if path.is_file() else (artifacts[name]["size"] or 0)
        if name not in excluded:
            published_bytes += size
        artifact_list.append(
            {
                "name": name,
                "sha256": artifacts[name]["sha256"],
                "size": size,
                "source_url": artifacts[name]["source_url"],
                "present": path.is_file(),
                "excluded": name in excluded,
            }
        )

    mirror_manifest = {
        "source": upstream,
        "base_url": base,
        "sync_time": sync_time,
        "source_revision": f"sha256:{repositories['v1']['raw_sha256']}",
        "complete": not incomplete,
        "artifact_count": len(artifacts),
        "published_count": len(artifacts) - len(excluded),
        "artifact_bytes": published_bytes,
        "excluded": [
            {"name": name, "reason": str(exc)}
            for name, exc in sorted(excluded.items())
        ],
        "size_mismatches": size_warnings,
        "image_tag": args.image_tag or os.environ.get("IMAGE_TAG", ""),
        "variants": variant_summary,
        "artifacts": artifact_list,
    }
    _write_atomic(out / "mirror-manifest.json", dumps_bytes(mirror_manifest))

    # 9) 快照校验
    if incomplete:
        log("已跳过快照校验（--limit 模式）")
    else:
        originals = {
            variant.key: {
                "original_repository": repositories[variant.key]["original"],
                "original_packages": pruned_packages[variant.key],
            }
            for variant in VARIANTS
        }
        summary = validate_snapshot(out, base, variants=originals)
        log(
            f"快照校验通过: {summary['artifact_count']} 个引用文件, "
            f"{summary['artifact_bytes']} 字节"
            + (
                f", {len(summary['warnings'])} 个 size 告警"
                if summary["warnings"]
                else ""
            )
        )

    return {
        "sync_time": sync_time,
        "artifact_count": len(artifacts),
        "published_count": len(artifacts) - len(excluded),
        "artifact_bytes": published_bytes,
        "complete": not incomplete,
        "excluded": len(excluded),
        "size_mismatches": len(size_warnings),
        "variants": variant_summary,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="同步 KiCad 官方插件仓库并生成可发布的镜像快照"
    )
    parser.add_argument("--out", default="dist", help="快照输出目录（默认 dist）")
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"对外访问的基础地址（默认 {DEFAULT_BASE_URL}）",
    )
    parser.add_argument(
        "--upstream",
        default=UPSTREAM_REPOSITORY_URL,
        help="上游 repository.json 地址",
    )
    parser.add_argument("--workers", type=int, default=8, help="下载并发数")
    parser.add_argument("--retries", type=int, default=3, help="单个文件最大尝试次数")
    parser.add_argument("--timeout", type=float, default=60.0, help="单次请求超时秒数")
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="本地已有且 sha256 匹配的文件不再重新下载",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="仅处理前 N 个产物（冒烟用，产物不可发布）",
    )
    parser.add_argument(
        "--image-tag",
        default="",
        help="写入 mirror-manifest.json 的镜像 Tag（默认读环境变量 IMAGE_TAG）",
    )
    args = parser.parse_args(argv)

    try:
        summary = build_snapshot(args)
    except (SyncError, ManifestError, SnapshotError) as exc:
        print(f"{LOG} 同步失败: {exc}", file=sys.stderr)
        return 1

    print(
        f"{LOG} 同步完成: 声明 {summary['artifact_count']} 个产物 / "
        f"发布 {summary['published_count']} 个 / "
        f"{summary['artifact_bytes']} 字节, 时间 {summary['sync_time']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
