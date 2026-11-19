import hashlib
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
DEFAULT_BASE = "https://www.eda.cn/kicad-addons/"
PAIRS = {
    "packages-v1.json": "repository.v1.json",
    "packages.json": "repository.v2.json",
}
RESOURCES_SUM = "1" * 64

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("sh") is None,
    reason="需要 POSIX sh 才能运行启动脚本",
)


def write_tree(root: Path, baked: str = BAKED):
    root.mkdir(parents=True, exist_ok=True)

    for pkg_name, repo_name in PAIRS.items():
        pkg_text = json.dumps(
            {
                "packages": [
                    {
                        "identifier": "com.example.alpha",
                        "versions": [
                            {
                                "version": "1.0",
                                "download_url": baked + "com.example.alpha-1.0.zip",
                            }
                        ],
                    }
                ]
            }
        )
        (root / pkg_name).write_text(pkg_text, encoding="utf-8")

        repo = {
            "packages": {
                "sha256": hashlib.sha256(pkg_text.encode()).hexdigest(),
                "url": baked + pkg_name,
            },
            "resources": {"sha256": RESOURCES_SUM, "url": baked + "resources.zip"},
        }
        (root / repo_name).write_text(json.dumps(repo, indent=4), encoding="utf-8")

    (root / "mirror-manifest.json").write_text(
        json.dumps({"base_url": baked, "image_tag": "test"}, indent=4),
        encoding="utf-8",
    )


@pytest.fixture
def html_root(tmp_path):
    root = tmp_path / "kicad-addons"
    write_tree(root)
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


def assert_consistent(root: Path, base: str):
    """地址前缀已替换，且 repository 中记录的 packages.sha256 与文件一致。"""
    for pkg_name, repo_name in PAIRS.items():
        pkg = root / pkg_name
        repo = json.loads((root / repo_name).read_text(encoding="utf-8"))
        digest = hashlib.sha256(pkg.read_bytes()).hexdigest()

        assert repo["packages"]["url"] == base + pkg_name
        assert repo["packages"]["sha256"] == digest
        assert repo["resources"]["url"] == base + "resources.zip"
        assert repo["resources"]["sha256"] == RESOURCES_SUM

        versions = json.loads(pkg.read_text(encoding="utf-8"))["packages"][0][
            "versions"
        ]
        assert versions[0]["download_url"] == base + "com.example.alpha-1.0.zip"


def test_host_only_base_is_completed(html_root):
    result = run_script(html_root, "http://192.168.1.10:8080")

    assert result.returncode == 0
    assert_consistent(html_root, "http://192.168.1.10:8080/kicad-addons/")


def test_explicit_path_without_trailing_slash(html_root):
    result = run_script(html_root, "http://mirror.example.com:8080/kicad-addons")

    assert result.returncode == 0
    assert_consistent(html_root, "http://mirror.example.com:8080/kicad-addons/")


def test_second_start_keeps_hashes_consistent(html_root):
    first = run_script(html_root, "http://192.168.1.10:8080")
    assert first.returncode == 0

    second = run_script(html_root, "http://192.168.1.10:8080")

    assert second.returncode == 0
    assert_consistent(html_root, "http://192.168.1.10:8080/kicad-addons/")


def test_base_prefix_comes_from_manifest(tmp_path):
    root = tmp_path / "kicad-addons"
    write_tree(root, baked="https://www.eda.cn/kicad-addons/")

    result = run_script(root, "http://192.168.1.10:8080")

    assert result.returncode == 0
    assert_consistent(root, "http://192.168.1.10:8080/kicad-addons/")


def test_unset_base_uses_default_www(html_root):
    result = run_script(html_root, None)

    assert result.returncode == 0
    assert_consistent(html_root, DEFAULT_BASE)


def test_unset_base_with_matching_baked_is_noop(tmp_path):
    root = tmp_path / "kicad-addons"
    write_tree(root, baked=DEFAULT_BASE)
    before = {name: (root / name).read_bytes() for name in (*PAIRS, *PAIRS.values())}

    result = run_script(root, None)

    assert result.returncode == 0
    for name, content in before.items():
        assert (root / name).read_bytes() == content
    assert_consistent(root, DEFAULT_BASE)


def test_invalid_base_aborts_without_modifying(html_root):
    result = run_script(html_root, "192.168.1.10:8080")

    assert result.returncode == 1
    assert "错误" in result.stderr
    assert_consistent(html_root, BAKED)
