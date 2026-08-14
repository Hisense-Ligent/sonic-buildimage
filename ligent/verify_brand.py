#!/usr/bin/env python3
"""Brand_Verifier —— 构建产物与构建输入的品牌校验（任务 8.1；design.md 4.6 节）。

```
用法: python3 ligent/verify_brand.py [--image PATH] [--workspace PATH]
                                     [--require-image] [--json] [--verbose]

  --image PATH      .bin 自解压安装包。给出时 BV-02/03/04 从包内 installer/ 读取
  --workspace PATH  工作区（仓库根），默认本脚本所在目录的上一级
  --require-image   没有 --image 时直接失败，供流水线使用（见「两种模式」）
  --json            以 JSON 输出全部检查项，供后续步骤机读
  --verbose         额外输出取证细节（解包出的成员、命中位置等）

退出码: 0 = 全部检查项通过；1 = 任一检查项失败或输入不可用（需求 4.3、9.3）
```

## 检查项（design.md 4.6 节）

===== ==================== =========================================================
ID    名称                 判据
===== ==================== =========================================================
BV-01 image_motd           可选深度校验，``LIGENT_DEEP_VERIFY=1`` 开启（见下）
BV-02 grub_display         ``default_platform.conf`` 含 ``$demo_brand_display``（2 处）
                           且 ``install.sh`` 含 ``demo_brand_display="Ligent-``
BV-03 volume_label_intact  ``demo_volume_label="SONiC-${demo_type}"`` 原样存在
BV-04 menuentry_intact     ``demo_grub_entry="$demo_volume_revision_label"`` 原样存在
BV-05 workspace_motd       工作区 ``motd`` 与 ``init_cfg.json.j2`` 的
                           ``BANNER_MESSAGE.motd`` 均含 LIGENT ASCII art 首行
===== ==================== =========================================================

**BV-03 与 BV-04 是反向检查**：它们确认底层标识**没有**被改动，是本设计里最关键的
安全网。GRUB menuentry 标题与分区卷标必须保持 ``SONiC-OS``——``sonic-utilities`` 的
``IMAGE_PREFIX = 'SONiC-OS-'`` 依赖它，一旦被改成 ``Ligent-OS-``，
``sonic-installer list/remove/set-default`` 会全部失效，而且是**装机时**才炸，
构建阶段一切正常。所以这两项越「无聊」越好：它们平时永远通过，真正报警的那一次
就是拦住了一个装机期故障。

## 两种模式

* **镜像模式**（给了 ``--image``）：BV-02/03/04 从 ``.bin`` 内解出的
  ``installer/`` 读取，BV-05 读工作区。这是流水线的正常路径。
* **工作区模式**（没给 ``--image``）：BV-02/03/04 改为读工作区的
  ``installer/install.sh`` 与 ``installer/default_platform.conf``，BV-05 不变。

工作区模式存在的理由：``.bin`` 要两小时才产出，而这四个检查项针对的文本在
``make`` 之前就已定型（``.bin`` 里的 ``installer/`` 就是工作区这两个文件的副本）。
有了它，品牌工具的改动能在 5 分钟内自证，任务 9 的检查点也不必等构建产物。

代价是它**不能证明打包环节没出问题**。所以每条工作区模式的结果都带
``[workspace]`` 前缀，且流水线里必须传 ``--require-image``——否则「.bin 路径写错」
会退化成「工作区模式全绿」，把一次漏检伪装成通过。这是本工具唯一需要小心的
误用方式，用一个显式开关把它钉死。

## 为什么不挂载 squashfs

``/etc/motd`` 位于 ``installer/fs.squashfs`` 内部。读它需要 ``unsquashfs``
（服务器上未确认有 ``squashfs-tools``），且解压整个 rootfs 代价不小。设计采用
两段式：BV-05 在构建输入侧确认（等价于 ``sonic_debian_extension.j2`` 会 ``cp``
进镜像的内容），BV-01 作为可选的端到端确认。

**BV-01 的扩展位已经留好**：:func:`check_image_motd` 已实现完整逻辑，只是默认
不注册——``LIGENT_DEEP_VERIFY=1`` 时才加入检查列表（任务 8.2 只需在服务器上
``sudo apt-get install squashfs-tools`` 并在仓库变量里打开这个开关，代码无需再改）。

## ``.bin`` 的解包手法

``.bin`` 是 ``installer/sharch_body.sh`` 生成的自解压包：一段 shell 脚本，以
``exit_marker`` 行结尾，紧随其后是 tar 数据。脚本自己就是这么读的：

```sh
sed -e '1,/^exit_marker$/d' "$0" | head -c $payload_image_size | tar xf -
```

本模块用 Python 复现同一手法（定位 ``exit_marker`` 行 → 按 header 里的
``payload_image_size`` 截断 → 流式 ``tarfile``），不起 shell、不落地临时文件、
不需要 root。用 ``r|``（流式）而非 ``r:``（随机访问）读 tar，是因为只需要两个
成员，流式读到即停，不必为几 GB 的 ``fs.squashfs`` 付解析代价。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

try:  # 直接跑脚本 / 测试把 ligent/ 注入 sys.path 时
    from brandconfig import BrandAssetsError, load_brand_assets
    from touchpoints import BRAND_DISPLAY_VAR
except ImportError:  # pragma: no cover - 以 `python3 -m ligent.verify_brand` 导入时
    from .brandconfig import BrandAssetsError, load_brand_assets  # type: ignore
    from .touchpoints import BRAND_DISPLAY_VAR  # type: ignore

EXIT_OK = 0
#: 任一检查项失败，或输入不可用（需求 4.3、9.3）
EXIT_FAIL = 1

OK = "OK"
FAIL = "FAIL"
SKIP = "SKIP"

# ---------------------------------------------------------------------------
# 被检查的路径与字面量
# ---------------------------------------------------------------------------

INSTALL_SH = "installer/install.sh"
PLATFORM_CONF = "installer/default_platform.conf"
MOTD_PATH = "files/image_config/environment/motd"
INIT_CFG_PATH = "files/build_templates/init_cfg.json.j2"
SQUASHFS_MEMBER = "installer/fs.squashfs"

#: 从 ``.bin`` 里解出来的成员（BV-02/03/04 全部只需要这两个文本文件）
IMAGE_MEMBERS = (INSTALL_SH, PLATFORM_CONF)

#: BV-03：分区卷标定义，必须逐字节保持（design.md 4.2 节「结论 1」）
VOLUME_LABEL_LINE = 'demo_volume_label="SONiC-${demo_type}"'
#: BV-04：GRUB menuentry 标题定义，必须逐字节保持（design.md 4.2 节「结论 2」）
MENUENTRY_LINE = 'demo_grub_entry="$demo_volume_revision_label"'
#: BV-02：GRUB 显示变量在 default_platform.conf 中应出现的次数
EXPECTED_DISPLAY_SITES = 2

#: ``exit_marker`` 行——sharch_body.sh 的自解压分界（该文件末行）
EXIT_MARKER = b"exit_marker"
#: 自解压 header 里声明的 tar 载荷字节数
PAYLOAD_SIZE_RE = re.compile(rb"^payload_image_size=(\d+)\s*$", re.MULTILINE)
#: 只在 ``.bin`` 的前若干字节里找 header（header 是几十行 shell，1 MiB 富余）
HEADER_SCAN_BYTES = 1 << 20

#: BV-01 的开关（design.md 4.6 节；任务 8.2）
DEEP_VERIFY_ENV = "LIGENT_DEEP_VERIFY"


class VerifyInputError(Exception):
    """输入不可用（``.bin`` 不存在、解包失败、品牌资产缺失等）。"""

    exit_code = EXIT_FAIL


# ---------------------------------------------------------------------------
# 检查结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    """一条检查结果。格式与 ``ligent/structure.py``、``ligent/preflight_eval.py`` 对齐。"""

    check_id: str
    name: str
    status: str
    detail: str
    evidence: tuple[str, ...] = field(default_factory=tuple)

    @property
    def failed(self) -> bool:
        return self.status == FAIL

    def format(self, verbose: bool = False) -> str:
        line = f"[{self.status:^4}] {self.check_id} {self.name:<24}: {self.detail}"
        if verbose or self.failed:
            for item in self.evidence:
                line += f"\n       ↳ {item}"
        return line

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.check_id,
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "evidence": list(self.evidence),
        }


# ---------------------------------------------------------------------------
# .bin 解包（不起 shell、不落地、不需要 root）
# ---------------------------------------------------------------------------


class _LimitedReader(io.RawIOBase):
    """把底层文件对象裁成「从当前位置起最多 N 字节」的只读流。

    对应 ``sharch_body.sh`` 里的 ``head -c $payload_image_size``：``.bin`` 末尾可能
    带 tar 之后的额外字节，不裁会让 ``tarfile`` 在流末尾读到垃圾数据。
    """

    def __init__(self, handle: io.BufferedReader, limit: int) -> None:
        self._handle = handle
        self._remaining = limit

    def readable(self) -> bool:  # pragma: no cover - RawIOBase 协议
        return True

    def readinto(self, buffer) -> int:  # type: ignore[override]
        if self._remaining <= 0:
            return 0
        view = memoryview(buffer)
        want = min(len(view), self._remaining)
        chunk = self._handle.read(want)
        if not chunk:
            self._remaining = 0
            return 0
        view[: len(chunk)] = chunk
        self._remaining -= len(chunk)
        return len(chunk)


def find_payload_offset(path: Path) -> tuple[int, int | None]:
    """定位 tar 载荷的起始偏移与声明长度。

    :returns: ``(offset, payload_image_size 或 None)``
    :raises VerifyInputError: 找不到 ``exit_marker`` 行（不是 sharch 自解压包）
    """
    with open(path, "rb") as handle:
        header = handle.read(HEADER_SCAN_BYTES)

    # 与 sed 的 /^exit_marker$/ 等价：整行匹配。首行即 marker 的情形一并处理。
    if header.startswith(EXIT_MARKER + b"\n"):
        offset = len(EXIT_MARKER) + 1
    else:
        index = header.find(b"\n" + EXIT_MARKER + b"\n")
        if index < 0:
            raise VerifyInputError(
                f"{path} 不像 sharch 自解压包：前 {HEADER_SCAN_BYTES} 字节里没有 "
                f"独占一行的 `exit_marker`。\n"
                f"  处置：确认传入的是 target/sonic-vs.bin 而不是别的产物"
            )
        offset = index + 1 + len(EXIT_MARKER) + 1

    match = PAYLOAD_SIZE_RE.search(header)
    size = int(match.group(1)) if match else None
    return offset, size


def extract_members(
    path: Path, members: tuple[str, ...] = IMAGE_MEMBERS
) -> dict[str, str]:
    """从 ``.bin`` 里流式取出指定成员的文本内容。

    成员名按「去掉前导 ``./``」后精确匹配，取到全部目标即停止读取——不为几 GB 的
    ``fs.squashfs`` 付解析代价。
    """
    offset, size = find_payload_offset(path)
    wanted = set(members)
    found: dict[str, str] = {}
    with open(path, "rb") as handle:
        handle.seek(offset)
        stream = handle if size is None else io.BufferedReader(_LimitedReader(handle, size))
        try:
            with tarfile.open(fileobj=stream, mode="r|") as archive:  # type: ignore[arg-type]
                for entry in archive:
                    name = entry.name[2:] if entry.name.startswith("./") else entry.name
                    if name not in wanted or not entry.isfile():
                        continue
                    payload = archive.extractfile(entry)
                    if payload is None:  # pragma: no cover - 目录/链接已被过滤
                        continue
                    found[name] = payload.read().decode("utf-8", errors="replace")
                    if len(found) == len(wanted):
                        break
        except tarfile.TarError as exc:
            raise VerifyInputError(
                f"解包 {path} 失败：{exc}\n"
                f"  等价的手工命令："
                f"sed -e '1,/^exit_marker$/d' {path} | tar tf - | head"
            ) from exc

    missing = sorted(wanted - found.keys())
    if missing:
        raise VerifyInputError(
            f"{path} 的 tar 载荷里缺少成员：{', '.join(missing)}\n"
            f"  处置：确认这是完整的安装包（sharch 打包应包含整个 installer/ 目录）"
        )
    return found


def list_image_members(path: Path, limit: int = 40) -> list[str]:
    """列出 ``.bin`` 内的成员名（仅供 ``--verbose`` 取证）。"""
    offset, size = find_payload_offset(path)
    names: list[str] = []
    with open(path, "rb") as handle:
        handle.seek(offset)
        stream = handle if size is None else io.BufferedReader(_LimitedReader(handle, size))
        with tarfile.open(fileobj=stream, mode="r|") as archive:  # type: ignore[arg-type]
            for entry in archive:
                names.append(entry.name)
                if len(names) >= limit:
                    break
    return names


# ---------------------------------------------------------------------------
# 检查项实现
# ---------------------------------------------------------------------------


def _source_label(origin: str) -> str:
    return "" if origin == "image" else "[workspace] "


def check_grub_display(
    install_sh: str, platform_conf: str, brand_name: str, origin: str
) -> CheckResult:
    """BV-02：GRUB 显示变量已定义且被两处 ``echo`` 引用（需求 4.2）。"""
    definition = f'{BRAND_DISPLAY_VAR}="{brand_name}-'
    sites = platform_conf.count(f"${BRAND_DISPLAY_VAR}")
    defined = definition in install_sh
    prefix = _source_label(origin)

    if defined and sites == EXPECTED_DISPLAY_SITES:
        return CheckResult(
            "BV-02",
            "grub_display",
            OK,
            f"{prefix}{PLATFORM_CONF} uses ${BRAND_DISPLAY_VAR} "
            f"({sites} sites), {INSTALL_SH} defines {definition}...",
        )

    evidence: list[str] = []
    if not defined:
        evidence.append(
            f"{INSTALL_SH} 中找不到 {definition}...：品牌改造未执行，"
            f"或 SENTINEL_BLOCK 块被移除"
        )
    if sites != EXPECTED_DISPLAY_SITES:
        evidence.append(
            f"{PLATFORM_CONF} 中 ${BRAND_DISPLAY_VAR} 出现 {sites} 次，"
            f"期望 {EXPECTED_DISPLAY_SITES} 次（kernel 与 initial ramdisk 两处 echo）"
        )
    evidence.append("处置：执行 `python3 ligent/rebrand.py --apply` 后重新构建")
    return CheckResult(
        "BV-02",
        "grub_display",
        FAIL,
        f"{prefix}${BRAND_DISPLAY_VAR} 未按预期出现（定义 "
        f"{'有' if defined else '无'}，引用 {sites}/{EXPECTED_DISPLAY_SITES} 处）",
        tuple(evidence),
    )


def check_volume_label_intact(install_sh: str, origin: str) -> CheckResult:
    """BV-03（反向检查）：分区卷标定义必须逐字节保持（需求 3.4）。"""
    prefix = _source_label(origin)
    if VOLUME_LABEL_LINE in install_sh:
        return CheckResult(
            "BV-03",
            "volume_label_intact",
            OK,
            f"{prefix}{VOLUME_LABEL_LINE} unchanged",
        )
    return CheckResult(
        "BV-03",
        "volume_label_intact",
        FAIL,
        f"{prefix}{INSTALL_SH} 中找不到原样的 {VOLUME_LABEL_LINE}",
        (
            "这是底层标识而非品牌标识：它同时是 ext4 卷标、GPT 分区名、"
            "EFI 启动项标签与 `search --label` 的查找键",
            "改动它会破坏分区识别、EFI 启动与 sonic-installer 原地升级，"
            "且只在装机时暴露",
            "处置：恢复该行为 SONiC-${demo_type}；品牌显示请改 "
            f"{BRAND_DISPLAY_VAR}（design.md 4.2 节）",
        ),
    )


def check_menuentry_intact(platform_conf: str, origin: str) -> CheckResult:
    """BV-04（反向检查）：GRUB menuentry 标题定义必须逐字节保持。"""
    prefix = _source_label(origin)
    if MENUENTRY_LINE in platform_conf:
        return CheckResult(
            "BV-04",
            "menuentry_intact",
            OK,
            f"{prefix}{MENUENTRY_LINE} unchanged",
        )
    return CheckResult(
        "BV-04",
        "menuentry_intact",
        FAIL,
        f"{prefix}{PLATFORM_CONF} 中找不到原样的 {MENUENTRY_LINE}",
        (
            "menuentry 标题必须含 `SONiC-OS-`：sonic-utilities 的 "
            "IMAGE_PREFIX = 'SONiC-OS-' 依赖它",
            "改动会让 sonic-installer list 返回空、set-default / remove 抛异常",
            "处置：恢复该行为 $demo_volume_revision_label（design.md 4.2 节「结论 2」）",
        ),
    )


def check_workspace_motd(workspace: Path, art_line: str) -> CheckResult:
    """BV-05：工作区两条 MOTD 路径都含 LIGENT ASCII art 首行（需求 4.1）。

    两条路径必须同源：``files/image_config/environment/motd`` 是静态 ``/etc/motd``，
    ``init_cfg.json.j2`` 的 ``BANNER_MESSAGE.motd`` 是 ``config banner state enabled``
    后由 ``banner-config.sh`` 写出的内容。只改一处会出现「开了 banner 反而变回
    SONiC」的割裂，所以这里两处一起断言。

    ``init_cfg.json.j2`` 侧比较的是 **JSON 转义后**的形态：ASCII art 里的 ``\\___``
    在 JSON 字符串里是 ``\\\\___``，用原始字面量去 ``in`` 一定不命中。
    """
    motd_file = workspace / MOTD_PATH
    init_cfg = workspace / INIT_CFG_PATH
    escaped = json.dumps(art_line)[1:-1]

    missing: list[str] = []
    evidence: list[str] = []
    for path, needle, rel in (
        (motd_file, art_line, MOTD_PATH),
        (init_cfg, escaped, INIT_CFG_PATH),
    ):
        if not path.is_file():
            missing.append(rel)
            evidence.append(f"{rel} 不存在：{path}")
            continue
        if needle not in path.read_text(encoding="utf-8", errors="replace"):
            missing.append(rel)
            evidence.append(f"LIGENT ascii art not found in {rel}")

    if missing:
        evidence.append("处置：执行 `python3 ligent/rebrand.py --apply`")
        return CheckResult(
            "BV-05",
            "workspace_motd",
            FAIL,
            f"LIGENT ascii art not found in {', '.join(missing)}",
            tuple(evidence),
        )
    return CheckResult(
        "BV-05",
        "workspace_motd",
        OK,
        f"{MOTD_PATH} 与 {INIT_CFG_PATH} 的 BANNER_MESSAGE.motd 均含 LIGENT ascii art",
    )


def check_image_motd(image: Path | None, art_line: str) -> CheckResult:
    """BV-01（可选，任务 8.2）：从 ``.bin`` 内的 squashfs 读 ``/etc/motd``。

    默认**不注册**；``LIGENT_DEEP_VERIFY=1`` 时才进入检查列表（design.md 4.6 节）。
    需要 ``squashfs-tools``：``sudo apt-get install -y squashfs-tools``。

    实现已完整，因此打开这个开关不需要再改代码——这就是设计里说的「留好扩展位」。
    唯一的代价是解 squashfs 要十几分钟，所以它不在默认路径上。
    """
    if image is None:
        return CheckResult(
            "BV-01",
            "image_motd",
            SKIP,
            f"深度校验需要 --image（{DEEP_VERIFY_ENV}=1 已开启但没有镜像可读）",
        )
    if shutil.which("unsquashfs") is None:
        return CheckResult(
            "BV-01",
            "image_motd",
            SKIP,
            f"{DEEP_VERIFY_ENV}=1 但本机没有 unsquashfs；"
            "安装 squashfs-tools 后此项才会执行",
        )
    import tempfile

    with tempfile.TemporaryDirectory(prefix="ligent-bv01-") as workdir:
        squashfs = Path(workdir) / "fs.squashfs"
        payload = extract_members(image, (SQUASHFS_MEMBER,))
        squashfs.write_bytes(payload[SQUASHFS_MEMBER].encode("utf-8", "surrogateescape"))
        proc = subprocess.run(  # noqa: S603 - 参数为已解析路径与固定成员名
            [shutil.which("unsquashfs") or "unsquashfs", "-cat", str(squashfs), "etc/motd"],
            capture_output=True,
            text=True,
            errors="replace",
        )
    if proc.returncode != 0:
        return CheckResult(
            "BV-01",
            "image_motd",
            FAIL,
            f"unsquashfs 读取 etc/motd 失败（退出码 {proc.returncode}）",
            tuple(proc.stderr.strip().splitlines()[:10]),
        )
    if art_line not in proc.stdout:
        return CheckResult(
            "BV-01",
            "image_motd",
            FAIL,
            "镜像内 /etc/motd 不含 LIGENT ascii art",
            tuple(proc.stdout.splitlines()[:12]),
        )
    return CheckResult("BV-01", "image_motd", OK, "镜像内 /etc/motd 含 LIGENT ascii art")


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------


def logo_first_line(assets_dir: Path | None = None) -> str:
    """LIGENT ASCII art 的首行——BV-01/BV-05 的比对基准。

    取自 Brand_Assets 而不是写死字面量：品牌改名后判据自动跟着变（需求 1.3），
    否则改完品牌会出现「rebrand 通过、verify 失败」的自相矛盾。
    """
    assets = load_brand_assets(assets_dir)
    for line in assets.logo_ascii.splitlines():
        if line.strip():
            return line
    raise VerifyInputError("logo.ascii 全是空行，取不到 ASCII art 首行")


def run_checks(
    workspace: Path,
    image: Path | None = None,
    assets_dir: Path | None = None,
    verbose: bool = False,
) -> list[CheckResult]:
    """跑全部检查项，返回结果列表（不抛出检查失败，只抛输入不可用）。"""
    assets = load_brand_assets(assets_dir)
    art_line = logo_first_line(assets_dir)

    if image is not None:
        if not image.is_file():
            raise VerifyInputError(f"镜像不存在：{image}")
        sources = extract_members(image)
        origin = "image"
    else:
        sources = {}
        for rel in IMAGE_MEMBERS:
            path = workspace / rel
            if not path.is_file():
                raise VerifyInputError(
                    f"工作区模式下找不到 {rel}：{path}\n"
                    f"  处置：用 --workspace 指定仓库根，或用 --image 指定 .bin"
                )
            sources[rel] = path.read_text(encoding="utf-8", errors="replace")
        origin = "workspace"

    results: list[CheckResult] = []
    if os.environ.get(DEEP_VERIFY_ENV) == "1":
        results.append(check_image_motd(image, art_line))
    results.append(
        check_grub_display(
            sources[INSTALL_SH], sources[PLATFORM_CONF], assets.config.brand_name, origin
        )
    )
    results.append(check_volume_label_intact(sources[INSTALL_SH], origin))
    results.append(check_menuentry_intact(sources[PLATFORM_CONF], origin))
    results.append(check_workspace_motd(workspace, art_line))

    if verbose and image is not None:
        offset, size = find_payload_offset(image)
        print(f"payload offset = {offset}, payload_image_size = {size}")
        print("members: " + ", ".join(list_image_members(image, limit=12)) + " ...")
    return results


def failures(results: list[CheckResult]) -> list[CheckResult]:
    return [result for result in results if result.failed]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def default_workspace() -> Path:
    return Path(__file__).resolve().parent.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify_brand.py",
        description="Brand_Verifier：校验品牌改造已生效且底层标识未被改动",
    )
    parser.add_argument("--image", type=Path, default=None, help=".bin 自解压安装包")
    parser.add_argument(
        "--workspace", type=Path, default=None, help="工作区（仓库根），默认脚本上一级"
    )
    parser.add_argument(
        "--assets", type=Path, default=None, help="Brand_Assets 目录，默认 ligent/brand"
    )
    parser.add_argument(
        "--require-image",
        action="store_true",
        help="没有 --image 时直接失败（流水线必须带上，避免退化成工作区模式）",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出全部检查项")
    parser.add_argument("--verbose", action="store_true", help="输出取证细节")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace = (args.workspace or default_workspace()).expanduser()

    if args.require_image and args.image is None:
        print(
            "错误：--require-image 已指定但没有给 --image。\n"
            "  工作区模式不能证明打包环节正确，流水线里不接受这种退化。",
            file=sys.stderr,
        )
        return EXIT_FAIL

    try:
        results = run_checks(
            workspace, args.image, args.assets, verbose=args.verbose and not args.json
        )
    except (VerifyInputError, BrandAssetsError) as exc:
        if args.json:
            print(json.dumps({"error": str(exc), "checks": []}, ensure_ascii=False))
        else:
            print(f"错误：{exc}", file=sys.stderr)
        return EXIT_FAIL

    bad = failures(results)
    mode = "image" if args.image is not None else "workspace"

    if args.json:
        print(
            json.dumps(
                {
                    "mode": mode,
                    "image": str(args.image) if args.image else None,
                    "workspace": str(workspace),
                    "passed": not bad,
                    "checks": [result.as_dict() for result in results],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        # 需求 4.4：无条件输出**全部**检查项的 ID 与通过状态
        for result in results:
            print(result.format(verbose=args.verbose))
        print(
            f"\nBrand_Verifier（{mode} 模式）：{len(results) - len(bad)}/{len(results)} "
            f"项通过"
        )
        if bad:
            print(
                "未通过：" + ", ".join(f"{r.check_id} {r.name}" for r in bad),
                file=sys.stderr,
            )
        if mode == "workspace":
            print(
                "注意：工作区模式只校验构建输入，不能证明 .bin 打包正确；"
                "流水线里请传 --image 与 --require-image"
            )
    return EXIT_FAIL if bad else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
