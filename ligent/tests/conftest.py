"""共享测试固件：ligent-sonic-cicd-rebrand

设计要点（见 design.md「测试策略 / 生成器设计」）：

上游 sonic-buildimage 有数 GB，属性测试跑 100 轮不可能每轮复制整棵树。
因此 ``tmp_repo`` 只复制 4 个 Brand_Touchpoints 文件与它们所在的目录，
内容取自 ``ligent/tests/fixtures/upstream/`` 下签入的**逐字节副本**。
这样单轮 I/O 只有约 47 KB，同时锚点匹配测的仍是真实上游文本。

后续任务会在此基础上扩展出三类工作区状态：

* ``WorkspaceState.UPSTREAM``  —— 上游基线（本任务已实现）
* ``WorkspaceState.REBRANDED`` —— 已改造（任务 4 起，由 rebrand.py 施加）
* ``WorkspaceState.DRIFTED``   —— 锚点被破坏（任务 4.11，由 generators.upstream_drift 施加）
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------

TESTS_DIR = Path(__file__).resolve().parent
LIGENT_DIR = TESTS_DIR.parent
REPO_ROOT = LIGENT_DIR.parent
FIXTURES_UPSTREAM = TESTS_DIR / "fixtures" / "upstream"
BRAND_ASSETS_DIR = LIGENT_DIR / "brand"


class WorkspaceState(Enum):
    """tmp_repo 可构造的三类工作区状态。"""

    UPSTREAM = "upstream"
    REBRANDED = "rebranded"
    DRIFTED = "drifted"


@dataclass(frozen=True)
class UpstreamFile:
    """一个触点文件的固件描述。

    ``fixture_name`` 与 ``repo_path`` 分开，是为了让 fixtures 目录扁平且文件名
    可追溯，同时仍能在临时仓库里还原成真实的仓库相对路径。
    """

    fixture_name: str
    repo_path: str
    mode: int
    sha256: str


# 上游基线：sonic-buildimage @ db6796e994099bc822fde5806052f378b7df0a1b
# sha256 由构建服务器 CI 工作目录实测；mode 取自 git index（100644 / 100755）。
# 上游 rebase 后若这些 hash 变化，说明触点文本可能漂移，需人工复核锚点。
UPSTREAM_FILES: tuple[UpstreamFile, ...] = (
    UpstreamFile(
        fixture_name="motd",
        repo_path="files/image_config/environment/motd",
        mode=0o644,
        sha256="3fbdd0d0074f546934194fe76dfe487bfcf07cd6119b98d1f6509e6a1b657c10",
    ),
    UpstreamFile(
        fixture_name="init_cfg.json.j2",
        repo_path="files/build_templates/init_cfg.json.j2",
        mode=0o644,
        sha256="2adf810fda82a6dc421e380076e9ec86f02c58de3859bca410cfa269fe0e5cb4",
    ),
    UpstreamFile(
        fixture_name="install.sh",
        repo_path="installer/install.sh",
        mode=0o755,
        sha256="aa16dec77d2c4b2c56f803eec4d1cf0c93cb49840c1b5747b5c73ad8285b5109",
    ),
    UpstreamFile(
        fixture_name="default_platform.conf",
        repo_path="installer/default_platform.conf",
        mode=0o755,
        sha256="ba7ea0212d209725b23038aa95761998740c73cd44acbb303d2e2cca8fb1287c",
    ),
)

# fixture 文件名 -> 仓库相对路径，供测试与生成器按任一侧寻址
FIXTURE_TO_REPO_PATH = {f.fixture_name: f.repo_path for f in UPSTREAM_FILES}
REPO_PATH_TO_FIXTURE = {f.repo_path: f.fixture_name for f in UPSTREAM_FILES}
UPSTREAM_SHA256 = {f.repo_path: f.sha256 for f in UPSTREAM_FILES}


# ---------------------------------------------------------------------------
# 底层工具函数
# ---------------------------------------------------------------------------


def sha256_of(path: Path) -> str:
    """文件内容的 sha256（十六进制小写）。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_upstream_bytes() -> dict[str, bytes]:
    """一次性读入全部固件内容，避免 100 轮属性测试反复读盘。"""
    payload: dict[str, bytes] = {}
    missing: list[str] = []
    for spec in UPSTREAM_FILES:
        src = FIXTURES_UPSTREAM / spec.fixture_name
        if not src.is_file():
            missing.append(str(src))
            continue
        payload[spec.repo_path] = src.read_bytes()
    if missing:
        raise AssertionError(
            "缺少上游固件文件，请重新从上游签入逐字节副本：\n  "
            + "\n  ".join(missing)
        )
    return payload


def materialize_upstream(dest: Path, upstream_bytes: dict[str, bytes]) -> Path:
    """把 4 个触点文件按真实仓库相对路径写入 ``dest``，并还原上游权限位。"""
    for spec in UPSTREAM_FILES:
        target = dest / spec.repo_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(upstream_bytes[spec.repo_path])
        target.chmod(spec.mode)
    return dest


def snapshot(root: Path) -> dict[str, bytes]:
    """递归文件内容清单，键为相对 ``root`` 的 POSIX 路径。

    供「作用域受限」与「check 无副作用」两类属性做逐字节比对。
    """
    result: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result


# ---------------------------------------------------------------------------
# pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def upstream_bytes() -> dict[str, bytes]:
    """签入固件的内容缓存，键为仓库相对路径。"""
    return _load_upstream_bytes()


@pytest.fixture(scope="session")
def brand_assets_dir() -> Path:
    """Brand_Assets 目录（``ligent/brand/``），任务 2 起填充内容。"""
    return BRAND_ASSETS_DIR


@pytest.fixture
def tmp_repo(tmp_path: Path, upstream_bytes: dict[str, bytes]) -> Path:
    """一个最小可用的临时工作区，处于上游基线状态。

    只含 4 个 Brand_Touchpoints 文件与它们所在目录，不含其余仓库内容。
    后续任务在此之上叠加「已改造」与「锚点被破坏」两种状态。
    """
    root = tmp_path / "repo"
    root.mkdir()
    return materialize_upstream(root, upstream_bytes)


@pytest.fixture
def make_tmp_repo(tmp_path: Path, upstream_bytes: dict[str, bytes]):
    """工厂版 ``tmp_repo``，用于同一个用例里需要两个独立工作区的属性。

    例如 Property 7（品牌名称变更完全覆盖）要比较
    ``apply(apply(repo, b1), b2)`` 与 ``apply(repo, b2)`` 两条路径的结果。
    """
    counter = {"n": 0}

    def _factory(state: WorkspaceState = WorkspaceState.UPSTREAM) -> Path:
        if state is not WorkspaceState.UPSTREAM:
            # 已改造 / 锚点被破坏两种状态依赖 rebrand.py 与 generators，
            # 分别在任务 4 与 4.13 落地。此处显式失败，避免静默返回基线状态
            # 让属性测试误判通过。
            raise NotImplementedError(
                f"工作区状态 {state.value} 尚未实现（见任务 4 / 4.13）"
            )
        counter["n"] += 1
        root = tmp_path / f"repo-{counter['n']}"
        root.mkdir()
        return materialize_upstream(root, upstream_bytes)

    return _factory
