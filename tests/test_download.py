import pytest

import download
from download import DownloadError, download_artifact, fetch_bytes
from helpers import ACCEPT_V1, ACCEPT_V2, make_zip
from verify import sha256_bytes


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(download.time, "sleep", lambda _seconds: None)


def test_download_artifact_success(tmp_path, upstream):
    body = make_zip({"metadata.json": b"{}"})
    upstream.add("/pkg.zip", body)

    result = download_artifact(
        f"{upstream.base}/pkg.zip",
        tmp_path / "pkg.zip",
        expected_sha256=sha256_bytes(body),
        expected_size=len(body),
    )

    assert result["skipped"] is False
    assert result["sha256"] == sha256_bytes(body)
    assert (tmp_path / "pkg.zip").read_bytes() == body


def test_download_artifact_sha_mismatch(tmp_path, upstream):
    body = make_zip({"metadata.json": b"{}"})
    upstream.add("/pkg.zip", body)

    with pytest.raises(DownloadError, match="sha256 不符"):
        download_artifact(
            f"{upstream.base}/pkg.zip",
            tmp_path / "pkg.zip",
            expected_sha256="0" * 64,
        )
    assert not (tmp_path / "pkg.zip").exists()


def test_download_artifact_size_mismatch(tmp_path, upstream):
    body = make_zip({"metadata.json": b"{}"})
    upstream.add("/pkg.zip", body)

    with pytest.raises(DownloadError, match="大小不符"):
        download_artifact(
            f"{upstream.base}/pkg.zip",
            tmp_path / "pkg.zip",
            expected_size=len(body) + 1,
        )


def test_download_artifact_rejects_broken_zip(tmp_path, upstream):
    upstream.add("/broken.zip", b"not-a-zip")

    with pytest.raises(DownloadError, match="ZIP"):
        download_artifact(f"{upstream.base}/broken.zip", tmp_path / "broken.zip")


def test_download_retries_then_succeeds(tmp_path, upstream):
    body = make_zip({"metadata.json": b"{}"})
    upstream.add("/pkg.zip", body)
    upstream.fail_next("/pkg.zip", 2)

    result = download_artifact(
        f"{upstream.base}/pkg.zip", tmp_path / "pkg.zip", retries=3
    )

    assert result["sha256"] == sha256_bytes(body)
    assert upstream.hits["/pkg.zip"] == 3


def test_download_gives_up_after_retries(tmp_path, upstream):
    upstream.add("/pkg.zip", make_zip())
    upstream.fail_next("/pkg.zip", 5)

    with pytest.raises(DownloadError):
        download_artifact(
            f"{upstream.base}/pkg.zip", tmp_path / "pkg.zip", retries=2
        )
    assert upstream.hits["/pkg.zip"] == 2


def test_download_skips_valid_existing_file(tmp_path, upstream):
    body = make_zip({"metadata.json": b"{}"})
    upstream.add("/pkg.zip", body)
    target = tmp_path / "pkg.zip"
    target.write_bytes(body)

    result = download_artifact(
        f"{upstream.base}/pkg.zip",
        target,
        expected_sha256=sha256_bytes(body),
        skip_existing=True,
    )

    assert result["skipped"] is True
    assert upstream.hits["/pkg.zip"] == 0


def test_fetch_bytes_routes_by_accept_header(upstream):
    upstream.add("/meta.json", b'{"schema":"v1"}', ACCEPT_V1)
    upstream.add("/meta.json", b'{"schema":"v2"}', ACCEPT_V2)

    assert fetch_bytes(f"{upstream.base}/meta.json", accept=ACCEPT_V1) == (
        b'{"schema":"v1"}'
    )
    assert fetch_bytes(f"{upstream.base}/meta.json", accept=ACCEPT_V2) == (
        b'{"schema":"v2"}'
    )
