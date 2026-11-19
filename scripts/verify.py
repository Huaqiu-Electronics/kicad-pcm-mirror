"""文件级完整性校验：sha256、大小、ZIP 结构。"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

CHUNK_SIZE = 1 << 20


class VerifyError(RuntimeError):
    """完整性校验失败。"""


def sha256_file(path: str | Path, chunk_size: int = CHUNK_SIZE) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def zip_error(path: str | Path) -> str | None:
    """返回 ZIP 结构错误描述，``None`` 表示可正常读取。"""
    try:
        with zipfile.ZipFile(path) as archive:
            bad = archive.testzip()
    except (zipfile.BadZipFile, OSError) as exc:
        return f"不是有效的 ZIP 文件: {exc}"
    if bad is not None:
        return f"ZIP 内文件损坏: {bad}"
    return None


def verify_artifact(
    path: str | Path,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
    check_zip: bool = True,
) -> dict:
    """校验单个产物，失败抛 ``VerifyError``。"""
    target = Path(path)
    if not target.is_file():
        raise VerifyError(f"文件不存在: {target}")

    size = target.stat().st_size
    if expected_size is not None and size != expected_size:
        raise VerifyError(
            f"大小不符: {target.name} 期望 {expected_size} 实际 {size}"
        )

    digest = sha256_file(target)
    if expected_sha256 and digest != expected_sha256.lower():
        raise VerifyError(
            f"sha256 不符: {target.name} 期望 {expected_sha256} 实际 {digest}"
        )

    if check_zip:
        problem = zip_error(target)
        if problem:
            raise VerifyError(f"{target.name}: {problem}")

    return {"name": target.name, "size": size, "sha256": digest}
