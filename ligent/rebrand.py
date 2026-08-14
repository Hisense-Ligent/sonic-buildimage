#!/usr/bin/env python3
"""Rebrand_Tool 命令行入口（任务 4.1、4.2；design.md 4.5 节与「退出码契约」）。

```
用法: python3 ligent/rebrand.py [动作] [选项]

动作（互斥，默认 --apply）:
  --apply              把 Brand_Assets 应用到全部 Brand_Touchpoints
  --check              只校验不写入；已改造则 0，未改造则 4
  --list-touchpoints   输出触点路径列表（每行一个），供 CI 做 sha256 比对
  --verify-structure   只跑改造后的结构校验（JSON 渲染 + bash -n）

选项:
  --repo-root PATH     仓库根目录，默认为本脚本所在目录的上一级
  --assets PATH        Brand_Assets 目录，默认 <repo-root>/ligent/brand
  --skip-structure     apply/check 后不跑结构校验（不建议，仅供缺 j2/bash 的环境）
  --verbose            逐触点输出诊断
  -h / --help          输出本用法

退出码:
  0  成功（apply 完成 / check 通过 / 结构校验通过）
  1  Brand_Assets 缺少必需文件或 BrandConfig 非法   (需求 1.4)
  2  Brand_Touchpoints 文件不存在                   (需求 2.4)
  3  锚点未命中，需人工适配上游变更                 (需求 2.5、9.2)
  4  --check 发现未改造或改造不完整                 (需求 2.8)
  5  改造后结构校验失败（JSON 渲染 / bash -n）       (需求 3.1、3.2)
```

## 这个入口刻意「薄」

真正的逻辑在三个下层模块里：:mod:`brandconfig` 负责品牌输入的加载与校验、
:mod:`touchpoints` 负责计算与写入、:mod:`structure` 负责改造后的结构校验。
三者抛出的异常都自带 ``exit_code``，所以本模块的错误处理收敛成一句
``except RebrandError as exc: return exc.exit_code``，不需要在 CLI 层重新判断
「这算哪种错」——退出码语义只在一个地方定义，不会两处漂移。

## ``--check`` 为什么不可能写盘

check 路径只调 :func:`touchpoints.plan_all`（纯函数，只读），完全不碰
:func:`touchpoints.apply_plan`（唯一的写入函数）。于是需求 2.6「检查模式不修改
任何文件」是**结构性**成立的，而不是靠某个 ``if dry_run`` 分支守住——后者只要有
一条路径漏判就会破功，而且属性测试很难覆盖到那条路径。

判别逻辑同样简单：任一 ``plan.changed`` 为真即说明还有触点没改造，退出码 4。

## 退出码 3 与 4 的区分

3 = 上游代码变了、锚点对不上，**需要人去改** ``touchpoints.py`` 的锚点或基线 hash；
4 = 代码没变，**只是漏跑了 apply**。两者在 CI 日志里的处置动作完全不同，
合并成一个非零码会让每次排障都多绕一圈（design.md「错误处理」节明确要求区分）。

## 「先全算后全写」在 CLI 层保持

apply 走 :func:`touchpoints.apply_all`：先算完 4 个触点的计划，再依次写入。
任一触点锚点漂移都在写第一个文件**之前**抛出，工作区保持原样，不会留下
「改了两个、剩两个没改」的中间态。CLI 不做「边算边写」的优化。

## 结构校验的时机

需求 3.1/3.2 的措辞是「Rebrand_Tool 执行完成后」构建输入应合法，因此
**apply 成功后自动跑一次**结构校验，失败以退出码 5 结束。

``--check`` 也跑，理由是 check 在流水线里承担「工作区当前是否可用」的守门角色：
一个已改造但 JSON 被手工编辑弄坏的工作区，check 若报 0 会把问题推到两小时后的
构建阶段。退出码优先级为 **4 先于 5**：未改造时先说「去跑 apply」，因为跑完
apply 再校验才有意义；已改造却结构破损时才报 5。

另外提供 ``--verify-structure`` 单独入口，供排障时不改任何东西地复现校验结果，
以及给「只想确认构建输入合法」的流水线步骤用。
"""

from __future__ import annotations

import sys
from pathlib import Path

try:  # 直接跑脚本 / 测试把 ligent/ 注入 sys.path 时
    from brandconfig import BrandAssetsError, load_brand_assets
    from structure import (
        EXIT_STRUCTURE_INVALID,
        failures,
        format_results,
        validate_structure,
    )
    from touchpoints import (
        EXIT_CHECK_FAILED,
        TOUCHPOINT_PATHS,
        RebrandError,
        TouchpointPlan,
        apply_all,
        plan_all,
    )
except ImportError:  # pragma: no cover - 以 `python3 -m ligent.rebrand` 导入时
    from .brandconfig import BrandAssetsError, load_brand_assets  # type: ignore
    from .structure import (  # type: ignore
        EXIT_STRUCTURE_INVALID,
        failures,
        format_results,
        validate_structure,
    )
    from .touchpoints import (  # type: ignore
        EXIT_CHECK_FAILED,
        TOUCHPOINT_PATHS,
        RebrandError,
        TouchpointPlan,
        apply_all,
        plan_all,
    )

EXIT_OK = 0
#: 参数用错。与「资产非法」共用退出码 1：两者都属于「输入不对，改输入」，
#: 且退出码契约里 1 之外没有为「用法错误」预留的码。
EXIT_USAGE = 1

#: 用法文本直接取自模块 docstring 的第一个代码块，避免两处描述漂移。
#: ``python3 -OO`` 会剥掉 docstring，此时给一句回落而不是崩在 ``None.split``。
USAGE = (
    __doc__.split("```")[1].strip("\n")
    if __doc__
    else "用法: python3 ligent/rebrand.py [--apply|--check|--list-touchpoints|"
    "--verify-structure] [--repo-root PATH] [--assets PATH] [--verbose]"
)


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------


class UsageError(Exception):
    """命令行参数用错。"""

    exit_code = EXIT_USAGE


class Options:
    """已解析的命令行参数。

    手写解析而不用 ``argparse``：需要「四个动作互斥且默认 --apply」以及
    「用法错误退出码为 1 而不是 argparse 默认的 2」——2 在本工具的契约里是
    「触点文件不存在」，被 argparse 占用会让 CI 日志里的退出码含义错位。
    """

    def __init__(self) -> None:
        self.action = "apply"
        self.repo_root: Path | None = None
        self.assets: Path | None = None
        self.verbose = False
        self.skip_structure = False


_ACTIONS = {
    "--apply": "apply",
    "--check": "check",
    "--list-touchpoints": "list",
    "--verify-structure": "verify-structure",
}


def parse_args(argv: list[str]) -> Options:
    options = Options()
    seen_action: str | None = None
    args = list(argv)
    while args:
        arg = args.pop(0)
        if arg in ("-h", "--help"):
            options.action = "help"
            return options
        if arg in _ACTIONS:
            action = _ACTIONS[arg]
            if seen_action is not None and seen_action != action:
                raise UsageError(f"动作参数互斥：{seen_action} 与 {action} 不能同时给出")
            seen_action = action
            options.action = action
            continue
        if arg == "--repo-root":
            options.repo_root = Path(_take_value(args, arg))
            continue
        if arg == "--assets":
            options.assets = Path(_take_value(args, arg))
            continue
        if arg == "--verbose":
            options.verbose = True
            continue
        if arg == "--skip-structure":
            options.skip_structure = True
            continue
        raise UsageError(f"未知参数：{arg}")
    return options


def _take_value(args: list[str], flag: str) -> str:
    if not args or args[0].startswith("--"):
        raise UsageError(f"{flag} 需要一个路径参数")
    return args.pop(0)


# ---------------------------------------------------------------------------
# 路径推断
# ---------------------------------------------------------------------------


def default_repo_root() -> Path:
    """默认仓库根：本脚本所在目录（``ligent/``）的上一级。"""
    return Path(__file__).resolve().parent.parent


def resolve_assets_dir(repo_root: Path, override: Path | None) -> Path:
    """Brand_Assets 目录：``--assets`` 优先，否则 ``<repo-root>/ligent/brand``。

    按 ``repo_root`` 推而不是固定用脚本旁的 ``brand/``，是为了让
    ``--repo-root <临时工作区>`` 能配一套独立品牌资产（属性测试要对同一份代码
    喂不同 BrandConfig）。回落到脚本旁的 ``brand/`` 则覆盖「临时工作区没有
    ligent/ 目录」这一常见情形。
    """
    if override is not None:
        return override
    candidate = repo_root / "ligent" / "brand"
    if candidate.is_dir():
        return candidate
    return Path(__file__).resolve().parent / "brand"


# ---------------------------------------------------------------------------
# 各动作
# ---------------------------------------------------------------------------


def _print_plans(plans: list[TouchpointPlan], verbose: bool) -> None:
    if not verbose:
        return
    for plan in plans:
        print("  " + plan.describe())


def _run_structure(repo_root: Path, verbose: bool) -> int:
    """跑结构校验；失败返回 5，通过返回 0。"""
    results = validate_structure(repo_root)
    bad = failures(results)
    if bad or verbose:
        print(format_results(results))
    if bad:
        print(
            f"错误：改造后结构校验未通过（{len(bad)}/{len(results)} 项失败）。\n"
            f"  处置：按可能性排序——(1) 触点文件的非锚点区域被上游变更或人工编辑\n"
            f"        弄出了不合法的 JSON / shell；(2) 品牌资产内容异常，检查\n"
            f"        ligent/brand/brand.yaml 与 logo.ascii。\n"
            f"        注意：通过白名单校验的 brand_name 不会破坏结构，"
            f"所以先查 (1)。",
            file=sys.stderr,
        )
        return EXIT_STRUCTURE_INVALID
    print(f"结构校验通过（{len(results)} 项）")
    return EXIT_OK


def action_list(repo_root: Path) -> int:
    """输出触点路径列表，供 CI 做 sha256 比对（需求 2.2 的幂等自证）。

    路径形态取决于 ``--repo-root`` 是否就是当前目录：

    * 是（流水线的常态，工作目录即仓库根）→ 输出**仓库相对路径**，
      可以直接 ``sha256sum $(rebrand.py --list-touchpoints)``；
    * 不是 → 输出**绝对路径**，同样能直接喂给 ``sha256sum``。

    两种形态都保证「输出即可用」，不需要调用方拼路径。
    """
    try:
        same = repo_root.resolve() == Path.cwd().resolve()
    except OSError:  # pragma: no cover - cwd 被删等极端情况
        same = False
    for rel in TOUCHPOINT_PATHS:
        print(rel if same else str(repo_root / rel))
    return EXIT_OK


def action_apply(repo_root: Path, assets_dir: Path, options: Options) -> int:
    assets = load_brand_assets(assets_dir)
    plans = apply_all(repo_root, assets)
    written = [plan for plan in plans if plan.changed]
    _print_plans(plans, options.verbose)
    print(
        f"apply 完成：品牌 {assets.config.brand_name!r}，"
        f"{len(written)}/{len(plans)} 个触点被写入"
        + ("（其余已是目标形态）" if len(written) < len(plans) else "")
    )
    if options.skip_structure:
        print("已跳过结构校验（--skip-structure）")
        return EXIT_OK
    return _run_structure(repo_root, options.verbose)


def action_check(repo_root: Path, assets_dir: Path, options: Options) -> int:
    assets = load_brand_assets(assets_dir)
    # 只调 plan_all（纯函数）。写入函数 apply_plan 在这条路径上根本不出现，
    # 因此「check 不打开任何写句柄」是结构性成立的（需求 2.6）。
    plans = plan_all(repo_root, assets)
    pending = [plan for plan in plans if plan.changed]
    _print_plans(plans, options.verbose)
    if pending:
        print(
            f"错误：{len(pending)}/{len(plans)} 个触点尚未改造或改造不完整：",
            file=sys.stderr,
        )
        for plan in pending:
            print(f"  - {plan.touchpoint.path}", file=sys.stderr)
        print(
            "  处置：在构建前执行 `python3 ligent/rebrand.py --apply`",
            file=sys.stderr,
        )
        return EXIT_CHECK_FAILED
    print(f"check 通过：{len(plans)} 个触点均已改造为 {assets.config.brand_name!r}")
    if options.skip_structure:
        print("已跳过结构校验（--skip-structure）")
        return EXIT_OK
    return _run_structure(repo_root, options.verbose)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    try:
        options = parse_args(list(sys.argv[1:] if argv is None else argv))
    except UsageError as exc:
        print(f"错误：{exc}\n", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return exc.exit_code

    if options.action == "help":
        print(USAGE)
        return EXIT_OK

    repo_root = (options.repo_root or default_repo_root()).expanduser()
    if not repo_root.is_dir():
        print(f"错误：仓库根目录不存在：{repo_root}", file=sys.stderr)
        return EXIT_USAGE

    if options.action == "list":
        return action_list(repo_root)

    if options.action == "verify-structure":
        return _run_structure(repo_root, verbose=True)

    assets_dir = resolve_assets_dir(repo_root, options.assets)
    if options.verbose:
        print(f"repo-root = {repo_root}")
        print(f"assets    = {assets_dir}")

    # 三类下层异常都自带 exit_code，CLI 只负责打印与转发：
    #   BrandAssetsError        -> 1
    #   TouchpointMissingError  -> 2（诊断含路径与 purpose，需求 2.4）
    #   AnchorMissError         -> 3（诊断含未命中锚点原文，需求 2.5）
    try:
        if options.action == "apply":
            return action_apply(repo_root, assets_dir, options)
        return action_check(repo_root, assets_dir, options)
    except BrandAssetsError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return exc.exit_code
    except RebrandError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
