"""上游固件与 tmp_repo 骨架的示例级单元测试。

只验证测试地基本身可用（固件齐全、hash 与基线一致、tmp_repo 能还原出
真实仓库相对路径），不涉及 Rebrand_Tool 逻辑。
"""

from __future__ import annotations

from conftest import (
    FIXTURES_UPSTREAM,
    REPO_PATH_TO_FIXTURE,
    UPSTREAM_FILES,
    UPSTREAM_SHA256,
    sha256_of,
    snapshot,
)


def test_upstream_fixtures_present():
    """4 个触点固件全部签入。"""
    for spec in UPSTREAM_FILES:
        path = FIXTURES_UPSTREAM / spec.fixture_name
        assert path.is_file(), f"缺少固件 {path}"


def test_upstream_fixtures_match_baseline_sha256():
    """固件内容与上游基线 sha256 逐字节一致。"""
    for spec in UPSTREAM_FILES:
        actual = sha256_of(FIXTURES_UPSTREAM / spec.fixture_name)
        assert actual == spec.sha256, (
            f"{spec.fixture_name} hash 漂移：期望 {spec.sha256}，实际 {actual}"
        )


def test_fixture_name_repo_path_mapping_is_bijective():
    """fixture 名与仓库相对路径一一对应，避免映射表写错导致覆盖。"""
    assert len(REPO_PATH_TO_FIXTURE) == len(UPSTREAM_FILES)
    assert len({f.fixture_name for f in UPSTREAM_FILES}) == len(UPSTREAM_FILES)


def test_tmp_repo_materializes_touchpoints_at_repo_paths(tmp_repo):
    """tmp_repo 按真实仓库相对路径还原触点，内容等于上游基线。"""
    files = snapshot(tmp_repo)
    assert set(files) == set(UPSTREAM_SHA256), "tmp_repo 内容集合应恰为 4 个触点"
    for spec in UPSTREAM_FILES:
        assert sha256_of(tmp_repo / spec.repo_path) == spec.sha256


def test_tmp_repo_preserves_executable_bits(tmp_repo):
    """installer 下两个脚本保持可执行位，否则 bash -n 之外的检查会失真。"""
    for spec in UPSTREAM_FILES:
        mode = (tmp_repo / spec.repo_path).stat().st_mode & 0o777
        assert mode == spec.mode, f"{spec.repo_path} 权限位为 {oct(mode)}"


def test_tmp_repo_is_isolated_per_test(tmp_repo):
    """写入 tmp_repo 不污染签入的固件本身。"""
    target = tmp_repo / "files/image_config/environment/motd"
    target.write_bytes(b"dirty\n")
    assert (
        sha256_of(FIXTURES_UPSTREAM / "motd")
        == UPSTREAM_SHA256["files/image_config/environment/motd"]
    )


def test_make_tmp_repo_returns_independent_workspaces(make_tmp_repo):
    """工厂 fixture 产出的两个工作区互不干扰。"""
    a = make_tmp_repo()
    b = make_tmp_repo()
    assert a != b
    (a / "files/image_config/environment/motd").write_bytes(b"changed\n")
    assert (
        sha256_of(b / "files/image_config/environment/motd")
        == UPSTREAM_SHA256["files/image_config/environment/motd"]
    )
