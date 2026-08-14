#!/usr/bin/env python3
"""BuildIdentity —— 构建标识生成（任务 10.1；design.md 5.4 节，需求 8.2）。

```
用法: python3 ligent/build_identity.py [--branch NAME] [--sha SHA]
                                       [--timestamp TS] [--field F] [--json]

  --branch NAME   分支名，默认取 $GITHUB_REF_NAME
  --sha SHA       完整提交 SHA，默认取 $GITHUB_SHA
  --timestamp TS  构建时间，接受 20260814T031500Z 或 2026-08-14T03:15:00Z，默认当前 UTC
  --field F       只输出一个字段：build_id（默认）/ timestamp / branch / short_sha
  --json          输出全部字段的 JSON

退出码: 0 = 成功；1 = 输入非法（SHA 不是十六进制、时间戳格式不认识）
```

## 标识格式

```
<UTC 时间戳>-<清洗后分支名>-<短 SHA>
20260814T031500Z-ligent_brand-db6796e99
```

**时间戳用 ``%Y%m%dT%H%M%SZ`` 是整套归档设计的承重墙，不是随手选的格式。**
它保证「字典序 == 时间序」，于是 :mod:`retention` 的清理策略可以直接对目录名
``sort`` 取最新 N 个，不必 ``stat`` 每个目录（design.md 5.4 / 6.5 节）。按 mtime
排序看起来更直观，但 ``cp -a`` 的时间戳保留行为、手工 ``touch``、rsync 回填都会
改动 mtime，一旦发生就会**删掉较新的构建而留下较旧的**——而且是静默发生。
把「时间」编码进名字本身，让排序依据不可被事后篡改，是这里唯一可靠的做法。

固定宽度也是有意的：``%Y%m%d`` 而不是 ``%Y-%m-%d`` 少两个字符，且没有任何变长
段落，所以字典序不会被「12 月 vs 2 月」这类补零问题破坏（``T``/``Z`` 分隔符使
时间部分与后面的分支名之间不存在长度歧义）。

## 分支名清洗

``[^A-Za-z0-9._-]`` → ``_``，连续 ``_`` 折叠为一个，首尾 ``.`` 去除，截断到 64 字符。

这一串规则的目的不是美观，而是**把结果关进「文件系统安全的单个路径分量」这个
集合里**。逐条对应它排除的形态：

* ``/`` 与 ``\\`` → ``_``：否则 ``feature/foo`` 会让 ``$ARTIFACT_STORE/$BUILD_ID``
  变成两层目录，``latest`` 链接与清理逻辑全部错位。
* 空白、``$``、`` ` ``、``;``、``|``、引号 → ``_``：``build_id`` 会出现在 shell
  赋值与 ``rm -rf`` 的参数位上，这类字符是注入面。
* 首尾 ``.`` 去除 + 空结果兜底：把 ``.``、``..``、``...`` 这些「路径遍历/自指」
  形态和空名一并排除。``..`` 经清洗后为空，落到兜底值 ``unknown``。
* 截断 64 字符：Linux 单个路径分量上限 255 字节，加上时间戳与短 SHA 后仍有大量
  余量；限制主要是防止 CI 里偶发的超长分支名把目录名变得不可读。

**为什么分支名清洗到空时兜底而 SHA 非法时报错**：分支名是展示信息，丢失它只是
让归档目录难认；提交 SHA 是这份归档「构建自哪次提交」的唯一凭据，取不到就该让
归档步骤失败，而不是写一个查不回源码的目录名。所以前者是全函数（任意输入都有
输出），后者是严格校验。

## 短 SHA 取 9 位

与 ``origin`` 当前 ``db6796e99`` 的显示位数一致，也高于 git 对本仓库的默认缩写
长度。9 位十六进制 = 36 bit，在单仓库尺度上碰撞概率可忽略；真正的唯一性由
``manifest.json`` 里的完整 ``commit_sha`` 承担（需求 8.3），目录名只需可读且不撞。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

#: 归档目录名里的时间戳格式（UTC）。改动它会破坏「字典序 == 时间序」，
#: 进而破坏 retention.py 的排序前提——这是整个归档设计的承重墙。
TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"

#: manifest 里的时间字段格式（ISO 8601 UTC，需求 8.3 的 started_at / finished_at）
ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

#: 短 SHA 位数（design.md 5.4 节）
SHORT_SHA_LENGTH = 9

#: 清洗后分支名的长度上限
MAX_BRANCH_LENGTH = 64

#: 分支名清洗到空时的兜底值（``..``、``/``、空名都会落到这里）
FALLBACK_BRANCH = "unknown"

#: 分支名中不安全的字符：一律替换为下划线
UNSAFE_CHAR_RE = re.compile(r"[^A-Za-z0-9._-]")
_REPEATED_UNDERSCORE_RE = re.compile(r"_{2,}")
_NON_HEX_RE = re.compile(r"[^0-9a-f]")

#: 完整 build_id 的形态校验
BUILD_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[A-Za-z0-9._-]+-[0-9a-f]+$")

#: 归档目录名前缀（只认时间戳段）。retention.py 与 preflight.sh 的
#: ``find -regex '.*/[0-9]{8}T[0-9]{6}Z-.*'`` 用的是同一判据。
ARCHIVE_DIR_RE = re.compile(r"^\d{8}T\d{6}Z-")

EXIT_OK = 0
#: 输入非法（需求 8.2 的反面：拿不到可信标识就不该继续归档）
EXIT_FAIL = 1


class BuildIdentityError(Exception):
    """构建标识的输入非法。退出码 1，与 BrandAssetsError 的约定一致。"""

    exit_code = EXIT_FAIL


@dataclass(frozen=True)
class BuildIdentity:
    """一个构建标识的三个组成部分。"""

    timestamp: str
    branch: str
    short_sha: str

    @property
    def build_id(self) -> str:
        return f"{self.timestamp}-{self.branch}-{self.short_sha}"

    def as_dict(self) -> dict[str, str]:
        return {
            "build_id": self.build_id,
            "timestamp": self.timestamp,
            "branch": self.branch,
            "short_sha": self.short_sha,
        }

    def __str__(self) -> str:  # pragma: no cover - 便于日志
        return self.build_id


# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    """当前 UTC 时间（秒精度）。

    去掉微秒：``build_id`` 只到秒，如果这里带着微秒，``format_timestamp`` 与
    ``iso_utc`` 会在同一次构建里给出不一致的「秒」（截断 vs 四舍五入的错觉），
    排障时对不上时间轴。
    """
    return datetime.now(timezone.utc).replace(microsecond=0)


def format_timestamp(moment: datetime | None = None) -> str:
    """格式化为 ``20260814T031500Z``（UTC）。"""
    return _as_utc(moment or utcnow()).strftime(TIMESTAMP_FORMAT)


def iso_utc(moment: datetime | None = None) -> str:
    """格式化为 ``2026-08-14T03:15:00Z``（manifest 的时间字段，需求 8.3）。"""
    return _as_utc(moment or utcnow()).strftime(ISO_FORMAT)


def _as_utc(moment: datetime) -> datetime:
    """把 naive 时间视为 UTC，把带时区的时间换算到 UTC。

    naive 视为 UTC 而不是本地时区：本流水线里所有时间源都是 UTC（``date -u``、
    ``datetime.now(timezone.utc)``），把 naive 当本地时区会在服务器时区非 UTC 时
    悄悄产生 8 小时偏移，而这种偏移只在排序跨日时才暴露。
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def parse_timestamp(text: str) -> datetime:
    """解析 ``20260814T031500Z`` 或 ``2026-08-14T03:15:00Z``（后者供 CLI 传参）。"""
    raw = text.strip()
    for fmt in (TIMESTAMP_FORMAT, ISO_FORMAT, "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return _as_utc(datetime.strptime(raw, fmt))
        except ValueError:
            continue
    raise BuildIdentityError(
        f"无法解析时间戳 {text!r}：接受 20260814T031500Z 或 2026-08-14T03:15:00Z"
    )


# ---------------------------------------------------------------------------
# 分支名与 SHA
# ---------------------------------------------------------------------------


def sanitize_branch(branch: str, max_length: int = MAX_BRANCH_LENGTH) -> str:
    """把任意分支名清洗成单个文件系统安全的路径分量。

    这是全函数：任意输入（含空串、``..``、纯 Unicode）都返回一个非空、不含
    ``/``、不等于 ``.`` / ``..`` 的结果。清洗顺序有讲究——

    1. 先替换不安全字符，再折叠 ``_``：反过来做的话 ``a//b`` 会先折叠不了（``/``
       还不是 ``_``），最终得到 ``a__b``，同一分支在不同工具里算出两个目录名。
    2. 截断放在折叠之后、去点之前：截断可能把结尾切成 ``.``（如 ``x.y.`` 截到
       ``x.``），所以去首尾点必须是最后一步。
    3. 末尾同时去掉 ``_``：截断切在 ``_`` 上产生的尾巴没有信息量，去掉让目录名
       更干净，且不会引入歧义（``a_`` 与 ``a`` 的原始分支名本来就不同，唯一性由
       短 SHA 与时间戳承担）。
    """
    cleaned = UNSAFE_CHAR_RE.sub("_", branch or "")
    cleaned = _REPEATED_UNDERSCORE_RE.sub("_", cleaned)
    if max_length > 0:
        cleaned = cleaned[:max_length]
    cleaned = cleaned.strip(".").strip("_").strip(".")
    return cleaned or FALLBACK_BRANCH


def short_sha(sha: str, length: int = SHORT_SHA_LENGTH) -> str:
    """取小写十六进制短 SHA。

    :raises BuildIdentityError: 输入不含任何十六进制字符，或长度不足 ``length``

    严格而不兜底：短 SHA 是「这份归档对应哪次提交」在目录名上的唯一线索，
    拿不到就应该让归档失败并暴露上游传参问题，而不是写一个 ``0000000`` 之类
    查不回源码的目录名。长度不足也报错——7 位的 SHA 混在 9 位的序列里会让
    ``build_id`` 的段长不一致，解析与人工比对都更容易出错。
    """
    normalized = _NON_HEX_RE.sub("", (sha or "").strip().lower())
    if not normalized:
        raise BuildIdentityError(
            f"提交 SHA {sha!r} 里没有十六进制字符：\n"
            f"  处置：确认 --sha 或 $GITHUB_SHA 传的是完整 commit sha"
        )
    if len(normalized) < length:
        raise BuildIdentityError(
            f"提交 SHA {sha!r} 的十六进制部分只有 {len(normalized)} 位，"
            f"不足 {length} 位"
        )
    return normalized[:length]


# ---------------------------------------------------------------------------
# 组装与解析
# ---------------------------------------------------------------------------


def make_identity(
    branch: str,
    sha: str,
    moment: datetime | None = None,
    length: int = SHORT_SHA_LENGTH,
) -> BuildIdentity:
    """由（时间、分支、SHA）构造构建标识。"""
    return BuildIdentity(
        timestamp=format_timestamp(moment),
        branch=sanitize_branch(branch),
        short_sha=short_sha(sha, length),
    )


def make_build_id(
    branch: str,
    sha: str,
    moment: datetime | None = None,
    length: int = SHORT_SHA_LENGTH,
) -> str:
    """:func:`make_identity` 的字符串形式。"""
    return make_identity(branch, sha, moment, length).build_id


def is_build_id(name: str) -> bool:
    """``name`` 是否是完整形态的 build_id。"""
    return bool(BUILD_ID_RE.match(name or ""))


def is_archive_dir_name(name: str) -> bool:
    """``name`` 是否是归档目录名（只要求时间戳前缀）。

    比 :func:`is_build_id` 宽松：清理逻辑面对的是磁盘上真实存在的目录，历史上
    可能由稍早版本的工具生成。判据与 preflight.sh 的 ``find -regex`` 保持一致，
    避免两处对「什么是归档目录」有不同看法。
    """
    return bool(ARCHIVE_DIR_RE.match(name or ""))


def parse_build_id(build_id: str) -> BuildIdentity:
    """把 build_id 拆回三部分。

    最后一个 ``-`` 之后的段一律视为短 SHA。分支名本身可以含 ``-``，因此
    ``a-b-c-1234abcde`` 的分支是 ``a-b-c``。这里存在原理上的歧义（分支名末段
    恰好是十六进制时无法与 SHA 区分），所以解析只用于展示与测试，权威值一律
    从 ``manifest.json`` 读（需求 8.3 就是为此存在的）。
    """
    if not is_build_id(build_id):
        raise BuildIdentityError(
            f"{build_id!r} 不是合法 build_id，期望 "
            f"<20260814T031500Z>-<branch>-<9 位短 sha>"
        )
    timestamp, rest = build_id.split("-", 1)
    branch, sha = rest.rsplit("-", 1)
    return BuildIdentity(timestamp=timestamp, branch=branch, short_sha=sha)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

FIELDS = ("build_id", "timestamp", "branch", "short_sha")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build_identity.py",
        description="生成构建标识 <UTC 时间戳>-<清洗后分支名>-<短 SHA>",
    )
    parser.add_argument(
        "--branch",
        default=os.environ.get("BRANCH") or os.environ.get("GITHUB_REF_NAME", ""),
        help="分支名，默认 $BRANCH / $GITHUB_REF_NAME",
    )
    parser.add_argument(
        "--sha",
        default=os.environ.get("COMMIT_SHA") or os.environ.get("GITHUB_SHA", ""),
        help="完整提交 SHA，默认 $COMMIT_SHA / $GITHUB_SHA",
    )
    parser.add_argument(
        "--timestamp", default=None, help="构建时间，默认当前 UTC"
    )
    parser.add_argument(
        "--short-sha-length", type=int, default=SHORT_SHA_LENGTH, help="短 SHA 位数"
    )
    parser.add_argument(
        "--field", choices=FIELDS, default="build_id", help="只输出指定字段"
    )
    parser.add_argument("--json", action="store_true", help="输出全部字段的 JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        moment = parse_timestamp(args.timestamp) if args.timestamp else utcnow()
        identity = make_identity(
            args.branch, args.sha, moment, args.short_sha_length
        )
    except BuildIdentityError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_FAIL

    if args.json:
        print(json.dumps(identity.as_dict(), ensure_ascii=False))
    else:
        print(identity.as_dict()[args.field])
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
