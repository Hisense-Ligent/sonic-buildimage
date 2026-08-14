"""构建标识、Manifest、归档与保留策略的示例级单元测试（任务 10.1–10.4）。

对任意输入的全覆盖由任务 10.5/10.6/10.7 的 Property 15/17/18 负责（本期跳过），
这里钉住边界与最容易写错的几处：

* **build_id 的字典序 == 时间序**：这是整个保留策略的前提，一旦格式被改成
  ``%Y-%m-%d`` 之类，排序仍然「看起来对」但跨月/跨年会错位。
* **保留策略按名字排序而不是 mtime**：用 ``os.utime`` 把 mtime 顺序刻意反转，
  再断言选择结果不变。这是 design.md 6.5 节点名的实现缺陷，也是本文件里最有
  价值的一个用例——按 mtime 实现的版本能通过其余所有测试，只会在这里翻车。
* **latest 链接始终指向存在的目录**：悬空链接是「看起来正常」的失败形态。
* **归档复制失败保留现场**：需求 8.8 的正面断言。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import BRAND_ASSETS_DIR, LIGENT_DIR

sys.path.insert(0, str(LIGENT_DIR))

from build_identity import (  # noqa: E402
    FALLBACK_BRANCH,
    MAX_BRANCH_LENGTH,
    SHORT_SHA_LENGTH,
    TIMESTAMP_FORMAT,
    BuildIdentityError,
    format_timestamp,
    is_archive_dir_name,
    is_build_id,
    iso_utc,
    make_build_id,
    make_identity,
    parse_build_id,
    parse_timestamp,
    sanitize_branch,
    short_sha,
)
from retention import (  # noqa: E402
    DEFAULT_RETENTION_COUNT,
    LATEST_LINK,
    RetentionError,
    apply_retention,
    list_archives,
    newest,
    read_latest,
    select_for_deletion,
    select_to_keep,
    update_latest,
)
from write_manifest import (  # noqa: E402
    MANIFEST_NAME,
    REQUIRED_KEYS,
    ManifestError,
    build_manifest,
    load_manifest,
    missing_keys,
    parse_brand_verify_text,
    render_manifest,
    sha256_of_file,
    write_manifest,
)

ARCHIVE_SH = LIGENT_DIR / "archive.sh"

FULL_SHA = "db6796e994099bc822fde5806052f378b7df0a1b"
SHORT = "db6796e99"
MOMENT = datetime(2026, 8, 14, 3, 15, 0, tzinfo=timezone.utc)


# ===========================================================================
# 任务 10.1 build_identity
# ===========================================================================


def test_build_id_matches_design_example() -> None:
    """design.md 5.4 节给的样例必须逐字符复现。"""
    assert (
        make_build_id("ligent_brand", FULL_SHA, MOMENT)
        == "20260814T031500Z-ligent_brand-db6796e99"
    )


def test_timestamp_format_is_utc_compact() -> None:
    assert format_timestamp(MOMENT) == "20260814T031500Z"
    assert iso_utc(MOMENT) == "2026-08-14T03:15:00Z"


def test_naive_timestamp_is_treated_as_utc() -> None:
    """naive 时间视为 UTC，而不是服务器本地时区（服务器是 CST，差 8 小时）。"""
    naive = datetime(2026, 8, 14, 3, 15, 0)
    assert format_timestamp(naive) == "20260814T031500Z"


def test_non_utc_timestamp_is_converted() -> None:
    beijing = datetime(2026, 8, 14, 11, 15, 0, tzinfo=timezone(timedelta(hours=8)))
    assert format_timestamp(beijing) == "20260814T031500Z"


def test_lexicographic_order_equals_chronological_order() -> None:
    """字典序 == 时间序：保留策略能直接 sort 目录名的全部依据。

    刻意覆盖跨月（8/31 → 9/1）与跨年（12/31 → 1/1）——补零格式在这两处最容易
    出问题，而一旦出问题，`sorted()` 会把 12 月排在 2 月之前。
    """
    moments = [
        datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 31, 23, 59, 59, tzinfo=timezone.utc),
        datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
        datetime(2027, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
    ]
    ids = [make_build_id("ligent_brand", FULL_SHA, m) for m in moments]
    assert ids == sorted(ids)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ligent_brand", "ligent_brand"),
        ("feature/foo", "feature_foo"),
        ("feature//foo", "feature_foo"),  # 折叠必须发生在替换之后
        ("a b c", "a_b_c"),
        ("release-1.2.3", "release-1.2.3"),  # 白名单内字符逐字保留
        ("..", FALLBACK_BRANCH),
        (".", FALLBACK_BRANCH),
        ("", FALLBACK_BRANCH),
        ("/", FALLBACK_BRANCH),
        ("...hotfix...", "hotfix"),
        ("分支名", FALLBACK_BRANCH),  # 全非 ASCII → 全部变 _ → 折叠 → 剥离 → 兜底
        ("br$(whoami)", "br_whoami"),  # 尾部 _ 一并剥离，没有信息量
        ("a;rm -rf /", "a_rm_-rf"),
    ],
)
def test_sanitize_branch(raw: str, expected: str) -> None:
    assert sanitize_branch(raw) == expected


@pytest.mark.parametrize(
    "raw", ["..", ".", "", "/", "feature/foo", "a b", "分支名", "x/../y", "a\nb"]
)
def test_sanitized_branch_is_filesystem_safe(raw: str) -> None:
    """清洗结果必须是「单个安全路径分量」——这是全函数，任意输入都成立。"""
    cleaned = sanitize_branch(raw)
    assert cleaned
    assert cleaned not in {".", ".."}
    assert "/" not in cleaned and "\\" not in cleaned
    assert not cleaned.startswith(".") and not cleaned.endswith(".")
    assert len(cleaned) <= MAX_BRANCH_LENGTH
    # 结果必须是它自己的稳定点，否则同一分支在不同调用路径下算出两个目录名
    assert sanitize_branch(cleaned) == cleaned


def test_long_branch_is_truncated_without_trailing_dot() -> None:
    raw = "x" * 70 + "."
    cleaned = sanitize_branch(raw)
    assert len(cleaned) == MAX_BRANCH_LENGTH
    assert not cleaned.endswith(".")


def test_short_sha_takes_nine_lowercase_hex() -> None:
    assert short_sha(FULL_SHA) == SHORT
    assert len(short_sha(FULL_SHA)) == SHORT_SHA_LENGTH
    assert short_sha(FULL_SHA.upper()) == SHORT


@pytest.mark.parametrize("bad", ["", "   ", "zzzz", "not-a-sha", "db6796"])
def test_short_sha_rejects_unusable_input(bad: str) -> None:
    """SHA 是「构建自哪次提交」的唯一凭据，取不到必须报错而不是兜底。"""
    with pytest.raises(BuildIdentityError):
        short_sha(bad)


def test_build_id_round_trip() -> None:
    identity = make_identity("ligent_brand", FULL_SHA, MOMENT)
    parsed = parse_build_id(identity.build_id)
    assert parsed == identity


def test_build_id_round_trip_with_hyphenated_branch() -> None:
    identity = make_identity("release-1.2", FULL_SHA, MOMENT)
    assert parse_build_id(identity.build_id).branch == "release-1.2"


def test_distinct_inputs_give_distinct_build_ids() -> None:
    base = make_build_id("ligent_brand", FULL_SHA, MOMENT)
    assert base != make_build_id("other_branch", FULL_SHA, MOMENT)
    assert base != make_build_id("ligent_brand", "a" * 40, MOMENT)
    assert base != make_build_id(
        "ligent_brand", FULL_SHA, MOMENT + timedelta(seconds=1)
    )


@pytest.mark.parametrize(
    "name",
    [
        "20260814T031500Z-ligent_brand-db6796e99",
        "20260101T000000Z-a-000000000",
    ],
)
def test_is_build_id_accepts_well_formed(name: str) -> None:
    assert is_build_id(name)
    assert is_archive_dir_name(name)


@pytest.mark.parametrize(
    "name",
    ["latest", "", "tmp", "2026-08-14T03:15:00Z-b-abc", "20260814T031500Z"],
)
def test_is_build_id_rejects_others(name: str) -> None:
    assert not is_build_id(name)


def test_parse_timestamp_accepts_both_shapes() -> None:
    assert parse_timestamp("20260814T031500Z") == MOMENT
    assert parse_timestamp("2026-08-14T03:15:00Z") == MOMENT


def test_parse_timestamp_rejects_garbage() -> None:
    with pytest.raises(BuildIdentityError):
        parse_timestamp("yesterday")


def test_build_identity_cli(tmp_path: Path) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(LIGENT_DIR / "build_identity.py"),
            "--branch",
            "feature/foo",
            "--sha",
            FULL_SHA,
            "--timestamp",
            "20260814T031500Z",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "20260814T031500Z-feature_foo-db6796e99"


def test_build_identity_cli_rejects_bad_sha() -> None:
    proc = subprocess.run(
        [sys.executable, str(LIGENT_DIR / "build_identity.py"), "--branch", "b", "--sha", ""],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "sha" in proc.stderr.lower()


# ===========================================================================
# 任务 10.2 write_manifest
# ===========================================================================


def _manifest_kwargs(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = dict(
        build_id="20260814T031500Z-ligent_brand-db6796e99",
        commit_sha=FULL_SHA,
        branch="ligent_brand",
        platform="vs",
        trigger="push",
        started_at="2026-08-14T03:15:00Z",
        finished_at="2026-08-14T05:41:12Z",
        image_file="sonic-vs.bin",
        image_sha256="0" * 64,
        image_size_bytes=2147483648,
        brand_name="Ligent",
        brand_verify=[{"id": "BV-02", "status": "OK"}],
        build_jobs=16,
        make_jobs=8,
        runner="hisense",
    )
    payload.update(overrides)
    return payload


def test_manifest_has_all_required_keys() -> None:
    manifest = build_manifest(**_manifest_kwargs())  # type: ignore[arg-type]
    assert missing_keys(manifest) == []
    assert set(manifest) == set(REQUIRED_KEYS)


def test_manifest_derives_short_sha() -> None:
    manifest = build_manifest(**_manifest_kwargs())  # type: ignore[arg-type]
    assert manifest["commit_sha_short"] == SHORT
    assert manifest["build_id"].endswith(SHORT)  # type: ignore[union-attr]


def test_manifest_keeps_unsanitized_branch() -> None:
    """manifest 的 branch 必须能拿去 git checkout，不是目录名里那个清洗版。"""
    manifest = build_manifest(
        **_manifest_kwargs(
            branch="feature/foo",
            build_id=make_build_id("feature/foo", FULL_SHA, MOMENT),
        )  # type: ignore[arg-type]
    )
    assert manifest["branch"] == "feature/foo"
    assert "feature_foo" in str(manifest["build_id"])


def test_manifest_round_trip(tmp_path: Path) -> None:
    """写出后读回，各键值等于输入（Property 15 的示例级版本）。"""
    manifest = build_manifest(**_manifest_kwargs())  # type: ignore[arg-type]
    path = write_manifest(tmp_path, manifest)
    assert path.name == MANIFEST_NAME
    assert load_manifest(path) == manifest


def test_manifest_is_group_and_world_readable(tmp_path: Path) -> None:
    """manifest 不能落地成 0600。

    write_text_atomic 以 0600 建临时文件、只在目标已存在时复制原权限，而 manifest
    每次都是新建。0600 会让 runner（ligent-ci）与人工排障账户（user）中的一方
    读不到自己的归档——design.md 7.1 节给 Artifact_Store 设 setgid 2775 就是为了
    两个账户都能访问。
    """
    manifest = build_manifest(**_manifest_kwargs())  # type: ignore[arg-type]
    path = write_manifest(tmp_path, manifest)
    mode = path.stat().st_mode & 0o777
    assert mode & 0o044, f"manifest 权限 {oct(mode)} 不可被同组/其他用户读取"


def test_manifest_is_valid_json_with_trailing_newline() -> None:
    text = render_manifest(build_manifest(**_manifest_kwargs()))  # type: ignore[arg-type]
    assert text.endswith("}\n")
    assert json.loads(text)["platform"] == "vs"


def test_write_manifest_rejects_incomplete_payload(tmp_path: Path) -> None:
    manifest = build_manifest(**_manifest_kwargs())  # type: ignore[arg-type]
    del manifest["image_sha256"]
    with pytest.raises(ManifestError):
        write_manifest(tmp_path, manifest)


def test_write_manifest_rejects_missing_dest(tmp_path: Path) -> None:
    with pytest.raises(ManifestError):
        write_manifest(tmp_path / "nope", build_manifest(**_manifest_kwargs()))  # type: ignore[arg-type]


def test_parse_brand_verify_text_extracts_ids() -> None:
    """消费 verify_brand.py 的统一格式输出，跳过取证行与汇总行。"""
    text = (
        "[ OK ] BV-02 grub_display            : ok\n"
        "[FAIL] BV-03 volume_label_intact     : missing\n"
        "       ↳ 处置：恢复该行\n"
        "[ OK ] BV-05 workspace_motd          : ok\n"
        "\nBrand_Verifier（image 模式）：2/3 项通过\n"
    )
    assert parse_brand_verify_text(text) == [
        {"id": "BV-02", "status": "OK"},
        {"id": "BV-03", "status": "FAIL"},
        {"id": "BV-05", "status": "OK"},
    ]


def test_brand_verify_matches_verify_brand_json_shape(tmp_repo: Path) -> None:
    """brand_verify 字段直接由 verify_brand.py --json 转换而来，不另定结构。"""
    from conftest import rebrand_workspace
    from write_manifest import normalize_brand_verify

    rebrand_workspace(tmp_repo)
    proc = subprocess.run(
        [
            sys.executable,
            str(LIGENT_DIR / "verify_brand.py"),
            "--workspace",
            str(tmp_repo),
            "--assets",
            str(BRAND_ASSETS_DIR),
            "--json",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    entries = normalize_brand_verify(payload["checks"])
    assert entries and all(set(e) == {"id", "status"} for e in entries)
    assert [e["id"] for e in entries] == [c["id"] for c in payload["checks"]]


def test_sha256_of_file(tmp_path: Path) -> None:
    from conftest import sha256_of

    target = tmp_path / "blob.bin"
    target.write_bytes(os.urandom(3 << 20))  # 跨多个 1 MiB 块，覆盖分块读逻辑
    assert sha256_of_file(target) == sha256_of(target)


def test_write_manifest_cli_stdout() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(LIGENT_DIR / "write_manifest.py"),
            "--stdout",
            "--branch",
            "ligent_brand",
            "--commit-sha",
            FULL_SHA,
            "--image-file",
            "sonic-vs.bin",
            "--image-sha256",
            "0" * 64,
            "--image-size-bytes",
            "2147483648",
            "--started-at",
            "20260814T031500Z",
            "--finished-at",
            "20260814T054112Z",
            "--build-jobs",
            "16",
            "--make-jobs",
            "8",
            "--runner",
            "hisense",
            "--platform",
            "vs",
            "--trigger",
            "push",
            "--assets",
            str(BRAND_ASSETS_DIR),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    manifest = json.loads(proc.stdout)
    assert missing_keys(manifest) == []
    assert manifest["build_id"] == "20260814T031500Z-ligent_brand-db6796e99"
    assert manifest["started_at"] == "2026-08-14T03:15:00Z"
    assert manifest["finished_at"] == "2026-08-14T05:41:12Z"
    assert manifest["brand_name"] == "Ligent"


# ===========================================================================
# 任务 10.4 retention —— 纯函数
# ===========================================================================

IDS = [
    "20260810T010000Z-ligent_brand-aaaaaaaaa",
    "20260811T010000Z-ligent_brand-bbbbbbbbb",
    "20260812T010000Z-ligent_brand-ccccccccc",
    "20260813T010000Z-ligent_brand-ddddddddd",
    "20260814T010000Z-ligent_brand-eeeeeeeee",
]


def test_default_retention_count_is_three() -> None:
    """design.md 6.5 节把需求 8.7 的 10 改为 3，实现不得改回去。"""
    assert DEFAULT_RETENTION_COUNT == 3


def test_select_for_deletion_keeps_newest_n() -> None:
    assert select_for_deletion(IDS, 3) == IDS[:2]
    assert select_to_keep(IDS, 3) == IDS[2:]


def test_select_for_deletion_is_order_independent() -> None:
    """输入顺序不影响结果——归档目录从 iterdir() 出来时顺序是任意的。"""
    shuffled = [IDS[3], IDS[0], IDS[4], IDS[2], IDS[1]]
    assert select_for_deletion(shuffled, 3) == IDS[:2]


@pytest.mark.parametrize("count", [1, 2, 3, 5, 6, 100])
def test_retention_invariant(count: int) -> None:
    """清理后份数 <= N，且保留的恰是最新 N 个（Property 18 的示例级版本）。"""
    kept = select_to_keep(IDS, count)
    assert len(kept) <= count
    assert kept == sorted(IDS)[max(0, len(IDS) - count) :]
    assert set(kept) | set(select_for_deletion(IDS, count)) == set(IDS)


def test_select_for_deletion_no_op_when_under_limit() -> None:
    assert select_for_deletion(IDS[:2], 3) == []
    assert select_for_deletion([], 3) == []


@pytest.mark.parametrize("bad", [0, -1])
def test_select_for_deletion_rejects_non_positive(bad: int) -> None:
    """N=0 大概率是配置笔误，清空 Artifact_Store 不可逆，必须报错。"""
    with pytest.raises(RetentionError):
        select_for_deletion(IDS, bad)


def test_newest_is_lexicographic_max() -> None:
    assert newest(IDS) == IDS[-1]
    assert newest([]) is None


# ===========================================================================
# 任务 10.4 retention —— 文件系统侧
# ===========================================================================


def _make_store(root: Path, names: list[str]) -> Path:
    """造一个假的 Artifact_Store：每个 build_id 目录里放小文件冒充产物。"""
    store = root / "artifacts"
    store.mkdir(exist_ok=True)
    for name in names:
        target = store / name
        target.mkdir()
        (target / "sonic-vs.bin").write_bytes(b"fake image " + name.encode())
        (target / "manifest.json").write_text(
            json.dumps({"build_id": name}), encoding="utf-8"
        )
    if names:
        update_latest(store, sorted(names)[-1])
    return store


def test_list_archives_ignores_latest_link_and_stray_files(tmp_path: Path) -> None:
    store = _make_store(tmp_path, IDS[:2])
    (store / "notes.txt").write_text("手工放的笔记", encoding="utf-8")
    (store / "scratch").mkdir()
    assert list_archives(store) == IDS[:2]


def test_apply_retention_deletes_oldest(tmp_path: Path) -> None:
    store = _make_store(tmp_path, IDS)
    result = apply_retention(store, 3)
    assert result.deleted == IDS[:2]
    assert list_archives(store) == IDS[2:]
    assert result.latest == IDS[-1]
    assert (store / LATEST_LINK).resolve().is_dir()


def test_apply_retention_ignores_mtime_order(tmp_path: Path) -> None:
    """**本文件最重要的用例**：mtime 顺序与 build_id 时间序完全相反时，
    清理仍必须按 build_id 排序。

    按 mtime 实现的版本能通过其余所有测试，只会在这里翻车——它会保留 mtime 最新
    的那三个，也就是 build_id 最旧的三个，恰好把该留的删掉。
    """
    store = _make_store(tmp_path, IDS)
    base = 1_700_000_000
    # 越新的 build_id 给越旧的 mtime
    for offset, name in enumerate(reversed(IDS)):
        stamp = base + offset * 86400
        os.utime(store / name, (stamp, stamp))

    mtime_order = sorted(IDS, key=lambda n: (store / n).stat().st_mtime)
    assert mtime_order == list(reversed(IDS)), "前置条件：mtime 顺序确实是反的"

    result = apply_retention(store, 3)
    assert result.deleted == IDS[:2]
    assert list_archives(store) == IDS[2:]
    assert read_latest(store) == IDS[-1]


def test_apply_retention_dry_run_changes_nothing(tmp_path: Path) -> None:
    store = _make_store(tmp_path, IDS)
    result = apply_retention(store, 3, dry_run=True)
    assert result.deleted == IDS[:2]
    assert list_archives(store) == IDS  # 一个都没真删


def test_apply_retention_no_op_under_limit(tmp_path: Path) -> None:
    store = _make_store(tmp_path, IDS[:2])
    result = apply_retention(store, 3)
    assert result.deleted == []
    assert list_archives(store) == IDS[:2]


def test_retention_repairs_dangling_latest(tmp_path: Path) -> None:
    """latest 指向已被删的目录时必须被修回存在的最新一份（需求 8.5）。"""
    store = _make_store(tmp_path, IDS)
    update_latest(store, IDS[0])  # 故意指向最旧的那个，它会被删掉
    result = apply_retention(store, 3)
    assert result.latest == IDS[-1]
    link = store / LATEST_LINK
    assert link.is_symlink() and link.resolve().is_dir()


def test_update_latest_is_relative(tmp_path: Path) -> None:
    """相对链接：整个 Artifact_Store 可以整体搬迁而不失效。"""
    store = _make_store(tmp_path, IDS[:1])
    assert os.readlink(store / LATEST_LINK) == IDS[0]


def test_retention_cli_default_count(tmp_path: Path) -> None:
    store = _make_store(tmp_path, IDS)
    proc = subprocess.run(
        [sys.executable, str(LIGENT_DIR / "retention.py"), "--store", str(store), "--json"],
        capture_output=True,
        text=True,
        env={**os.environ, "LIGENT_RETENTION_COUNT": ""},
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["count"] == DEFAULT_RETENTION_COUNT
    assert payload["deleted"] == IDS[:2]
    assert payload["latest"] == IDS[-1]


def test_retention_cli_reads_env_count(tmp_path: Path) -> None:
    store = _make_store(tmp_path, IDS)
    proc = subprocess.run(
        [sys.executable, str(LIGENT_DIR / "retention.py"), "--store", str(store), "--json"],
        capture_output=True,
        text=True,
        env={**os.environ, "LIGENT_RETENTION_COUNT": "2"},
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["kept"] == IDS[3:]


# ===========================================================================
# 任务 10.3 archive.sh
# ===========================================================================


def _run_archive(
    store: Path,
    image: Path,
    *,
    build_id: str = "20260814T031500Z-ligent_brand-db6796e99",
    extra: list[str] | None = None,
    env: dict[str, str] | None = None,
    summary: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    argv = [
        "bash",
        str(ARCHIVE_SH),
        "--store",
        str(store),
        "--image",
        str(image),
        "--build-id",
        build_id,
        *(extra or []),
    ]
    environ = {**os.environ, "GITHUB_SHA": FULL_SHA, "GITHUB_REF_NAME": "ligent_brand"}
    if summary is not None:
        environ["GITHUB_STEP_SUMMARY"] = str(summary)
    else:
        environ.pop("GITHUB_STEP_SUMMARY", None)
    environ.update(env or {})
    return subprocess.run(argv, capture_output=True, text=True, env=environ)


@pytest.fixture
def fake_build(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """一份假的构建产物：小文件冒充 .bin、build.log、brand-verify.txt。"""
    work = tmp_path / "work"
    work.mkdir()
    image = work / "sonic-vs.bin"
    image.write_bytes(b"fake sonic image payload\n" * 100)
    log = work / "build.log"
    log.write_text("make target/sonic-vs.bin\n...\n", encoding="utf-8")
    verify = work / "brand-verify.txt"
    verify.write_text(
        "[ OK ] BV-02 grub_display            : ok\n"
        "[ OK ] BV-03 volume_label_intact     : unchanged\n"
        "[ OK ] BV-04 menuentry_intact        : unchanged\n"
        "[ OK ] BV-05 workspace_motd          : ok\n",
        encoding="utf-8",
    )
    store = tmp_path / "artifacts"
    store.mkdir()
    return store, image, log, verify


def test_archive_sh_syntax() -> None:
    proc = subprocess.run(["bash", "-n", str(ARCHIVE_SH)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_archive_creates_expected_layout(
    fake_build: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    """归档目录同时含 .bin、manifest.json、build.log、brand-verify.txt（需求 8.1、8.3、8.4）。"""
    from conftest import sha256_of

    store, image, log, verify = fake_build
    summary = tmp_path / "summary.md"
    build_id = "20260814T031500Z-ligent_brand-db6796e99"
    proc = _run_archive(
        store,
        image,
        build_id=build_id,
        extra=["--log", str(log), "--verify", str(verify)],
        summary=summary,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    dest = store / build_id
    assert sorted(p.name for p in dest.iterdir()) == [
        "brand-verify.txt",
        "build.log",
        "manifest.json",
        "sonic-vs.bin",
    ]
    # 镜像逐字节相同（需求 8.1）
    assert sha256_of(dest / "sonic-vs.bin") == sha256_of(image)

    manifest = load_manifest(dest / MANIFEST_NAME)
    assert missing_keys(manifest) == []
    assert manifest["build_id"] == build_id
    assert manifest["image_sha256"] == sha256_of(image)
    assert manifest["image_size_bytes"] == image.stat().st_size
    # brand_verify 由归档进去的 brand-verify.txt 解析而来
    assert [c["id"] for c in manifest["brand_verify"]] == [  # type: ignore[union-attr]
        "BV-02",
        "BV-03",
        "BV-04",
        "BV-05",
    ]

    # latest 链接指向该目录且目标存在（需求 8.5）
    link = store / LATEST_LINK
    assert link.is_symlink()
    assert os.readlink(link) == build_id
    assert link.resolve() == dest.resolve()

    # 摘要含归档绝对路径与镜像 SHA256（需求 8.9）
    text = summary.read_text(encoding="utf-8")
    assert str(dest.resolve()) in text
    assert sha256_of(image) in text


def test_archive_summary_falls_back_to_stdout(
    fake_build: tuple[Path, Path, Path, Path]
) -> None:
    """不在 Actions 环境（无 GITHUB_STEP_SUMMARY）时摘要打到 stdout，便于 SSH 排障。"""
    from conftest import sha256_of

    store, image, log, verify = fake_build
    proc = _run_archive(store, image, extra=["--log", str(log), "--verify", str(verify)])
    assert proc.returncode == 0, proc.stderr
    assert sha256_of(image) in proc.stdout
    assert "AR-03 archive" in proc.stdout


def test_archive_tolerates_missing_log(fake_build: tuple[Path, Path, Path, Path]) -> None:
    """build.log 缺失只告警：构建可能在 tee 之前就失败了，让归档整体失败会掩盖原因。"""
    store, image, log, verify = fake_build
    log.unlink()
    proc = _run_archive(store, image, extra=["--log", str(log), "--verify", str(verify)])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "build.log" in proc.stderr


def test_archive_fails_when_image_missing(
    fake_build: tuple[Path, Path, Path, Path]
) -> None:
    store, image, _log, _verify = fake_build
    image.unlink()
    proc = _run_archive(store, image)
    assert proc.returncode == 1
    assert "镜像不存在" in proc.stderr


def test_archive_preserves_partial_files_on_copy_failure(
    fake_build: tuple[Path, Path, Path, Path]
) -> None:
    """复制失败时非零退出并保留现场（需求 8.8）。

    用只读的归档目录制造复制失败：mkdir 已成功、cp 会失败。断言目录仍在，
    且诊断里给出了「保留供排查」与 `ls`/`df` 取证。
    """
    store, image, log, verify = fake_build
    build_id = "20260814T031500Z-ligent_brand-db6796e99"
    dest = store / build_id
    dest.mkdir()
    dest.chmod(0o500)  # r-x：可进入、不可写
    try:
        proc = _run_archive(store, image, build_id=build_id)
        assert proc.returncode == 1
        assert "复制镜像失败" in proc.stderr
        assert "保留供排查" in proc.stderr
        assert dest.is_dir(), "现场必须保留（需求 8.8）"
    finally:
        dest.chmod(0o755)


def test_archive_then_retention_keeps_latest_valid(
    fake_build: tuple[Path, Path, Path, Path]
) -> None:
    """连续归档 4 次后清理到 3 份，latest 仍指向存在的最新目录（需求 8.6、8.7）。"""
    store, image, log, verify = fake_build
    ids = [
        f"2026081{day}T010000Z-ligent_brand-{c * 9}"
        for day, c in zip("1234", "abcd")
    ]
    for build_id in ids:
        proc = _run_archive(
            store,
            image,
            build_id=build_id,
            extra=["--log", str(log), "--verify", str(verify)],
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr

    assert read_latest(store) == ids[-1]
    result = apply_retention(store, 3)
    assert result.deleted == ids[:1]
    assert list_archives(store) == ids[1:]
    link = store / LATEST_LINK
    assert link.resolve().is_dir()
    assert read_latest(store) == ids[-1]
