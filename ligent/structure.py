#!/usr/bin/env python3
"""改造后的构建输入结构校验（任务 4.2，需求 3.1、3.2）。

需求 3.1 与 3.2 的要求是「Rebrand_Tool 执行完成后」两类构建输入仍然合法：

* ``files/build_templates/init_cfg.json.j2`` 经 Jinja2 渲染后是合法 JSON 文档；
* ``installer/default_platform.conf``（以及同为 shell 的 ``installer/install.sh``）
  通过 ``bash -n``。

这两条是**装机期才会暴露的错误的构建期防线**。JSON 破损的后果尤其重：
``init_cfg.json`` 解析失败会让设备起不来，而 MOTD 的 ASCII art 里满是
``\\___``、``\\| |`` 这样的反斜杠，是最容易漏转义的一处。

## 为什么渲染要起子进程调 ``j2``

上游 ``sonic_debian_extension.j2`` 里就是 ``j2 files/build_templates/init_cfg.json.j2``
（无数据文件，j2cli 回落到 env 格式读环境变量）。用同一个二进制、同一种取参方式
校验，测的才是构建实际会走的路径；换成进程内 Jinja2 会引入「两套渲染器行为差异」
这个新的失效来源。服务器已实测 ``j2cli 0.3.12b0 / Jinja2 3.0.3``。

进程内 Jinja2（需 ``jinja2.ext.do``，模板用了 ``{% do %}``）作为唯一一级回落，
只在 ``j2`` 不在 ``PATH`` 上时启用，且在输出里标明用的是哪个渲染器——降级必须可见，
否则「校验通过」会悄悄变成安慰剂。两者都不可用时输出 ``SKIP`` 而不是 ``OK``。

## 渲染参数从哪来

模板引用了 26 个构建期变量（``include_*``、``sonic_asic_platform``、
``installer_services`` 等），真实取值由 ``rules/config`` 与各平台规则注入。校验只
需要「一组能走通全部结构分支的代表性取值」，因此这里显式给出两组：

* ``vs-full``：贴近本流水线的 ``PLATFORM=vs`` + 多数 feature 打开；
* ``minimal``：几乎全部 feature 关闭 + ``BUILD_REDUCE_IMAGE_SIZE=y``。

两组走的是模板里不同的 ``{% if %}`` 分支（尤其是 ``FEATURE`` 列表的元素个数与
``{% if not loop.last %},{% endif %}`` 逗号逻辑），一起校验能挡住「只在某个
feature 组合下才缺/多一个逗号」这类问题。**环境不继承** ``os.environ`` 的模板变量，
只传显式给定的取值 + ``PATH``/locale，避免宿主 shell 里恰好设了同名变量而让校验
结果随机漂移。

退出码 5（design.md 4.5 节 / 「错误处理 → 退出码契约」）：结构校验失败。
它与退出码 3（锚点漂移）的区别是：3 说明上游变了需要改锚点，5 说明改造结果本身
把构建输入弄坏了，该去查 ``brand.yaml`` 与策略实现。

## 退出码 5 的可达性

已在构建服务器实测：**任何通过 :mod:`brandconfig` 白名单校验的 ``brand_name``
都不会触发退出码 5。** 白名单 ``[A-Za-z0-9 ._-]`` 已经把 ``"``、``\\``、``$``、
反引号、``;`` 等唯一可能破坏 JSON 字符串或 bash 语法的字符挡在退出码 1；加上
``json.dumps`` 生成 JSON 值，「品牌资产内容破坏结构」这条路在正常配置下不可达。

这不是把校验做成安慰剂的理由。它真正拦住的是**不经本工具产生的破损**：

* 上游 rebase 在触点的非锚点区域改出了不合法的 JSON/shell（锚点仍在，所以退出码
  3 拦不住，Property 10 明确要求这种情况 apply 仍为 0）；
* 有人手工编辑了这些文件；
* 将来放宽白名单、或新增触点/策略时引入的回归。

也就是说，退出码 5 是**纵深防御**而不是主路径。它的价值在于把「装机才炸」变成
「构建前 30 秒就炸」。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

try:  # 直接跑脚本 / 测试把 ligent/ 注入 sys.path 时
    from fileops import ENCODING
except ImportError:  # pragma: no cover - 以 `python3 -m ligent.structure` 导入时
    from .fileops import ENCODING  # type: ignore

# ---------------------------------------------------------------------------
# 退出码与异常
# ---------------------------------------------------------------------------

#: 改造后结构校验失败（需求 3.1、3.2）
EXIT_STRUCTURE_INVALID = 5


class StructureError(Exception):
    """结构校验失败，携带退出码 5。"""

    exit_code = EXIT_STRUCTURE_INVALID


# ---------------------------------------------------------------------------
# 被校验对象
# ---------------------------------------------------------------------------

INIT_CFG_PATH = "files/build_templates/init_cfg.json.j2"
BASH_PATHS: tuple[str, ...] = (
    "installer/default_platform.conf",
    "installer/install.sh",
)

#: 渲染 init_cfg.json.j2 用的代表性参数组（见模块 docstring）
RENDER_PARAM_SETS: dict[str, dict[str, str]] = {
    "vs-full": {
        "default_buffer_model": "traditional",
        "sonic_asic_platform": "vs",
        "shutdown_bgp_on_start": "n",
        "enable_pfcwd_on_start": "n",
        "enable_auto_tech_support": "y",
        "BUILD_REDUCE_IMAGE_SIZE": "n",
        "include_router_advertiser": "y",
        "include_lldp": "y",
        "include_snmp": "y",
        "include_teamd": "y",
        "include_dhcp_server": "n",
        "include_iccpd": "n",
        "include_mgmt_framework": "y",
        "include_mux": "n",
        "include_nat": "n",
        "include_p4rt": "n",
        "include_restapi": "n",
        "include_sflow": "y",
        "include_macsec": "y",
        "include_system_gnmi": "y",
        "include_system_telemetry": "y",
        "include_system_otel": "n",
        "include_system_eventd": "y",
        "include_kubernetes": "n",
        "installer_services": (
            "bgp.service database.service pmon.service swss.service syncd.service "
            "lldp.service snmp.service teamd.service radv.service dhcp_relay.service "
            "mgmt-framework.service sflow.service macsec.service gnmi.service "
            "telemetry.service eventd.service gbsyncd.service "
            "bgp@.service swss@.service syncd@.service"
        ),
    },
    # 另一条分支：feature 最少、开启体积裁剪、非 vs 平台。
    "minimal": {
        "default_buffer_model": "dynamic",
        "sonic_asic_platform": "broadcom",
        "shutdown_bgp_on_start": "y",
        "enable_pfcwd_on_start": "y",
        "enable_auto_tech_support": "n",
        "BUILD_REDUCE_IMAGE_SIZE": "y",
        "include_router_advertiser": "n",
        "include_lldp": "n",
        "include_snmp": "n",
        "include_teamd": "n",
        "include_dhcp_server": "n",
        "include_iccpd": "n",
        "include_mgmt_framework": "n",
        "include_mux": "n",
        "include_nat": "n",
        "include_p4rt": "n",
        "include_restapi": "n",
        "include_sflow": "n",
        "include_macsec": "n",
        "include_system_gnmi": "n",
        "include_system_telemetry": "n",
        "include_system_otel": "n",
        "include_system_eventd": "n",
        "include_kubernetes": "n",
        "installer_services": "database.service swss.service syncd.service",
    },
}

#: 渲染/语法检查的子进程超时（秒）。模板渲染实测 < 1s，给足余量即可。
SUBPROCESS_TIMEOUT = 120

OK = "OK"
FAIL = "FAIL"
SKIP = "SKIP"


@dataclass(frozen=True)
class CheckResult:
    """一条结构校验结果。

    ``SKIP`` 是刻意保留的第三态：宿主缺 ``bash`` 或 ``j2`` 时如果直接判失败，
    会让「本机没装某个工具」和「品牌资产把构建输入弄坏了」混成同一个退出码；
    如果静默判通过，又会让校验变成安慰剂。折中是「不阻断、但在输出里显式标注」，
    真正的门禁在构建服务器上——那里两个工具都实测存在。
    """

    check_id: str
    name: str
    status: str
    detail: str

    @property
    def failed(self) -> bool:
        return self.status == FAIL

    def format(self) -> str:
        return f"[{self.status:^4}] {self.check_id} {self.name:<24}: {self.detail}"


# ---------------------------------------------------------------------------
# init_cfg.json.j2 渲染校验（需求 3.1）
# ---------------------------------------------------------------------------


def _render_env(params: dict[str, str]) -> dict[str, str]:
    """构造只含模板参数与必要系统变量的干净环境。"""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
    }
    for name in ("HOME", "PYTHONPATH", "SYSTEMROOT", "TMPDIR"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    env.update(params)
    return env


def _render_with_j2(template: Path, params: dict[str, str], set_name: str) -> str:
    """用 ``j2`` 二进制渲染（与构建实际路径一致）。

    :raises FileNotFoundError: ``PATH`` 上没有 ``j2``，由调用方决定回落
    :raises StructureError: ``j2`` 渲染失败（退出码 5）
    """
    executable = shutil.which("j2")
    if executable is None:
        raise FileNotFoundError("j2")
    proc = subprocess.run(  # noqa: S603 - 参数全部由本模块常量与已校验路径构成
        [executable, str(template)],
        cwd=str(template.parent),
        env=_render_env(params),
        capture_output=True,
        text=True,
        encoding=ENCODING,
        errors="replace",
        timeout=SUBPROCESS_TIMEOUT,
    )
    if proc.returncode != 0:
        raise StructureError(
            f"j2 渲染失败（参数组 {set_name}，退出码 {proc.returncode}）\n"
            + "\n".join(
                f"    | {line}" for line in (proc.stderr or "").strip().splitlines()[:10]
            )
            + "\n  处置：若上游给模板新增了变量，请更新 ligent/structure.py 的 "
            "RENDER_PARAM_SETS"
        )
    return proc.stdout


def _render_with_jinja2(template: Path, params: dict[str, str]) -> str:
    """回落一：进程内 Jinja2（需 ``jinja2.ext.do``，模板用了 ``{% do %}``）。"""
    import jinja2  # 延迟 import：主路径用不到它

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(template.parent)),
        extensions=["jinja2.ext.do"],
        keep_trailing_newline=True,
    )
    return env.get_template(template.name).render(**params)


def validate_init_cfg(repo_root: str | Path) -> list[CheckResult]:
    """需求 3.1：渲染 ``init_cfg.json.j2`` 并断言结果是合法 JSON。

    对 :data:`RENDER_PARAM_SETS` 的每一组参数各出一条结果，任一为 ``FAIL``
    即由调用方以退出码 5 结束。
    """
    template = Path(repo_root) / INIT_CFG_PATH
    if not template.is_file():
        return [
            CheckResult(
                "SC-01",
                "init_cfg_json",
                FAIL,
                f"模板不存在：{template}",
            )
        ]

    results: list[CheckResult] = []
    for index, (set_name, params) in enumerate(RENDER_PARAM_SETS.items(), start=1):
        check_id = f"SC-01.{index}"
        name = f"init_cfg_json[{set_name}]"
        try:
            rendered = _render_with_j2(template, params, set_name)
            renderer = "j2"
        except StructureError as exc:
            results.append(CheckResult(check_id, name, FAIL, str(exc)))
            continue
        except FileNotFoundError:
            try:
                rendered = _render_with_jinja2(template, params)
                renderer = "jinja2(进程内回落)"
            except ImportError:
                results.append(
                    CheckResult(
                        check_id,
                        name,
                        SKIP,
                        "本机既无 j2 也无 jinja2，跳过渲染校验"
                        "（构建服务器上此项必跑）",
                    )
                )
                continue
            except Exception as exc:  # noqa: BLE001 - 渲染层任何异常都算校验失败
                results.append(
                    CheckResult(
                        check_id, name, FAIL, f"jinja2 渲染失败：{type(exc).__name__}: {exc}"
                    )
                )
                continue

        try:
            document = json.loads(rendered)
        except json.JSONDecodeError as exc:
            context = rendered.splitlines()[max(0, exc.lineno - 2) : exc.lineno + 1]
            results.append(
                CheckResult(
                    check_id,
                    name,
                    FAIL,
                    f"渲染结果不是合法 JSON（{renderer}）：{exc}\n"
                    + "\n".join(f"    | {line}" for line in context),
                )
            )
            continue

        # 顺带确认本工具重写的那两行确实活到了渲染后：JSON 合法但 BANNER_MESSAGE
        # 结构被改坏（例如字段整行丢失）同样是需求 3.1 想拦住的情况。
        banner = document.get("BANNER_MESSAGE", {}).get("global", {})
        missing = [f for f in ("login", "motd") if not isinstance(banner.get(f), str)]
        if missing:
            results.append(
                CheckResult(
                    check_id,
                    name,
                    FAIL,
                    "渲染结果是合法 JSON，但 BANNER_MESSAGE.global 缺少字符串字段 "
                    + ", ".join(missing),
                )
            )
            continue

        results.append(
            CheckResult(
                check_id,
                name,
                OK,
                f"{renderer} 渲染后 json.loads 通过（{len(document)} 个顶层表，"
                f"BANNER_MESSAGE.motd {len(banner['motd'])} 字符）",
            )
        )
    return results


# ---------------------------------------------------------------------------
# bash -n 校验（需求 3.2）
# ---------------------------------------------------------------------------


def validate_bash_syntax(repo_root: str | Path) -> list[CheckResult]:
    """需求 3.2：对两个 shell 构建输入执行 ``bash -n``。

    ``%%IMAGE_VERSION%%`` 之类构建期占位符不影响语法检查——它们出现在字符串
    或赋值右侧，``bash -n`` 只做解析不做展开。
    """
    executable = shutil.which("bash")
    results: list[CheckResult] = []
    for index, rel in enumerate(BASH_PATHS, start=1):
        check_id = f"SC-02.{index}"
        name = f"bash_n[{Path(rel).name}]"
        path = Path(repo_root) / rel
        if not path.is_file():
            results.append(CheckResult(check_id, name, FAIL, f"文件不存在：{path}"))
            continue
        if executable is None:
            results.append(
                CheckResult(
                    check_id,
                    name,
                    SKIP,
                    "本机找不到 bash，跳过语法检查（构建服务器上此项必跑）",
                )
            )
            continue
        proc = subprocess.run(  # noqa: S603 - 参数为已解析的 bash 与已校验路径
            [executable, "-n", str(path)],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
            encoding=ENCODING,
            errors="replace",
        )
        if proc.returncode != 0:
            results.append(
                CheckResult(
                    check_id,
                    name,
                    FAIL,
                    f"bash -n 失败（退出码 {proc.returncode}）：\n"
                    + "\n".join(
                        f"    | {line}"
                        for line in proc.stderr.strip().splitlines()[:10]
                    ),
                )
            )
            continue
        results.append(CheckResult(check_id, name, OK, f"{rel} 语法合法"))
    return results


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------


def validate_structure(repo_root: str | Path) -> list[CheckResult]:
    """跑全部结构校验，返回全部结果（不抛异常，由调用方决定退出码）。"""
    return list(validate_init_cfg(repo_root)) + list(validate_bash_syntax(repo_root))


def format_results(results: list[CheckResult]) -> str:
    """把结果列成每行一条，供 CI 日志直读。"""
    return "\n".join(result.format() for result in results)


def failures(results: list[CheckResult]) -> list[CheckResult]:
    return [result for result in results if result.failed]
