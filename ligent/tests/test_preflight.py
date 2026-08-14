"""Preflight_Check 的示例级单元测试（任务 7.1、7.2、7.5）。

范围限定在**判定逻辑**（``preflight_eval.py``）与 ``preflight.sh`` 的静态自洽性：
* 每个维度的阈值边界与严重级别映射（需求 6.2、6.3、6.4、6.5、6.6）
* HTTP 状态码 → 可达性（需求 6.7），重点是 401 必须判可达
* 诊断文本里必须出现的实测值与所需下限（需求 6.2、6.3）
* PF-09 失败时必须出现 mirror 列表与回滚命令

对任意输入组合的全覆盖由任务 7.3/7.4 的 Property 13/14 负责，这里只钉住边界与
最容易回归的几处文本约定。``preflight.sh`` 的端到端行为在构建服务器上实测，
不在单测里模拟 docker 与网络——mock 出来的「通过」没有信息量。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from conftest import LIGENT_DIR

sys.path.insert(0, str(LIGENT_DIR))

from preflight_eval import (  # noqa: E402
    DOCKER_GROUP,
    EXIT_FATAL,
    EXIT_OK,
    EXIT_WARN,
    FAIL,
    MIN_CPU_CORES,
    MIN_FREE_DISK_GIB,
    MIN_MEMORY_GIB,
    MIRROR_ROLLBACK_COMMAND,
    OK,
    WARN,
    classify_http_status,
    evaluate_capacity_gate,
    evaluate_cpu,
    evaluate_disk,
    evaluate_docker_daemon,
    evaluate_docker_permission,
    evaluate_memory,
    evaluate_mirror_probe,
    evaluate_reachability,
    main,
    summarize,
)

PREFLIGHT_SH = LIGENT_DIR / "preflight.sh"


# ---------------------------------------------------------------------------
# PF-01 CPU：不足只告警（需求 6.4）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cores", [MIN_CPU_CORES, MIN_CPU_CORES + 1, 80])
def test_cpu_at_or_above_threshold_is_ok(cores: int) -> None:
    finding = evaluate_cpu(cores)
    assert finding.status == OK
    assert finding.exit_code == EXIT_OK


@pytest.mark.parametrize("cores", [0, 1, MIN_CPU_CORES - 1])
def test_cpu_below_threshold_warns_but_continues(cores: int) -> None:
    """需求 6.4：CPU 不足是**告警**，不是失败。"""
    finding = evaluate_cpu(cores)
    assert finding.status == WARN
    assert finding.exit_code == EXIT_WARN
    assert not finding.fatal
    # 告警不能把整体判定拉成失败
    assert summarize([finding]) == EXIT_OK


# ---------------------------------------------------------------------------
# PF-02 内存 / PF-03/04 磁盘：不足即致命（需求 6.2、6.3）
# ---------------------------------------------------------------------------


def test_memory_boundary() -> None:
    assert evaluate_memory(MIN_MEMORY_GIB).status == OK
    assert evaluate_memory(MIN_MEMORY_GIB - 0.1).status == FAIL


def test_memory_failure_reports_measured_and_required() -> None:
    """需求 6.3：必须输出实测内存总量与所需下限。"""
    finding = evaluate_memory(8.0)
    assert finding.fatal
    assert "8.0" in finding.measured
    assert "16" in finding.required
    assert "8.0" in finding.format() and "16" in finding.format()


def test_disk_boundary_and_diagnostics() -> None:
    """需求 6.2：必须输出实测可用空间与所需下限。"""
    assert evaluate_disk("PF-03", "artifact_store_free", MIN_FREE_DISK_GIB).status == OK
    finding = evaluate_disk("PF-03", "artifact_store_free", 12.5, path="/data")
    assert finding.fatal
    assert "12.5" in finding.measured
    assert "/data" in finding.measured
    assert "150" in finding.required


def test_baseline_server_values_all_pass() -> None:
    """构建服务器实测基线（design.md 6.2 节）：80 核 / 188.4 GiB / 369 GiB 全通过。"""
    findings = [
        evaluate_cpu(80),
        evaluate_memory(188.4),
        evaluate_disk("PF-03", "artifact_store_free", 369.0),
        evaluate_disk("PF-04", "workspace_free", 369.0),
        evaluate_capacity_gate(369.0),
    ]
    assert [f.status for f in findings] == [OK] * 5
    assert summarize(findings) == EXIT_OK


# ---------------------------------------------------------------------------
# PF-10 组合门禁
# ---------------------------------------------------------------------------


def test_capacity_gate_requires_both_static_floor_and_estimate() -> None:
    # 过了静态下限但装不下「构建 + 归档」估值 → 仍然失败
    finding = evaluate_capacity_gate(160.0, build_estimate_gib=200.0, archive_estimate_gib=6.0)
    assert finding.fatal
    assert "206" in finding.required


def test_capacity_gate_marks_cleanup_stage() -> None:
    """清理前后的诊断必须可区分，否则日志里看不出清理有没有起作用。"""
    before = evaluate_capacity_gate(10.0, cleaned=False)
    after = evaluate_capacity_gate(10.0, cleaned=True)
    assert "清理前" in before.measured and "应先执行归档清理" in before.detail
    assert "清理后" in after.measured and "归档清理已执行仍不足" in after.detail


# ---------------------------------------------------------------------------
# PF-05 / PF-06 Docker（需求 6.5、6.6）
# ---------------------------------------------------------------------------


def test_docker_daemon_failure_points_at_systemctl_output() -> None:
    finding = evaluate_docker_daemon(1)
    assert finding.fatal
    assert any("systemctl status docker" in hint for hint in finding.hints)


def test_docker_group_hint() -> None:
    """任务 7.5：docker 权限失败诊断必须含 `docker` 组与 usermod -aG（需求 6.6）。"""
    finding = evaluate_docker_permission(1, user="ligent-ci")
    assert finding.fatal
    text = finding.format()
    assert DOCKER_GROUP == "docker"
    assert "docker" in text
    assert "usermod -aG" in text
    assert "usermod -aG docker ligent-ci" in text


def test_docker_permission_ok_has_no_hint() -> None:
    finding = evaluate_docker_permission(0, user="user")
    assert finding.status == OK
    assert finding.hints == ()


# ---------------------------------------------------------------------------
# PF-09 mirror 探针
# ---------------------------------------------------------------------------


def test_mirror_failure_lists_mirrors_and_rollback() -> None:
    """mirror 是第三方服务，失败日志必须自带 mirror 列表与回滚命令。"""
    finding = evaluate_mirror_probe(
        1, mirrors=("https://docker.m.daocloud.io", "https://dockerproxy.net")
    )
    text = finding.format()
    assert finding.fatal
    assert "docker.m.daocloud.io" in text
    assert MIRROR_ROLLBACK_COMMAND in text
    assert "/etc/docker/.ligent-last-backup" in text


def test_mirror_success_records_elapsed() -> None:
    """探针耗时是每次构建的固定开销，必须出现在日志里以便观察退化。"""
    finding = evaluate_mirror_probe(0, elapsed_seconds=1.85)
    assert finding.status == OK
    assert "1.8" in finding.measured or "1.9" in finding.measured


# ---------------------------------------------------------------------------
# PF-07 / PF-08 可达性（需求 6.7）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["200", "301", "401", "403", "500", "503", 200, 401])
def test_any_http_status_other_than_000_is_reachable(code) -> None:
    """401 是 ACR 的正常鉴权响应：判失败会让 Preflight 100% 误报。"""
    assert classify_http_status(code) is True


@pytest.mark.parametrize("code", ["000", 0, "", "   ", "curl: (6)"])
def test_only_connection_layer_failure_is_unreachable(code) -> None:
    assert classify_http_status(code) is False


def test_acr_401_passes_and_000_fails() -> None:
    ok = evaluate_reachability(
        "PF-08", "acr:publicmirror", "https://publicmirror.azurecr.io/v2/", "401"
    )
    bad = evaluate_reachability(
        "PF-08", "acr:publicmirror", "https://publicmirror.azurecr.io/v2/", "000"
    )
    assert ok.status == OK and "401" in ok.measured
    assert bad.status == FAIL and "000" in bad.measured


def test_any_unreachable_target_makes_preflight_fail() -> None:
    """需求 6.7：至少一个目标不可达即失败。"""
    findings = [
        evaluate_reachability("PF-07", "net:github.com", "https://github.com/", "200"),
        evaluate_reachability("PF-07", "net:deb.debian.org", "https://deb.debian.org/", "000"),
    ]
    assert summarize(findings) == EXIT_FATAL


# ---------------------------------------------------------------------------
# 输出格式与 CLI 契约
# ---------------------------------------------------------------------------


def test_format_matches_structure_module_convention() -> None:
    """与 ``structure.py`` / ``verify_brand.py`` 的 ``[STATUS] ID name : detail`` 一致。"""
    line = evaluate_cpu(80).format()
    assert line.startswith("[ OK ] PF-01 cpu_cores")
    assert ": 80 (>= 8)" in line


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["cpu", "--measured", "80"], EXIT_OK),
        (["cpu", "--measured", "4"], EXIT_WARN),
        (["memory", "--measured", "8"], EXIT_FATAL),
        (["http", "--check-id", "PF-08", "--name", "acr", "--target", "x", "--status", "401"], EXIT_OK),
        (["http", "--check-id", "PF-08", "--name", "acr", "--target", "x", "--status", "000"], EXIT_FATAL),
    ],
)
def test_cli_exit_codes(argv: list[str], expected: int, capsys) -> None:
    """bash 侧靠退出码分流，0/1/2 的映射是接口的一部分。"""
    assert main(argv) == expected
    assert capsys.readouterr().out.strip()


# ---------------------------------------------------------------------------
# preflight.sh 的静态自洽性
# ---------------------------------------------------------------------------


def test_preflight_sh_passes_bash_syntax_check() -> None:
    proc = subprocess.run(["bash", "-n", str(PREFLIGHT_SH)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_preflight_sh_has_no_hardcoded_thresholds() -> None:
    """阈值只能有一处定义：bash 里出现 150/16/8 的比较就说明已经分叉了。"""
    text = PREFLIGHT_SH.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    for forbidden in ("-ge 150", "-lt 150", "-ge 16", "-lt 16", "-ge 8", "-lt 8"):
        assert forbidden not in code, f"preflight.sh 里出现了硬编码阈值比较：{forbidden}"


def test_preflight_sh_invokes_every_eval_subcommand() -> None:
    """10 项检查必须都被真的调用到。

    check ID 字面量归 ``preflight_eval.py``（它才是判定方），所以这里断言的是
    **调用关系**：每个判定子命令都出现在 ``preflight.sh`` 里。这样「加了判定却忘了
    在脚本里调」这个最容易犯的错会被立刻抓住，而不是等到跑完发现少了两行输出。
    """
    text = PREFLIGHT_SH.read_text(encoding="utf-8")
    for subcommand in (
        "cpu",  # PF-01
        "memory",  # PF-02
        "disk",  # PF-03 / PF-04
        "docker-daemon",  # PF-05
        "docker-perm",  # PF-06
        "http",  # PF-07 / PF-08
        "mirror",  # PF-09
        "capacity",  # PF-10
    ):
        assert f"evaluate {subcommand} " in text or f"({subcommand} " in text, (
            f"preflight.sh 没有调用判定子命令 {subcommand}"
        )
    # PF-03/PF-04 与 PF-07/PF-08 共用子命令，靠 --check-id 区分，必须显式传对
    for check_id in ("PF-03", "PF-04", "PF-07", "PF-08"):
        assert check_id in text, f"preflight.sh 未给共用子命令传 {check_id}"


def test_every_check_id_is_defined_in_eval_module() -> None:
    """PF-01..PF-10 十项判定在判定模块里都有实现。"""
    source = (LIGENT_DIR / "preflight_eval.py").read_text(encoding="utf-8")
    for check_id in [f"PF-{n:02d}" for n in range(1, 11)]:
        assert check_id in source, f"preflight_eval.py 未涉及 {check_id}"


def test_preflight_sh_prints_snapshot_unconditionally() -> None:
    """需求 6.1：CPU、内存、可用空间、Docker 状态四项无条件输出。"""
    text = PREFLIGHT_SH.read_text(encoding="utf-8")
    for field in ("cpu_cores", "memory_total", "store_filesystem", "docker_status"):
        assert field in text


def test_http_probe_uses_head_and_retries() -> None:
    """PF-07/PF-08 的探测方式是判定正确性的一部分。

    实测：github.com 首页 body 慢到会撞上 ``--max-time``，此时 curl 的
    ``%{http_code}`` 归 000，headers 里的 200 被丢掉——GET 探测会让 PF-07 随机
    误报。用 HEAD + 有界重试把链路抖动与「真的连不上」区分开。
    """
    text = PREFLIGHT_SH.read_text(encoding="utf-8")
    assert "curl -s -I" in text, "PF-07/PF-08 必须用 HEAD 探测，避免慢 body 撞超时"
    assert "--connect-timeout" in text
    assert "CURL_ATTEMPTS" in text, "单次探测会让链路抖动变成 Preflight 误报"
