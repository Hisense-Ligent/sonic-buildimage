#!/usr/bin/env python3
"""Artifact_Store 保留策略（任务 10.4；design.md 6.5 节，需求 8.6、8.7）。

```
用法: python3 ligent/retention.py [--store DIR] [--count N]
                                  [--dry-run] [--json] [--quiet]

  --store DIR   Artifact_Store，默认 $ARTIFACT_STORE 或 $LIGENT_ARTIFACT_STORE
  --count N     保留份数，默认 $LIGENT_RETENTION_COUNT，再默认 3
  --dry-run     只列出将删除的目录，不实际删除
  --json        机读输出（kept / deleted / latest）
  --quiet       只输出结论行

退出码: 0 = 清理完成（含「无需清理」）；1 = 参数非法或删除失败
```

## 排序依据是目录名，不是 mtime

```python
select_for_deletion(build_ids, n)   # 纯函数，可直接被测试驱动
```

``build_id`` 以 ``%Y%m%dT%H%M%SZ`` 开头（见 :mod:`build_identity`），所以**字典序
就是时间序**，``sorted()`` 之后取末尾 N 个即最新 N 个。design.md 6.5 节给的等价
shell 是 ``ls -1d 2*Z-* | sort | head -n -"$RETENTION_COUNT" | xargs -r rm -rf``。

用目录名而**不用 mtime**是这个模块最重要的一个决定。mtime 看起来更"真实"，但它
是可被事后改写的：

* ``cp`` / ``cp -r`` 到归档目录会把父目录 mtime 刷成拷贝时刻；
* 手工 ``touch``、``rsync`` 回填、备份工具还原都会改 mtime；
* 在归档目录里补写一个文件（例如事后补 ``brand-verify.txt``）会让这个**较旧**的
  归档看起来最新。

任一情形发生，按 mtime 排序都会**删掉较新的构建而留下较旧的**，且不报任何错。
把时间编码进目录名，让排序依据与内容一起不可变，是唯一能让「保留最新 N 个」这
句话始终为真的做法。这也是属性测试要求生成器覆盖「时间戳乱序」模式的原因——
乱序场景是唯一能把 mtime 实现和名字排序实现区分开的输入。

## 为什么删除后还要修 latest

``latest`` 指向被删掉的目录会留下一个悬空链接：``ls -l`` 看着正常，``cat
latest/manifest.json`` 直接 ENOENT。需求 8.5 要求这个固定名称链接指向「最新成功
构建目录」，那么在删除动作改变了「最新且存在」的集合之后，把链接重新指到保留下
来的最新一份，是同一条需求的必然推论，而不是额外功能。正常路径下不会触发（被删
的都比 ``latest`` 旧），只有在 ``--count`` 被调小或人为删目录后才会用到。

## 只碰归档目录

删除操作的对象集合被三重收窄：``$ARTIFACT_STORE`` 一层之下、``is_dir()`` 且
``not is_symlink()``、名字匹配 ``^\\d{8}T\\d{6}Z-``。于是 ``latest`` 链接、任何手工
放进去的笔记文件、以及嵌在归档内部的任何路径都不可能成为 ``rmtree`` 的目标。
这个模块执行的是 ``rm -rf``，收窄输入集合比事后检查更重要。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:  # 直接跑脚本 / 测试把 ligent/ 注入 sys.path 时
    from build_identity import is_archive_dir_name
except ImportError:  # pragma: no cover - 以 `python3 -m ligent.retention` 导入时
    from .build_identity import is_archive_dir_name  # type: ignore

#: 保留份数默认值。**设计上从需求 8.7 的 10 改为 3**（design.md 6.5 节）：
#: 单个 sonic-vs.bin 约 2 GiB，全量 build.log 可达数百 MB，一份归档约 2.5 GiB。
#: 关键不在归档本身占多少，而在于它与构建工作区共享同一个 439 GiB 分区
#: （/dev/sda2 挂 /，home 也在其上），而工作区峰值占用在 100 GiB 量级、
#: /var/lib/docker 还会单调增长。压到 3 份是为了让归档在容量账上小到可以忽略。
#: 需要更多历史版本时应该挂第二块盘，而不是调大这个数字（任务 18.1）。
DEFAULT_RETENTION_COUNT = 3

#: 保留数量的环境变量（GitHub Actions 仓库变量 LIGENT_RETENTION_COUNT）
RETENTION_ENV = "LIGENT_RETENTION_COUNT"

#: Artifact_Store 路径的环境变量（两个名字都认：workflow 里用前者，
#: 仓库变量名是后者，SSH 排障时两种写法都有人用）
STORE_ENVS = ("ARTIFACT_STORE", "LIGENT_ARTIFACT_STORE")

#: 需求 8.5 的固定名称链接
LATEST_LINK = "latest"

EXIT_OK = 0
EXIT_FAIL = 1

OK = "OK"
FAIL = "FAIL"


class RetentionError(Exception):
    """保留策略的输入非法或删除失败。"""

    exit_code = EXIT_FAIL


@dataclass
class RetentionResult:
    """一次清理的结果。"""

    store: Path
    count: int
    kept: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    latest: str | None = None
    dry_run: bool = False

    @property
    def status(self) -> str:
        return FAIL if self.failed else OK

    def as_dict(self) -> dict[str, object]:
        return {
            "store": str(self.store),
            "count": self.count,
            "kept": list(self.kept),
            "deleted": list(self.deleted),
            "failed": [{"name": name, "error": err} for name, err in self.failed],
            "latest": self.latest,
            "dry_run": self.dry_run,
            "status": self.status,
        }


# ---------------------------------------------------------------------------
# 纯函数：选择删除对象
# ---------------------------------------------------------------------------


def sort_build_ids(build_ids: list[str]) -> list[str]:
    """按 build_id 字典序（== 时间序）升序排序，最旧在前。

    ``sorted()`` 用的是码位序而不是 locale 序：``build_id`` 的时间戳段只含
    ASCII 数字与 ``T``/``Z``，两者在这一段上一致；显式不走 locale 也让结果与
    shell 侧 ``LC_ALL=C sort`` 一致，避免同一份归档在不同环境下算出不同顺序。
    """
    return sorted(build_ids)


def select_for_deletion(build_ids: list[str], n: int = DEFAULT_RETENTION_COUNT) -> list[str]:
    """返回应删除的 build_id 列表（最旧在前），保留字典序最新的 ``n`` 个。

    这是本模块的核心纯函数：不碰文件系统、不看 mtime、只依赖名字。属性测试
    （Property 18）与 shell 侧都通过它来判定，保证「判定只有一处」。

    :param build_ids: 归档目录名列表，顺序任意（属性测试会喂乱序输入）
    :param n: 保留份数，必须 ``>= 1``
    :raises RetentionError: ``n < 1``

    ``n < 1`` 报错而不是「删光」：``LIGENT_RETENTION_COUNT`` 写成 0 或空串大概率
    是配置笔误，此时清空整个 Artifact_Store 是不可逆的。要清空应该显式 ``rm -rf``，
    而不是通过一个配置项的边界值。
    """
    if n < 1:
        raise RetentionError(
            f"保留份数必须 >= 1，收到 {n}。\n"
            f"  想清空 Artifact_Store 请显式手工删除，不要靠把 "
            f"{RETENTION_ENV} 设成 0 来实现"
        )
    ordered = sort_build_ids(build_ids)
    return ordered[: max(0, len(ordered) - n)] if len(ordered) > n else []


def select_to_keep(build_ids: list[str], n: int = DEFAULT_RETENTION_COUNT) -> list[str]:
    """:func:`select_for_deletion` 的补集（最旧在前），即清理后保留下来的。"""
    ordered = sort_build_ids(build_ids)
    doomed = set(select_for_deletion(build_ids, n))
    return [name for name in ordered if name not in doomed]


def newest(build_ids: list[str]) -> str | None:
    """字典序最大者，即时间上最新的一份；空列表返回 ``None``。"""
    ordered = sort_build_ids(build_ids)
    return ordered[-1] if ordered else None


# ---------------------------------------------------------------------------
# 文件系统侧
# ---------------------------------------------------------------------------


def list_archives(store: Path) -> list[str]:
    """列出 ``store`` 下的归档目录名（升序）。

    三重收窄：一层之下、真目录（排除 ``latest`` 这类符号链接）、名字带时间戳
    前缀。这些约束决定了后面 ``rmtree`` 能碰到的全部对象。
    """
    if not store.is_dir():
        return []
    names = [
        entry.name
        for entry in store.iterdir()
        if entry.is_dir()
        and not entry.is_symlink()
        and is_archive_dir_name(entry.name)
    ]
    return sort_build_ids(names)


def read_latest(store: Path) -> str | None:
    """``latest`` 链接当前指向的目录名；不是符号链接或不存在时返回 ``None``。"""
    link = store / LATEST_LINK
    if not link.is_symlink():
        return None
    return os.path.basename(os.readlink(link))


def update_latest(store: Path, target: str) -> None:
    """把 ``latest`` 原子地指向 ``target``（与 archive.sh 的做法一致）。

    先建临时链接再 ``os.replace`` 覆盖：``rm latest && ln -s`` 中间存在一个
    ``latest`` 不存在的窗口，而 ``os.symlink`` 不能覆盖已有路径。``os.replace``
    对同目录内的重命名是原子的，于是外部观察者看到的 ``latest`` 要么是旧目标
    要么是新目标，不会是「不存在」或「悬空」。
    """
    link = store / LATEST_LINK
    tmp = store / ".latest.tmp"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(target, tmp)  # 相对链接：整个 Artifact_Store 可整体搬迁
    os.replace(tmp, link)


def refresh_latest(store: Path, kept: list[str]) -> str | None:
    """确保 ``latest`` 指向一个存在的目录，返回其最终目标。

    只在链接缺失或悬空时才动它。正常路径下 ``latest`` 指向最新一份，而被删的都
    比它旧，所以这个函数什么也不做——它是为「``--count`` 被调小」「有人手工删了
    目录」这些情形准备的安全网（需求 8.5）。
    """
    if not kept:
        return read_latest(store)
    current = read_latest(store)
    if current is not None and (store / current).is_dir():
        return current
    target = newest(kept)
    if target is None:  # pragma: no cover - kept 非空则必有最新者
        return None
    update_latest(store, target)
    return target


def apply_retention(
    store: Path,
    count: int = DEFAULT_RETENTION_COUNT,
    dry_run: bool = False,
) -> RetentionResult:
    """在真实 Artifact_Store 上执行保留策略。

    删除失败不立即抛出，而是记进 ``failed`` 后继续处理其余目录：一个目录因权限
    或残留挂载点删不掉，不应该让其余可删的目录继续占着磁盘。最终退出码仍为非零。
    """
    archives = list_archives(store)
    doomed = select_for_deletion(archives, count)
    result = RetentionResult(
        store=store,
        count=count,
        kept=select_to_keep(archives, count),
        dry_run=dry_run,
    )

    for name in doomed:
        target = store / name
        if dry_run:
            result.deleted.append(name)
            continue
        try:
            shutil.rmtree(target)
            result.deleted.append(name)
        except OSError as exc:
            result.failed.append((name, str(exc)))
            # 删不掉的目录仍然存在，如实记进 kept，避免摘要谎报数量
            result.kept.append(name)

    result.kept = sort_build_ids(result.kept)
    result.latest = (
        read_latest(store) if dry_run else refresh_latest(store, result.kept)
    )
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def default_store() -> str:
    for name in STORE_ENVS:
        value = os.environ.get(name)
        if value:
            return value
    return ""


def default_count() -> int:
    """保留份数：``LIGENT_RETENTION_COUNT`` → 3。

    非数字或空串一律回落到默认 3 并不报错：这个值来自 GitHub 仓库变量，
    未设置时 ``${{ vars.X }}`` 展开为空串是常态，不是错误。而**负数或 0 会在
    :func:`select_for_deletion` 里报错**——那是明确写错了值，不该被悄悄修正。
    """
    raw = (os.environ.get(RETENTION_ENV) or "").strip()
    if not raw:
        return DEFAULT_RETENTION_COUNT
    try:
        return int(raw)
    except ValueError:
        print(
            f"警告：{RETENTION_ENV}={raw!r} 不是整数，回落到默认 "
            f"{DEFAULT_RETENTION_COUNT}",
            file=sys.stderr,
        )
        return DEFAULT_RETENTION_COUNT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="retention.py",
        description="按 build_id 字典序保留最新 N 份归档，删除更早的（需求 8.6、8.7）",
    )
    parser.add_argument(
        "--store", default=default_store(), help="Artifact_Store 目录"
    )
    parser.add_argument(
        "--count", type=int, default=None, help=f"保留份数，默认 ${RETENTION_ENV} 或 3"
    )
    parser.add_argument("--dry-run", action="store_true", help="只列出，不删除")
    parser.add_argument("--json", action="store_true", help="机读输出")
    parser.add_argument("--quiet", action="store_true", help="只输出结论行")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.store:
        print(
            "错误：未指定 Artifact_Store。用 --store 或设置 "
            + " / ".join(f"${name}" for name in STORE_ENVS),
            file=sys.stderr,
        )
        return EXIT_FAIL

    store = Path(args.store).expanduser()
    count = args.count if args.count is not None else default_count()

    try:
        result = apply_retention(store, count, dry_run=args.dry_run)
    except RetentionError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_FAIL

    if args.json:
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
        return EXIT_FAIL if result.failed else EXIT_OK

    prefix = "[dry-run] " if args.dry_run else ""
    if not args.quiet:
        # 与 preflight.sh / verify_brand.py 同一行格式：[ 状态 ] ID 名称 : 细节
        print(
            f"[{result.status:^4}] AR-01 retention_policy    : "
            f"{prefix}{store} 现有 {len(result.kept) + len(result.deleted)} 份，"
            f"保留最新 {count} 份"
        )
        for name in result.deleted:
            print(f"       ↳ {prefix}删除 {name}")
        for name, err in result.failed:
            print(f"       ↳ 删除失败 {name}：{err}", file=sys.stderr)
        for name in result.kept:
            marker = " (latest)" if name == result.latest else ""
            print(f"       ↳ 保留 {name}{marker}")

    print(
        f"保留策略：{prefix}删除 {len(result.deleted)} 份，"
        f"保留 {len(result.kept)} 份，latest -> {result.latest or '（无）'}"
    )
    return EXIT_FAIL if result.failed else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
