"""Brand_Verifier 的示例级单元测试（任务 8.1）。

覆盖三件事：

1. **两种模式的判定结果**：工作区模式与镜像模式在同样的输入上给出同样的结论
   （BV-02..BV-05 全过 / 单项失败），退出码符合需求 4.3。
2. **反向检查真的会报警**：BV-03/BV-04 是本设计最关键的安全网（改了卷标或
   menuentry 标题会在装机时炸），必须有用例证明它们不是恒真的装饰。
3. **`.bin` 解包手法与上游一致**：用 ``sharch_body.sh`` 同款结构（``exit_marker``
   行 + ``payload_image_size`` 截断）合成一个最小自解压包，验证解包逻辑。

合成 ``.bin`` 而不是找真实产物：真实 ``.bin` 有几 GB 且需要两小时构建，而解包
逻辑只依赖 sharch 的头部结构，几 KB 的合成包能覆盖同样的分支，还能构造出
「载荷后有多余字节」这种真实产物里不好复现的情况。
"""

from __future__ import annotations

import io
import json
import sys
import tarfile
from pathlib import Path

import pytest
from conftest import BRAND_ASSETS_DIR, LIGENT_DIR, WorkspaceState

sys.path.insert(0, str(LIGENT_DIR))

from verify_brand import (  # noqa: E402
    EXIT_FAIL,
    EXIT_OK,
    FAIL,
    INSTALL_SH,
    MENUENTRY_LINE,
    OK,
    PLATFORM_CONF,
    VOLUME_LABEL_LINE,
    VerifyInputError,
    extract_members,
    find_payload_offset,
    logo_first_line,
    main,
    run_checks,
)

ALL_CHECK_IDS = ("BV-02", "BV-03", "BV-04", "BV-05")


# ---------------------------------------------------------------------------
# 合成 .bin
# ---------------------------------------------------------------------------


def make_sharch_bin(dest: Path, members: dict[str, bytes], trailing: bytes = b"") -> Path:
    """按 ``installer/sharch_body.sh`` 的结构合成一个自解压包。

    ``trailing`` 模拟载荷之后的多余字节：解包必须按 ``payload_image_size`` 截断，
    否则 tarfile 会读到垃圾数据。
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    tar_bytes = buffer.getvalue()
    header = (
        "#!/bin/sh\n"
        f"payload_image_size={len(tar_bytes)}\n"
        "payload_sha1=deadbeef\n"
        "echo 'this is a synthetic sharch header'\n"
        "exit 0\n"
        "exit_marker\n"
    ).encode("utf-8")
    dest.write_bytes(header + tar_bytes + trailing)
    return dest


@pytest.fixture
def rebranded_repo(make_tmp_repo) -> Path:
    return make_tmp_repo(WorkspaceState.REBRANDED)


@pytest.fixture
def upstream_repo(make_tmp_repo) -> Path:
    return make_tmp_repo(WorkspaceState.UPSTREAM)


def bin_from_repo(repo: Path, tmp_path: Path, name: str = "sonic-vs.bin", **kwargs) -> Path:
    return make_sharch_bin(
        tmp_path / name,
        {
            f"./{INSTALL_SH}": (repo / INSTALL_SH).read_bytes(),
            f"./{PLATFORM_CONF}": (repo / PLATFORM_CONF).read_bytes(),
            "./installer/fs.squashfs": b"not a real squashfs" * 100,
        },
        **kwargs,
    )


def statuses(results) -> dict[str, str]:
    return {r.check_id: r.status for r in results}


# ---------------------------------------------------------------------------
# 工作区模式（无 --image）
# ---------------------------------------------------------------------------


def test_workspace_mode_all_pass_after_rebrand(rebranded_repo: Path) -> None:
    results = run_checks(rebranded_repo, assets_dir=BRAND_ASSETS_DIR)
    assert statuses(results) == {cid: OK for cid in ALL_CHECK_IDS}


def test_workspace_mode_fails_on_upstream(upstream_repo: Path) -> None:
    """未改造的工作区：BV-02/BV-05 失败，BV-03/BV-04 仍应通过（底层标识本来就完好）。"""
    results = run_checks(upstream_repo, assets_dir=BRAND_ASSETS_DIR)
    result_map = statuses(results)
    assert result_map["BV-02"] == FAIL
    assert result_map["BV-05"] == FAIL
    assert result_map["BV-03"] == OK
    assert result_map["BV-04"] == OK


def test_workspace_results_are_labeled_as_workspace(rebranded_repo: Path) -> None:
    """工作区模式不能证明打包环节正确，输出必须自带标记。"""
    results = run_checks(rebranded_repo, assets_dir=BRAND_ASSETS_DIR)
    assert all("[workspace]" in r.detail for r in results if r.check_id != "BV-05")


def test_all_check_ids_reported_regardless_of_outcome(upstream_repo: Path) -> None:
    """需求 4.4：无条件输出全部检查项的 ID 与状态。"""
    results = run_checks(upstream_repo, assets_dir=BRAND_ASSETS_DIR)
    assert tuple(r.check_id for r in results) == ALL_CHECK_IDS


# ---------------------------------------------------------------------------
# 反向检查 BV-03 / BV-04
# ---------------------------------------------------------------------------


def test_bv03_detects_volume_label_change(rebranded_repo: Path) -> None:
    """把卷标改成 Ligent-* 必须报 BV-03 FAIL——这正是装机期故障的来源。"""
    path = rebranded_repo / INSTALL_SH
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            VOLUME_LABEL_LINE, 'demo_volume_label="Ligent-${demo_type}"'
        ),
        encoding="utf-8",
    )
    result_map = statuses(run_checks(rebranded_repo, assets_dir=BRAND_ASSETS_DIR))
    assert result_map["BV-03"] == FAIL
    # 只有 BV-03 该失败，其余不受影响——诊断必须精确指向被破坏的那一项
    assert [cid for cid, st in result_map.items() if st == FAIL] == ["BV-03"]


def test_bv03_diagnostic_explains_the_blast_radius(rebranded_repo: Path) -> None:
    path = rebranded_repo / INSTALL_SH
    path.write_text(
        path.read_text(encoding="utf-8").replace(VOLUME_LABEL_LINE, "# removed"),
        encoding="utf-8",
    )
    result = next(
        r for r in run_checks(rebranded_repo, assets_dir=BRAND_ASSETS_DIR) if r.check_id == "BV-03"
    )
    text = result.format()
    assert "sonic-installer" in text or "分区识别" in text
    assert "demo_brand_display" in text


def test_bv04_detects_menuentry_change(rebranded_repo: Path) -> None:
    path = rebranded_repo / PLATFORM_CONF
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            MENUENTRY_LINE, 'demo_grub_entry="$demo_brand_display"'
        ),
        encoding="utf-8",
    )
    result_map = statuses(run_checks(rebranded_repo, assets_dir=BRAND_ASSETS_DIR))
    assert result_map["BV-04"] == FAIL
    assert "IMAGE_PREFIX" in next(
        r.format() for r in run_checks(rebranded_repo, assets_dir=BRAND_ASSETS_DIR)
        if r.check_id == "BV-04"
    )


# ---------------------------------------------------------------------------
# BV-02 / BV-05 的单点注入
# ---------------------------------------------------------------------------


def test_bv02_requires_two_display_sites(rebranded_repo: Path) -> None:
    """只改一处 echo 也算失败：两处提示文字必须同时改，否则启动版式割裂。"""
    path = rebranded_repo / PLATFORM_CONF
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("$demo_brand_display", "$demo_volume_label", 1), encoding="utf-8")
    result = next(
        r for r in run_checks(rebranded_repo, assets_dir=BRAND_ASSETS_DIR) if r.check_id == "BV-02"
    )
    assert result.status == FAIL
    assert "1/2" in result.detail or "1 次" in result.format()


def test_bv05_requires_both_motd_paths(rebranded_repo: Path) -> None:
    """静态 MOTD 与 BANNER_MESSAGE.motd 必须同源，只改一处会出现开 banner 变回 SONiC。"""
    motd = rebranded_repo / "files/image_config/environment/motd"
    motd.write_text("You are on nothing\n", encoding="utf-8")
    result = next(
        r for r in run_checks(rebranded_repo, assets_dir=BRAND_ASSETS_DIR) if r.check_id == "BV-05"
    )
    assert result.status == FAIL
    assert "files/image_config/environment/motd" in result.detail


def test_bv05_checks_json_escaped_form(rebranded_repo: Path) -> None:
    """``init_cfg.json.j2`` 里的 art 是 JSON 转义形态，判据必须按转义后比较。

    art 首行恰好不含反斜杠（``  _     ___ ____ ...``），所以「首行原样也能命中」。
    真正的转义风险在后面几行（``|_____|___\\____|``）。这里两件事一起断言：

    1. BV-05 用的判据（首行的 JSON 转义形态）确实出现在模板里；
    2. 含反斜杠的那些行在模板里是**双写**的——若哪天 art 首行改成带反斜杠的样式，
       按原样比较会立刻失效，这条断言就是那时的护栏。
    """
    art = logo_first_line(BRAND_ASSETS_DIR)
    raw = (rebranded_repo / "files/build_templates/init_cfg.json.j2").read_text(encoding="utf-8")
    assert json.dumps(art)[1:-1] in raw

    sys.path.insert(0, str(LIGENT_DIR))
    from brandconfig import load_brand_assets

    logo = load_brand_assets(BRAND_ASSETS_DIR).logo_ascii
    backslash_lines = [line for line in logo.splitlines() if "\\" in line]
    assert backslash_lines, "logo.ascii 里没有反斜杠，这条转义断言失去意义"
    for line in backslash_lines:
        escaped = json.dumps(line)[1:-1]
        assert "\\\\" in escaped
        assert escaped in raw, f"含反斜杠的 art 行未按 JSON 转义写入模板：{line!r}"


# ---------------------------------------------------------------------------
# 镜像模式
# ---------------------------------------------------------------------------


def test_image_mode_all_pass(rebranded_repo: Path, tmp_path: Path) -> None:
    image = bin_from_repo(rebranded_repo, tmp_path)
    results = run_checks(rebranded_repo, image=image, assets_dir=BRAND_ASSETS_DIR)
    assert statuses(results) == {cid: OK for cid in ALL_CHECK_IDS}
    # 镜像模式的结果不带 [workspace] 标记
    assert not any("[workspace]" in r.detail for r in results if r.check_id != "BV-05")


def test_image_mode_reads_from_bin_not_workspace(
    rebranded_repo: Path, upstream_repo: Path, tmp_path: Path
) -> None:
    """镜像模式的 BV-02 必须读 ``.bin`` 内容：包里已改造、工作区未改造时也应通过。"""
    image = bin_from_repo(rebranded_repo, tmp_path)
    result = next(
        r
        for r in run_checks(upstream_repo, image=image, assets_dir=BRAND_ASSETS_DIR)
        if r.check_id == "BV-02"
    )
    assert result.status == OK


def test_extract_members_respects_payload_size(rebranded_repo: Path, tmp_path: Path) -> None:
    """载荷后有多余字节时仍能正确解包（对应 sharch 的 ``head -c``）。"""
    image = bin_from_repo(rebranded_repo, tmp_path, trailing=b"\x00" * 4096 + b"junk")
    members = extract_members(image)
    assert VOLUME_LABEL_LINE in members[INSTALL_SH]
    assert MENUENTRY_LINE in members[PLATFORM_CONF]


def test_find_payload_offset_locates_exit_marker(rebranded_repo: Path, tmp_path: Path) -> None:
    image = bin_from_repo(rebranded_repo, tmp_path)
    offset, size = find_payload_offset(image)
    assert size is not None and size > 0
    assert image.read_bytes()[offset - len("exit_marker\n") : offset] == b"exit_marker\n"


def test_non_sharch_file_is_rejected(tmp_path: Path) -> None:
    bogus = tmp_path / "not-an-image.bin"
    bogus.write_bytes(b"#!/bin/sh\necho hello\n")
    with pytest.raises(VerifyInputError) as excinfo:
        extract_members(bogus)
    assert "exit_marker" in str(excinfo.value)


# ---------------------------------------------------------------------------
# CLI 契约（需求 4.3）
# ---------------------------------------------------------------------------


def test_cli_exit_zero_when_all_pass(rebranded_repo: Path) -> None:
    assert (
        main(["--workspace", str(rebranded_repo), "--assets", str(BRAND_ASSETS_DIR)]) == EXIT_OK
    )


def test_cli_exit_one_when_any_check_fails(upstream_repo: Path) -> None:
    assert (
        main(["--workspace", str(upstream_repo), "--assets", str(BRAND_ASSETS_DIR)]) == EXIT_FAIL
    )


def test_cli_prints_every_check_id(upstream_repo: Path, capsys) -> None:
    main(["--workspace", str(upstream_repo), "--assets", str(BRAND_ASSETS_DIR)])
    out = capsys.readouterr().out
    for check_id in ALL_CHECK_IDS:
        assert check_id in out


def test_cli_json_output_is_machine_readable(rebranded_repo: Path, capsys) -> None:
    main(["--workspace", str(rebranded_repo), "--assets", str(BRAND_ASSETS_DIR), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "workspace"
    assert payload["passed"] is True
    assert [c["id"] for c in payload["checks"]] == list(ALL_CHECK_IDS)


def test_require_image_rejects_workspace_only_run(rebranded_repo: Path) -> None:
    """流水线必须带 --require-image：否则 .bin 路径写错会退化成工作区模式全绿。"""
    assert (
        main(
            [
                "--workspace",
                str(rebranded_repo),
                "--assets",
                str(BRAND_ASSETS_DIR),
                "--require-image",
            ]
        )
        == EXIT_FAIL
    )


def test_missing_image_is_an_error(rebranded_repo: Path, tmp_path: Path) -> None:
    assert (
        main(
            [
                "--workspace",
                str(rebranded_repo),
                "--assets",
                str(BRAND_ASSETS_DIR),
                "--image",
                str(tmp_path / "nope.bin"),
            ]
        )
        == EXIT_FAIL
    )
