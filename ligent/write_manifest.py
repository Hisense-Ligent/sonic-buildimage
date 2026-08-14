#!/usr/bin/env python3
"""版本清单写出（任务 10.2；design.md 5.4 节，需求 8.3）。

```
用法: python3 ligent/write_manifest.py --dest DIR --image PATH [选项]

  --dest DIR            归档目录（manifest.json 写在这里）
  --image PATH          镜像文件；缺省时用 --image-file / --image-sha256 手工给值
  --build-id ID         构建标识，默认由 --branch/--commit-sha/--started-at 推导
  --commit-sha SHA      完整提交 SHA，默认 $GITHUB_SHA
  --branch NAME         分支名，默认 $GITHUB_REF_NAME
  --platform P          目标平台，默认 $PLATFORM 或 vs
  --trigger EVENT       触发事件，默认 $GITHUB_EVENT_NAME 或 manual
  --started-at TS       构建开始时间，默认 $BUILD_STARTED_AT 或当前 UTC
  --finished-at TS      构建结束时间，默认当前 UTC
  --brand-verify FILE   verify_brand.py --json 的输出（`-` 表示 stdin）
  --brand-verify-text F verify_brand.py 的文本输出（brand-verify.txt）
  --build-jobs N        SONIC_BUILD_JOBS，默认 $SONIC_BUILD_JOBS
  --make-jobs N         SONIC_CONFIG_MAKE_JOBS，默认 $SONIC_CONFIG_MAKE_JOBS
  --runner NAME         runner 名称，默认 $RUNNER_NAME
  --print               同时把 manifest 打到 stdout
  --stdout              只打到 stdout，不落盘（供校验/预览）

退出码: 0 = 写出成功；1 = 输入非法或写入失败
```

## 为什么 manifest 值得一个独立模块

归档目录名（``build_id``）只放了「时间 + 分支 + 短 SHA」三样，且分支名经过清洗、
SHA 被截断——它是给人看的索引，不是权威记录。``manifest.json`` 承担的是**可机读、
可往返的完整事实**：完整 40 位 ``commit_sha``（目录名里的 9 位查不回 tag 或
signature）、原始未清洗的 ``branch``、镜像 SHA256 与字节数、以及当次构建实际用的
并行度参数。

并行度（``build_jobs`` / ``make_jobs``）写进 manifest 是有具体用途的：任务 15.1 要
根据首次全量构建的实测值回调这两个仓库变量，而「这次构建用的是哪组参数」在事后
只能从这里查——GitHub Actions 的日志保留期有限，归档在磁盘上的 manifest 不会过期。

## 校验和的算法

``image_sha256`` 用分块读（1 MiB）而不是 ``read()``：镜像约 2 GiB，一次性读进内存
在 160 GiB 内存的机器上不会 OOM，但会让 ``write_manifest`` 的驻留内存莫名多出
2 GiB，与同时可能在跑的 ``make`` 抢内存。分块读的代价只是几行代码。

允许 ``--image-sha256`` 手工传入，是因为归档步骤往往已经为 step summary 算过一次
（需求 8.9），没必要为同一个 2 GiB 文件再读一遍盘。传入时**不做重算校验**——
这是刻意的：重算就抵消了省掉一次 I/O 的全部意义。

## brand_verify 的来源

直接消费 :mod:`verify_brand` 的 ``--json`` 输出（``checks`` 数组里的 ``id`` 与
``status``），不另定一套结构。这样 BV 检查项增删时 manifest 自动跟着变，不会出现
「verify 加了 BV-06 而 manifest 还是 4 项」的静默不一致。

也支持解析文本输出（``brand-verify.txt``），因为归档步骤本来就要把这个文件复制进
归档目录（需求 4.3 的输出重定向）。文本格式 ``[ OK ] BV-02 name : detail`` 是
``preflight.sh`` 与 ``verify_brand.py`` 共用的统一格式，解析它比要求流水线多产出
一份 JSON 更省事。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

try:  # 直接跑脚本 / 测试把 ligent/ 注入 sys.path 时
    from brandconfig import BrandAssetsError, load_brand_assets
    from build_identity import (
        BuildIdentityError,
        iso_utc,
        make_build_id,
        parse_timestamp,
        short_sha,
        utcnow,
    )
    from fileops import write_text_atomic
except ImportError:  # pragma: no cover - 以 `python3 -m ligent.write_manifest` 导入时
    from .brandconfig import BrandAssetsError, load_brand_assets  # type: ignore
    from .build_identity import (  # type: ignore
        BuildIdentityError,
        iso_utc,
        make_build_id,
        parse_timestamp,
        short_sha,
        utcnow,
    )
    from .fileops import write_text_atomic  # type: ignore

#: 清单文件名（design.md 5.5 节的归档布局）
MANIFEST_NAME = "manifest.json"

#: 需求 8.3 要求的全部键。顺序即写入顺序——manifest 是给人 `cat` 看的，
#: 按「标识 → 来源 → 时间 → 产物 → 品牌 → 构建参数」分组比字母序好读。
REQUIRED_KEYS: tuple[str, ...] = (
    "build_id",
    "commit_sha",
    "commit_sha_short",
    "branch",
    "platform",
    "trigger",
    "started_at",
    "finished_at",
    "image_file",
    "image_sha256",
    "image_size_bytes",
    "brand_name",
    "brand_verify",
    "build_jobs",
    "make_jobs",
    "runner",
)

#: 分块读的块大小（1 MiB）
CHUNK_SIZE = 1 << 20

#: manifest.json 的权限位。必须显式设置：``fileops.write_text_atomic`` 以 0600 建
#: 临时文件，只在**目标已存在**时才把原权限复制过去，而 manifest 每次都是新建，
#: 于是落地就是 0600。归档目录的读者不止一个——runner 以 ``ligent-ci`` 运行、
#: 人工排障用 ``user``（design.md 7.1 节给 Artifact_Store 设了 setgid 2775 正是
#: 为此），0600 会让另一个账户连 `cat manifest.json` 都做不到。归档内容不是机密，
#: 与同目录 `.bin`/`build.log` 的 0644 保持一致。
MANIFEST_MODE = 0o644

#: verify_brand.py / preflight.sh 的统一文本行格式：``[ OK ] BV-02 name : detail``
VERIFY_LINE_RE = re.compile(
    r"^\[\s*(?P<status>[A-Z]+)\s*\]\s+(?P<id>[A-Z]{2}-\d{2}(?:\.\d+)?)\b"
)

EXIT_OK = 0
EXIT_FAIL = 1


class ManifestError(Exception):
    """清单输入非法或写入失败。"""

    exit_code = EXIT_FAIL


# ---------------------------------------------------------------------------
# 镜像信息
# ---------------------------------------------------------------------------


def sha256_of_file(path: Path, chunk_size: int = CHUNK_SIZE) -> str:
    """分块计算文件 sha256（十六进制小写）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# brand_verify
# ---------------------------------------------------------------------------


def normalize_brand_verify(checks: list[dict]) -> list[dict[str, str]]:
    """把 ``verify_brand.py`` 的 ``checks`` 压成 ``[{"id","status"}, ...]``。

    只留 ``id`` 与 ``status``（design.md 5.4 节的样例形态）：``detail`` 与
    ``evidence`` 是给人读的诊断文本，完整内容已经在同一归档目录的
    ``brand-verify.txt`` 里，抄进 manifest 只会让它变成日志文件。
    """
    result: list[dict[str, str]] = []
    for item in checks:
        check_id = item.get("id") or item.get("check_id")
        if not check_id:
            continue
        result.append({"id": str(check_id), "status": str(item.get("status", ""))})
    return result


def load_brand_verify_json(source: str) -> list[dict[str, str]]:
    """读 ``verify_brand.py --json`` 的输出；``-`` 表示 stdin。"""
    raw = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ManifestError(
            f"{source} 不是合法 JSON：{exc}\n"
            f"  处置：确认它是 `verify_brand.py --json` 的输出"
        ) from exc
    if isinstance(payload, list):  # 已经是 checks 数组
        return normalize_brand_verify(payload)
    checks = payload.get("checks")
    if not isinstance(checks, list):
        raise ManifestError(f"{source} 里没有 checks 数组（期望 verify_brand --json 输出）")
    return normalize_brand_verify(checks)


def parse_brand_verify_text(text: str) -> list[dict[str, str]]:
    """从 ``brand-verify.txt`` 的统一格式行里提取检查项。

    只认 ``[ OK ] BV-02 ...`` 这类行首形态，因此汇总行、``↳`` 取证行、以及
    「注意：工作区模式……」这类说明文字都会被自然跳过，不需要额外过滤规则。
    """
    return [
        {"id": match.group("id"), "status": match.group("status")}
        for match in (VERIFY_LINE_RE.match(line) for line in text.splitlines())
        if match
    ]


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------


def _as_int(value: object, field: str) -> int | None:
    """把并行度这类可空整数字段转成 int；空值返回 ``None``。"""
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        raise ManifestError(f"{field} 期望整数，收到 {value!r}") from None


def build_manifest(
    *,
    build_id: str,
    commit_sha: str,
    branch: str,
    platform: str,
    trigger: str,
    started_at: str,
    finished_at: str,
    image_file: str,
    image_sha256: str,
    image_size_bytes: int,
    brand_name: str,
    brand_verify: list[dict[str, str]],
    build_jobs: int | None,
    make_jobs: int | None,
    runner: str,
) -> dict[str, object]:
    """组装 manifest 字典（纯函数，不碰文件系统）。

    ``branch`` 存的是**原始**分支名而不是 ``build_id`` 里那个清洗后的版本：
    ``feature/foo`` 在目录名里是 ``feature_foo``，从目录名反推不回斜杠。需求 8.3
    要求清单包含「分支名称」，那就应该是能拿去 ``git checkout`` 的那一个。
    """
    return {
        "build_id": build_id,
        "commit_sha": commit_sha,
        "commit_sha_short": short_sha(commit_sha) if commit_sha else "",
        "branch": branch,
        "platform": platform,
        "trigger": trigger,
        "started_at": started_at,
        "finished_at": finished_at,
        "image_file": image_file,
        "image_sha256": image_sha256,
        "image_size_bytes": image_size_bytes,
        "brand_name": brand_name,
        "brand_verify": brand_verify,
        "build_jobs": build_jobs,
        "make_jobs": make_jobs,
        "runner": runner,
    }


def missing_keys(manifest: dict[str, object]) -> list[str]:
    """返回缺失的必需键（需求 8.3 的完整性判据）。"""
    return [key for key in REQUIRED_KEYS if key not in manifest]


def render_manifest(manifest: dict[str, object]) -> str:
    """序列化为带缩进、以换行结尾的 UTF-8 JSON 文本。

    ``ensure_ascii=False``：``brand_name`` 白名单虽然只允许 ASCII，但 ``branch``
    与 runner 名不受此限制，转义成 ``\\uXXXX`` 会让人工 ``cat`` 时读不出来。
    末尾换行是为了 ``tail``/``cat`` 不把下一个提示符黏在 ``}`` 后面。
    """
    return json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def write_manifest(dest: Path, manifest: dict[str, object]) -> Path:
    """把 manifest 原子写入 ``dest/manifest.json``。

    走 :func:`fileops.write_text_atomic`：归档步骤可能被 ``cancel-in-progress``
    中途 kill，半写入的 ``manifest.json`` 会让这份归档看起来完整而实际不可解析
    （比根本没有 manifest 更糟——前者需要人去发现，后者一眼可见）。
    """
    absent = missing_keys(manifest)
    if absent:
        raise ManifestError(f"manifest 缺少必需键：{', '.join(absent)}")
    if not dest.is_dir():
        raise ManifestError(
            f"归档目录不存在：{dest}\n  处置：先 mkdir -p，或检查 --dest 取值"
        )
    path = write_text_atomic(dest / MANIFEST_NAME, render_manifest(manifest))
    # umask 之后再收紧一次：022 下 0644 & ~umask == 0644，与同目录其他归档文件一致。
    os.chmod(path, MANIFEST_MODE & ~_umask())
    return path


def _umask() -> int:
    """当前进程的 umask（读了要立刻恢复——umask 没有只读接口）。"""
    current = os.umask(0o022)
    os.umask(current)
    return current


def load_manifest(path: Path) -> dict[str, object]:
    """读回 manifest（供归档后自检与属性测试的往返比对）。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{path} 不是合法 JSON：{exc}") from exc


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="write_manifest.py",
        description="写出归档目录里的 manifest.json（需求 8.3）",
    )
    parser.add_argument("--dest", type=Path, default=None, help="归档目录")
    parser.add_argument("--image", type=Path, default=None, help="镜像文件路径")
    parser.add_argument("--image-file", default=None, help="镜像文件名，默认取 --image 的名字")
    parser.add_argument("--image-sha256", default=None, help="镜像 SHA256（给了就不重算）")
    parser.add_argument("--image-size-bytes", type=int, default=None, help="镜像字节数")
    parser.add_argument("--build-id", default=None, help="构建标识，默认自动推导")
    parser.add_argument(
        "--commit-sha", default=_env("COMMIT_SHA", "GITHUB_SHA"), help="完整提交 SHA"
    )
    parser.add_argument(
        "--branch", default=_env("BRANCH", "GITHUB_REF_NAME"), help="分支名"
    )
    parser.add_argument(
        "--platform", default=_env("PLATFORM", default="vs"), help="目标平台"
    )
    parser.add_argument(
        "--trigger",
        default=_env("GITHUB_EVENT_NAME", default="manual"),
        help="触发事件",
    )
    parser.add_argument(
        "--started-at", default=_env("BUILD_STARTED_AT"), help="构建开始时间"
    )
    parser.add_argument("--finished-at", default=_env("BUILD_FINISHED_AT"), help="构建结束时间")
    parser.add_argument("--brand-name", default=None, help="品牌名，默认取 Brand_Assets")
    parser.add_argument("--assets", type=Path, default=None, help="Brand_Assets 目录")
    parser.add_argument(
        "--brand-verify", default=None, help="verify_brand.py --json 输出（`-` 为 stdin）"
    )
    parser.add_argument(
        "--brand-verify-text", default=None, help="verify_brand.py 文本输出（brand-verify.txt）"
    )
    parser.add_argument(
        "--build-jobs", default=_env("SONIC_BUILD_JOBS", "LIGENT_BUILD_JOBS"), help="构建并行度"
    )
    parser.add_argument(
        "--make-jobs",
        default=_env("SONIC_CONFIG_MAKE_JOBS", "LIGENT_MAKE_JOBS"),
        help="make 并行度",
    )
    parser.add_argument("--runner", default=_env("RUNNER_NAME"), help="runner 名称")
    parser.add_argument("--print", action="store_true", help="同时打到 stdout")
    parser.add_argument("--stdout", action="store_true", help="只打到 stdout，不落盘")
    return parser


def _resolve_brand_verify(args: argparse.Namespace) -> list[dict[str, str]]:
    if args.brand_verify:
        return load_brand_verify_json(args.brand_verify)
    if args.brand_verify_text:
        path = Path(args.brand_verify_text)
        if not path.is_file():
            raise ManifestError(f"品牌校验文本不存在：{path}")
        return parse_brand_verify_text(path.read_text(encoding="utf-8", errors="replace"))
    return []


def _resolve_image(args: argparse.Namespace) -> tuple[str, str, int]:
    """返回 ``(image_file, image_sha256, image_size_bytes)``。"""
    if args.image is not None:
        image = args.image
        if not image.is_file():
            raise ManifestError(
                f"镜像不存在：{image}\n"
                f"  处置：确认 --image 指向已产出的 .bin；只想预览 manifest 时"
                f"请用 --image-file/--image-sha256/--image-size-bytes 手工给值"
            )
        return (
            args.image_file or image.name,
            args.image_sha256 or sha256_of_file(image),
            args.image_size_bytes if args.image_size_bytes is not None else image.stat().st_size,
        )
    if not args.image_file:
        raise ManifestError("需要 --image，或用 --image-file 手工给出镜像文件名")
    return (
        args.image_file,
        args.image_sha256 or "",
        args.image_size_bytes if args.image_size_bytes is not None else 0,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        now = utcnow()
        started_at = iso_utc(parse_timestamp(args.started_at)) if args.started_at else iso_utc(now)
        finished_at = (
            iso_utc(parse_timestamp(args.finished_at)) if args.finished_at else iso_utc(now)
        )
        build_id = args.build_id or make_build_id(
            args.branch, args.commit_sha, parse_timestamp(started_at)
        )
        brand_name = args.brand_name or load_brand_assets(args.assets).config.brand_name
        image_file, image_sha256, image_size = _resolve_image(args)

        manifest = build_manifest(
            build_id=build_id,
            commit_sha=args.commit_sha,
            branch=args.branch,
            platform=args.platform,
            trigger=args.trigger,
            started_at=started_at,
            finished_at=finished_at,
            image_file=image_file,
            image_sha256=image_sha256,
            image_size_bytes=image_size,
            brand_name=brand_name,
            brand_verify=_resolve_brand_verify(args),
            build_jobs=_as_int(args.build_jobs, "--build-jobs"),
            make_jobs=_as_int(args.make_jobs, "--make-jobs"),
            runner=args.runner,
        )

        if args.stdout:
            sys.stdout.write(render_manifest(manifest))
            return EXIT_OK
        if args.dest is None:
            raise ManifestError("需要 --dest 指定归档目录（或用 --stdout 只预览）")
        path = write_manifest(args.dest.expanduser(), manifest)
    except (ManifestError, BuildIdentityError, BrandAssetsError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_FAIL
    except OSError as exc:
        print(f"错误：写入 manifest 失败：{exc}", file=sys.stderr)
        return EXIT_FAIL

    if args.print:
        sys.stdout.write(render_manifest(manifest))
    print(f"[ OK ] AR-02 manifest           : {path}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
