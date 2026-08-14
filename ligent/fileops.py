#!/usr/bin/env python3
"""原子写入与编码/行尾保持（任务 3.6，需求 2.6、3.3、8.8）。

这个模块是 Rebrand_Tool 唯一接触文件系统写入的地方。把它单独拆出来的理由：

* **「不产生半写入文件」必须是结构性保证而不是测试期望。** 流水线配了
  ``cancel-in-progress: true``，构建被中途 kill 是常态而非异常。所有写入统一走
  ``<path>.ligent.tmp`` + :func:`os.replace`，于是目标文件在任意时刻要么是旧内容
  要么是新内容（design.md「部分写入的防御」、Property 8）。
* **``--check`` 模式不能打开任何写句柄。** 策略层被设计成「纯函数产出目标文本 →
  由调用方决定是否写入」，check 路径根本不会走到这里，这比在写函数里加
  ``if dry_run`` 判断更难写错。
* **行尾风格保持**（需求 3.3）需要一套逐行处理原语。Python 自带的
  ``str.splitlines`` 会在 ``\\x0b``、``\\x0c``、``\\u2028`` 等 Unicode 行边界上切分，
  用它处理 shell 脚本会在含换页符的文件上悄悄改变字节。这里用显式正则只认
  ``\\r\\n`` / ``\\r`` / ``\\n``，并保证 ``join(split(text)) == text``。
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

#: 全部读写使用的编码（需求 3.3）
ENCODING = "utf-8"

#: 原子写入的临时文件后缀（design.md 5.3 节末）
TMP_SUFFIX = ".ligent.tmp"

#: UTF-8 BOM 的解码形态。我们从不主动写入它（需求 3.3）。
BOM = "\ufeff"

# 只认 CRLF / CR / LF 三种行尾，其余字符一律当普通内容。
_LINE_RE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)")
_NEWLINE_RE = re.compile(r"\r\n|\r|\n")


# ---------------------------------------------------------------------------
# 逐行原语
# ---------------------------------------------------------------------------


def split_lines_keepends(text: str) -> list[str]:
    """按 CRLF/CR/LF 切分并保留行尾符，满足 ``"".join(result) == text``。

    保留行尾符是「行尾风格保持」的关键：替换一行时只改行体、原样带回该行自己的
    行尾符，于是混合行尾的文件也能逐字节保持（需求 3.3）。
    """
    lines = _LINE_RE.findall(text)
    consumed = sum(len(line) for line in lines)
    if consumed < len(text):
        # 末行没有行尾符
        lines.append(text[consumed:])
    return lines


def join_lines(lines: list[str]) -> str:
    """:func:`split_lines_keepends` 的逆运算。"""
    return "".join(lines)


def line_terminator(line: str) -> str:
    """返回该行的行尾符（``""`` 表示末行无行尾符）。"""
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith(("\n", "\r")):
        return line[-1]
    return ""


def line_body(line: str) -> str:
    """返回去掉行尾符的行体。"""
    term = line_terminator(line)
    return line[: len(line) - len(term)] if term else line


def detect_newline(text: str, default: str = "\n") -> str:
    """返回文本中出现次数最多的行尾风格；无行尾符时返回 ``default``。

    「出现次数最多」而不是「第一个出现的」：混合行尾的文件里第一行可能恰好是异类，
    按多数派决定新增行的风格更不容易让 diff 变脏。
    """
    found = _NEWLINE_RE.findall(text)
    if not found:
        return default
    counts: dict[str, int] = {}
    for item in found:
        counts[item] = counts.get(item, 0) + 1
    return max(counts.items(), key=lambda kv: (kv[1], kv[0] == "\n"))[0]


def normalize_newlines(text: str) -> str:
    """把全部行尾统一成 LF（只用于内容比较，不用于写入）。"""
    return _NEWLINE_RE.sub("\n", text)


def apply_newline_style(text: str, newline: str) -> str:
    """把 LF 文本转成指定行尾风格。``newline == "\\n"`` 时是恒等变换。"""
    normalized = normalize_newlines(text)
    return normalized if newline == "\n" else normalized.replace("\n", newline)


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------


def read_text(path: str | Path) -> str:
    """以 UTF-8、``newline=""`` 读取文本，行尾符原样保留在字符串里。

    不做 ``utf-8-sig`` 解码：BOM 若存在会以 ``\\ufeff`` 字符形态留在文本首部并被原样
    写回，这样「不主动增删 BOM」这一点是自然成立的（需求 3.3）。
    """
    with open(path, "r", encoding=ENCODING, newline="") as handle:
        return handle.read()


def file_mode(path: str | Path) -> int:
    """文件的权限位（``stat.S_IMODE`` 结果）。"""
    return stat.S_IMODE(os.stat(path).st_mode)


def write_text_atomic(path: str | Path, text: str) -> Path:
    """原子写入：先写 ``<path>.ligent.tmp``，再 :func:`os.replace` 覆盖目标。

    ``os.replace`` 在同一文件系统上是原子的，且替换的是目录项而不是原 inode 的内容，
    因此**必须**在替换前把原文件的权限位复制到临时文件上——否则 ``install.sh`` 会从
    0755 掉到 0600，打包进 ``.bin`` 后装机时无法执行（需求 3.3「不改权限位」）。

    :returns: 写入的目标路径
    """
    target = Path(path)
    tmp = target.with_name(target.name + TMP_SUFFIX)
    mode = file_mode(target) if target.exists() else None
    data = text.encode(ENCODING)

    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        # 包含 KeyboardInterrupt 与 SystemExit：中途失败不留垃圾临时文件，
        # 也绝不触碰目标文件。
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    return target
