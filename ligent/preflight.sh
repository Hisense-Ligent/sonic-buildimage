#!/usr/bin/env bash
#
# ligent/preflight.sh —— 构建环境自检（任务 7.1；design.md 6.2 节，需求 6.1–6.7）
#
# 用法:
#   bash ligent/preflight.sh [--no-mirror-probe] [--no-cleanup] [--help]
#
# 环境变量（全部可选，取值来自 GitHub Actions 仓库变量或 SSH 排障时手工指定）:
#   ARTIFACT_STORE              归档目录，默认 /home/user/ligent-ci/artifacts
#   LIGENT_WORKSPACE            构建工作区，默认本脚本所在目录的上一级
#   LIGENT_RETENTION_COUNT      PF-10 触发清理时保留的归档份数，默认 3
#   LIGENT_BUILD_ESTIMATE_GB    PF-10 单次构建磁盘占用估值，默认取自 preflight_eval.py
#   LIGENT_ARCHIVE_ESTIMATE_GB  PF-10 归档增量估值，默认同上
#
# 退出码: 0 = 全部通过（可含告警）；1 = 至少一项致命失败
#
# ## 为什么是纯 bash
#
# 这个脚本要能在任何时候单独 SSH 上去执行排障（design.md 6.2 节明确要求），
# 因此除了同目录的 preflight_eval.py 之外不引用仓库里的任何内容：把它和
# preflight_eval.py 两个文件 scp 到任意机器都能跑。反过来说，**采集**（本文件）
# 与**判定**（preflight_eval.py）的分工必须严守——阈值数字一律不在 bash 里出现，
# 否则两处阈值早晚漂移，而这种漂移在自检脚本里是最难发现的一类缺陷。
#
# ## set -e 为什么不开
#
# 自检的本质是「把所有问题一次性摊开」。开了 errexit，第一项失败就退出，运维
# 得反复跑 5 遍才能看全。这里改为逐项累加 fatal/warn 计数，最后统一决定退出码。
# 但 -u 与 pipefail 保留：变量拼错或管道中段失败属于脚本自身缺陷，应当立刻暴露。

set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EVAL="${SCRIPT_DIR}/preflight_eval.py"

ARTIFACT_STORE="${ARTIFACT_STORE:-/home/user/ligent-ci/artifacts}"
LIGENT_WORKSPACE="${LIGENT_WORKSPACE:-$(cd -- "${SCRIPT_DIR}/.." && pwd)}"
RETENTION_COUNT="${LIGENT_RETENTION_COUNT:-3}"

MIRROR_PROBE=1
ALLOW_CLEANUP=1

# PF-07：构建期必须能到的三个源。deb.debian.org 是 Debian 包，
# packages.microsoft.com 是上游 sonic 构建脚本会加的 apt 源。
NET_TARGETS=(
  "https://github.com/"
  "https://deb.debian.org/"
  "https://packages.microsoft.com/"
)

# PF-08：容器仓库。未携带 token 时返回 401 属正常鉴权响应，判据是「拿到任何
# 非 000 的状态码即通过」，详见 preflight_eval.py 的 classify_http_status。
ACR_TARGETS=(
  "https://sonicdev-microsoft.azurecr.io/v2/"
  "https://publicmirror.azurecr.io/v2/"
)

CURL_CONNECT_TIMEOUT=10
CURL_TIMEOUT=20
# 单次探测就判定会让 PF-07 频繁误报：本机到 github.com 的链路实测很不稳定
# （连接建立偶尔 >10s，一次 GET 也可能在收完 body 前撞上 --max-time）。
# 瞬时抖动不等于不可达，所以每个目标最多试 3 次，取第一个非 000 的结果。
CURL_ATTEMPTS=3
PULL_TIMEOUT=180
PROBE_IMAGE="hello-world"

FATAL=0
WARN=0
TOTAL=0

usage() {
  sed -n '3,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --no-mirror-probe) MIRROR_PROBE=0 ;;
    --no-cleanup) ALLOW_CLEANUP=0 ;;
    -h|--help) usage; exit 0 ;;
    *) printf '错误：未知参数 %s\n\n' "$1" >&2; usage >&2; exit 1 ;;
  esac
  shift
done

section() { printf '\n==> %s\n' "$1"; }

# 跑一条判定：打印 preflight_eval.py 的输出，并按退出码累加计数。
# 未预料到的退出码（Python 崩了、参数写错）一律归入致命——自检脚本自己出错时
# 不应该悄悄放行。
evaluate() {
  local rc output
  output="$(python3 "$EVAL" "$@" 2>&1)"
  rc=$?
  printf '%s\n' "$output"
  TOTAL=$((TOTAL + 1))
  case "$rc" in
    0) ;;
    2) WARN=$((WARN + 1)) ;;
    *) FATAL=$((FATAL + 1)) ;;
  esac
  return 0
}

# 向上找到最近的存在的祖先目录，让 df 对「还没创建的 Artifact_Store」也能给出
# 它将来会落在的那个文件系统的数字。
nearest_existing() {
  local path="$1"
  while [ -n "$path" ] && [ "$path" != "/" ] && [ ! -d "$path" ]; do
    path="$(dirname -- "$path")"
  done
  printf '%s' "${path:-/}"
}

free_gib() {
  local path
  path="$(nearest_existing "$1")"
  df -Pk -- "$path" 2>/dev/null | awk 'NR==2 {printf "%.2f", $4/1048576}'
}

filesystem_of() {
  local path
  path="$(nearest_existing "$1")"
  df -Pk -- "$path" 2>/dev/null | awk 'NR==2 {print $1" on "$6}'
}

# ---------------------------------------------------------------------------
# 前置条件
# ---------------------------------------------------------------------------

if ! command -v python3 >/dev/null 2>&1; then
  printf '错误：找不到 python3，Preflight 的判定逻辑无法执行\n' >&2
  exit 1
fi
if [ ! -f "$EVAL" ]; then
  printf '错误：找不到判定模块 %s\n' "$EVAL" >&2
  printf '      单独排障时请把 preflight.sh 与 preflight_eval.py 一并复制到同一目录\n' >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 机器快照（需求 6.1：无条件输出，即便全部通过）
# ---------------------------------------------------------------------------

CPU_CORES="$(nproc 2>/dev/null || echo 0)"
MEM_GIB="$(awk '/^MemTotal:/ {printf "%.2f", $2/1048576}' /proc/meminfo 2>/dev/null || echo 0)"
STORE_FREE="$(free_gib "$ARTIFACT_STORE")"
WORK_FREE="$(free_gib "$LIGENT_WORKSPACE")"
STORE_FS="$(filesystem_of "$ARTIFACT_STORE")"
WORK_FS="$(filesystem_of "$LIGENT_WORKSPACE")"

docker info >/dev/null 2>&1
DOCKER_INFO_RC=$?
DOCKER_VERSION="$(docker info --format '{{.ServerVersion}}' 2>/dev/null || true)"
DOCKER_DRIVER="$(docker info --format '{{.Driver}}' 2>/dev/null || true)"
docker ps >/dev/null 2>&1
DOCKER_PS_RC=$?
if [ "$DOCKER_INFO_RC" -eq 0 ]; then
  DOCKER_STATE="running (${DOCKER_VERSION:-unknown}, storage-driver=${DOCKER_DRIVER:-unknown})"
else
  DOCKER_STATE="unavailable (docker info rc=${DOCKER_INFO_RC})"
fi

MIRRORS="$(python3 - <<'PY' 2>/dev/null || true
import json
try:
    with open("/etc/docker/daemon.json", encoding="utf-8") as handle:
        print(",".join(json.load(handle).get("registry-mirrors", [])))
except Exception:
    print("")
PY
)"

printf 'Ligent Preflight_Check —— %s @ %s\n' "$(hostname)" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
section '机器快照（需求 6.1，无条件输出）'
printf '  cpu_cores        : %s\n' "$CPU_CORES"
printf '  memory_total     : %s GiB\n' "$MEM_GIB"
printf '  artifact_store   : %s\n' "$ARTIFACT_STORE"
printf '  store_filesystem : %s（可用 %s GiB）\n' "$STORE_FS" "$STORE_FREE"
printf '  workspace        : %s\n' "$LIGENT_WORKSPACE"
printf '  work_filesystem  : %s（可用 %s GiB）\n' "$WORK_FS" "$WORK_FREE"
printf '  docker_status    : %s\n' "$DOCKER_STATE"
printf '  docker_user      : %s（docker ps rc=%s）\n' "$(id -un)" "$DOCKER_PS_RC"
printf '  registry_mirrors : %s\n' "${MIRRORS:-（未配置）}"
printf '  kernel / os      : %s / %s\n' "$(uname -r)" \
  "$(. /etc/os-release 2>/dev/null && printf '%s' "${PRETTY_NAME:-unknown}")"
printf '  proxy_env        : %s\n' \
  "$(env | grep -Ei '^(http|https|no)_proxy=' | tr '\n' ' ' | sed 's/ $//' || true)"

# ---------------------------------------------------------------------------
# PF-01 .. PF-04 资源
# ---------------------------------------------------------------------------

section '资源检查（PF-01 .. PF-04）'
evaluate cpu --measured "$CPU_CORES"
evaluate memory --measured "$MEM_GIB"
evaluate disk --check-id PF-03 --name artifact_store_free \
  --measured "$STORE_FREE" --path "$ARTIFACT_STORE"
evaluate disk --check-id PF-04 --name workspace_free \
  --measured "$WORK_FREE" --path "$LIGENT_WORKSPACE"

# ---------------------------------------------------------------------------
# PF-05 / PF-06 Docker
# ---------------------------------------------------------------------------

section 'Docker 检查（PF-05 .. PF-06）'
evaluate docker-daemon --rc "$DOCKER_INFO_RC" --version "${DOCKER_VERSION:-}"
if [ "$DOCKER_INFO_RC" -ne 0 ]; then
  # 需求 6.5：输出 Docker 状态检测命令的输出内容（全文，不截断）
  printf '\n--- systemctl status docker --no-pager ---\n'
  systemctl status docker --no-pager 2>&1 || true
  printf -- '--- end ---\n\n'
fi
evaluate docker-perm --rc "$DOCKER_PS_RC" --user "$(id -un)"

# ---------------------------------------------------------------------------
# PF-07 / PF-08 网络
# ---------------------------------------------------------------------------

# 取一个目标的 HTTP 状态码。
#
# 两个刻意的选择：
#
# * **用 HEAD（-I）而不是 GET。** 判据只关心「能不能到 HTTP 层」，不关心页面内容。
#   实测 github.com 的首页 body 会慢到撞上 --max-time，此时 curl 的
#   %{http_code} 归 000——headers 明明已经拿到 200，却被判成不可达。HEAD 不传
#   body，把「链路通不通」和「首页多大、多慢」这两件事解耦。
# * **重试至多 3 次。** 到 github.com 的链路实测存在秒级抖动（TCP 连接偶尔
#   >10s）。单次探测判定会让 PF-07 随机失败，而 Preflight 一旦有误报，运维学到
#   的第一件事就是「失败了再跑一遍」——那等于整个门禁作废。拿到任何非 000 的
#   状态码立即返回，最坏情况才付满 3 次的时间。
http_status() {
  local target="$1" code attempt=1
  while [ "$attempt" -le "$CURL_ATTEMPTS" ]; do
    code="$(curl -s -I -o /dev/null -w '%{http_code}' \
      --connect-timeout "$CURL_CONNECT_TIMEOUT" --max-time "$CURL_TIMEOUT" \
      -- "$target" 2>/dev/null)"
    code="${code:-000}"
    if [ "$code" != "000" ]; then
      printf '%s' "$code"
      return 0
    fi
    attempt=$((attempt + 1))
  done
  printf '000'
}

section '网络可达性（PF-07）'
for target in "${NET_TARGETS[@]}"; do
  host="${target#https://}"; host="${host%%/*}"
  evaluate http --check-id PF-07 --name "net:${host}" \
    --target "$target" --status "$(http_status "$target")"
done

section '容器仓库可达性（PF-08，401 视为可达）'
for target in "${ACR_TARGETS[@]}"; do
  host="${target#https://}"; host="${host%%/*}"
  evaluate http --check-id PF-08 --name "acr:${host}" \
    --target "$target" --status "$(http_status "$target")"
done

# ---------------------------------------------------------------------------
# PF-09 registry mirror 探针
# ---------------------------------------------------------------------------

section 'registry mirror 可用性（PF-09）'
if [ "$MIRROR_PROBE" -eq 0 ]; then
  printf '[SKIP] PF-09 registry_mirror        : 已由 --no-mirror-probe 跳过\n'
elif [ "$DOCKER_PS_RC" -ne 0 ]; then
  # 没有 docker 权限时这个探针必然失败，且失败原因与 mirror 无关。
  # 报成 PF-09 失败会把排障指向错误的方向，所以显式跳过并说明依赖。
  printf '[SKIP] PF-09 registry_mirror        : 依赖 PF-06，当前用户无 docker 权限，跳过\n'
else
  PULL_START="$(date +%s.%N)"
  PULL_OUTPUT="$(timeout "$PULL_TIMEOUT" docker pull "$PROBE_IMAGE" 2>&1)"
  PULL_RC=$?
  PULL_ELAPSED="$(awk -v a="$PULL_START" -v b="$(date +%s.%N)" 'BEGIN {printf "%.2f", b-a}')"
  evaluate mirror --rc "$PULL_RC" --mirrors "${MIRRORS:-}" --elapsed "$PULL_ELAPSED"
  if [ "$PULL_RC" -ne 0 ]; then
    printf '\n--- docker pull %s 输出 ---\n%s\n--- end ---\n\n' \
      "$PROBE_IMAGE" "$PULL_OUTPUT"
    printf -- '--- /etc/docker/daemon.json ---\n'
    cat /etc/docker/daemon.json 2>&1 || true
    printf -- '\n--- end ---\n\n'
  fi
fi

# ---------------------------------------------------------------------------
# PF-10 磁盘容量组合门禁
# ---------------------------------------------------------------------------

# 归档清理：只在 PF-10 判失败后调用。刻意保守——只删 Artifact_Store 一层之下、
# 名字匹配 build_id 形态（<YYYYmmdd>T<HHMMSS>Z-...）的真实目录，保留最新 N 个；
# 符号链接（latest）与任何其他文件一概不碰。ARTIFACT_STORE 不存在时直接返回。
cleanup_archives() {
  local store="$1" keep="$2" dirs=() victim
  [ -d "$store" ] || return 0
  while IFS= read -r line; do
    [ -n "$line" ] && dirs+=("$line")
  done < <(find "$store" -mindepth 1 -maxdepth 1 -type d \
    -regextype posix-extended -regex '.*/[0-9]{8}T[0-9]{6}Z-.*' \
    -printf '%f\n' 2>/dev/null | sort)
  if [ "${#dirs[@]}" -le "$keep" ]; then
    printf '  归档清理：现有 %s 份归档，未超过保留数 %s，无可删除项\n' \
      "${#dirs[@]}" "$keep"
    return 0
  fi
  local remove=$(( ${#dirs[@]} - keep ))
  printf '  归档清理：现有 %s 份，保留最新 %s 份，删除最早 %s 份\n' \
    "${#dirs[@]}" "$keep" "$remove"
  local i=0
  while [ "$i" -lt "$remove" ]; do
    victim="${store}/${dirs[$i]}"
    printf '    - rm -rf %s\n' "$victim"
    rm -rf -- "$victim"
    i=$((i + 1))
  done
}

section '磁盘容量门禁（PF-10，不足先清理再判定）'
CAP_ARGS=(capacity --free "$STORE_FREE")
[ -n "${LIGENT_BUILD_ESTIMATE_GB:-}" ] && CAP_ARGS+=(--build-estimate "$LIGENT_BUILD_ESTIMATE_GB")
[ -n "${LIGENT_ARCHIVE_ESTIMATE_GB:-}" ] && CAP_ARGS+=(--archive-estimate "$LIGENT_ARCHIVE_ESTIMATE_GB")

python3 "$EVAL" "${CAP_ARGS[@]}" >/dev/null 2>&1
CAP_RC=$?
if [ "$CAP_RC" -eq 0 ]; then
  evaluate "${CAP_ARGS[@]}"
elif [ "$ALLOW_CLEANUP" -eq 0 ]; then
  printf '  容量不足，但已由 --no-cleanup 禁止清理，直接判定\n'
  evaluate "${CAP_ARGS[@]}"
else
  printf '  容量不足，先触发归档清理（需求 8.6）\n'
  cleanup_archives "$ARTIFACT_STORE" "$RETENTION_COUNT"
  STORE_FREE_AFTER="$(free_gib "$ARTIFACT_STORE")"
  printf '  清理后可用：%s GiB（清理前 %s GiB）\n' "$STORE_FREE_AFTER" "$STORE_FREE"
  CAP_ARGS=(capacity --free "$STORE_FREE_AFTER" --cleaned)
  [ -n "${LIGENT_BUILD_ESTIMATE_GB:-}" ] && CAP_ARGS+=(--build-estimate "$LIGENT_BUILD_ESTIMATE_GB")
  [ -n "${LIGENT_ARCHIVE_ESTIMATE_GB:-}" ] && CAP_ARGS+=(--archive-estimate "$LIGENT_ARCHIVE_ESTIMATE_GB")
  evaluate "${CAP_ARGS[@]}"
fi

# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

section '汇总'
printf '  检查项 %s 个：致命 %s，告警 %s\n' "$TOTAL" "$FATAL" "$WARN"
if [ "$FATAL" -gt 0 ]; then
  printf '  结论：Preflight 失败，作业应终止（上方 [FAIL] 行给出实测值与所需下限）\n'
  exit 1
fi
if [ "$WARN" -gt 0 ]; then
  printf '  结论：Preflight 通过（含 %s 项告警，按需求 6.4 继续构建）\n' "$WARN"
else
  printf '  结论：Preflight 全部通过\n'
fi
exit 0
