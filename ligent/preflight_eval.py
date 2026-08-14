#!/usr/bin/env python3
"""Preflight_Check 的可测判定逻辑（任务 7.2；design.md 6.2 节，需求 6.2–6.7）。

``ligent/preflight.sh`` 负责**采集**（``nproc``、``/proc/meminfo``、``df``、
``docker``、``curl``），本模块负责**判定**。这样切分的理由有三条：

1. **判定可被测试直接驱动。** 采集依赖真实机器状态，无法在测试里穷举；而
   「80 核 → OK」「4 核 → WARN」「状态码 000 → 不可达」这类映射是纯函数，
   属性测试（任务 7.3、7.4）可以对任意输入组合直接调用，不需要 mock 任何命令。
2. **阈值只有一处定义。** :data:`MIN_CPU_CORES` 等常量是唯一真源，bash 侧不再
   写第二份数字比较——两处阈值漂移是这类脚本最典型的失效方式。
3. **严重级别与退出码的映射只有一处。** 「磁盘/内存/Docker 不足 = 致命」与
   「CPU 不足 = 告警继续」（需求 6.4）是需求层面的区别，写在 Python 里比散落在
   bash 的 ``if`` 分支里更难写错。

## 严重级别与进程退出码

每个判定子命令都只输出**一行**结果并用退出码表达严重级别，供 bash 累加：

===== ============================================
退出码 含义
===== ============================================
0     ``OK``   —— 该维度通过
1     ``FAIL`` —— 致命，作业必须失败（需求 6.2/6.3/6.5/6.6/6.7）
2     ``WARN`` —— 告警但继续（需求 6.4，目前只有 PF-01）
===== ============================================

用 2 而不是别的值表示告警：``bash`` 里 ``case $rc in 0) ;; 2) warn ;; *) fatal`` 的
写法能把「未预料到的退出码」（Python 崩了、参数写错）自动归入致命，这是安全的
默认方向——自检脚本自己出错时不应该悄悄放行。

## 单位一律用 GiB

``/proc/meminfo`` 的 ``MemTotal`` 是 KiB，``df -k`` 的输出是 KiB，两者除以
1024² 得到的都是 GiB。需求文本里的「16 GB」「150 GB」按 GiB 解释——这也是
``free -g`` / ``df -h`` 给运维看的口径，日志里的数字与人工 ``df -h`` 对得上，
排障时不会因为 GB/GiB 差 7% 而怀疑脚本算错。

## PF-07 的判据为什么也是「非 000」

design.md 6.2 节 PF-07 那一行的「HTTP 状态码 200」是**实测值**而非判据。判据
统一为 :func:`classify_http_status`：只有 ``000``（curl 连接层失败）算不可达。
理由与 PF-08 相同——``deb.debian.org`` 这类站点在 CDN 波动时可能返回 301/403/503，
它们都说明域名解析、TLS、路由是通的，构建照样能走；把它们判成失败只会制造
误报。「至少一个目标不可达时 Preflight 失败」这条（需求 6.7）由调用方对全部
目标的结果做累加实现。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 阈值（唯一真源；design.md 6.2 节，实测值见该节表格）
# ---------------------------------------------------------------------------

#: PF-01 CPU 逻辑核数下限。不足**仅告警继续**（需求 6.4）
MIN_CPU_CORES = 8

#: PF-02 内存总量下限（GiB，需求 6.3）
MIN_MEMORY_GIB = 16.0

#: PF-03/PF-04/PF-10 可用磁盘下限（GiB）。
#: requirements.md 需求 6.2 原写 300 GB，design.md 6.2 节据实测下调为 150 GiB：
#: 实测可用 369 GiB，门禁设在 300 会让归档存下 30 余 GB 就永久拒绝启动，
#: 门禁比它保护的对象更早失效。
MIN_FREE_DISK_GIB = 150.0

#: PF-10 单次全量构建的磁盘占用估值（GiB）：target/ + docker 层 + dpkg 缓存
#: 量级 80–120 GiB，取上界。可由 ``LIGENT_BUILD_ESTIMATE_GB`` 覆盖。
DEFAULT_BUILD_ESTIMATE_GIB = 120.0

#: PF-10 归档增量估值（GiB）：一个 sonic-vs.bin 约 2 GiB，保留 3 份。
DEFAULT_ARCHIVE_ESTIMATE_GIB = 6.0

#: curl 连接层失败时 ``%{http_code}`` 的取值。唯一判为不可达的状态码。
UNREACHABLE_STATUS = "000"

# ---------------------------------------------------------------------------
# 状态与退出码
# ---------------------------------------------------------------------------

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_WARN = 2

_EXIT_BY_STATUS = {OK: EXIT_OK, FAIL: EXIT_FATAL, WARN: EXIT_WARN}

#: docker 权限修复提示（需求 6.6：必须输出所需用户组名称）。
#: 用例 ``test_docker_group_hint`` 直接断言这段文本，因此它是接口而不是随手写的话。
DOCKER_GROUP = "docker"
DOCKER_GROUP_HINT = (
    "需将用户加入 `{group}` 组：`sudo usermod -aG {group} {user}`"
    "（执行后需重新登录或 `newgrp {group}` 使组生效）"
)

#: PF-09 失败时给出的回滚命令。mirror 是第三方公共服务，随时可能失效，
#: 所以「怎么退回原配置」必须直接出现在失败日志里，而不是让人去翻文档。
MIRROR_ROLLBACK_COMMAND = (
    "sudo cp -a $(cat /etc/docker/.ligent-last-backup) /etc/docker/daemon.json"
    " && sudo systemctl restart docker"
)


@dataclass(frozen=True)
class Finding:
    """一个维度的判定结果。

    ``measured`` 与 ``required`` 分开存放，是因为需求 6.2/6.3 明确要求「输出实测值
    与所需下限」——把它们拼进一句 ``detail`` 字符串虽然打印一样，但属性测试
    （任务 7.3）需要分别断言这两项存在。
    """

    check_id: str
    name: str
    status: str
    measured: str
    required: str = ""
    detail: str = ""
    #: 失败时附带的多行诊断（修复命令、mirror 列表等）
    hints: tuple[str, ...] = field(default_factory=tuple)

    @property
    def fatal(self) -> bool:
        return self.status == FAIL

    @property
    def warned(self) -> bool:
        return self.status == WARN

    @property
    def exit_code(self) -> int:
        return _EXIT_BY_STATUS[self.status]

    def format(self) -> str:
        """与 ``ligent/structure.py`` / ``ligent/verify_brand.py`` 一致的单行格式。

        形如 ``[ OK ] PF-01 cpu_cores                : 80 (>= 8)``。
        """
        body = self.measured
        if self.required:
            body = f"{body} ({self.required})"
        if self.detail:
            body = f"{body} — {self.detail}" if body else self.detail
        line = f"[{self.status:^4}] {self.check_id} {self.name:<24}: {body}"
        for hint in self.hints:
            line += f"\n       ↳ {hint}"
        return line


# ---------------------------------------------------------------------------
# 资源维度判定（需求 6.2、6.3、6.4）
# ---------------------------------------------------------------------------


def evaluate_cpu(cores: int) -> Finding:
    """PF-01：``nproc >= 8``。**不足只告警**并继续（需求 6.4）。"""
    ok = cores >= MIN_CPU_CORES
    return Finding(
        check_id="PF-01",
        name="cpu_cores",
        status=OK if ok else WARN,
        measured=str(cores),
        required=f">= {MIN_CPU_CORES}",
        detail="" if ok else "核数偏低，构建会明显变慢，但不阻断（需求 6.4）",
    )


def evaluate_memory(total_gib: float) -> Finding:
    """PF-02：``MemTotal >= 16 GiB``，不足即致命（需求 6.3）。"""
    ok = total_gib >= MIN_MEMORY_GIB
    return Finding(
        check_id="PF-02",
        name="memory_total",
        status=OK if ok else FAIL,
        measured=f"{total_gib:.1f} GiB",
        required=f">= {MIN_MEMORY_GIB:.0f} GiB",
        detail="" if ok else "内存低于下限，sonic-slave 并行构建会 OOM",
    )


def evaluate_disk(check_id: str, name: str, free_gib: float, path: str = "") -> Finding:
    """PF-03/PF-04：目标文件系统可用空间 ``>= 150 GiB``，不足即致命（需求 6.2）。"""
    ok = free_gib >= MIN_FREE_DISK_GIB
    return Finding(
        check_id=check_id,
        name=name,
        status=OK if ok else FAIL,
        measured=f"{free_gib:.1f} GiB free" + (f" on {path}" if path else ""),
        required=f">= {MIN_FREE_DISK_GIB:.0f} GiB",
        detail="" if ok else "可用空间低于下限",
    )


def evaluate_capacity_gate(
    free_gib: float,
    build_estimate_gib: float = DEFAULT_BUILD_ESTIMATE_GIB,
    archive_estimate_gib: float = DEFAULT_ARCHIVE_ESTIMATE_GIB,
    cleaned: bool = False,
) -> Finding:
    """PF-10：组合判据 = 静态下限 **且** 预估「本次构建 + 归档」不超可用空间。

    ``cleaned`` 表示这次判定发生在归档清理**之后**。清理前不足不算失败（需求 8.6
    的清理逻辑先介入），清理后仍不足才失败——这正是把「容量不足先清理再判断」
    显式化的地方，而不是靠一个静态阈值硬扛（design.md 6.2 节）。
    """
    needed = build_estimate_gib + archive_estimate_gib
    ok = free_gib >= MIN_FREE_DISK_GIB and free_gib >= needed
    stage = "清理后" if cleaned else "清理前"
    return Finding(
        check_id="PF-10",
        name="capacity_gate",
        status=OK if ok else FAIL,
        measured=f"{free_gib:.1f} GiB free（{stage}）",
        required=(
            f">= {MIN_FREE_DISK_GIB:.0f} GiB 且 >= 预估 {needed:.0f} GiB"
            f"（构建 {build_estimate_gib:.0f} + 归档 {archive_estimate_gib:.0f}）"
        ),
        detail=""
        if ok
        else (
            "容量不足以完成本次构建与归档"
            + ("（归档清理已执行仍不足）" if cleaned else "，应先执行归档清理")
        ),
        hints=()
        if ok
        else (
            "处置：清理 $ARTIFACT_STORE 下的历史归档，或 "
            "`docker image prune --filter until=168h`；"
            "长期方案见 tasks.md 任务 18.1（加盘而不是调大保留数）",
        ),
    )


# ---------------------------------------------------------------------------
# Docker 维度（需求 6.5、6.6）
# ---------------------------------------------------------------------------


def evaluate_docker_daemon(returncode: int, version: str = "") -> Finding:
    """PF-05：``docker info`` 退出码 0。失败即致命（需求 6.5）。

    ``systemctl status docker --no-pager`` 的全文由 :mod:`preflight.sh` 采集后打印
    ——它是采集侧的事，本函数只负责判定与提示去看哪条命令的输出。
    """
    ok = returncode == 0
    return Finding(
        check_id="PF-05",
        name="docker_daemon",
        status=OK if ok else FAIL,
        measured=(version or "available") if ok else f"docker info rc={returncode}",
        required="docker info rc=0",
        detail="" if ok else "Docker daemon 不可用",
        hints=()
        if ok
        else (
            "已在下方输出 `systemctl status docker --no-pager` 全文",
            "处置：`sudo systemctl start docker` 后重试",
        ),
    )


def evaluate_docker_permission(returncode: int, user: str = "$(whoami)") -> Finding:
    """PF-06：``docker ps`` 退出码 0。失败即致命，并输出所需用户组（需求 6.6）。"""
    ok = returncode == 0
    return Finding(
        check_id="PF-06",
        name="docker_permission",
        status=OK if ok else FAIL,
        measured=f"user={user} " + ("ok" if ok else f"docker ps rc={returncode}"),
        required="docker ps rc=0",
        detail="" if ok else "当前用户无权执行 docker 命令",
        hints=()
        if ok
        else (DOCKER_GROUP_HINT.format(group=DOCKER_GROUP, user=user),),
    )


def evaluate_mirror_probe(
    returncode: int,
    mirrors: tuple[str, ...] = (),
    elapsed_seconds: float | None = None,
) -> Finding:
    """PF-09：``docker pull hello-world`` 探针。

    mirror 是第三方公共服务（阿里云 ``0e7iorp9.mirror.aliyuncs.com`` 的失效就是
    前车之鉴），配置正确不等于当下可用。探针镜像只有几 KB，代价可以忽略；
    失败时把 mirror 列表与回滚命令一并打进日志（design.md 6.2 节 PF-09）。
    """
    ok = returncode == 0
    measured = "hello-world pull ok" if ok else f"docker pull rc={returncode}"
    if elapsed_seconds is not None:
        measured += f"，耗时 {elapsed_seconds:.1f}s"
    hints: list[str] = []
    if not ok:
        hints.append(
            "当前 registry-mirrors: "
            + (", ".join(mirrors) if mirrors else "（/etc/docker/daemon.json 未配置）")
        )
        hints.append(f"回滚命令：{MIRROR_ROLLBACK_COMMAND}")
        hints.append(
            "mirror 是第三方公共服务，失效后需换用可用的 mirror 或直连；"
            "换配置后 `sudo systemctl restart docker`"
        )
    return Finding(
        check_id="PF-09",
        name="registry_mirror",
        status=OK if ok else FAIL,
        measured=measured,
        required="docker pull rc=0",
        detail="" if ok else "registry mirror 不可用，构建期拉取基础镜像会失败",
        hints=tuple(hints),
    )


# ---------------------------------------------------------------------------
# 网络可达性（需求 6.7）
# ---------------------------------------------------------------------------


def classify_http_status(status_code: str | int) -> bool:
    """状态码 → 是否可达。``000`` 不可达，其余（含 200/401/403/5xx）可达。

    ACR 在未携带 token 时返回 **401 是正常鉴权响应**，说明域名解析、TLS 握手、
    路由全通。把 401 当失败会让 Preflight 100% 误报（design.md 6.2 节 PF-08）。
    ``000`` 是 curl 在连接层失败时 ``%{http_code}`` 的取值，是唯一的不可达信号。

    非数字输入（空串、``curl: (6)`` 之类）一并判为不可达：拿不到状态码本身就
    说明这次探测没有走到 HTTP 层。
    """
    text = str(status_code).strip()
    if not text.isdigit():
        return False
    return int(text) != int(UNREACHABLE_STATUS)


def evaluate_reachability(
    check_id: str, name: str, target: str, status_code: str | int
) -> Finding:
    """PF-07/PF-08 的单目标判定。判据统一为 :func:`classify_http_status`。"""
    reachable = classify_http_status(status_code)
    code = str(status_code).strip() or UNREACHABLE_STATUS
    return Finding(
        check_id=check_id,
        name=name,
        status=OK if reachable else FAIL,
        measured=f"{target} HTTP {code}",
        required=f"HTTP != {UNREACHABLE_STATUS}",
        detail=""
        if reachable
        else "curl 连接层失败（DNS / TLS / 路由），未取得任何 HTTP 状态码",
        hints=()
        if reachable
        else (
            f"处置：在服务器上执行 `curl -sSv --max-time 10 {target}` 看失败在哪一层；"
            "若需代理，配置 docker 与 shell 的 proxy 环境变量",
        ),
    )


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


def summarize(findings: list[Finding]) -> int:
    """任一致命即 1；否则 0（告警不影响退出码，需求 6.4）。"""
    return EXIT_FATAL if any(f.fatal for f in findings) else EXIT_OK


def thresholds() -> dict[str, object]:
    """全部阈值，供 ``preflight.sh --thresholds`` 与文档核对。"""
    return {
        "MIN_CPU_CORES": MIN_CPU_CORES,
        "MIN_MEMORY_GIB": MIN_MEMORY_GIB,
        "MIN_FREE_DISK_GIB": MIN_FREE_DISK_GIB,
        "DEFAULT_BUILD_ESTIMATE_GIB": DEFAULT_BUILD_ESTIMATE_GIB,
        "DEFAULT_ARCHIVE_ESTIMATE_GIB": DEFAULT_ARCHIVE_ESTIMATE_GIB,
        "UNREACHABLE_STATUS": UNREACHABLE_STATUS,
    }


# ---------------------------------------------------------------------------
# CLI（供 preflight.sh 逐项调用）
# ---------------------------------------------------------------------------


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="preflight_eval.py",
        description=(
            "Preflight_Check 判定逻辑。每个子命令输出一行结果，"
            "退出码 0=OK / 1=FAIL / 2=WARN。"
        ),
    )
    sub = parser.add_subparsers(dest="kind", required=True)

    cpu = sub.add_parser("cpu", help="PF-01 CPU 逻辑核数")
    cpu.add_argument("--measured", type=int, required=True)

    mem = sub.add_parser("memory", help="PF-02 内存总量（GiB）")
    mem.add_argument("--measured", type=float, required=True)

    disk = sub.add_parser("disk", help="PF-03/PF-04 可用空间（GiB）")
    disk.add_argument("--check-id", required=True)
    disk.add_argument("--name", required=True)
    disk.add_argument("--measured", type=float, required=True)
    disk.add_argument("--path", default="")

    cap = sub.add_parser("capacity", help="PF-10 磁盘容量组合门禁")
    cap.add_argument("--free", type=float, required=True)
    cap.add_argument("--build-estimate", type=float, default=DEFAULT_BUILD_ESTIMATE_GIB)
    cap.add_argument(
        "--archive-estimate", type=float, default=DEFAULT_ARCHIVE_ESTIMATE_GIB
    )
    cap.add_argument("--cleaned", action="store_true")

    daemon = sub.add_parser("docker-daemon", help="PF-05 docker info")
    daemon.add_argument("--rc", type=int, required=True)
    daemon.add_argument("--version", default="")

    perm = sub.add_parser("docker-perm", help="PF-06 docker ps 权限")
    perm.add_argument("--rc", type=int, required=True)
    perm.add_argument("--user", default="$(whoami)")

    mirror = sub.add_parser("mirror", help="PF-09 docker pull hello-world 探针")
    mirror.add_argument("--rc", type=int, required=True)
    mirror.add_argument("--mirrors", default="")
    mirror.add_argument("--elapsed", type=float, default=None)

    http = sub.add_parser("http", help="PF-07/PF-08 单目标可达性")
    http.add_argument("--check-id", required=True)
    http.add_argument("--name", required=True)
    http.add_argument("--target", required=True)
    http.add_argument("--status", required=True)

    sub.add_parser("thresholds", help="输出全部阈值")
    return parser


def evaluate_from_args(args: argparse.Namespace) -> Finding:
    if args.kind == "cpu":
        return evaluate_cpu(args.measured)
    if args.kind == "memory":
        return evaluate_memory(args.measured)
    if args.kind == "disk":
        return evaluate_disk(args.check_id, args.name, args.measured, args.path)
    if args.kind == "capacity":
        return evaluate_capacity_gate(
            args.free, args.build_estimate, args.archive_estimate, args.cleaned
        )
    if args.kind == "docker-daemon":
        return evaluate_docker_daemon(args.rc, args.version)
    if args.kind == "docker-perm":
        return evaluate_docker_permission(args.rc, args.user)
    if args.kind == "mirror":
        return evaluate_mirror_probe(args.rc, _split_csv(args.mirrors), args.elapsed)
    if args.kind == "http":
        return evaluate_reachability(args.check_id, args.name, args.target, args.status)
    raise AssertionError(f"未处理的子命令：{args.kind}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.kind == "thresholds":
        for key, value in thresholds().items():
            print(f"{key}={value}")
        return EXIT_OK
    finding = evaluate_from_args(args)
    print(finding.format())
    return finding.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
