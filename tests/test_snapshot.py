import json

import pytest

from helpers import BASE_URL, build_upstream, make_args
from sync import SyncError, build_snapshot
from validate import SnapshotError, validate_snapshot


@pytest.fixture
def data(upstream):
    return build_upstream(upstream)


def _originals(data):
    return {
        "v1": {
            "original_repository": data["repository_v1"],
            "original_packages": data["packages_doc"],
        },
        "v2": {
            "original_repository": data["repository_v2"],
            "original_packages": data["packages_doc"],
        },
    }


def test_builds_complete_snapshot(upstream, tmp_path, data):
    summary = build_snapshot(make_args(upstream, tmp_path))
    out = tmp_path / "dist"

    assert summary["complete"] is True
    assert summary["artifact_count"] == 2

    repository_v1 = json.loads((out / "repository.v1.json").read_text())
    repository_v2 = json.loads((out / "repository.v2.json").read_text())

    assert repository_v1["packages"]["url"] == BASE_URL + "packages-v1.json"
    assert repository_v2["packages"]["url"] == BASE_URL + "packages.json"
    assert repository_v1["resources"]["url"] == BASE_URL + "resources.zip"
    assert "schema_version" not in repository_v1
    assert repository_v2["schema_version"] == 2
    # resources 原始字节不变，官方 sha256 原样保留
    assert (out / "resources.zip").read_bytes() == data["resources"]
    assert repository_v1["resources"]["sha256"] == data["repository_v1"]["resources"]["sha256"]

    packages = json.loads((out / "packages-v1.json").read_text())
    urls = {
        version["download_url"]
        for package in packages["packages"]
        for version in package["versions"]
    }
    assert urls == {
        BASE_URL + data["alpha_name"],
        BASE_URL + data["beta_name"],
    }
    alpha = packages["packages"][0]["versions"][0]
    assert alpha["platforms"] == ["linux", "windows"]
    assert alpha["kicad_version_max"] == "10"
    assert alpha["download_size"] == len(data["zip_alpha"])

    assert (out / data["alpha_name"]).read_bytes() == data["zip_alpha"]
    assert (out / data["beta_name"]).read_bytes() == data["zip_beta"]

    manifest = json.loads((out / "mirror-manifest.json").read_text())
    assert manifest["complete"] is True
    assert manifest["artifact_count"] == 2
    assert manifest["image_tag"] == "test-tag"
    assert manifest["variants"]["v2"]["schema_version"] == 2
    assert manifest["variants"]["v1"]["packages"] == "packages-v1.json"


def test_repeated_sync_is_deterministic(upstream, tmp_path, data):
    build_snapshot(make_args(upstream, tmp_path))
    first = {
        path.name: path.read_bytes()
        for path in (tmp_path / "dist").iterdir()
        if path.name != "mirror-manifest.json"
    }

    build_snapshot(make_args(upstream, tmp_path))
    second = {
        path.name: path.read_bytes()
        for path in (tmp_path / "dist").iterdir()
        if path.name != "mirror-manifest.json"
    }

    assert first == second


def test_download_failure_aborts_and_writes_no_manifest(upstream, tmp_path, data):
    upstream.remove("/alpha.zip")

    with pytest.raises(SyncError):
        build_snapshot(make_args(upstream, tmp_path, retries=1))

    out = tmp_path / "dist"
    assert not (out / "repository.v1.json").exists()
    assert not (out / "packages-v1.json").exists()
    assert not (out / "mirror-manifest.json").exists()


def test_validate_detects_tampered_artifact(upstream, tmp_path, data):
    build_snapshot(make_args(upstream, tmp_path))
    out = tmp_path / "dist"
    (out / data["alpha_name"]).write_bytes(b"tampered")

    with pytest.raises(SnapshotError, match="sha256"):
        validate_snapshot(out, BASE_URL, variants=_originals(data))


def test_validate_detects_unexpected_metadata_change(upstream, tmp_path, data):
    build_snapshot(make_args(upstream, tmp_path))
    out = tmp_path / "dist"
    path = out / "repository.v2.json"
    doc = json.loads(path.read_text())
    doc["name"] = "Tampered repository"
    path.write_text(json.dumps(doc, indent=4))

    with pytest.raises(SnapshotError, match="非预期变更"):
        validate_snapshot(out, BASE_URL, variants=_originals(data))


def test_validate_detects_unreferenced_file(upstream, tmp_path, data):
    build_snapshot(make_args(upstream, tmp_path))
    out = tmp_path / "dist"
    (out / "orphan.zip").write_bytes(data["zip_alpha"])

    with pytest.raises(SnapshotError, match="未被引用"):
        validate_snapshot(out, BASE_URL, variants=_originals(data))


def test_limit_mode_marks_snapshot_incomplete(upstream, tmp_path, data):
    summary = build_snapshot(make_args(upstream, tmp_path, limit=1))
    out = tmp_path / "dist"

    assert summary["complete"] is False
    assert (out / data["alpha_name"]).exists()
    assert not (out / data["beta_name"]).exists()

    relaxed = validate_snapshot(out, BASE_URL, complete=False)
    # alpha + beta + resources.zip
    assert relaxed["artifact_count"] == 3

    with pytest.raises(SnapshotError, match="缺少插件文件"):
        validate_snapshot(out, BASE_URL)
