"""Rebrand_Tool CLI 的退出码契约与结构校验单元测试（任务 4.1、4.2）。

范围刻意限定在 **CLI 这一层**：每个退出码至少一条用例、`--list-touchpoints` 的
输出形态、以及结构校验的通过/失败两条路径。替换策略本身的行为由
``test_touchpoints.py`` 覆盖，全面的属性覆盖由任务 4.3–4.12 的 Property 1–10 负责，
这里不重复。

退出码 3 与 4 各有独立用例：二者在 CI 日志里的处置动作完全不同（3 要人改锚点，
4 只是漏跑 apply），合并测试等于放弃了这个区分的回归保护。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from conftest import BRAND_ASSETS_DIR, FIXTURES_UPSTREAM, LIGENT_DIR, snapshot

sys.path.insert(0, str(LIGENT_DIR))

from rebrand import EXIT_OK, EXIT_USAGE, main  # noqa: E402
from structure import (  # noqa: E402
    EXIT_STRUCTURE_INVALID,
    FAIL,
    OK,
    SKIP,
    RENDER_PARAM_SETS,
    validate_bash_syntax,
    validate_init_cfg,
)
from touchpoints import (  # noqa: E402
    EXIT_ANCHOR_MISS,
    EXIT_CHECK_FAILED,
    EXIT_TOUCHPOINT_MISSING,
    TOUCHPOINT_PATHS,
)

from brandconfig import EXIT_ASSETS_INVALID  # noqa: E402

INIT_CFG_PATH = "files/build_templates/init_cfg.json.j2"
PLATFORM_CONF_PATH = "installer/default_platform.conf"


def run(*args: str) -> int:
    """跑一次 CLI（进程内），返回退出码。"""
    return main(list(args))


def apply_to(repo: Path, *extra: str) -> int:
    return run("--apply", "--repo-root", str(repo), "--assets", str(BRAND_ASSETS_DIR), *extra)


def check_of(repo: Path, *extra: str) -> int:
    return run("--check", "--repo-root", str(repo), "--assets", str(BRAND_ASSETS_DIR), *extra)


# ---------------------------------------------------------------------------
# 退出码 0
# ---------------------------------------------------------------------------


def test_apply_then_check_exit_zero(tmp_repo: Path) -> None:
    """apply 后 check 通过：退出码 0（需求 2.1、2.7）。"""
    assert apply_to(tmp_repo) == EXIT_OK
    assert check_of(tmp_repo) == EXIT_OK


def test_apply_twice_is_idempotent_and_zero(tmp_repo: Path) -> None:
    """第二次 apply 仍为 0，且触点内容逐字节不变（需求 2.2）。"""
    assert apply_to(tmp_repo) == EXIT_OK
    after_first = snapshot(tmp_repo)
    assert apply_to(tmp_repo) == EXIT_OK
    assert snapshot(tmp_repo) == after_first


def test_apply_default_action_is_apply(tmp_repo: Path) -> None:
    """不给动作参数时默认 apply（design.md 4.5 节）。"""
    assert run("--repo-root", str(tmp_repo), "--assets", str(BRAND_ASSETS_DIR)) == EXIT_OK
    assert check_of(tmp_repo) == EXIT_OK


# ---------------------------------------------------------------------------
# 退出码 1：资产缺失 / 配置非法 / 用法错误
# ---------------------------------------------------------------------------


def test_missing_assets_dir_exit_one(tmp_repo: Path, tmp_path: Path) -> None:
    """Brand_Assets 目录不存在：退出码 1（需求 1.4）。"""
    assert run(
        "--apply", "--repo-root", str(tmp_repo), "--assets", str(tmp_path / "nope")
    ) == EXIT_ASSETS_INVALID


def test_incomplete_assets_dir_exit_one(
    tmp_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """资产目录缺文件：退出码 1，且诊断输出缺失文件路径（需求 1.4）。"""
    partial = tmp_path / "assets"
    partial.mkdir()
    (partial / "brand.yaml").write_text("brand_name: Ligent\n", encoding="utf-8")
    assert run(
        "--apply", "--repo-root", str(tmp_repo), "--assets", str(partial)
    ) == EXIT_ASSETS_INVALID
    assert "logo.ascii" in capsys.readouterr().err


def test_unknown_argument_exit_one(capsys: pytest.CaptureFixture[str]) -> None:
    """未知参数：退出码 1 并打印用法。退出码 2 已被「触点缺失」占用，不能给 argparse。"""
    assert run("--frobnicate") == EXIT_USAGE
    assert "用法" in capsys.readouterr().err


def test_conflicting_actions_exit_one() -> None:
    assert run("--apply", "--check") == EXIT_USAGE


def test_help_exit_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert run("--help") == EXIT_OK
    assert "--list-touchpoints" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 退出码 2：触点文件不存在
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rel", TOUCHPOINT_PATHS)
def test_missing_touchpoint_exit_two(
    tmp_repo: Path, rel: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """任一触点文件不存在：退出码 2，诊断含路径与预期用途（需求 2.4）。"""
    (tmp_repo / rel).unlink()
    assert apply_to(tmp_repo) == EXIT_TOUCHPOINT_MISSING
    err = capsys.readouterr().err
    assert rel in err.replace("\\", "/")
    assert "预期用途" in err


def test_missing_touchpoint_in_check_mode_exit_two(tmp_repo: Path) -> None:
    """check 模式下也是 2 而不是 4：文件不存在与「忘记 apply」是两回事。"""
    (tmp_repo / PLATFORM_CONF_PATH).unlink()
    assert check_of(tmp_repo) == EXIT_TOUCHPOINT_MISSING


# ---------------------------------------------------------------------------
# 退出码 3：锚点未命中
# ---------------------------------------------------------------------------


def test_broken_anchor_exit_three(
    tmp_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """锚点被上游改写：退出码 3，诊断含未命中锚点原文（需求 2.5、9.2）。"""
    path = tmp_repo / PLATFORM_CONF_PATH
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "echo    'Loading $demo_volume_label $demo_type kernel ...'",
            "echo \"Loading upstream rewrote this line\"",
        ),
        encoding="utf-8",
    )
    assert apply_to(tmp_repo) == EXIT_ANCHOR_MISS
    assert "未命中锚点原文" in capsys.readouterr().err


def test_broken_anchor_in_check_mode_also_exit_three(tmp_repo: Path) -> None:
    """check 遇到锚点漂移同样是 3，不是 4——排障动作不同，必须区分开。"""
    path = tmp_repo / PLATFORM_CONF_PATH
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "echo    'Loading $demo_volume_label $demo_type initial ramdisk ...'",
            "echo    'upstream drift'",
        ),
        encoding="utf-8",
    )
    assert check_of(tmp_repo) == EXIT_ANCHOR_MISS


# ---------------------------------------------------------------------------
# 退出码 4：check 发现未改造
# ---------------------------------------------------------------------------


def test_check_on_upstream_exit_four(
    tmp_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """未改造工作区上 check：退出码 4，并提示去跑 apply（需求 2.8）。"""
    assert check_of(tmp_repo) == EXIT_CHECK_FAILED
    err = capsys.readouterr().err
    assert "--apply" in err


def test_check_has_no_side_effects(tmp_repo: Path) -> None:
    """check 不写盘，也不留临时文件（需求 2.6）。"""
    before = snapshot(tmp_repo)
    assert check_of(tmp_repo) == EXIT_CHECK_FAILED
    assert snapshot(tmp_repo) == before
    assert not list(tmp_repo.rglob("*.ligent.tmp"))


def test_partial_rebrand_exit_four(tmp_repo: Path) -> None:
    """只改了一部分触点：check 仍报 4（「改造不完整」也归 4）。"""
    assert apply_to(tmp_repo) == EXIT_OK
    path = tmp_repo / "files/image_config/environment/motd"
    path.write_bytes((FIXTURES_UPSTREAM / "motd").read_bytes())
    assert check_of(tmp_repo) == EXIT_CHECK_FAILED


# ---------------------------------------------------------------------------
# 退出码 5：结构校验失败
# ---------------------------------------------------------------------------


def _break_unrelated_json(repo: Path) -> None:
    """删掉 NTP 段落前的一个逗号：JSON 破损，但 BANNER_MESSAGE 窗口完好。

    这模拟「触点都已改造，但有人手工把模板别处编辑坏了」——正是需求 3.1 想在
    构建前拦住的情形，也是退出码 5 在真实流程里的可达路径。
    """
    path = repo / INIT_CFG_PATH
    text = path.read_text(encoding="utf-8")
    broken = text.replace('    },\n    "NTP": {', '    }\n    "NTP": {')
    assert broken != text, "固件结构变化，需更新本用例的破坏点"
    path.write_text(broken, encoding="utf-8", newline="")


def test_verify_structure_on_upstream_exit_zero(tmp_repo: Path) -> None:
    """上游基线本身应通过结构校验，否则校验器自己就是坏的。"""
    assert run("--verify-structure", "--repo-root", str(tmp_repo)) == EXIT_OK


def test_verify_structure_detects_broken_json(tmp_repo: Path) -> None:
    assert apply_to(tmp_repo) == EXIT_OK
    _break_unrelated_json(tmp_repo)
    assert (
        run("--verify-structure", "--repo-root", str(tmp_repo))
        == EXIT_STRUCTURE_INVALID
    )


def test_check_reports_structure_failure_after_rebrand(tmp_repo: Path) -> None:
    """已改造 + 结构破损：check 报 5（不是 0）。"""
    assert apply_to(tmp_repo) == EXIT_OK
    _break_unrelated_json(tmp_repo)
    assert check_of(tmp_repo) == EXIT_STRUCTURE_INVALID


def test_unrebranded_and_broken_reports_four_not_five(tmp_repo: Path) -> None:
    """未改造 + 结构破损：优先报 4。先跑 apply 才谈得上校验改造结果。"""
    _break_unrelated_json(tmp_repo)
    assert check_of(tmp_repo) == EXIT_CHECK_FAILED


def test_skip_structure_bypasses_validation(tmp_repo: Path) -> None:
    assert apply_to(tmp_repo) == EXIT_OK
    _break_unrelated_json(tmp_repo)
    assert check_of(tmp_repo, "--skip-structure") == EXIT_OK


def test_bash_syntax_failure_detected(tmp_repo: Path) -> None:
    """bash -n 失败（需求 3.2）：未闭合的 if 会被拦下。"""
    assert apply_to(tmp_repo) == EXIT_OK
    path = tmp_repo / PLATFORM_CONF_PATH
    path.write_text(
        path.read_text(encoding="utf-8") + '\nif [ -z "$x" ]; then\n',
        encoding="utf-8",
        newline="",
    )
    results = validate_bash_syntax(tmp_repo)
    statuses = {r.check_id: r.status for r in results}
    assert FAIL in statuses.values()
    assert run("--verify-structure", "--repo-root", str(tmp_repo)) == EXIT_STRUCTURE_INVALID


# ---------------------------------------------------------------------------
# 结构校验的细节
# ---------------------------------------------------------------------------


def test_init_cfg_rendered_for_every_param_set(tmp_repo: Path) -> None:
    """每组代表性参数各出一条结果，且都不是 FAIL。"""
    assert apply_to(tmp_repo) == EXIT_OK
    results = validate_init_cfg(tmp_repo)
    assert len(results) == len(RENDER_PARAM_SETS)
    assert all(r.status in (OK, SKIP) for r in results), [r.format() for r in results]


def test_rendered_banner_motd_survives_json_roundtrip(tmp_repo: Path) -> None:
    """渲染后的 BANNER_MESSAGE.motd 能被 json 解析回带反斜杠的 ASCII art。

    ASCII art 里的 ``\\___`` 是最容易漏转义的一处，漏了会让整个 init_cfg.json
    解析失败、设备起不来（design.md「测试策略」）。
    """
    assert apply_to(tmp_repo) == EXIT_OK
    window = (tmp_repo / INIT_CFG_PATH).read_text(encoding="utf-8")
    motd_line = next(
        line for line in window.splitlines() if line.strip().startswith('"motd":')
    )
    value = json.loads(motd_line.strip().removeprefix('"motd":').rstrip(",").strip())
    assert "LIGENT" in value or "\\" in value
    assert "\n" in value


def test_list_touchpoints_output(capsys: pytest.CaptureFixture[str]) -> None:
    """``--list-touchpoints`` 每行一个路径、顺序与 TOUCHPOINT_PATHS 一致，
    且不夹带任何额外文字——CI 会把它直接喂给 ``sha256sum``。"""
    assert run("--list-touchpoints") == EXIT_OK
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == len(TOUCHPOINT_PATHS)
    assert [Path(line).name for line in lines] == [
        Path(rel).name for rel in TOUCHPOINT_PATHS
    ]


def test_list_touchpoints_paths_are_usable(tmp_repo: Path, capsys) -> None:
    """非当前目录的 ``--repo-root`` 输出绝对路径，且这些路径确实存在。"""
    assert run("--list-touchpoints", "--repo-root", str(tmp_repo)) == EXIT_OK
    for line in capsys.readouterr().out.splitlines():
        assert Path(line).is_file(), line


def test_missing_repo_root_exit_one(tmp_path: Path) -> None:
    assert run("--apply", "--repo-root", str(tmp_path / "absent")) == EXIT_USAGE
