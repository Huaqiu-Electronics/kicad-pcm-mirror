"""HTTP 下载：流式落盘、有限重试、下载即校验。"""

from __future__ import annotations

import hashlib
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from verify import VerifyError, sha256_file, zip_error

USER_AGENT = (
    "kicad-pcm-mirror/1.0 "
    "(+https://github.com/Huaqiu-Electronics/kicad-pcm-mirror)"
)
CHUNK_SIZE = 1 << 20


class DownloadError(RuntimeError):
    """下载失败（网络错误、状态码异常或校验失败）。"""


class MissingUpstream(DownloadError):
    """上游链接已永久失效（404/410），重试无意义。"""


class ChecksumMismatch(DownloadError):
    """内容与上游声明的 sha256 不符（常见于作者重传同版本资源）。"""


def _build_request(url: str, accept: str | None) -> urllib.request.Request:
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    return urllib.request.Request(url, headers=headers)


def fetch_bytes(
    url: str,
    accept: str | None = None,
    timeout: float = 60.0,
    retries: int = 6,
) -> bytes:
    """下载小文件到内存（元数据用），失败按指数退避重试。

    上游 CDN 偶发 5xx/504，默认多等几轮（约 1 分钟）再放弃。
    """
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(
                _build_request(url, accept), timeout=timeout
            ) as response:
                if response.status != 200:
                    raise DownloadError(f"HTTP {response.status}: {url}")
                body = response.read()
                if not body:
                    raise DownloadError(f"空响应: {url}")
                return body
        except (urllib.error.URLError, OSError, DownloadError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(min(2 ** attempt, 20))
    raise DownloadError(f"下载失败（尝试 {retries} 次）: {url}: {last_error}")


def _stream_to_file(
    url: str, tmp_path: Path, accept: str | None, timeout: float
) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    request = _build_request(url, accept)
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            raise MissingUpstream(f"HTTP {exc.code}: {url}") from exc
        raise
    with response:
        if response.status != 200:
            raise DownloadError(f"HTTP {response.status}: {url}")
        with open(tmp_path, "wb") as handle:
            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                handle.write(chunk)
                digest.update(chunk)
                size += len(chunk)
    return size, digest.hexdigest()


def download_artifact(
    url: str,
    dest: str | Path,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
    timeout: float = 60.0,
    retries: int = 3,
    check_zip: bool = True,
    skip_existing: bool = False,
    accept: str | None = None,
) -> dict:
    """下载单个产物到 ``dest``，成功后才原子替换。

    返回值包含 ``name`` / ``size`` / ``sha256`` / ``skipped``。
    """
    target = Path(dest)
    target.parent.mkdir(parents=True, exist_ok=True)
    expected_sha256 = expected_sha256.lower() if expected_sha256 else None

    if skip_existing and target.is_file() and expected_sha256:
        if sha256_file(target) == expected_sha256:
            actual_size = target.stat().st_size
            return {
                "name": target.name,
                "size": actual_size,
                "sha256": expected_sha256,
                "skipped": True,
                "size_mismatch": expected_size is not None
                and actual_size != expected_size,
            }

    tmp_path = target.with_name(target.name + ".part")
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            size, digest = _stream_to_file(url, tmp_path, accept, timeout)
            # 上游 download_size 由作者手填，常见写错；以 sha256 为准，大小仅记录
            size_mismatch = expected_size is not None and size != expected_size
            if expected_sha256 and digest != expected_sha256:
                raise ChecksumMismatch(
                    f"sha256 不符: {target.name} 期望 {expected_sha256} 实际 {digest}"
                )
            if check_zip:
                problem = zip_error(tmp_path)
                if problem:
                    raise DownloadError(f"{target.name}: {problem}")
            os.replace(tmp_path, target)
            return {
                "name": target.name,
                "size": size,
                "sha256": digest,
                "skipped": False,
                "size_mismatch": size_mismatch,
            }
        except MissingUpstream:
            tmp_path.unlink(missing_ok=True)
            raise
        except ChecksumMismatch:
            # 校验失败先重试（可能是截断），重试耗尽后交由调用方决定是否剔除
            tmp_path.unlink(missing_ok=True)
            if attempt < retries:
                time.sleep(min(2 ** (attempt - 1), 8))
                continue
            raise
        except (urllib.error.URLError, OSError, DownloadError, VerifyError) as exc:
            last_error = exc
            tmp_path.unlink(missing_ok=True)
            if attempt < retries:
                time.sleep(min(2 ** (attempt - 1), 8))

    raise DownloadError(f"下载失败: {url}: {last_error}")


def download_many(
    tasks: list[dict],
    workers: int = 8,
    retries: int = 3,
    timeout: float = 60.0,
    skip_existing: bool = False,
    accept: str | None = None,
    on_result=None,
) -> tuple[dict[str, dict], list[tuple[str, Exception]]]:
    """并发下载，返回（成功结果，失败列表）；本函数不中断，由调用方决定退出。

    失败项为 ``(文件名, 异常)``，调用方可通过 ``MissingUpstream`` 区分上游死链。
    """
    results: dict[str, dict] = {}
    errors: list[tuple[str, Exception]] = []
    if not tasks:
        return results, errors

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(
                download_artifact,
                task["url"],
                task["dest"],
                task.get("sha256"),
                task.get("size"),
                timeout,
                retries,
                True,
                skip_existing,
                accept,
            ): task["name"]
            for task in tasks
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
                if on_result:
                    on_result(name, results[name], None)
            except Exception as exc:  # noqa: BLE001 - 统一收集为构建失败原因
                errors.append((name, exc))
                if on_result:
                    on_result(name, None, exc)
    return results, errors
