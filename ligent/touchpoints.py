#!/usr/bin/env python3
"""Brand_Touchpoints 声明与四种替换策略（任务 3，design.md 4.2–4.4 / 5.2 / 5.3 节）。

本模块的组织原则是**「策略层是纯函数，写入是调用方的决定」**：

* :func:`plan_touchpoint` 只读文件、算出目标文本，返回 :class:`TouchpointPlan`，
  绝不写盘。``--check`` 模式只调用它，于是「check 不打开任何写句柄」（需求 2.6）
  是结构性成立的，而不是靠某个 ``if dry_run`` 分支守住。
* :func:`apply_plan` 是唯一会写盘的函数，且仅在 ``plan.changed`` 为真时写，
  写入走 :func:`fileops.write_text_atomic`（临时文件 + ``os.replace``）。

四种策略的幂等论证见 design.md 5.3 节，实现里每个策略的 docstring 复述了它依赖的
关键性质。共同的模式是：**目标文本只由 Brand_Assets 与「文件的非品牌部分」决定，
与「文件当前是否已改造」无关**，于是第二次执行必然算出与第一次相同的文本，
`changed` 为假、不写盘、mtime 与 git diff 都保持干净。

退出码（design.md 4.5 节）：
  2  触点文件不存在（需求 2.4）
  3  锚点未命中，需人工适配上游变更（需求 2.5、9.2）
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

try:  # 直接跑脚本 / 测试把 ligent/ 注入 sys.path 时
    from fileops import (
        ENCODING,
        detect_newline,
        join_lines,
        line_body,
        line_terminator,
        read_text,
        split_lines_keepends,
        write_text_atomic,
    )
except ImportError:  # pragma: no cover - 以 `python3 -m ligent.touchpoints` 导入时
    from .fileops import (  # type: ignore[no-redef]
        ENCODING,
        detect_newline,
        join_lines,
        line_body,
        line_terminator,
        read_text,
        split_lines_keepends,
        write_text_atomic,
    )

# ---------------------------------------------------------------------------
# 退出码与异常
# ---------------------------------------------------------------------------

#: 触点文件不存在（需求 2.4）
EXIT_TOUCHPOINT_MISSING = 2
#: 锚点未命中，上游漂移需人工适配（需求 2.5、9.2）
EXIT_ANCHOR_MISS = 3
#: --check 发现未改造或改造不完整（需求 2.8）——由 rebrand.py 使用
EXIT_CHECK_FAILED = 4


class RebrandError(Exception):
    """携带退出码的替换失败基类。"""

    exit_code = EXIT_ANCHOR_MISS


class TouchpointMissingError(RebrandError):
    """触点文件不存在。诊断必须含路径与预期用途（需求 2.4）。"""

    exit_code = EXIT_TOUCHPOINT_MISSING


class AnchorMissError(RebrandError):
    """锚点未命中。诊断必须含文件路径与未命中锚点原文（需求 2.5）。"""

    exit_code = EXIT_ANCHOR_MISS


# ---------------------------------------------------------------------------
# 策略与触点数据模型（design.md 5.2 节）
# ---------------------------------------------------------------------------


class Strategy(Enum):
    """四种替换策略。

    之所以不是「一个通用的正则替换 + 配置」，是因为四个触点的幂等性论证方式
    根本不同：整文件覆盖靠「目标只由资产决定」，哨兵块靠「替换/插入两分支」，
    行重写靠「锚点与替换结果互不包含」，JSON 字段靠「整行按固定格式重写」。
    把它们压成一个通用机制会让这四条论证全部退化成「大概没问题」。
    """

    WHOLE_FILE = "WHOLE_FILE"
    JSON_FIELD = "JSON_FIELD"
    SENTINEL_BLOCK = "SENTINEL_BLOCK"
    LINE_REWRITE = "LINE_REWRITE"


#: 哨兵块标记（design.md 4.2 节）。BEGIN 行带说明后缀，匹配时只比前缀。
SENTINEL_BEGIN = "# LIGENT-BRAND-BEGIN"
SENTINEL_END = "# LIGENT-BRAND-END"
SENTINEL_BEGIN_COMMENT = (
    "# LIGENT-BRAND-BEGIN: display-only label, "
    "never used for volume/partition/EFI identity"
)

#: GRUB 显示专用变量名。它**不参与**卷标、分区名、EFI 标识（design.md 4.2 节）。
BRAND_DISPLAY_VAR = "demo_brand_display"


@dataclass(frozen=True)
class Touchpoint:
    """一个品牌触点的完整声明（design.md 5.2 节）。

    数据驱动是刻意的：属性测试（任务 4.3–4.12）直接遍历 :data:`TOUCHPOINTS`
    与每个触点的 ``anchors``，因此「新增触点」自动被全部属性覆盖，不需要改测试。
    """

    path: str
    strategy: Strategy
    purpose: str
    #: 定位用的逐字节字面量；命中 0 次且无已改造标记即退出码 3
    anchors: tuple[str, ...] = ()
    #: 与 ``anchors`` 一一对应的替换结果（仅 LINE_REWRITE 使用）
    replacements: tuple[str, ...] = ()
    #: 上游基线 sha256。4 个触点都记录，便于与 conftest 的基线交叉校验；
    #: 但只有 WHOLE_FILE 把它当作 apply 的前置校验（design.md 5.3(a)）。
    upstream_sha256: str | None = None
    #: JSON_FIELD 要重写的字段名
    fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.strategy is Strategy.LINE_REWRITE and len(self.anchors) != len(
            self.replacements
        ):
            raise ValueError(
                f"{self.path}: LINE_REWRITE 的 anchors 与 replacements 必须一一对应"
            )


# ---------------------------------------------------------------------------
# 触点清单
# ---------------------------------------------------------------------------

TOUCHPOINTS: tuple[Touchpoint, ...] = (
    Touchpoint(
        path="files/image_config/environment/motd",
        strategy=Strategy.WHOLE_FILE,
        purpose=(
            "登录后 MOTD 与 ASCII LOGO；由 sonic_debian_extension.j2 直接 "
            "cp 到镜像的 /etc/motd"
        ),
        upstream_sha256=(
            "3fbdd0d0074f546934194fe76dfe487bfcf07cd6119b98d1f6509e6a1b657c10"
        ),
    ),
    Touchpoint(
        path="files/build_templates/init_cfg.json.j2",
        strategy=Strategy.JSON_FIELD,
        purpose=(
            "CONFIG_DB 的 BANNER_MESSAGE.login/motd；banner state 为 enabled 时 "
            "由 banner-config.sh 写出 /etc/issue、/etc/issue.net、/etc/motd"
        ),
        anchors=('"BANNER_MESSAGE": {', '"global": {'),
        fields=("login", "motd"),
        upstream_sha256=(
            "2adf810fda82a6dc421e380076e9ec86f02c58de3859bca410cfa269fe0e5cb4"
        ),
    ),
    Touchpoint(
        path="installer/install.sh",
        strategy=Strategy.SENTINEL_BLOCK,
        purpose=(
            f"定义 GRUB 显示专用变量 {BRAND_DISPLAY_VAR}；必须位于 "
            ". ./default_platform.conf 之前"
        ),
        anchors=('demo_volume_revision_label="SONiC-${demo_type}-${image_version}"',),
        upstream_sha256=(
            "aa16dec77d2c4b2c56f803eec4d1cf0c93cb49840c1b5747b5c73ad8285b5109"
        ),
    ),
    Touchpoint(
        path="installer/default_platform.conf",
        strategy=Strategy.LINE_REWRITE,
        purpose="GRUB 启动过程的两处 echo 提示文字（menuentry 标题与卷标不动）",
        anchors=(
            "echo    'Loading $demo_volume_label $demo_type kernel ...'",
            "echo    'Loading $demo_volume_label $demo_type initial ramdisk ...'",
        ),
        replacements=(
            f"echo    'Loading ${BRAND_DISPLAY_VAR} $demo_type kernel ...'",
            f"echo    'Loading ${BRAND_DISPLAY_VAR} $demo_type initial ramdisk ...'",
        ),
        upstream_sha256=(
            "ba7ea0212d209725b23038aa95761998740c73cd44acbb303d2e2cca8fb1287c"
        ),
    ),
)

#: 仓库相对路径 -> Touchpoint
TOUCHPOINTS_BY_PATH = {tp.path: tp for tp in TOUCHPOINTS}

#: 触点路径列表，供 ``rebrand.py --list-touchpoints`` 与 CI 的 sha256 比对使用
TOUCHPOINT_PATHS: tuple[str, ...] = tuple(tp.path for tp in TOUCHPOINTS)


# ---------------------------------------------------------------------------
# 计划结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TouchpointPlan:
    """一个触点的替换计划：读到了什么、目标是什么、要不要写。

    ``changed`` 为假有两种含义完全相同的来源——「本来就已改造」与「算出的目标
    恰好等于当前内容」。对调用方来说没有区别：不写盘，且 ``check`` 判为已改造。
    """

    touchpoint: Touchpoint
    path: Path
    original_text: str
    new_text: str
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def changed(self) -> bool:
        return self.new_text != self.original_text

    def describe(self) -> str:
        status = "rewrite" if self.changed else "up-to-date"
        detail = "; ".join(self.notes)
        return f"[{status}] {self.touchpoint.path} ({self.touchpoint.strategy.value})" + (
            f" - {detail}" if detail else ""
        )


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode(ENCODING)).hexdigest()


# ---------------------------------------------------------------------------
# 策略实现
# ---------------------------------------------------------------------------


def _plan_whole_file(tp: Touchpoint, path: Path, current: str, assets) -> TouchpointPlan:
    """WHOLE_FILE：整文件由 ``motd.tmpl`` + ``logo.ascii`` 渲染（design.md 5.3(a)）。

    幂等性在这里是平凡成立的：目标内容 ``T`` 只由 Brand_Assets 决定，与当前内容
    无关，所以 apply 后再 apply 必然 ``C == T`` 走「不写入」分支。

    代价是失去了「按锚点定位」的漂移检测能力，因此改用 hash 基线补上：当前内容
    必须是「上游基线」或「本次目标」之一，否则说明有人（或上游 rebase）改了这个
    文件，退出码 3 交人处理。这一条同时挡住了「上游给 MOTD 加了新段落而我们把它
    整文件覆盖掉」这种静默丢失。
    """
    target = assets.render_motd()
    if current == target:
        return TouchpointPlan(tp, path, current, target, ("内容已是目标形态",))

    actual = _sha256_text(current)
    allowed = {tp.upstream_sha256, _sha256_text(target)} - {None}
    if actual not in allowed:
        raise AnchorMissError(
            f"{tp.path}: 当前内容既不是已知的上游基线、也不是本次改造目标，"
            f"拒绝整文件覆盖以免丢失上游变更。\n"
            f"  实际 sha256   : {actual}\n"
            f"  上游基线      : {tp.upstream_sha256}\n"
            f"  本次目标      : {_sha256_text(target)}\n"
            f"  处置：人工核对该文件的上游变更后，更新 touchpoints.py 中的 "
            f"upstream_sha256 基线"
        )
    return TouchpointPlan(
        tp, path, current, target, (f"整文件覆盖（{len(target.splitlines())} 行）",)
    )


def _find_json_window(tp: Touchpoint, lines: list[str]) -> tuple[int, int]:
    """定位 ``"BANNER_MESSAGE": {`` 到其配对 ``}`` 的行区间（左闭右闭）。

    ``init_cfg.json.j2`` 是 Jinja2 模板不是合法 JSON，不能整体 ``json.load``；
    但 ``BANNER_MESSAGE`` 段是纯静态 JSON，可以按行定位（design.md 5.3(d)）。

    花括号计数是 **JSON 字符串感知**的：``motd`` 的值里全是 ASCII art 与 ``\\n``
    转义，虽然当前不含花括号，但把「字符串内的字符不计入括号深度」这一点写对，
    才能保证将来品牌 art 变化时不会莫名把窗口算歪。
    """
    open_anchor = tp.anchors[0]
    start = next(
        (i for i, line in enumerate(lines) if line_body(line).strip() == open_anchor),
        None,
    )
    if start is None:
        raise AnchorMissError(
            f"{tp.path}: 未找到锚点 {open_anchor!r}，无法定位 BANNER_MESSAGE 段落"
        )

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(lines)):
        body = line_body(lines[index])
        if not in_string and ("{%" in body or "{{" in body):
            raise AnchorMissError(
                f"{tp.path}: BANNER_MESSAGE 段落内出现 Jinja2 控制结构 "
                f"（第 {index + 1} 行：{body.strip()!r}），"
                f"按行定位不再安全，需人工适配触点定义"
            )
        for char in body:
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return start, index
    raise AnchorMissError(
        f"{tp.path}: 锚点 {open_anchor!r} 之后找不到配对的 '}}'，"
        f"BANNER_MESSAGE 段落结构已变化"
    )


def _plan_json_field(tp: Touchpoint, path: Path, current: str, assets) -> TouchpointPlan:
    """JSON_FIELD：在 BANNER_MESSAGE 窗口内整行重写 ``login`` 与 ``motd``。

    幂等性的关键是**整行按固定格式重写**而不是在原值上做子串替换
    （design.md 5.3(d)）：目标行 = 原行缩进 + ``"字段": `` + ``json.dumps(值)`` +
    原行尾随逗号。第二次执行读到的是第一次写出的行，重新生成的结果逐字节相同。

    值一律经 :func:`json.dumps` 生成，禁止手工拼接转义——ASCII art 里满是
    ``\\___``、``\\| |`` 这样的反斜杠，漏转义会让整个 ``init_cfg.json`` 解析失败，
    后果是设备起不来（design.md「测试策略」把它列为最容易错、后果最严重的一处）。

    ``motd`` 与 WHOLE_FILE 触点**同源**：同一个 ``render_motd()`` 结果，保证静态
    ``/etc/motd`` 与 ``config banner state enabled`` 两条路径显示一致。
    """
    lines = split_lines_keepends(current)
    start, end = _find_json_window(tp, lines)

    for anchor in tp.anchors[1:]:
        if not any(line_body(lines[i]).strip() == anchor for i in range(start, end + 1)):
            raise AnchorMissError(
                f"{tp.path}: BANNER_MESSAGE 窗口（第 {start + 1}–{end + 1} 行）内"
                f"未找到锚点 {anchor!r}"
            )

    values = {
        "login": assets.render_login_banner(),
        "motd": assets.render_motd(),
    }
    new_lines = list(lines)
    notes: list[str] = []
    for name in tp.fields:
        if name not in values:
            raise AnchorMissError(f"{tp.path}: 未知的 JSON_FIELD 字段 {name!r}")
        prefix = f'"{name}":'
        index = next(
            (
                i
                for i in range(start, end + 1)
                if line_body(lines[i]).strip().startswith(prefix)
            ),
            None,
        )
        if index is None:
            raise AnchorMissError(
                f"{tp.path}: BANNER_MESSAGE 窗口（第 {start + 1}–{end + 1} 行）内"
                f"未找到字段行 {prefix!r}"
            )
        original = lines[index]
        body = line_body(original)
        indent = body[: len(body) - len(body.lstrip())]
        # 尾随逗号原样保留：窗口内 login/motd 后面都还有字段，删掉逗号会让
        # 渲染结果不是合法 JSON。
        comma = "," if body.rstrip().endswith(",") else ""
        new_lines[index] = (
            f"{indent}{prefix} {json.dumps(values[name])}{comma}"
            f"{line_terminator(original)}"
        )
        if new_lines[index] != original:
            notes.append(f"重写 BANNER_MESSAGE.{name}（第 {index + 1} 行）")

    return TouchpointPlan(
        tp,
        path,
        current,
        join_lines(new_lines),
        tuple(notes) or ("BANNER_MESSAGE.login/motd 已是目标形态",),
    )


def _find_sentinel_block(lines: list[str]) -> tuple[int, int] | None:
    """定位 ``# LIGENT-BRAND-BEGIN`` .. ``# LIGENT-BRAND-END`` 行区间（左闭右闭）。"""
    begin = next(
        (i for i, line in enumerate(lines) if line_body(line).startswith(SENTINEL_BEGIN)),
        None,
    )
    if begin is None:
        return None
    end = next(
        (
            i
            for i in range(begin + 1, len(lines))
            if line_body(lines[i]).startswith(SENTINEL_END)
        ),
        None,
    )
    if end is None:
        raise AnchorMissError(
            f"发现 {SENTINEL_BEGIN} 但缺少配对的 {SENTINEL_END}，"
            f"哨兵块被破坏，需人工修复"
        )
    return begin, end


def _plan_sentinel_block(
    tp: Touchpoint, path: Path, current: str, assets
) -> TouchpointPlan:
    """SENTINEL_BLOCK：在 ``install.sh`` 中维护 ``demo_brand_display`` 定义块。

    幂等性的关键是**「已存在哨兵块则整块替换」与「不存在则在锚点后插入」是两条
    互斥分支**（design.md 5.3(b)）。无条件插入会让每次执行都多出一个块，
    这是文本改造工具最典型的失效模式。

    块内只写 ``demo_brand_display``。``demo_volume_label`` 与
    ``demo_volume_revision_label`` 逐字节不动——它们的值 ``SONiC-OS`` 被 ext4 卷标、
    GPT 分区名、EFI 启动项、``search --label`` 与 ``sonic-installer`` 的
    ``IMAGE_PREFIX`` 共享，改它会在装机时炸而不是构建时炸（design.md 4.2 节）。

    插入点选在锚点行之后，因此天然早于 ``. ./default_platform.conf``（上游相隔
    3 行），``bootloader_menu_config()`` 执行时该变量已定义。
    """
    lines = split_lines_keepends(current)
    newline = detect_newline(current)
    block_body = [
        SENTINEL_BEGIN_COMMENT,
        f'{BRAND_DISPLAY_VAR}="{assets.config.brand_name}-${{demo_type}}"',
        SENTINEL_END,
    ]

    found = _find_sentinel_block(lines)
    if found is not None:
        begin, end = found
        # 复用块首行的行尾风格，混合行尾的文件也不会被改脏。
        term = line_terminator(lines[begin]) or newline
        block = [text + term for text in block_body]
        new_lines = lines[:begin] + block + lines[end + 1 :]
        return TouchpointPlan(
            tp,
            path,
            current,
            join_lines(new_lines),
            (f"整块替换哨兵块（第 {begin + 1}–{end + 1} 行）",),
        )

    anchor = tp.anchors[0]
    index = next(
        (i for i, line in enumerate(lines) if line_body(line) == anchor),
        None,
    )
    if index is None:
        raise AnchorMissError(
            f"{tp.path}: 未命中锚点，无法确定 {BRAND_DISPLAY_VAR} 的插入位置。\n"
            f"  未命中锚点原文: {anchor}\n"
            f"  处置：上游可能改写了该赋值语句，请更新 touchpoints.py 的锚点"
        )
    term = line_terminator(lines[index]) or newline
    if not term:
        # 锚点恰在无行尾的末行：先补一个行尾再插块，否则会拼成一行。
        lines[index] = lines[index] + newline
        term = newline
    block = [text + term for text in block_body]
    new_lines = lines[: index + 1] + block + lines[index + 1 :]
    return TouchpointPlan(
        tp,
        path,
        current,
        join_lines(new_lines),
        (f"在第 {index + 1} 行锚点后插入哨兵块",),
    )


def _plan_line_rewrite(tp: Touchpoint, path: Path, current: str, assets) -> TouchpointPlan:
    """LINE_REWRITE：``default_platform.conf`` 的两处 GRUB ``echo`` 提示文字。

    三分支且互斥完备（design.md 5.3(c)）：

    * ``count(anchor) >= 1`` → 全部替换；
    * ``count(anchor) == 0`` 且 ``count(replacement) >= 1`` → 已改造，不写入；
    * 两者皆 0 → 退出码 3，输出未命中锚点原文（需求 2.5）。

    互斥性来自 ``anchor`` 与 ``replacement`` 互不包含（``$demo_volume_label`` vs
    ``$demo_brand_display``），所以不存在「替换结果又被当成锚点再替换一次」的退化。

    只动这两个 ``echo``。``demo_grub_entry="$demo_volume_revision_label"``、
    ``search --no-floppy --label`` 以及全部分区/EFI 相关的 ``$demo_volume_label``
    引用逐字节不动（Property 5 会逐条断言）。
    """
    del assets  # 替换文本只引用变量名，与品牌值无关（品牌值在 install.sh 里）
    text = current
    notes: list[str] = []
    for anchor, replacement in zip(tp.anchors, tp.replacements):
        hits = text.count(anchor)
        if hits:
            text = text.replace(anchor, replacement)
            notes.append(f"替换 {hits} 处：{anchor}")
            continue
        if text.count(replacement) >= 1:
            notes.append(f"已改造：{replacement}")
            continue
        raise AnchorMissError(
            f"{tp.path}: 锚点与已改造结果均未命中，需人工适配上游变更。\n"
            f"  未命中锚点原文: {anchor}\n"
            f"  期望改造结果  : {replacement}"
        )
    return TouchpointPlan(tp, path, current, text, tuple(notes))


_PLANNERS = {
    Strategy.WHOLE_FILE: _plan_whole_file,
    Strategy.JSON_FIELD: _plan_json_field,
    Strategy.SENTINEL_BLOCK: _plan_sentinel_block,
    Strategy.LINE_REWRITE: _plan_line_rewrite,
}


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------


def resolve(repo_root: str | Path, tp: Touchpoint) -> Path:
    """触点的绝对路径。"""
    return Path(repo_root) / tp.path


def plan_touchpoint(tp: Touchpoint, repo_root: str | Path, assets) -> TouchpointPlan:
    """算出一个触点的替换计划。**只读不写。**

    :raises TouchpointMissingError: 文件不存在（退出码 2），诊断含路径与用途
    :raises AnchorMissError: 锚点未命中或 hash 基线不匹配（退出码 3）
    """
    path = resolve(repo_root, tp)
    if not path.is_file():
        raise TouchpointMissingError(
            f"触点文件不存在：{path}\n  预期用途：{tp.purpose}"
        )
    try:
        current = read_text(path)
    except UnicodeDecodeError as exc:
        raise AnchorMissError(f"{tp.path}: 不是合法的 UTF-8 文本（{exc}）") from exc
    return _PLANNERS[tp.strategy](tp, path, current, assets)


def plan_all(
    repo_root: str | Path, assets, touchpoints: tuple[Touchpoint, ...] = TOUCHPOINTS
) -> list[TouchpointPlan]:
    """算出全部触点的替换计划。**只读不写**，供 ``--check`` 使用。

    合流性（触点处理顺序不影响结果）在这里是结构性成立的：每个触点的计划只依赖
    自己的文件内容与 Brand_Assets，触点之间没有任何共享可变状态。
    """
    return [plan_touchpoint(tp, repo_root, assets) for tp in touchpoints]


def apply_plan(plan: TouchpointPlan) -> bool:
    """按计划写入（仅当 ``plan.changed``）。返回是否真的写了盘。"""
    if not plan.changed:
        return False
    write_text_atomic(plan.path, plan.new_text)
    return True


def apply_all(
    repo_root: str | Path, assets, touchpoints: tuple[Touchpoint, ...] = TOUCHPOINTS
) -> list[TouchpointPlan]:
    """先算全部计划、再统一写入。

    「先全算后全写」不是可有可无的讲究：任何一个触点的锚点漂移都会在**写第一个
    文件之前**抛出退出码 3，于是工作区保持原样，不会留下「改了两个、剩两个没改」
    的中间态。配合 :func:`fileops.write_text_atomic`，需求 2.3 与 Property 8 在实现
    层面成立而不是靠测试期望。
    """
    plans = plan_all(repo_root, assets, touchpoints)
    for plan in plans:
        apply_plan(plan)
    return plans
