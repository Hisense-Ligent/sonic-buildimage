#!/usr/bin/env python3
"""Brand_Assets 的加载与校验（design.md 4.1 / 5.1 节，需求 1.1、1.2、1.4、3.3）。

这个模块是 Rebrand_Tool 唯一读取品牌输入的地方。它做三件事：

1. 确认 ``ligent/brand/`` 下 4 个必需文件都存在，缺任一即以退出码 1 失败并
   输出缺失路径（需求 1.4）。
2. 把 ``brand.yaml`` 反序列化为 :class:`BrandConfig` 并做字段级校验；非法输入
   一律以退出码 1 拒绝并说明原因，**不写入任何文件**（需求 3.3 与 Property 8）。
3. 提供模板渲染入口 :meth:`BrandAssets.render_motd` /
   :meth:`BrandAssets.render_login_banner`，供任务 3 的四种替换策略调用。
   替换策略只消费渲染结果字符串，不需要知道模板长什么样。

**模板机制选择：``%%NAME%%`` 占位符 + 纯字符串替换。** 理由：

* 不引入新依赖。服务器上 ``pyyaml`` 是 sonic-buildimage 构建环境既有依赖，
  可以放心用；Jinja2 在本机的可用性未被确认，而模板需求只是「把 5 个标量与
  1 个 art 块填进固定版式」，用模板引擎属于杠杆用错方向。
* ``string.Template`` 的 ``$`` 记号与本特性处理的 shell 文本冲突严重
  （``$demo_brand_display``、``${demo_type}`` 满地都是），一旦模板演进到需要
  内嵌这类文本就会踩 ``$`` 转义。``%%NAME%%`` 在 shell、JSON、ASCII art 三种
  上下文里都不是特殊记号。
* 渲染结果必须逐字节可预测（MOTD 触点是整文件覆盖，且行数要与上游一致），
  纯替换最容易论证这一点。

行尾一律 LF：MOTD 触点的目标内容与上游保持一致（design.md 5.3 节末）。
"""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: Brand_Assets 缺文件或 BrandConfig 非法时的退出码（design.md 4.5 节）
EXIT_ASSETS_INVALID = 1

BRAND_YAML = "brand.yaml"
LOGO_ASCII = "logo.ascii"
MOTD_TMPL = "motd.tmpl"
LOGIN_BANNER_TMPL = "login_banner.tmpl"

#: Brand_Assets 目录必需的文件（需求 1.2、1.4）
REQUIRED_ASSET_FILES: tuple[str, ...] = (
    BRAND_YAML,
    LOGO_ASCII,
    MOTD_TMPL,
    LOGIN_BANNER_TMPL,
)

#: brand.yaml 必需的标量字段（design.md 5.1 节）
REQUIRED_FIELDS: tuple[str, ...] = (
    "brand_name",
    "brand_name_upper",
    "tagline",
    "help_url",
    "login_banner_text",
)

# brand_name 的字符白名单。**这条收紧是有意的，不要放宽。**
#
# brand_name 最终会进入 installer/install.sh 的 shell 双引号赋值：
#     demo_brand_display="Ligent-${demo_type}"
# 双引号内 `$`、反引号会被 shell 展开（命令注入 / 变量展开），`"` 会提前闭合
# 字符串、`\` 会吃掉后续字符，`;` `|` `&` `(` `)` 换行等则可能让 `bash -n`
# 直接失败或改变脚本语义。同一个值还会出现在 GRUB 生成的单引号 echo 里，
# 单引号内的 `'` 同样会破坏结构。
#
# 与之相对，空格与连字符是真实品牌名常见形态，必须支持（需求 3.3 的属性测试
# 候选点名要求），它们在 shell 双引号内与 GRUB echo 里都是安全字面量。
# Unicode 一律拒绝：镜像里 /etc/motd 与 GRUB 早期启动阶段的字体/locale 都不
# 保证能正确渲染非 ASCII，"显示成乱码" 比 "构建时报错" 难排查得多。
BRAND_NAME_PATTERN = re.compile(r"\A[A-Za-z0-9 ._-]+\Z")
BRAND_NAME_MIN_LEN = 1
BRAND_NAME_MAX_LEN = 32

#: 模板占位符记号，例如 ``%%BRAND_NAME%%``
PLACEHOLDER_PATTERN = re.compile(r"%%[A-Z0-9_]+%%")

#: ``%%LOGO%%`` 单独成行时，整行连带行尾换行一起被 art 块替换，
#: 使渲染结果行数 = 模板行数 - 1 + logo 行数，可静态推算（任务 4.14 依赖）。
LOGO_PLACEHOLDER = "%%LOGO%%"


class BrandAssetsError(Exception):
    """Brand_Assets 缺失或非法。

    携带退出码，让调用方（``rebrand.py``）无需自己判断该用哪个码。
    """

    exit_code = EXIT_ASSETS_INVALID


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BrandConfig:
    """``brand.yaml`` 的反序列化结果（design.md 5.1 节）。

    不可变：品牌配置在一次 Rebrand_Tool 运行内是常量，冻结掉能避免某个策略
    顺手改了字段而让「合流性」这类属性悄悄失效。
    """

    brand_name: str
    brand_name_upper: str
    tagline: str
    help_url: str
    login_banner_text: str

    def as_placeholders(self) -> dict[str, str]:
        """字段名 -> ``%%NAME%%`` 占位符映射（不含 ``%%LOGO%%``）。"""
        return {f"%%{name.upper()}%%": getattr(self, name) for name in REQUIRED_FIELDS}


@dataclass(frozen=True)
class BrandAssets:
    """一整套已校验的品牌资产：配置 + 3 个模板/文本文件。

    任务 3 的替换策略只依赖这个类的两个 ``render_*`` 方法与 ``config``，
    因此后续换模板机制不会波及策略实现。
    """

    config: BrandConfig
    logo_ascii: str
    motd_tmpl: str
    login_banner_tmpl: str
    assets_dir: Path

    # -- 渲染入口（任务 3 的策略层调用这两个方法）--------------------------

    def render_motd(self) -> str:
        """渲染 ``/etc/motd`` 的完整内容（WHOLE_FILE 策略的目标内容）。"""
        return self._render(self.motd_tmpl, MOTD_TMPL)

    def render_login_banner(self) -> str:
        """渲染 ``BANNER_MESSAGE.login`` 的值（单行，不含行尾换行）。

        JSON 字段是单行字符串，模板文件为了可读性以换行结尾，这里剥掉。
        """
        rendered = self._render(self.login_banner_tmpl, LOGIN_BANNER_TMPL)
        return rendered.rstrip("\n")

    # -- 内部实现 ----------------------------------------------------------

    def _render(self, template: str, source_name: str) -> str:
        text = template.replace(f"{LOGO_PLACEHOLDER}\n", self.logo_ascii)
        # 容错分支：%%LOGO%% 未独占一行时不注入额外换行，避免版式莫名多一行。
        text = text.replace(LOGO_PLACEHOLDER, self.logo_ascii.rstrip("\n"))
        for token, value in self.config.as_placeholders().items():
            text = text.replace(token, value)
        leftover = PLACEHOLDER_PATTERN.findall(text)
        if leftover:
            # 模板里写了未知占位符（多半是拼写错误）。静默留下 %%FOO%% 会把
            # 字面量写进镜像的 /etc/motd，属于必须当场失败的错误。
            raise BrandAssetsError(
                f"{source_name}: 存在无法解析的占位符 "
                f"{', '.join(sorted(set(leftover)))}；"
                f"可用占位符为 {LOGO_PLACEHOLDER} 与 "
                f"{', '.join(sorted(self.config.as_placeholders()))}"
            )
        return text


# ---------------------------------------------------------------------------
# 字段级校验
# ---------------------------------------------------------------------------


def _reject(field: str, value: object, reason: str) -> "BrandAssetsError":
    """构造一条含字段名、原因与实际值的诊断。

    实际值用 ``repr`` 输出，这样换行、不可见字符与 Unicode 都能被人眼看见——
    「非法在哪」比「非法」有用得多。
    """
    return BrandAssetsError(f"brand.yaml: 字段 {field} {reason}；实际值 {value!r}")


def _require_str(field: str, value: object) -> str:
    if not isinstance(value, str):
        # YAML 会把 1.0 / yes / null 解析成 float / bool / None，
        # 这类值一路带到 shell 赋值里会产生难以理解的结果。
        raise _reject(field, value, f"必须是字符串，实际类型 {type(value).__name__}")
    return value


def _require_single_line(field: str, value: str) -> str:
    if not value:
        raise _reject(field, value, "不能为空")
    if "\n" in value or "\r" in value:
        raise _reject(field, value, "必须是单行（不能含换行符）")
    if not value.strip():
        # 纯空白在 YAML 里很容易由缩进事故产生，且渲染后是「看不见的空值」。
        raise _reject(field, value, "不能只由空白字符组成")
    return value


def validate_brand_name(value: object) -> str:
    """校验 ``brand_name``：白名单 ``[A-Za-z0-9 ._-]``、长度 1–32。

    白名单收紧的原因见模块顶部 ``BRAND_NAME_PATTERN`` 处的注释：该值会进入
    ``demo_brand_display="<brand_name>-${demo_type}"`` 这样的 shell 双引号赋值，
    放开 ``$``、反引号、``"``、``\\`` 会造成注入或让 ``bash -n`` 失败。
    """
    name = _require_str("brand_name", value)
    if not name:
        raise _reject("brand_name", name, "不能为空")
    if len(name) > BRAND_NAME_MAX_LEN:
        raise _reject(
            "brand_name",
            name,
            f"长度必须在 {BRAND_NAME_MIN_LEN}–{BRAND_NAME_MAX_LEN} 之间，"
            f"实际 {len(name)}",
        )
    if not BRAND_NAME_PATTERN.match(name):
        illegal = sorted({ch for ch in name if not BRAND_NAME_PATTERN.match(ch)})
        raise _reject(
            "brand_name",
            name,
            "只允许字符 [A-Za-z0-9 ._-]（shell 元字符、换行与非 ASCII 会造成"
            "命令注入或 bash -n 失败），非法字符 "
            + ", ".join(repr(ch) for ch in illegal),
        )
    if not name.strip():
        raise _reject("brand_name", name, "不能只由空白字符组成")
    return name


def validate_help_url(value: object) -> str:
    """校验 ``help_url``：单行、非空、``http://`` 或 ``https://`` 前缀。"""
    url = _require_single_line("help_url", _require_str("help_url", value))
    if not url.startswith(("http://", "https://")):
        raise _reject("help_url", url, "必须以 http:// 或 https:// 开头")
    return url


def build_brand_config(raw: Mapping[str, object]) -> BrandConfig:
    """把 ``brand.yaml`` 的映射校验并构造成 :class:`BrandConfig`。

    与文件 I/O 解耦，便于单元测试与属性测试直接喂字典。
    """
    missing = [name for name in REQUIRED_FIELDS if name not in raw]
    if missing:
        raise BrandAssetsError(
            "brand.yaml: 缺少必需字段 " + ", ".join(missing)
        )
    return BrandConfig(
        brand_name=validate_brand_name(raw["brand_name"]),
        brand_name_upper=_require_single_line(
            "brand_name_upper", _require_str("brand_name_upper", raw["brand_name_upper"])
        ),
        tagline=_require_single_line("tagline", _require_str("tagline", raw["tagline"])),
        help_url=validate_help_url(raw["help_url"]),
        login_banner_text=_require_single_line(
            "login_banner_text",
            _require_str("login_banner_text", raw["login_banner_text"]),
        ),
    )


# ---------------------------------------------------------------------------
# 文件加载
# ---------------------------------------------------------------------------


def default_assets_dir() -> Path:
    """默认 Brand_Assets 目录：``<本文件所在目录>/brand``。"""
    return Path(__file__).resolve().parent / "brand"


def _check_required_files(assets_dir: Path) -> None:
    """需求 1.4：缺任一必需文件即失败，并输出**全部**缺失路径。

    一次报全比一次报一个强：CI 日志里一眼看完要补哪些文件。
    """
    missing = [
        str(assets_dir / name)
        for name in REQUIRED_ASSET_FILES
        if not (assets_dir / name).is_file()
    ]
    if missing:
        raise BrandAssetsError(
            "Brand_Assets 缺少必需文件：\n  " + "\n  ".join(missing)
        )


def _read_text(path: Path) -> str:
    """以 UTF-8 读文本，保留 LF 原样（``newline=""`` 不做行尾转换）。"""
    try:
        with open(path, "r", encoding="utf-8", newline="") as handle:
            return handle.read()
    except UnicodeDecodeError as exc:
        raise BrandAssetsError(f"{path}: 不是合法的 UTF-8 文本（{exc}）") from exc


def load_brand_config(assets_dir: Path | None = None) -> BrandConfig:
    """只加载并校验 ``brand.yaml``（仍会检查 4 个必需文件是否齐全）。"""
    return load_brand_assets(assets_dir).config


def load_brand_assets(assets_dir: Path | None = None) -> BrandAssets:
    """加载并校验整套 Brand_Assets。

    失败一律抛 :class:`BrandAssetsError`（``exit_code == 1``），调用方决定是
    打印后 ``sys.exit`` 还是继续传播。此函数不写任何文件。
    """
    directory = Path(assets_dir) if assets_dir is not None else default_assets_dir()
    if not directory.is_dir():
        raise BrandAssetsError(f"Brand_Assets 目录不存在：{directory}")
    _check_required_files(directory)

    raw_text = _read_text(directory / BRAND_YAML)
    try:
        parsed = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise BrandAssetsError(
            f"{directory / BRAND_YAML}: YAML 解析失败（{exc}）"
        ) from exc
    if not isinstance(parsed, Mapping):
        raise BrandAssetsError(
            f"{directory / BRAND_YAML}: 顶层必须是键值映射，"
            f"实际为 {type(parsed).__name__}"
        )

    logo = _read_text(directory / LOGO_ASCII)
    if not logo.strip():
        raise BrandAssetsError(f"{directory / LOGO_ASCII}: ASCII LOGO 不能为空")
    if "\r" in logo:
        # 带 CR 的 art 会让 /etc/motd 在终端里出现 ^M，也会破坏 WHOLE_FILE
        # 策略「目标内容为纯 LF」的前提。
        raise BrandAssetsError(
            f"{directory / LOGO_ASCII}: 必须使用 LF 行尾（检测到 CR）"
        )

    assets = BrandAssets(
        config=build_brand_config(parsed),
        logo_ascii=logo,
        motd_tmpl=_read_text(directory / MOTD_TMPL),
        login_banner_tmpl=_read_text(directory / LOGIN_BANNER_TMPL),
        assets_dir=directory,
    )
    # 提前触发渲染：模板里的占位符拼写错误属于「资产非法」，应该在加载期就以
    # 退出码 1 暴露，而不是等到某个策略写文件时才炸。
    assets.render_motd()
    assets.render_login_banner()
    return assets


# ---------------------------------------------------------------------------
# 独立入口：便于人工排障与退出码契约的直接验证
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """``python3 -m ligent.brandconfig [--assets DIR] [--motd]``

    正常时打印已校验的配置（或渲染出的 MOTD），退出码 0；
    资产缺失或配置非法时把诊断写到 stderr，退出码 1。
    """
    args = list(sys.argv[1:] if argv is None else argv)
    assets_dir: Path | None = None
    show_motd = False
    while args:
        arg = args.pop(0)
        if arg == "--assets":
            if not args:
                print("--assets 需要一个目录参数", file=sys.stderr)
                return EXIT_ASSETS_INVALID
            assets_dir = Path(args.pop(0))
        elif arg == "--motd":
            show_motd = True
        else:
            print(f"未知参数：{arg}", file=sys.stderr)
            return EXIT_ASSETS_INVALID

    try:
        assets = load_brand_assets(assets_dir)
    except BrandAssetsError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return exc.exit_code

    if show_motd:
        sys.stdout.write(assets.render_motd())
        return 0
    for name in REQUIRED_FIELDS:
        print(f"{name} = {getattr(assets.config, name)!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
