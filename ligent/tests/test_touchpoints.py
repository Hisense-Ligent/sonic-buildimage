"""四种替换策略的示例级单元测试（任务 3）。

范围刻意收窄：只钉住 apply / 幂等 / 锚点漂移 / 编码与权限保持四类核心行为，
以及 design.md 4.2 节点名的「绝对不能动的行」。全面的属性覆盖是任务 4.3–4.12
的 Property 1–10，这里不重复。
"""

from __future__ import annotations

import json
import re
import stat
import sys
from pathlib import Path

import pytest
from conftest import (
    BRAND_ASSETS_DIR,
    LIGENT_DIR,
    UPSTREAM_SHA256,
    snapshot,
)

sys.path.insert(0, str(LIGENT_DIR))

from brandconfig import load_brand_assets  # noqa: E402
from fileops import TMP_SUFFIX, read_text  # noqa: E402
from touchpoints import (  # noqa: E402
    BRAND_DISPLAY_VAR,
    EXIT_ANCHOR_MISS,
    EXIT_TOUCHPOINT_MISSING,
    SENTINEL_BEGIN,
    SENTINEL_END,
    TOUCHPOINT_PATHS,
    TOUCHPOINTS,
    TOUCHPOINTS_BY_PATH,
    AnchorMissError,
    RebrandError,
    Strategy,
    TouchpointMissingError,
    apply_all,
    plan_all,
    plan_touchpoint,
    resolve,
)

MOTD_PATH = "files/image_config/environment/motd"
INIT_CFG_PATH = "files/build_templates/init_cfg.json.j2"
INSTALL_SH_PATH = "installer/install.sh"
PLATFORM_CONF_PATH = "installer/default_platform.conf"


@pytest.fixture(scope="session")
def assets():
    return load_brand_assets(BRAND_ASSETS_DIR)


def _apply(repo: Path, assets) -> list:
    return apply_all(repo, assets)


def _text(repo: Path, rel: str) -> str:
    return read_text(repo / rel)


# ---------------------------------------------------------------------------
# 3.1 触点声明
# ---------------------------------------------------------------------------


def test_touchpoint_declaration_matches_design():
    """4 个触点与各自策略与 design.md 5.2 / tasks.md 3.1 一致。"""
    assert {tp.path: tp.strategy for tp in TOUCHPOINTS} == {
        MOTD_PATH: Strategy.WHOLE_FILE,
        INIT_CFG_PATH: Strategy.JSON_FIELD,
        INSTALL_SH_PATH: Strategy.SENTINEL_BLOCK,
        PLATFORM_CONF_PATH: Strategy.LINE_REWRITE,
    }
    assert TOUCHPOINT_PATHS == tuple(tp.path for tp in TOUCHPOINTS)
    for tp in TOUCHPOINTS:
        assert tp.purpose.strip(), f"{tp.path} 缺少 purpose（需求 2.4 要输出预期用途）"


def test_upstream_baselines_agree_with_fixtures():
    """触点声明里的 upstream_sha256 与测试固件基线一致。

    两处独立记录同一个事实是有意的：任一侧漂移都会立刻暴露，而不是让
    WHOLE_FILE 的 hash 门禁与固件悄悄脱节。
    """
    for tp in TOUCHPOINTS:
        assert tp.upstream_sha256 == UPSTREAM_SHA256[tp.path], tp.path


def test_line_rewrite_anchor_replacement_pairs_are_disjoint():
    """LINE_REWRITE 的锚点与替换结果互不包含（design.md 5.3(c) 的互斥前提）。"""
    tp = TOUCHPOINTS_BY_PATH[PLATFORM_CONF_PATH]
    for anchor, replacement in zip(tp.anchors, tp.replacements):
        assert anchor not in replacement
        assert replacement not in anchor


# ---------------------------------------------------------------------------
# 3.2 WHOLE_FILE
# ---------------------------------------------------------------------------


def test_motd_rewritten_from_assets(tmp_repo, assets):
    """MOTD 被整文件覆盖为 motd.tmpl + logo.ascii 的渲染结果。"""
    _apply(tmp_repo, assets)
    assert _text(tmp_repo, MOTD_PATH) == assets.render_motd()


def test_motd_line_count_and_structure_preserved(tmp_repo, assets, upstream_bytes):
    """行数与上游一致，且保留 You are on 首行、两行警告与末尾空行结构。"""
    before = upstream_bytes[MOTD_PATH].decode("utf-8")
    _apply(tmp_repo, assets)
    after = _text(tmp_repo, MOTD_PATH)

    assert after.count("\n") == before.count("\n")
    assert after.startswith("You are on\n")
    assert "Unauthorized access and/or use are prohibited.\n" in after
    assert "All access and/or use are subject to monitoring.\n" in after
    assert after.endswith("\n\n")
    assert "LIGENT" not in after  # art 是图形不是字面量
    assert "SONiC" not in after


def test_motd_second_apply_does_not_rewrite(tmp_repo, assets):
    """已是目标形态时不写盘（保持 mtime，避免无谓 git diff）。"""
    _apply(tmp_repo, assets)
    plans = plan_all(tmp_repo, assets)
    motd_plan = next(p for p in plans if p.touchpoint.path == MOTD_PATH)
    assert not motd_plan.changed


def test_motd_unknown_content_rejected_with_hash(tmp_repo, assets):
    """内容既非上游基线也非目标时退出码 3，且输出实际 hash。"""
    (tmp_repo / MOTD_PATH).write_text("上游改了这个文件\n", encoding="utf-8")
    with pytest.raises(AnchorMissError) as excinfo:
        plan_touchpoint(TOUCHPOINTS_BY_PATH[MOTD_PATH], tmp_repo, assets)
    assert excinfo.value.exit_code == EXIT_ANCHOR_MISS
    message = str(excinfo.value)
    assert MOTD_PATH in message
    assert re.search(r"实际 sha256\s*:\s*[0-9a-f]{64}", message)


# ---------------------------------------------------------------------------
# 3.3 JSON_FIELD
# ---------------------------------------------------------------------------


def _banner_global(repo: Path) -> dict:
    """从 init_cfg.json.j2 抠出 BANNER_MESSAGE.global 并解析成 dict。

    整个文件是 Jinja2 模板不能 json.load，但这一段是纯静态 JSON。
    """
    text = _text(repo, INIT_CFG_PATH)
    start = text.index('"BANNER_MESSAGE": {')
    depth = 0
    for offset in range(start, len(text)):
        if text[offset] == "{":
            depth += 1
        elif text[offset] == "}":
            depth -= 1
            if depth == 0:
                block = text[start : offset + 1]
                break
    else:  # pragma: no cover
        raise AssertionError("BANNER_MESSAGE 段落未闭合")
    return json.loads("{" + block + "}")["BANNER_MESSAGE"]["global"]


def test_banner_message_login_and_motd_rewritten(tmp_repo, assets):
    """login 用 login_banner.tmpl 结果；motd 与 MOTD 触点同源。"""
    _apply(tmp_repo, assets)
    banner = _banner_global(tmp_repo)
    assert banner["login"] == assets.render_login_banner()
    assert banner["motd"] == assets.render_motd()
    assert banner["motd"] == _text(tmp_repo, MOTD_PATH)  # 同源
    assert "Debian GNU/Linux 11" not in _text(tmp_repo, INIT_CFG_PATH)


def test_banner_message_state_and_logout_untouched(tmp_repo, assets):
    """state 与 logout 不动。"""
    before = _banner_global(tmp_repo)
    _apply(tmp_repo, assets)
    after = _banner_global(tmp_repo)
    assert after["state"] == before["state"] == "disabled"
    assert after["logout"] == before["logout"] == ""


def test_json_escape_backslash(tmp_repo, assets):
    """ASCII art 的反斜杠在 JSON 里被正确转义为 ``\\\\``（最高风险点）。"""
    _apply(tmp_repo, assets)
    text = _text(tmp_repo, INIT_CFG_PATH)
    motd_line = next(
        line for line in text.splitlines() if line.strip().startswith('"motd":')
    )
    assert "\\\\" in motd_line
    assert "\\n" in motd_line
    # 未转义的裸反斜杠一律以 \\ 形式出现：把行体切成 token 后不应有孤立 \
    assert re.search(r'(?<!\\)\\(?![\\n"/bfrtu])', motd_line) is None
    # 且反解析回来与源 MOTD 逐字节相同
    assert _banner_global(tmp_repo)["motd"] == assets.render_motd()


def test_json_field_indentation_preserved(tmp_repo, assets):
    """整行重写沿用原缩进与尾随逗号，因此第二次执行逐字节相同。"""
    _apply(tmp_repo, assets)
    first = _text(tmp_repo, INIT_CFG_PATH)
    for line in first.splitlines():
        if line.strip().startswith(('"login":', '"motd":')):
            assert line.startswith(" " * 12), repr(line[:20])
            assert line.rstrip().endswith(",")
    plans = plan_all(tmp_repo, assets)
    assert not next(p for p in plans if p.touchpoint.path == INIT_CFG_PATH).changed


def test_json_field_missing_field_line_is_exit_3(tmp_repo, assets):
    """窗口内缺 login 字段行时退出码 3。"""
    path = tmp_repo / INIT_CFG_PATH
    text = read_text(path)
    text = "\n".join(
        line for line in text.split("\n") if not line.strip().startswith('"login":')
    )
    path.write_text(text, encoding="utf-8", newline="")
    with pytest.raises(AnchorMissError) as excinfo:
        plan_touchpoint(TOUCHPOINTS_BY_PATH[INIT_CFG_PATH], tmp_repo, assets)
    assert excinfo.value.exit_code == EXIT_ANCHOR_MISS
    assert '"login":' in str(excinfo.value)


def test_json_field_missing_window_is_exit_3(tmp_repo, assets):
    """BANNER_MESSAGE 段落整体消失时退出码 3，诊断含锚点原文。"""
    path = tmp_repo / INIT_CFG_PATH
    path.write_text(
        read_text(path).replace('"BANNER_MESSAGE": {', '"BANNER_MSG": {'),
        encoding="utf-8",
        newline="",
    )
    with pytest.raises(AnchorMissError) as excinfo:
        plan_touchpoint(TOUCHPOINTS_BY_PATH[INIT_CFG_PATH], tmp_repo, assets)
    assert '"BANNER_MESSAGE": {' in str(excinfo.value)


# ---------------------------------------------------------------------------
# 3.4 SENTINEL_BLOCK
# ---------------------------------------------------------------------------


def test_sentinel_block_inserted_after_anchor(tmp_repo, assets):
    """哨兵块插在 demo_volume_revision_label 行之后，块内只有一条赋值。"""
    _apply(tmp_repo, assets)
    lines = _text(tmp_repo, INSTALL_SH_PATH).split("\n")
    anchor = 'demo_volume_revision_label="SONiC-${demo_type}-${image_version}"'
    index = lines.index(anchor)
    assert lines[index + 1].startswith(SENTINEL_BEGIN)
    assert lines[index + 2] == f'{BRAND_DISPLAY_VAR}="Ligent-${{demo_type}}"'
    assert lines[index + 3].startswith(SENTINEL_END)


def test_grub_display_var_defined_before_source(tmp_repo, assets):
    """demo_brand_display 的定义早于 ``. ./default_platform.conf``。

    default_platform.conf 的 bootloader_menu_config() 会用这个变量，晚定义就是
    GRUB 里打印空串。
    """
    _apply(tmp_repo, assets)
    text = _text(tmp_repo, INSTALL_SH_PATH)
    assert text.index(f"{BRAND_DISPLAY_VAR}=") < text.index(". ./default_platform.conf")


def test_sentinel_block_idempotent_and_not_duplicated(tmp_repo, assets):
    """第二次执行走「整块替换」分支，块只出现一次、内容逐字节相同。"""
    _apply(tmp_repo, assets)
    first = _text(tmp_repo, INSTALL_SH_PATH)
    _apply(tmp_repo, assets)
    second = _text(tmp_repo, INSTALL_SH_PATH)
    assert first == second
    assert second.count(SENTINEL_BEGIN) == 1
    assert second.count(SENTINEL_END) == 1
    assert second.count(f"{BRAND_DISPLAY_VAR}=") == 1


def test_sentinel_block_stale_content_replaced(tmp_repo, assets):
    """旧品牌名的哨兵块被整块替换，而不是并列出现两个块。"""
    path = tmp_repo / INSTALL_SH_PATH
    anchor = 'demo_volume_revision_label="SONiC-${demo_type}-${image_version}"'
    stale = (
        f"{anchor}\n"
        f"{SENTINEL_BEGIN}: stale\n"
        f'{BRAND_DISPLAY_VAR}="OldBrand-${{demo_type}}"\n'
        f"{SENTINEL_END}"
    )
    path.write_text(read_text(path).replace(anchor, stale), encoding="utf-8", newline="")
    _apply(tmp_repo, assets)
    text = _text(tmp_repo, INSTALL_SH_PATH)
    assert text.count(SENTINEL_BEGIN) == 1
    assert "OldBrand" not in text
    assert f'{BRAND_DISPLAY_VAR}="Ligent-${{demo_type}}"' in text


def test_sentinel_block_anchor_drift_is_exit_3(tmp_repo, assets):
    """锚点被破坏且无哨兵块时退出码 3，诊断含未命中锚点原文。"""
    path = tmp_repo / INSTALL_SH_PATH
    anchor = 'demo_volume_revision_label="SONiC-${demo_type}-${image_version}"'
    path.write_text(
        read_text(path).replace(anchor, 'demo_volume_revision_label="$other"'),
        encoding="utf-8",
        newline="",
    )
    with pytest.raises(AnchorMissError) as excinfo:
        plan_touchpoint(TOUCHPOINTS_BY_PATH[INSTALL_SH_PATH], tmp_repo, assets)
    assert excinfo.value.exit_code == EXIT_ANCHOR_MISS
    assert anchor in str(excinfo.value)
    assert INSTALL_SH_PATH in str(excinfo.value)


def test_install_sh_volume_labels_untouched(tmp_repo, assets):
    """install.sh 中卷标相关的三行逐字节不动（Property 5 的样例版）。"""
    _apply(tmp_repo, assets)
    text = _text(tmp_repo, INSTALL_SH_PATH)
    for line in (
        'demo_volume_label="SONiC-${demo_type}"',
        'demo_volume_revision_label="SONiC-${demo_type}-${image_version}"',
        "mkfs.ext4 -L $demo_volume_label $demo_dev",
    ):
        assert line in text


# ---------------------------------------------------------------------------
# 3.5 LINE_REWRITE
# ---------------------------------------------------------------------------


def test_grub_echo_lines_rewritten(tmp_repo, assets):
    """两处 echo 改为引用 $demo_brand_display，锚点原文不再存在。"""
    _apply(tmp_repo, assets)
    text = _text(tmp_repo, PLATFORM_CONF_PATH)
    for suffix in ("kernel", "initial ramdisk"):
        assert f"echo    'Loading ${BRAND_DISPLAY_VAR} $demo_type {suffix} ...'" in text
        assert f"echo    'Loading $demo_volume_label $demo_type {suffix} ...'" not in text
    assert text.count(f"${BRAND_DISPLAY_VAR}") == 2


#: design.md 4.2 节点名的「绝对不能动的行」。改这些会在装机或升级时炸，
#: 而不是在构建时炸——所以必须由测试逐条钉住。
IMMUTABLE_PLATFORM_CONF_LINES = (
    'demo_grub_entry="$demo_volume_revision_label"',
    "search --no-floppy --label --set=root $demo_volume_label",
    "mkfs.ext4 -L $demo_volume_label $demo_dev",
    "--change-name=${demo_part}:$demo_volume_label $blk_dev \\",
    '--bootloader-id="$demo_volume_label" \\',
    '--label "$demo_volume_label" \\',
    '--loader "/EFI/$demo_volume_label/shimx64.efi"',
    "mkdir -p /boot/efi/EFI/$demo_volume_label",
    "sed \"/^menuentry '${demo_volume_label}-${running_sonic_revision}'/,/}/!d\"",
    'blkid | grep -e "$demo_volume_label"',
    'sgdisk -p $blk_dev | grep -e "$demo_volume_label"',
    'efibootmgr | grep -e "$demo_volume_label"',
    'echo "Installed SONiC base image $demo_volume_label successfully"',
)


def test_platform_conf_identity_lines_untouched(tmp_repo, assets):
    """分区识别、EFI、search --label、menuentry 标题等行逐字节不变。"""
    before = _text(tmp_repo, PLATFORM_CONF_PATH)
    for line in IMMUTABLE_PLATFORM_CONF_LINES:
        assert line in before, f"固件里就没有这行，测试基线过期：{line}"
    _apply(tmp_repo, assets)
    after = _text(tmp_repo, PLATFORM_CONF_PATH)
    for line in IMMUTABLE_PLATFORM_CONF_LINES:
        assert after.count(line) == before.count(line), line


def test_platform_conf_diff_is_exactly_two_lines(tmp_repo, assets):
    """apply 前后 default_platform.conf 只有那两行发生变化。"""
    before = _text(tmp_repo, PLATFORM_CONF_PATH).split("\n")
    _apply(tmp_repo, assets)
    after = _text(tmp_repo, PLATFORM_CONF_PATH).split("\n")
    assert len(before) == len(after)
    changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    assert len(changed) == 2
    for index in changed:
        assert "Loading" in before[index]


def test_line_rewrite_second_apply_does_not_write(tmp_repo, assets):
    """锚点为 0 且替换结果存在 → 不写入（三分支的第二支）。"""
    _apply(tmp_repo, assets)
    plans = plan_all(tmp_repo, assets)
    plan = next(p for p in plans if p.touchpoint.path == PLATFORM_CONF_PATH)
    assert not plan.changed
    assert any("已改造" in note for note in plan.notes)


def test_line_rewrite_both_missing_is_exit_3(tmp_repo, assets):
    """锚点与替换结果皆不存在 → 退出码 3，输出未命中锚点原文（三分支第三支）。"""
    path = tmp_repo / PLATFORM_CONF_PATH
    anchor = "echo    'Loading $demo_volume_label $demo_type kernel ...'"
    path.write_text(
        read_text(path).replace(anchor, "echo 'upstream rewrote this'"),
        encoding="utf-8",
        newline="",
    )
    with pytest.raises(AnchorMissError) as excinfo:
        plan_touchpoint(TOUCHPOINTS_BY_PATH[PLATFORM_CONF_PATH], tmp_repo, assets)
    assert excinfo.value.exit_code == EXIT_ANCHOR_MISS
    assert anchor in str(excinfo.value)


# ---------------------------------------------------------------------------
# 3.6 原子写入、编码、权限
# ---------------------------------------------------------------------------


def test_apply_is_idempotent_bytewise(tmp_repo, assets):
    """全部触点：apply 两次的结果逐字节相同（需求 2.2 的样例版）。"""
    _apply(tmp_repo, assets)
    first = snapshot(tmp_repo)
    _apply(tmp_repo, assets)
    assert snapshot(tmp_repo) == first


def test_apply_touches_only_declared_touchpoints(tmp_repo, assets):
    """变化的文件集合是触点集合的子集（需求 2.3 的样例版）。"""
    before = snapshot(tmp_repo)
    _apply(tmp_repo, assets)
    after = snapshot(tmp_repo)
    assert set(after) == set(before)
    changed = {path for path in before if before[path] != after[path]}
    assert changed <= set(TOUCHPOINT_PATHS)
    assert changed == set(TOUCHPOINT_PATHS)  # 上游基线上四个都会变


def test_no_temp_files_left_behind(tmp_repo, assets):
    """原子写入的临时文件不残留。"""
    _apply(tmp_repo, assets)
    leftovers = [p for p in tmp_repo.rglob(f"*{TMP_SUFFIX}")]
    assert leftovers == []


def test_file_modes_preserved(tmp_repo, assets):
    """权限位不变：install.sh / default_platform.conf 仍是 0755。"""
    before = {
        rel: stat.S_IMODE((tmp_repo / rel).stat().st_mode) for rel in TOUCHPOINT_PATHS
    }
    _apply(tmp_repo, assets)
    after = {
        rel: stat.S_IMODE((tmp_repo / rel).stat().st_mode) for rel in TOUCHPOINT_PATHS
    }
    assert after == before
    assert before[INSTALL_SH_PATH] & stat.S_IXUSR


def test_no_bom_written(tmp_repo, assets):
    """不写 BOM。"""
    _apply(tmp_repo, assets)
    for rel in TOUCHPOINT_PATHS:
        assert not (tmp_repo / rel).read_bytes().startswith(b"\xef\xbb\xbf"), rel


def test_crlf_line_endings_preserved(tmp_repo, assets):
    """CRLF 风格的 install.sh / default_platform.conf 在 apply 后仍是纯 CRLF。"""
    for rel in (INSTALL_SH_PATH, PLATFORM_CONF_PATH):
        path = tmp_repo / rel
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    _apply(tmp_repo, assets)
    for rel in (INSTALL_SH_PATH, PLATFORM_CONF_PATH):
        data = (tmp_repo / rel).read_bytes()
        assert data.count(b"\n") == data.count(b"\r\n"), rel


def test_plan_all_has_no_side_effects(tmp_repo, assets):
    """plan_all（--check 的实现基础）不改动任何文件（需求 2.6）。"""
    before = snapshot(tmp_repo)
    plan_all(tmp_repo, assets)
    assert snapshot(tmp_repo) == before
    assert list(tmp_repo.rglob(f"*{TMP_SUFFIX}")) == []


def test_missing_touchpoint_reports_path_and_purpose(tmp_repo, assets):
    """触点文件缺失 → 退出码 2，诊断含路径与预期用途（需求 2.4）。"""
    tp = TOUCHPOINTS_BY_PATH[MOTD_PATH]
    resolve(tmp_repo, tp).unlink()
    with pytest.raises(TouchpointMissingError) as excinfo:
        plan_touchpoint(tp, tmp_repo, assets)
    assert excinfo.value.exit_code == EXIT_TOUCHPOINT_MISSING
    assert MOTD_PATH in str(excinfo.value)
    assert tp.purpose[:8] in str(excinfo.value)


def test_apply_all_writes_nothing_when_any_anchor_drifts(tmp_repo, assets):
    """任一触点漂移 → 先抛错再写盘，工作区保持原样（Property 8 的样例版）。

    install.sh 的锚点被破坏，但 motd 本来是要改的；apply_all 必须让 motd 也
    保持未改，避免留下「改了两个、剩两个没改」的中间态。
    """
    path = tmp_repo / INSTALL_SH_PATH
    path.write_text(
        read_text(path).replace(
            'demo_volume_revision_label="SONiC-${demo_type}-${image_version}"',
            'demo_volume_revision_label="$x"',
        ),
        encoding="utf-8",
        newline="",
    )
    before = snapshot(tmp_repo)
    with pytest.raises(RebrandError):
        apply_all(tmp_repo, assets)
    assert snapshot(tmp_repo) == before


# ---------------------------------------------------------------------------
# 工作区状态固件（conftest 的 REBRANDED 态）
# ---------------------------------------------------------------------------


def test_rebranded_workspace_state(make_tmp_repo, assets):
    """make_tmp_repo(REBRANDED) 产出的工作区已改造，且再 apply 不再变化。"""
    from conftest import WorkspaceState

    repo = make_tmp_repo(WorkspaceState.REBRANDED)
    assert _text(repo, MOTD_PATH) == assets.render_motd()
    before = snapshot(repo)
    _apply(repo, assets)
    assert snapshot(repo) == before
