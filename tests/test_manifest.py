import copy

import pytest

from manifest import (
    ManifestError,
    artifact_name,
    collect_artifacts,
    diff_paths,
    normalize_base_url,
    rewrite_package_urls,
    rewrite_repository,
)


def sample_packages():
    return {
        "packages": [
            {
                "$schema": "https://go.kicad.org/pcm/schemas/v1",
                "identifier": "com.example.alpha",
                "name": "Alpha",
                "versions": [
                    {
                        "version": "1.0",
                        "download_url": "https://github.com/example/alpha.zip",
                        "download_sha256": "AA" * 32,
                        "download_size": 100,
                        "install_size": 200,
                        "kicad_version": "8.0",
                        "kicad_version_max": "10",
                        "platforms": ["linux", "windows"],
                        "status": "stable",
                    }
                ],
            }
        ]
    }


def test_artifact_name_is_deterministic_and_safe():
    assert artifact_name("com.example.alpha", "1.0") == "com.example.alpha-1.0.zip"
    with pytest.raises(ManifestError):
        artifact_name("../evil", "1.0")


def test_rewrite_package_urls_only_touches_download_url():
    original = sample_packages()
    mirrored = copy.deepcopy(original)

    assert rewrite_package_urls(mirrored, "https://eda.cn/kicad-addons") == 1

    version = mirrored["packages"][0]["versions"][0]
    assert version["download_url"] == (
        "https://eda.cn/kicad-addons/com.example.alpha-1.0.zip"
    )
    # 未知字段与键序保持不变
    assert version["platforms"] == ["linux", "windows"]
    assert version["kicad_version_max"] == "10"
    assert list(version) == list(original["packages"][0]["versions"][0])
    assert list(mirrored["packages"][0]) == list(original["packages"][0])

    allowed = {("packages", 0, "versions", 0, "download_url")}
    assert diff_paths(original, mirrored, allowed) == []


def test_diff_paths_detects_unexpected_change():
    original = sample_packages()
    mirrored = copy.deepcopy(original)
    mirrored["packages"][0]["name"] = "Renamed"

    diffs = diff_paths(original, mirrored, set())
    assert [path for path, _, _ in diffs] == [("packages", 0, "name")]


def test_collect_artifacts_rejects_conflicting_content():
    doc = sample_packages()
    duplicate = copy.deepcopy(doc["packages"][0])
    duplicate["versions"][0]["download_sha256"] = "BB" * 32
    doc["packages"].append(duplicate)

    with pytest.raises(ManifestError, match="冲突"):
        collect_artifacts(doc)


def test_collect_artifacts_records_source_and_metadata():
    artifacts = collect_artifacts(sample_packages())
    item = artifacts["com.example.alpha-1.0.zip"]
    assert item["source_url"] == "https://github.com/example/alpha.zip"
    assert item["size"] == 100
    assert item["sha256"] == "aa" * 32


def test_rewrite_repository_keeps_official_fields():
    repo = {
        "$schema": "https://go.kicad.org/pcm/schemas/v2#/definitions/Repository",
        "schema_version": 2,
        "name": "KiCad official repository",
        "maintainer": {"name": "KiCad team"},
        "packages": {
            "url": "https://gitlab.com/upstream/packages.json",
            "sha256": "old",
            "update_time_utc": "2026-11-16 11:02:17",
            "update_timestamp": 1794826937,
        },
        "resources": {
            "url": "https://gitlab.com/upstream/resources.zip",
            "sha256": "resource-sha",
            "update_time_utc": "2026-11-16 11:02:17",
            "update_timestamp": 1794826937,
        },
    }

    rewrite_repository(
        repo,
        "https://eda.cn/kicad-addons/",
        "packages.json",
        "new-sha",
        "resources.zip",
    )

    assert repo["packages"]["url"] == "https://eda.cn/kicad-addons/packages.json"
    assert repo["packages"]["sha256"] == "new-sha"
    assert repo["resources"]["url"] == "https://eda.cn/kicad-addons/resources.zip"
    # resources 字节未变，官方 sha 必须保留
    assert repo["resources"]["sha256"] == "resource-sha"
    assert repo["name"] == "KiCad official repository"
    assert repo["schema_version"] == 2
    assert repo["packages"]["update_timestamp"] == 1794826937


def test_normalize_base_url():
    assert normalize_base_url("https://eda.cn/kicad-addons") == (
        "https://eda.cn/kicad-addons/"
    )
    with pytest.raises(ManifestError):
        normalize_base_url("  ")
