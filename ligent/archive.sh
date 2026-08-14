#!/usr/bin/env bash
#
# ligent/archive.sh —— 产物归档与 latest 链接（任务 10.3；design.md 5.5 / 6.5 节）
#
# 用法:
#   bash ligent/archive.sh [--image PATH] [--log PATH] [--verify PATH]
#                          [--build-id ID] [--store DIR] [--no-manifest] [--help]
#
# 环境变量（流水线里由 GitHub Actions 仓库变量注入，SSH 排障时可手工给）:
#   ARTIFACT_STORE / LIGENT_ARTIFACT_STORE  归档根目录（必需）
#   BUILD_ID              构建标识；未给出时由 build_identity.py 现算
#   GITHUB_SHA            完整提交 SHA（算 BUILD_ID 与写 manifest 用）
#   GITHUB_REF_NAME       分支名
#   GITHUB_EVENT_NAME     触发事件，进 manifest 的 trigger
#   PLATFORM              目标平台，默认 vs
#   BUILD_STARTED_AT      构建开始时间，进 manifest 的 started_at
#   SONIC_BUILD_JOBS / SONIC_CONFIG_MAKE_JOBS / RUNNER_NAME   进 manifest
#   GITHUB_STEP_SUMMARY   作业摘要文件（需求 8.9）；不在 Actions 里时退化为 stdout
#
# 退出码: 0 = 归档完成；1 = 参数/输入非法或复制失败（已产生文件一律保留）
#
# ## set -e 为什么开（与 preflight.sh 相反）
#
# preflight.sh 的目标是「把所有问题一次摊开」，所以不开 errexit。归档正好相反：
# 它是一串有依赖关系的写操作（建目录 → 复制镜像 → 复制日志 → 写 manifest →
# 切 latest），前一步失败后继续做下一步只会产出一个「看起来完整、实际缺镜像」的
# 归档目录，而这种半成品比明确失败危险得多。所以这里 errexit + pipefail 全开，
# 任何一步失败立刻停下。
#
# ## 失败时不清理现场（需求 8.8）
#
# 没有 trap 去 rm 半成品目录，这是刻意的。复制 2 GiB 镜像失败最常见的原因是磁盘
# 满或权限不对，此时最需要的信息恰恰是「已经写进去多少、目录属主是谁」。清掉现场
# 等于把证据一起删了。代价是 Artifact_Store 里可能留下不完整的目录——由
# retention.py 按 build_id 排序在后续构建中自然回收（它不检查目录内容完整性，
# 半成品和成品一样按时间序淘汰）。
#
# ## latest 链接为什么绕一道临时名
#
# `ln -sfn` 对已存在的**符号链接**是原子替换，但对已存在的**目录**会把链接建到
# 目录里面去（变成 $store/latest/<target>），一旦有人手工 mkdir 过 latest 就会
# 静默错位。所以这里统一走「建到 .latest.tmp → mv -T 覆盖」：
#
#   * `mv -T` 明确「把目标当普通文件覆盖」，不做「移动进目录」的推断；
#   * rename(2) 是原子的，外部观察者看到的 latest 要么是旧目标要么是新目标，
#     不存在指向不存在目录的瞬间（design.md 5.5 节）。
#
# 链接用**相对**目标（只写目录名），这样整个 Artifact_Store 可以整体搬迁或
# 换挂载点而不失效。

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BUILD_IDENTITY="${SCRIPT_DIR}/build_identity.py"
WRITE_MANIFEST="${SCRIPT_DIR}/write_manifest.py"

STORE="${ARTIFACT_STORE:-${LIGENT_ARTIFACT_STORE:-}}"
BUILD_ID="${BUILD_ID:-}"
PLATFORM="${PLATFORM:-vs}"

# 默认路径相对仓库根（脚本所在目录的上一级），与 workflow 的工作目录一致。
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
IMAGE="${REPO_ROOT}/target/sonic-${PLATFORM}.bin"
BUILD_LOG="${REPO_ROOT}/build.log"
VERIFY_TXT="${REPO_ROOT}/brand-verify.txt"
VERIFY_JSON=""
WRITE_MANIFEST_ENABLED=1

# 需求 8.4 要求日志必须归档，但 brand-verify.txt 在冒烟模式下可能不存在。
# 二者的缺失处置不同：日志缺失只告警（构建可能是在 tee 之前就失败的），
# 镜像缺失直接失败（没有镜像的归档没有意义）。
usage() {
  sed -n '3,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --image) IMAGE="$2"; shift ;;
    --log) BUILD_LOG="$2"; shift ;;
    --verify) VERIFY_TXT="$2"; shift ;;
    --verify-json) VERIFY_JSON="$2"; shift ;;
    --build-id) BUILD_ID="$2"; shift ;;
    --store) STORE="$2"; shift ;;
    --platform) PLATFORM="$2"; shift ;;
    --no-manifest) WRITE_MANIFEST_ENABLED=0 ;;
    -h|--help) usage; exit 0 ;;
    *) printf '错误：未知参数 %s\n\n' "$1" >&2; usage >&2; exit 1 ;;
  esac
  shift
done

die() { printf '错误：%s\n' "$1" >&2; exit 1; }

# 摘要输出（需求 8.9）。不在 Actions 环境里时写 stdout，保证 SSH 手工执行也能
# 看到同样的内容——排障时最需要的就是「归档去哪了、SHA 是多少」这两行。
summary() {
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    printf '%s\n' "$1" >>"$GITHUB_STEP_SUMMARY"
  else
    printf '%s\n' "$1"
  fi
}

# ---------------------------------------------------------------------------
# 前置检查
# ---------------------------------------------------------------------------

[ -n "$STORE" ] || die "未指定 Artifact_Store：用 --store 或设置 \$ARTIFACT_STORE"
[ -f "$IMAGE" ] || die "镜像不存在：$IMAGE
  处置：确认构建已成功产出 target/sonic-${PLATFORM}.bin，或用 --image 指定路径"
command -v python3 >/dev/null 2>&1 || die "找不到 python3（写 manifest 与算 build_id 需要）"

if [ -z "$BUILD_ID" ]; then
  BUILD_ID="$(python3 "$BUILD_IDENTITY")" \
    || die "生成 build_id 失败：检查 \$GITHUB_REF_NAME 与 \$GITHUB_SHA 是否已设置"
fi

DEST="${STORE}/${BUILD_ID}"

printf '==> 归档 %s\n' "$BUILD_ID"
printf '  store    : %s\n' "$STORE"
printf '  build_id : %s\n' "$BUILD_ID"
printf '  image    : %s\n' "$IMAGE"

mkdir -p -- "$DEST" || die "无法创建归档目录 $DEST（检查父目录权限与磁盘空间）"
# 绝对路径用于摘要（需求 8.9）。$DEST 可能由相对的 --store 拼出，
# 摘要里给相对路径等于没给。
DEST_ABS="$(cd -- "$DEST" && pwd)"

# ---------------------------------------------------------------------------
# 复制产物（需求 8.1、8.4；失败即非零退出并保留现场，需求 8.8）
# ---------------------------------------------------------------------------

# 镜像先复制到 .part 再改名：2 GiB 的 cp 中途被 kill（cancel-in-progress 会）
# 会留下一个尺寸不足的 sonic-vs.bin，它的 sha256 与 manifest 里的不一致但文件名
# 完全正常，是最难发现的一类损坏。改名让「有正式文件名」等价于「复制已完成」。
IMAGE_NAME="$(basename -- "$IMAGE")"
printf '  复制镜像 → %s/%s\n' "$DEST_ABS" "$IMAGE_NAME"
if ! cp -f -- "$IMAGE" "${DEST}/${IMAGE_NAME}.part"; then
  printf '错误：复制镜像失败：%s → %s\n' "$IMAGE" "$DEST" >&2
  printf '  已产生的文件一律保留供排查（需求 8.8）：\n' >&2
  ls -la -- "$DEST" >&2 || true
  df -h -- "$STORE" >&2 || true
  exit 1
fi
mv -f -- "${DEST}/${IMAGE_NAME}.part" "${DEST}/${IMAGE_NAME}"

# 日志与品牌校验输出：缺失只告警。构建在 `| tee build.log` 之前就失败（例如
# make init 挂了）时 build.log 可能根本不存在，此时让归档整体失败会掩盖真正的
# 失败原因。
for extra in "$BUILD_LOG" "$VERIFY_TXT"; do
  name="$(basename -- "$extra")"
  if [ ! -f "$extra" ]; then
    printf '  警告：%s 不存在，跳过（构建可能在产出它之前就失败了）\n' "$extra" >&2
    continue
  fi
  printf '  复制 %s → %s/%s\n' "$name" "$DEST_ABS" "$name"
  if ! cp -f -- "$extra" "${DEST}/${name}"; then
    printf '错误：复制 %s 失败，已产生文件保留供排查\n' "$extra" >&2
    ls -la -- "$DEST" >&2 || true
    exit 1
  fi
done

# ---------------------------------------------------------------------------
# 镜像校验和（需求 8.3、8.9）
# ---------------------------------------------------------------------------

# 对**归档后**的副本算 sha256 而不是对源文件算：这样这个值同时验证了「复制没有
# 出错」。对源文件算再写进 manifest 的话，一次静默的坏块会让 manifest 与归档内容
# 不一致，而 manifest 本来的用途就是校验归档内容。
printf '  计算 SHA256（约 2 GiB，需要几十秒）\n'
IMAGE_SHA256="$(sha256sum -- "${DEST}/${IMAGE_NAME}" | cut -d' ' -f1)"
IMAGE_SIZE="$(stat -c '%s' -- "${DEST}/${IMAGE_NAME}")"
printf '  sha256   : %s\n' "$IMAGE_SHA256"
printf '  size     : %s bytes\n' "$IMAGE_SIZE"

# ---------------------------------------------------------------------------
# manifest.json（需求 8.3）
# ---------------------------------------------------------------------------

if [ "$WRITE_MANIFEST_ENABLED" -eq 1 ]; then
  MANIFEST_ARGS=(
    --dest "$DEST"
    --build-id "$BUILD_ID"
    --platform "$PLATFORM"
    --image-file "$IMAGE_NAME"
    --image-sha256 "$IMAGE_SHA256"
    --image-size-bytes "$IMAGE_SIZE"
  )
  # 品牌校验结果优先用 --json（结构精确），否则解析已归档的文本输出。
  if [ -n "$VERIFY_JSON" ] && [ -f "$VERIFY_JSON" ]; then
    MANIFEST_ARGS+=(--brand-verify "$VERIFY_JSON")
  elif [ -f "${DEST}/$(basename -- "$VERIFY_TXT")" ]; then
    MANIFEST_ARGS+=(--brand-verify-text "${DEST}/$(basename -- "$VERIFY_TXT")")
  fi
  if ! python3 "$WRITE_MANIFEST" "${MANIFEST_ARGS[@]}"; then
    printf '错误：写 manifest.json 失败，已归档文件保留供排查\n' >&2
    ls -la -- "$DEST" >&2 || true
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# latest 链接（需求 8.5）
# ---------------------------------------------------------------------------

LATEST_TMP="${STORE}/.latest.tmp"
if ! (ln -sfn -- "$BUILD_ID" "$LATEST_TMP" && mv -T -- "$LATEST_TMP" "${STORE}/latest"); then
  rm -f -- "$LATEST_TMP" || true
  printf '错误：更新 latest 链接失败（归档本身已完成：%s）\n' "$DEST_ABS" >&2
  exit 1
fi
printf '  latest   : %s -> %s\n' "${STORE}/latest" "$(readlink -- "${STORE}/latest")"

# ---------------------------------------------------------------------------
# 作业摘要（需求 8.9）
# ---------------------------------------------------------------------------

summary "### Ligent 归档"
summary ""
summary "| 项 | 值 |"
summary "| --- | --- |"
summary "| 归档目录 | \`${DEST_ABS}\` |"
summary "| 镜像文件 | \`${IMAGE_NAME}\` |"
summary "| 镜像 SHA256 | \`${IMAGE_SHA256}\` |"
summary "| 镜像大小 | ${IMAGE_SIZE} bytes |"
summary "| build_id | \`${BUILD_ID}\` |"
summary "| latest | \`${STORE}/latest\` -> \`${BUILD_ID}\` |"

# 与 preflight.sh / verify_brand.py 同一行格式：[ 状态 ] ID 名称 : 细节
printf '\n[ OK ] AR-03 archive             : %s\n' "$DEST_ABS"
printf '[ OK ] AR-04 image_sha256       : %s\n' "$IMAGE_SHA256"
printf '[ OK ] AR-05 latest_link        : latest -> %s\n' "$BUILD_ID"
ls -la -- "$DEST_ABS"
exit 0
