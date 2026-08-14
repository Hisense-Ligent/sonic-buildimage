"""Brand_Assets 与 BrandConfig 的示例级单元测试（任务 2.3）。

覆盖两类断言：
* 资产齐全（需求 1.2）——对应设计文档「示例级单元测试」表的 ``test_brand_assets_present``
* 品牌名与其余字段的合法/非法边界样例（需求 1.4、3.3）

属性测试留给任务 4.3 起的 Property 1–10，这里只钉住具体边界值。
"""

from __future__ import annotations

import sys

import pytest
import yaml
from conftest import BRAND_ASSETS_DIR, LIGENT_DIR

sys.path.insert(0, str(LIGENT_DIR))

from brandconfig import (  # noqa: E402  (需先注入 sys.path)
    BRAND_NAME_MAX_LEN,
    EXIT_ASSETS_INVALID,
    REQUIRED_ASSET_FILES,
    REQUIRED_FIELDS,
    BrandAssetsError,
    build_brand_config,
    load_brand_assets,
    main,
    validate_brand_name,
)

# ---------------------------------------------------------------------------
# 资产存在性
# ---------------------------------------------------------------------------


def test_brand_assets_present():
    """brand.yaml、logo.ascii、motd.tmpl、login_banner.tmpl 均存在（需求 1.2）。"""
    for name in REQUIRED_ASSET_FILES:
        path = BRAND_ASSETS_DIR / name
        assert path.is_file(), f"缺少品牌资产 {path}"


def test_required_asset_files_covers_design_list():
    """必需文件清单与 design.md 4.1 节的目录布局一致。"""
    assert set(REQUIRED_ASSET_FILES) == {
        "brand.yaml",
        "logo.ascii",
        "motd.tmpl",
        "login_banner.tmpl",
    }


def test_repo_brand_assets_load_successfully():
    """仓库内实际签入的资产可被加载并通过全部校验。"""
    assets = load_brand_assets(BRAND_ASSETS_DIR)
    assert assets.config.brand_name == "Ligent"
    assert assets.config.brand_name_upper == "LIGENT"
    assert assets.config.help_url.startswith("https://")
    assert assets.config.login_banner_text == "Ligent Network OS"


def test_logo_ascii_is_six_lines_ending_with_blank():
    """logo.ascii 为 6 行（5 行 art + 1 行空行），与上游 SONiC art 同高。"""
    logo = load_brand_assets(BRAND_ASSETS_DIR).logo_ascii
    assert logo.endswith("\n")
    lines = logo.split("\n")[:-1]  # 去掉尾部 "" 元素
    assert len(lines) == 6, f"logo.ascii 行数为 {len(lines)}"
    assert lines[-1] == "", "第 6 行应为空行，保证 MOTD 版式不跳"
    assert "\\" in logo, "ASCII art 的反斜杠不应被吞掉"


def test_rendered_motd_contains_brand_and_keeps_legal_text():
    """渲染出的 MOTD 含品牌内容，且法务文案与首行原样保留。"""
    assets = load_brand_assets(BRAND_ASSETS_DIR)
    motd = assets.render_motd()
    assert motd.startswith("You are on\n")
    assert assets.config.tagline in motd
    assert f"Help:    {assets.config.help_url}\n" in motd
    assert "Unauthorized access and/or use are prohibited.\n" in motd
    assert "All access and/or use are subject to monitoring.\n" in motd
    assert motd.endswith("\n\n"), "文件末尾空行结构不能丢"
    assert "%%" not in motd, "渲染后不应残留占位符"


def test_rendered_login_banner_is_single_line():
    """BANNER_MESSAGE.login 的渲染结果是单行、不含行尾换行。"""
    assets = load_brand_assets(BRAND_ASSETS_DIR)
    assert assets.render_login_banner() == assets.config.login_banner_text


def test_missing_asset_file_reports_path_and_exit_code(tmp_path):
    """缺文件时以退出码 1 失败，并输出全部缺失路径（需求 1.4）。"""
    partial = tmp_path / "brand"
    partial.mkdir()
    (partial / "brand.yaml").write_text("brand_name: Ligent\n", encoding="utf-8")
    with pytest.raises(BrandAssetsError) as excinfo:
        load_brand_assets(partial)
    message = str(excinfo.value)
    assert excinfo.value.exit_code == EXIT_ASSETS_INVALID
    for name in ("logo.ascii", "motd.tmpl", "login_banner.tmpl"):
        assert str(partial / name) in message
    assert main(["--assets", str(partial)]) == EXIT_ASSETS_INVALID


def test_missing_assets_dir_reports_path(tmp_path):
    """目录整体不存在时也走退出码 1，并输出该路径。"""
    absent = tmp_path / "nope"
    with pytest.raises(BrandAssetsError) as excinfo:
        load_brand_assets(absent)
    assert str(absent) in str(excinfo.value)


def test_main_on_repo_assets_exits_zero():
    """合法资产下独立入口退出码 0。"""
    assert main(["--assets", str(BRAND_ASSETS_DIR)]) == 0


# ---------------------------------------------------------------------------
# brand_name 边界样例
# ---------------------------------------------------------------------------

VALID_BRAND_NAMES = [
    "L",                            # 单字符（下界）
    "Ligent",
    "Ligent Networks",              # 含空格
    "Ligent-OS",                    # 含连字符
    "Ligent.OS_2024",               # 含点与下划线
    "L" * BRAND_NAME_MAX_LEN,       # 恰好 32 字符（上界）
]

INVALID_BRAND_NAMES = [
    "",                             # 空字符串
    "L" * (BRAND_NAME_MAX_LEN + 1),  # 33 字符，越界
    "Ligent$USER",                  # shell 变量展开
    "Ligent${demo_type}",           # 同上
    "Ligent`id`",                   # 反引号命令替换
    'Ligent"OS',                    # 双引号提前闭合赋值
    "Ligent\\OS",                   # 反斜杠转义
    "Ligent;rm -rf /",              # 命令分隔符
    "Ligent|cat",                   # 管道
    "Ligent&",                      # 后台执行
    "Ligent'OS",                    # 单引号，破坏 GRUB echo
    "Ligent\nOS",                   # 换行
    "Ligent\rOS",                   # CR
    "Ligent(1)",                    # 子 shell
    "锐捷",                          # 非 ASCII
    "Ligenté",                      # 非 ASCII
    "   ",                          # 纯空白
]


@pytest.mark.parametrize("name", VALID_BRAND_NAMES)
def test_valid_brand_names_accepted(name):
    assert validate_brand_name(name) == name


@pytest.mark.parametrize("name", INVALID_BRAND_NAMES)
def test_invalid_brand_names_rejected_with_reason(name):
    """非法品牌名以退出码 1 拒绝，且诊断说明原因（需求 1.4、3.3）。"""
    with pytest.raises(BrandAssetsError) as excinfo:
        validate_brand_name(name)
    assert excinfo.value.exit_code == EXIT_ASSETS_INVALID
    assert "brand_name" in str(excinfo.value)


def test_non_string_brand_name_rejected():
    """YAML 把 ``1.0`` / ``yes`` 解析成非字符串，必须拒绝而不是隐式转换。"""
    for value in (1.0, True, None, ["Ligent"]):
        with pytest.raises(BrandAssetsError):
            validate_brand_name(value)


# ---------------------------------------------------------------------------
# 其余字段的校验
# ---------------------------------------------------------------------------


def _base_mapping() -> dict[str, str]:
    return {
        "brand_name": "Ligent",
        "brand_name_upper": "LIGENT",
        "tagline": "-- Ligent Data Center Network Operating System --",
        "help_url": "https://github.com/Hisense-Ligent/sonic-buildimage",
        "login_banner_text": "Ligent Network OS",
    }


def test_base_mapping_is_valid():
    config = build_brand_config(_base_mapping())
    assert config.brand_name == "Ligent"


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_missing_field_rejected(field):
    raw = _base_mapping()
    del raw[field]
    with pytest.raises(BrandAssetsError) as excinfo:
        build_brand_config(raw)
    assert field in str(excinfo.value)


@pytest.mark.parametrize(
    "url",
    ["github.com/x", "ftp://example.com", "://example.com", "HTTPS://example.com", ""],
)
def test_help_url_requires_http_prefix(url):
    raw = _base_mapping()
    raw["help_url"] = url
    with pytest.raises(BrandAssetsError) as excinfo:
        build_brand_config(raw)
    assert "help_url" in str(excinfo.value)


@pytest.mark.parametrize("url", ["http://x.internal/docs", "https://example.com"])
def test_help_url_accepts_both_schemes(url):
    raw = _base_mapping()
    raw["help_url"] = url
    assert build_brand_config(raw).help_url == url


@pytest.mark.parametrize("field", ["tagline", "login_banner_text", "brand_name_upper"])
@pytest.mark.parametrize("value", ["", "a\nb", "a\r\nb", "   "])
def test_single_line_fields_reject_empty_and_multiline(field, value):
    raw = _base_mapping()
    raw[field] = value
    with pytest.raises(BrandAssetsError) as excinfo:
        build_brand_config(raw)
    assert field in str(excinfo.value)


# ---------------------------------------------------------------------------
# 模板与 YAML 结构的健壮性
# ---------------------------------------------------------------------------


def _write_assets(directory, *, motd=None, logo=None, banner=None, raw=None):
    directory.mkdir(parents=True, exist_ok=True)
    payload = _base_mapping() if raw is None else raw
    (directory / "brand.yaml").write_text(
        yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8"
    )
    (directory / "logo.ascii").write_text(
        logo if logo is not None else "  _\n | |\n | |\n | |\n |_|\n\n",
        encoding="utf-8",
        newline="",
    )
    (directory / "motd.tmpl").write_text(
        motd if motd is not None else "You are on\n%%LOGO%%\n%%TAGLINE%%\n",
        encoding="utf-8",
        newline="",
    )
    (directory / "login_banner.tmpl").write_text(
        banner if banner is not None else "%%LOGIN_BANNER_TEXT%%\n",
        encoding="utf-8",
        newline="",
    )
    return directory


def test_unknown_placeholder_in_template_rejected(tmp_path):
    """模板里的未知占位符在加载期即失败，避免 %%FOO%% 被写进镜像。"""
    directory = _write_assets(tmp_path / "brand", motd="You are on\n%%TYPO%%\n")
    with pytest.raises(BrandAssetsError) as excinfo:
        load_brand_assets(directory)
    assert "%%TYPO%%" in str(excinfo.value)


def test_logo_with_crlf_rejected(tmp_path):
    """带 CR 的 art 会让 /etc/motd 出现 ^M，必须拒绝。"""
    directory = _write_assets(tmp_path / "brand", logo="  _\r\n | |\r\n\r\n")
    with pytest.raises(BrandAssetsError) as excinfo:
        load_brand_assets(directory)
    assert "LF" in str(excinfo.value)


def test_empty_logo_rejected(tmp_path):
    directory = _write_assets(tmp_path / "brand", logo="\n\n")
    with pytest.raises(BrandAssetsError):
        load_brand_assets(directory)


def test_non_mapping_yaml_rejected(tmp_path):
    directory = _write_assets(tmp_path / "brand")
    (directory / "brand.yaml").write_text("- Ligent\n", encoding="utf-8")
    with pytest.raises(BrandAssetsError) as excinfo:
        load_brand_assets(directory)
    assert "映射" in str(excinfo.value)


def test_malformed_yaml_rejected(tmp_path):
    directory = _write_assets(tmp_path / "brand")
    (directory / "brand.yaml").write_text("brand_name: [unclosed\n", encoding="utf-8")
    with pytest.raises(BrandAssetsError) as excinfo:
        load_brand_assets(directory)
    assert "YAML" in str(excinfo.value)


def test_invalid_brand_name_in_yaml_exits_one(tmp_path):
    """非法品牌名经独立入口时退出码为 1，且不写入任何文件（Property 8 前置）。"""
    raw = _base_mapping()
    raw["brand_name"] = "Ligent$(id)"
    directory = _write_assets(tmp_path / "brand", raw=raw)
    before = sorted(p.name for p in directory.iterdir())
    assert main(["--assets", str(directory)]) == EXIT_ASSETS_INVALID
    assert sorted(p.name for p in directory.iterdir()) == before
