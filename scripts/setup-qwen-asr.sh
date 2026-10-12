#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT_DIR/.venv-qwen-asr"
MODELS_DIR="${VOICE_IME_QWEN_ASR_MODELS_DIR:-$ROOT_DIR/vendor/models/qwen3-asr}"
# 共享模型缓存：分档真实数据统一放这里，仓库 vendor 下只建符号链接（既有
# vendor 路径语义不变，引擎/验收/REUSE 判据照旧读 vendor 路径）。跨克隆复用，
# 卸载或更换克隆目录不丢 6.2GB 权重；VOICE_IME_MODEL_CACHE 可自定义位置。
MODEL_CACHE="${VOICE_IME_MODEL_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/ibus-voice-ime/models}"
[[ "$MODEL_CACHE" == /* ]] || MODEL_CACHE="$ROOT_DIR/$MODEL_CACHE"
ENABLE_NOW="${VOICE_IME_ENABLE_QWEN_ASR:-1}"
BOOTSTRAP="$(command -v python3)"
CHECK="$ROOT_DIR/scripts/qwen-preflight.py"
FORCE=0 VERIFY_ONLY=0 PROXY=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    --verify-only) VERIFY_ONLY=1; shift ;;
    --proxy) [[ $# -ge 2 ]] || { echo "--proxy 需要参数" >&2; exit 2; }; PROXY="$2"; shift 2 ;;
    --proxy=*) PROXY="${1#--proxy=}"; shift ;;
    -h|--help)
      echo "用法：$0 [--verify-only] [--force] [--proxy http://127.0.0.1:7890]"
      echo "默认自动准备仓库内 Python 3.12（含头文件）及 uv；不安装系统包。--force 仅跳过 GPU 门控，无 GPU 时只准备不启用。"
      echo "--verify-only 不下载/不改配置：静态检查 + CUDA/Triton + 模型短音频推理。"
      exit 0 ;;
    *) echo "未知参数：$1（查看 --help）" >&2; exit 2 ;;
  esac
done
if [[ -n "$PROXY" ]]; then
  export http_proxy="$PROXY" https_proxy="$PROXY" HTTP_PROXY="$PROXY" HTTPS_PROXY="$PROXY"
fi
# 只读验收（--verify-only）在任何下载集合/默认模型变量解析之前完成：
# 验收读取的是实际持久化的 sidecar 配置，不应被下载集合变量阻断（红队发现）。
TIMEOUT="${VOICE_IME_QWEN_ASR_VERIFY_TIMEOUT:-180}"
# 下载到缓存内的临时目录（.part-*）；中断/失败时由 finish() 统一清理。
PART_DIRS=()
# 共享缓存分档锁：安装锁（fd 9）是每克隆的，挡不住跨克隆对同一共享缓存的
# TOCTOU（红队实测并发 mv 嵌套）；分档 flock 在缓存目录上跨克隆串行化。
CACHE_LOCK_FD=8
cache_lock() {  # <size>
  mkdir -p -- "$MODEL_CACHE"
  eval "exec $CACHE_LOCK_FD>\"\$MODEL_CACHE/.lock-Qwen3-ASR-\$1\""
  flock "$CACHE_LOCK_FD" || { echo "无法锁定共享缓存（$MODEL_CACHE）。" >&2; exit 1; }
}
cache_unlock() {
  flock -u "$CACHE_LOCK_FD" 2>/dev/null || true
  eval "exec $CACHE_LOCK_FD>&-" 2>/dev/null || true
}

# 验收 receipt：记录「何时用哪套解释器/依赖/模型通过了 smoke 验收」，doctor
# 匹配当前实际（reqs sha256/解释器/模型清单）时把 WARN 降为 INFO。纯附加产物：
# 写入失败只提示，绝不阻断验收成功。
write_verify_receipt() {  # <python> <model_dir>
  local receipt="$VENV/verify-receipt.json"
  if [[ ! -d "$VENV" || -L "$VENV" ]]; then
    echo "验收 receipt 未写入：$VENV 不是常规目录（--verify-only 不创建 venv）。"
    return 0
  fi
  if ! "$BOOTSTRAP" - "$receipt" "$1" "$2" "$ROOT_DIR/requirements-qwen-asr.txt" \
      "$ROOT_DIR" "${GPU_BRIEF:-unknown}" "${VOICE_IME_QWEN_ASR_DTYPE:-bfloat16}" <<'PY'
import hashlib, json, os, sys, time
receipt_path, python, model_path, reqs_path, root_dir, gpu, dtype = sys.argv[1:8]
def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()
files = {}
for name in sorted(os.listdir(model_path)):
    full = os.path.join(model_path, name)
    if os.path.isfile(full):
        status = os.stat(full)
        # mtime_ns：秒级 mtime 挡不住同秒篡改（红队实测同长度替换+还原秒
        # 可骗过 INFO）；保留 mtime 字段仅为兼容旧 receipt（旧 receipt 会因
        # 无 mtime_ns 匹配失败 → 恢复 WARN，属安全方向的失效）。
        files[name] = {'size': status.st_size, 'mtime': int(status.st_mtime),
                       'mtime_ns': status.st_mtime_ns}
receipt = {
    'stage': 'smoke', 'python': python, 'reqs_sha256': sha256(reqs_path),
    'model_path': model_path, 'model_files': files, 'gpu': gpu, 'dtype': dtype,
    'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'root_dir': root_dir,
}
tmp = receipt_path + '.tmp'
with open(tmp, 'w', encoding='utf-8') as stream:
    json.dump(receipt, stream, ensure_ascii=False, indent=2, sort_keys=True)
os.replace(tmp, receipt_path)
PY
  then
    echo "⚠ 验收 receipt 写入失败（不影响验收结果）：$receipt" >&2
    return 0
  fi
  echo "验收 receipt 已写入：$receipt"
}

if [[ $VERIFY_ONLY -eq 1 ]]; then
  # Match the launcher's persisted overrides, without sourcing shell syntax or
  # ever modifying environment.d / JSON. Values containing spaces remain safe.
  if [[ -f "$ROOT_DIR/scripts/env-file-load.sh" ]]; then
    eval "$(bash "$ROOT_DIR/scripts/env-file-load.sh" "$HOME/.config/environment.d/ibus-voice-ime.conf" | grep -E '^export (VOICE_IME_[A-Z0-9_]+|PYTORCH_CUDA_ALLOC_CONF)=' || true)"
  fi
  TARGETS="$(PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" "$BOOTSTRAP" -c 'from ibus_voice_ime.asr import qwen_asr_runtime as r; print(r._python()); print(r.model_id())')"
  mapfile -t CHECK_TARGETS <<< "$TARGETS"
  [[ ${#CHECK_TARGETS[@]} -eq 2 ]] || { echo "无法解析实际 sidecar 配置" >&2; exit 1; }
  GPU_STATE="" GPU_REASON="" GPU_ACTION="" GPU_BRIEF=""
  if [[ -x "$ROOT_DIR/scripts/gpu-probe.sh" ]]; then
    eval "$("$ROOT_DIR/scripts/gpu-probe.sh" --env)"
  fi
  if "$BOOTSTRAP" "$CHECK" --python "${CHECK_TARGETS[0]}" --stage smoke --model "${CHECK_TARGETS[1]}" --timeout "$TIMEOUT"; then
    write_verify_receipt "${CHECK_TARGETS[0]}" "${CHECK_TARGETS[1]}"
    exit 0
  fi
  exit 1
fi

# 未指定集合时保持旧行为：两档均下载。显式空集合与未知分档须在任何安装前拒绝。
QWEN_SIZES=()
SIZES_VALUE="${VOICE_IME_QWEN_ASR_SIZES-0.6B 1.7B}"
read -r -a QWEN_SIZES <<< "${SIZES_VALUE//$'\n'/ }" || true
[[ ${#QWEN_SIZES[@]} -gt 0 ]] || { echo "无效的 VOICE_IME_QWEN_ASR_SIZES：集合不能为空（支持 0.6B / 1.7B）" >&2; exit 2; }
for size in "${QWEN_SIZES[@]}"; do
  case "$size" in
    0.6B|1.7B) ;;
    *) echo "无效的 VOICE_IME_QWEN_ASR_SIZES：$size（支持空格分隔的 0.6B / 1.7B）" >&2; exit 2 ;;
  esac
done
DEFAULT_MODEL="${VOICE_IME_QWEN_ASR_MODEL:-1.7b}"
case "${DEFAULT_MODEL,,}" in
  0.6|0.6b|qwen3-asr-0.6b) DEFAULT_MODEL=0.6b ;;
  1.7|1.7b|qwen3-asr-1.7b) DEFAULT_MODEL=1.7b ;;
  *) echo "无效的 VOICE_IME_QWEN_ASR_MODEL：$DEFAULT_MODEL（支持 0.6b / 1.7b）" >&2; exit 2 ;;
esac
DEFAULT_MODEL_DIR="$MODELS_DIR/Qwen3-ASR-$( [[ "$DEFAULT_MODEL" == "1.7b" ]] && echo 1.7B || echo 0.6B )"

# Single writer: init/doctor/manual installation must not mutate the same venv
# concurrently. Kernel lock is released even on interruption; no stale PID file.
command -v flock >/dev/null || { echo "缺少 flock（util-linux），无法安全串行安装。" >&2; exit 1; }
[[ ! -L "$ROOT_DIR/.qwen-asr-setup.lock" ]] || { echo "拒绝使用符号链接安装锁" >&2; exit 1; }
exec 9>"$ROOT_DIR/.qwen-asr-setup.lock"
flock -n 9 || { echo "另一个 Qwen 安装/修复正在运行，请等待完成。" >&2; exit 1; }

# pip index: torch CUDA wheels are multi-GB; the default TUNA mirror is a full
# PyPI replica and avoids crawling overseas PyPI. Override or disable per run:
#   VOICE_IME_PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple ./scripts/setup-qwen-asr.sh
#   VOICE_IME_PIP_INDEX_URL=default ./scripts/setup-qwen-asr.sh   # 官方 PyPI
PIP_INDEX_URL="${VOICE_IME_PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
[[ "$PIP_INDEX_URL" != "default" ]] || PIP_INDEX_URL="https://pypi.org/simple"
# Explicit URL even when the caller exported PIP_INDEX_URL; ignore unrelated
# extra indices/pip.conf without editing any user configuration.
export PIP_CONFIG_FILE=/dev/null PIP_INDEX_URL
unset PIP_EXTRA_INDEX_URL
PIP_INDEX_ARGS=(--index-url "$PIP_INDEX_URL" --timeout 30 --retries 2)
echo "pip 包源：$PIP_INDEX_URL（VOICE_IME_PIP_INDEX_URL=default 使用官方 PyPI；不是 Python 下载源）"

# Crash recovery journal: SIGKILL/power loss during an install can leave the
# user's original venv parked in a .bak- dir with a half-built replacement in
# place. The EXIT trap cannot help there, so the rename is journaled and the
# next run restores the original before doing anything else.
STATE_FILE="$ROOT_DIR/.qwen-asr-setup.state"
recover_interrupted() {
  [[ ! -L "$STATE_FILE" ]] || { echo "拒绝使用符号链接安装状态文件：$STATE_FILE" >&2; exit 1; }
  [[ -f "$STATE_FILE" ]] || return 0
  [[ ! -L "$VENV" ]] || { echo "拒绝恢复到符号链接 venv：$VENV" >&2; exit 1; }
  local recorded
  recorded="$(sed -n 's/^backup=//p' "$STATE_FILE" | head -n1)"
  # A journal is data, not authority to move arbitrary user directories.
  [[ "$recorded" == "$ROOT_DIR"/.venv-qwen-asr.bak-* && "${recorded##*/}" != *..* && ! -L "$recorded" && "$(dirname "$recorded")" == "$ROOT_DIR" ]] || {
    echo "拒绝恢复非预期/符号链接备份：$recorded（保留状态文件供检查）" >&2; exit 1;
  }
  if [[ -n "$recorded" && -d "$recorded" ]]; then
    if [[ -d "$VENV" && ! -L "$VENV" ]]; then
      local failed="$ROOT_DIR/.venv-qwen-asr.failed-crash-$(date +%Y%m%d-%H%M%S)-$$"
      mv -- "$VENV" "$failed"
      echo "上次安装被中断：半成品环境移至 $failed" >&2
    elif [[ -e "$VENV" ]]; then
      echo "venv 路径异常（$VENV），请手动检查后删除状态文件 $STATE_FILE" >&2
      exit 1
    fi
    mv -- "$recorded" "$VENV"
    echo "已恢复上次中断前的原环境：$recorded" >&2
  else
    echo "发现过期的安装状态文件（备份已不存在），已忽略：$STATE_FILE" >&2
  fi
  rm -f "$STATE_FILE"
}
recover_interrupted

GPU_STATE="" GPU_REASON="" GPU_ACTION=""
if [[ -x "$ROOT_DIR/scripts/gpu-probe.sh" ]]; then
  eval "$("$ROOT_DIR/scripts/gpu-probe.sh" --env)"
fi
if [[ "$GPU_STATE" != "ok" ]]; then
  if [[ $FORCE -eq 0 && "${VOICE_IME_FORCE_QWEN_SETUP:-0}" != "1" ]]; then
    echo "无法安装可用的本地 Qwen 后端：${GPU_REASON:-GPU 探测未通过}" >&2
    [[ -n "$GPU_ACTION" ]] && printf '%s\n' "$GPU_ACTION" >&2
    echo "仅准备调试环境可用 --force；不会宣称语音就绪。" >&2
    exit 1
  fi
  ENABLE_NOW=0
  echo "[WARN] 强制准备环境，GPU 未通过检查：不做 GPU 推理验收、不启用后端。"
fi

# Verify the SAME device/dtype that activation will write, not stale JSON
# settings from another backend. Repair-only mode leaves current settings intact.
if [[ "$ENABLE_NOW" != "0" ]]; then
  export VOICE_IME_QWEN_ASR_DEVICE_MAP="${VOICE_IME_QWEN_ASR_DEVICE_MAP:-cuda:0}"
  export VOICE_IME_QWEN_ASR_DTYPE="${VOICE_IME_QWEN_ASR_DTYPE:-bfloat16}"
fi

# 下载预算只作提示，不改变缓存、下载集合或完整性判据。模型常量来自
# Qwen/Qwen3-ASR-{0.6B,1.7B} 发布快照的权重/配套文件体积（十进制 GB，
# 四舍五入：约 1.8 / 4.4GB）；依赖按 torch CUDA 13 全家桶典型安装估算，
# 并非精确承诺，版本/平台、已有缓存和重建备份都会改变实际峰值。
MODEL_TENTHS=0 SEEN_SIZES=" "
printf '\n== 下载预算（估算，缓存命中会减少下载）==\n'
for size in "${QWEN_SIZES[@]}"; do
  [[ "$SEEN_SIZES" != *" $size "* ]] || continue
  SEEN_SIZES+="$size "
  case "$size" in
    0.6B) echo '  模型 0.6B ≈1.8GB'; MODEL_TENTHS=$((MODEL_TENTHS + 18)) ;;
    1.7B) echo '  模型 1.7B ≈4.4GB'; MODEL_TENTHS=$((MODEL_TENTHS + 44)) ;;
  esac
done
echo '  依赖 venv 典型 ~6GB（torch cu13 全家桶；下载缓存/临时解包另占空间）。'
if [[ -d "$HOME/.cache/pip" ]]; then
  echo '  检测到 ~/.cache/pip：同机重装依赖通常直接命中缓存（不扫描缓存大小，实际命中以 pip 为准）。'
else
  echo '  pip 缓存未检测到：首次可能需下载数 GB 依赖；后续同机重装可复用缓存。'
fi
# 保守预留：新 venv 6GB + pip 缓存/临时解包约 12GB + 所选模型；旧备份另计。
PEAK_TENTHS=$((MODEL_TENTHS + 180))
printf '  预计磁盘峰值（新装保守预留）：约 %d.%dGB；重建保留旧 venv 时还需额外 ~6GB，实际依版本/缓存而变。\n' "$((PEAK_TENTHS / 10))" "$((PEAK_TENTHS % 10))"
echo '  中断可续装：重跑同一命令，已就绪依赖/模型会复用；尚未完成的临时模型下载可能重下。'

# CPython downloads are independent from the pip package mirror. Default is
# repository-local managed 3.12, including headers, even if distro 3.12 exists.
# The helper bootstraps local uv when absent; explicit choices never fall back.
PYTHON_BIN="$("$BOOTSTRAP" "$ROOT_DIR/scripts/setup-qwen-python.py")"
# Check before moving an existing venv or downloading dependencies/models.
"$BOOTSTRAP" "$CHECK" --python "$PYTHON_BIN" --stage static
if ! "$PYTHON_BIN" -c 'import ensurepip' >/dev/null 2>&1; then
  echo "缺少目标解释器的 ensurepip/venv：请安装 python3.12-venv（Debian/Ubuntu），或使用 uv managed Python。" >&2
  exit 1
fi

BACKUP=""
COMMITTED=0
# Verify exact targets before any rename. Never follow a venv symlink to another
# project, delete user environments, or move the repository itself.
verify_venv_target() {
  [[ "$VENV" == "$ROOT_DIR/.venv-qwen-asr" && ! -L "$VENV" ]] || {
    echo "拒绝修改非预期/符号链接 venv：$VENV" >&2; exit 1;
  }
}
finish() {
  local rc=$?
  if [[ $COMMITTED -eq 0 && -n "$BACKUP" && -d "$BACKUP" ]]; then
    if [[ -d "$VENV" && ! -L "$VENV" ]]; then
      local failed="$ROOT_DIR/.venv-qwen-asr.failed-$(date +%Y%m%d-%H%M%S)-$$"
      mv -- "$VENV" "$failed"
      echo "失败环境保留供排查：$failed" >&2
    fi
    mv -- "$BACKUP" "$VENV"
    echo "已恢复原 venv；未切换后端。" >&2
  fi
  if [[ ${#PART_DIRS[@]} -gt 0 ]]; then
    rm -rf -- "${PART_DIRS[@]}"
  fi
  rm -f "$STATE_FILE"
  exit "$rc"
}
trap finish EXIT
verify_venv_target
REBUILD=0 REUSE=0
if [[ -d "$VENV" ]]; then
  if [[ ! -x "$VENV/bin/python" || ! -x "$VENV/bin/pip" ]]; then
    REBUILD=1
  elif ! "$BOOTSTRAP" - "$PYTHON_BIN" "$VENV/bin/python" "$ROOT_DIR/src" <<'PY'
import sys
sys.path.insert(0, sys.argv[3])
from ibus_voice_ime.asr.qwen_preflight import interpreter_info
wanted = interpreter_info(sys.argv[1])
actual = interpreter_info(sys.argv[2])
sys.exit(0 if wanted['version'][:2] == actual['version'][:2] and wanted['base'] == actual['base'] else 1)
PY
  then
    REBUILD=1
  fi
  if [[ $REBUILD -eq 0 ]] && "$VENV/bin/python" -m pip --isolated check && \
    "$VENV/bin/python" - "$ROOT_DIR/requirements-qwen-asr.txt" - <<'PY'
# REUSE 判据直接读 requirements 文件逐项核验（packaging.Requirement），
# 不再手写第二份基线——两处漂移会让旧 venv 绕过新增的 pin（红队 2026-10-11 发现）。
import importlib.metadata as m
import sys
try:
    from packaging.requirements import Requirement
    from packaging.version import Version
except ImportError:
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.version import Version
ok = True
with open(sys.argv[1]) as f:
    for raw in f:
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        req = Requirement(line)
        try:
            version = Version(m.version(req.name))
        except m.PackageNotFoundError:
            ok = False
            print(f'  [REUSE 失败] 未安装：{req.name}')
            break
        except Exception as exc:
            # 元数据损坏（如非正常版本字符串）按不满足处理，不让核验自身崩溃。
            ok = False
            print(f'  [REUSE 失败] {req.name} 版本无法解析：{exc}')
            break
        if not req.specifier.contains(version, prereleases=True):
            ok = False
            print(f'  [REUSE 失败] {req.name} {version} 不满足 {req.specifier}')
            break
sys.exit(0 if ok else 1)
PY
  then
    REUSE=1
    echo "解释器和依赖基线匹配：不升级现有包，仍执行运行时及模型验收。"
  else
    REBUILD=1
  fi
  if [[ $REBUILD -eq 1 ]]; then
    BACKUP="$ROOT_DIR/.venv-qwen-asr.bak-$(date +%Y%m%d-%H%M%S)-$$"
    [[ ! -e "$BACKUP" ]] || { echo "备份目标已存在：$BACKUP" >&2; exit 1; }
    echo "解释器不匹配或 venv 不完整，备份后重建：$BACKUP"
    # journal BEFORE the rename: a crash between the two leaves a recoverable state
    printf 'backup=%s\n' "$BACKUP" > "$STATE_FILE"
    mv -- "$VENV" "$BACKUP"
  fi
elif [[ -e "$VENV" ]]; then
  echo "venv 路径不是目录：$VENV" >&2; exit 1
fi
if [[ ! -x "$VENV/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$VENV"
fi
PY="$VENV/bin/python"
echo "Qwen3-ASR venv Python：$("$PY" -V 2>&1)"
if [[ $REUSE -eq 0 ]]; then
  # torch 范围内最新版（当前 2.12.x）捆绑 CUDA 13 运行库，NVIDIA 官方要求驱动 >=580；
  # gpu-probe 只验证 nvidia-smi 可用，不校验驱动版本，旧驱动会在下载数 GB 后运行时失败
  # （红队 2026-10-11 P1）。这里在安装前给出明确预警与降档指引，不硬性阻断。
  if NVIDIA_DRIVER="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -n1)" \
     && [[ "$NVIDIA_DRIVER" =~ ^([0-9]+) ]] && (( BASH_REMATCH[1] < 580 )); then
    echo "⚠ 检测到 NVIDIA 驱动 $NVIDIA_DRIVER < 580：requirements 中 torch 将解析为 2.12.x（捆绑 CUDA 13，"
    echo "  官方要求驱动 >=580）。若后续运行时验收（CUDA/Triton 矩阵运算）失败，请在 requirements-qwen-asr.txt"
    echo "  临时收紧为 torch==2.9.0（CUDA 12.8 线，要求驱动 >=525）后重跑本脚本。" >&2
  fi
  # pip 自升级失败不致命：旧 pip 仍可能完成依赖安装，真正的安装错误由下一行报告
  if ! "$PY" -m pip --isolated install --upgrade pip "${PIP_INDEX_ARGS[@]}"; then
    echo "⚠ pip 自升级失败（网络？），继续使用现有 pip 安装依赖……" >&2
  fi
  if ! "$PY" -m pip --isolated install -r "$ROOT_DIR/requirements-qwen-asr.txt" "${PIP_INDEX_ARGS[@]}"; then
    echo "依赖安装失败：检查上方 pip 错误、网络/代理和目标解释器。旧环境如有备份会恢复。" >&2
    exit 1
  fi
fi
"$PY" -m pip --isolated check
"$BOOTSTRAP" "$CHECK" --python "$PY" --stage static
if [[ "$GPU_STATE" == "ok" ]]; then
  # Fail early, BEFORE downloading multi-GB assets.
  "$BOOTSTRAP" "$CHECK" --python "$PY" --stage runtime --timeout "$TIMEOUT"
fi

MS_CLI="$VENV/bin/modelscope"
[[ -x "$MS_CLI" ]] || { echo "venv 未安装 modelscope CLI。" >&2; exit 1; }
mkdir -p "$MODELS_DIR" "$MODEL_CACHE"
echo "共享模型缓存：$MODEL_CACHE（VOICE_IME_MODEL_CACHE 可自定义；vendor 下建符号链接）"

# 分档完整性判定（配置和全部权重分片存在）：下载前、下载后、缓存复用三处共用，
# 与验收器 --model-only 同一逻辑，vendor 符号链接会透明穿透到缓存目录。
model_ready() {
  "$BOOTSTRAP" "$CHECK" --model-only --model "$1" >/dev/null 2>&1
}

for size in "${QWEN_SIZES[@]}"; do
  model_dir="$MODELS_DIR/Qwen3-ASR-$size"
  cache_dir="$MODEL_CACHE/Qwen3-ASR-$size"
  if [[ -L "$model_dir" ]]; then
    if model_ready "$model_dir"; then
      echo "模型配置及全部分片已存在：$model_dir"
      continue
    fi
    # 链接目标不完整：指向本缓存的删链接走下方缓存补齐流程；指向其他位置
    # （用户自定义布局）保持旧语义在原位补齐，不擅自改写。
    if [[ "$(readlink -- "$model_dir")" == "$cache_dir" ]]; then
      rm -f -- "$model_dir"
    else
      echo "下载/补齐 Qwen/Qwen3-ASR-$size（保留已有文件）……"
      "$MS_CLI" download --model "Qwen/Qwen3-ASR-$size" --local_dir "$model_dir"
      "$BOOTSTRAP" "$CHECK" --model-only --model "$model_dir"
      continue
    fi
  fi
  if [[ -d "$model_dir" ]]; then
    # 旧布局：vendor 下真实目录。完整则迁入共享缓存并在原位留符号链接；
    # 迁移与缓存发布共用分档 flock（跨克隆串行，防 TOCTOU 嵌套）；
    # mv 失败或缓存位已被占用时保留原状，不阻断安装。
    if model_ready "$model_dir"; then
      echo "模型配置及全部分片已存在：$model_dir"
      cache_lock "$size"
      if [[ ! -e "$cache_dir" ]] && mv -- "$model_dir" "$cache_dir"; then
        ln -s -- "$cache_dir" "$model_dir"
        echo "已迁移到共享缓存：$cache_dir（vendor 内留符号链接；卸载/更换克隆目录不丢模型）"
      else
        echo "共享缓存迁移跳过（缓存目录已存在或移动失败）：继续使用 $model_dir"
      fi
      cache_unlock
      continue
    fi
    echo "下载/补齐 Qwen/Qwen3-ASR-$size（保留已有文件）……"
    "$MS_CLI" download --model "Qwen/Qwen3-ASR-$size" --local_dir "$model_dir"
    "$BOOTSTRAP" "$CHECK" --model-only --model "$model_dir"
    continue
  fi
  cache_lock "$size"
  if model_ready "$cache_dir"; then
    echo "模型配置及全部分片已在共享缓存（跳过下载，重建符号链接）：$cache_dir"
    ln -s -- "$cache_dir" "$model_dir"
    cache_unlock
    continue
  fi
  if [[ ! -e "$cache_dir" ]]; then
    # 全新下载：mktemp 临时目录（不依赖 $$ 的跨 PID 唯一性）；分档 flock
    # 串行化并发克隆——先在 .part 内通过真实 model-only 校验才发布，
    # CLI 伪成功（rc0 但缺权重）不会把半成品定稿成正式分档（红队实测）。
    part_dir="$(mktemp -d "$MODEL_CACHE/.part-Qwen3-ASR-${size}-XXXXXX")"
    PART_DIRS=("$part_dir")
    echo "下载 Qwen/Qwen3-ASR-$size 到共享缓存（临时目录校验通过后落位）……"
    if ! "$MS_CLI" download --model "Qwen/Qwen3-ASR-$size" --local_dir "$part_dir"; then
      rm -rf -- "$part_dir"; PART_DIRS=(); cache_unlock
      echo "下载 Qwen/Qwen3-ASR-$size 失败（临时目录已清理，可重试续装）。" >&2
      exit 1
    fi
    if ! "$BOOTSTRAP" "$CHECK" --model-only --model "$part_dir"; then
      rm -rf -- "$part_dir"; PART_DIRS=(); cache_unlock
      echo "临时目录未通过完整性校验，已清理；不发布半成品到共享缓存。" >&2
      exit 1
    fi
    if [[ ! -e "$cache_dir" ]]; then
      mv -- "$part_dir" "$cache_dir"
    else
      rm -rf -- "$part_dir"   # 并发克隆已抢先定稿：复用其成果
    fi
    PART_DIRS=()
  else
    echo "补齐共享缓存中的 Qwen/Qwen3-ASR-$size（保留已有文件）……"
    "$MS_CLI" download --model "Qwen/Qwen3-ASR-$size" --local_dir "$cache_dir"
  fi
  "$BOOTSTRAP" "$CHECK" --model-only --model "$cache_dir"
  ln -s -- "$cache_dir" "$model_dir"
  cache_unlock
done
if [[ "$GPU_STATE" == "ok" ]]; then
  VOICE_IME_QWEN_ASR_MAX_NEW_TOKENS=16 "$BOOTSTRAP" "$CHECK" --python "$PY" \
    --stage smoke --model "$DEFAULT_MODEL_DIR" --timeout "$TIMEOUT"
  write_verify_receipt "$PY" "$DEFAULT_MODEL_DIR"
fi
# Preserve exact resolved dependency versions for issue reports; not a universal
# CUDA lock. No user configuration is changed before the gates above pass.
"$PY" -m pip --isolated freeze > "$VENV/installed-requirements.txt"
COMMITTED=1
# The environment is now committed. Never restore an old venv after a crash
# during configuration/desktop activation (which can already reference the new one).
rm -f "$STATE_FILE"
if [[ "$ENABLE_NOW" != "0" ]]; then
  # Reuse JSON configuration/legacy-env cleanup rather than introducing a second
  # competing backend source. Custom model directories are explicitly retained.
  # Do NOT let ibus-daemon inherit fd 9 and hold the install lock indefinitely.
  if ! VOICE_IME_QWEN_SWITCH_MODEL_PATH="$DEFAULT_MODEL_DIR" \
    VOICE_IME_QWEN_SWITCH_DEVICE_MAP="$VOICE_IME_QWEN_ASR_DEVICE_MAP" \
    VOICE_IME_QWEN_SWITCH_DTYPE="$VOICE_IME_QWEN_ASR_DTYPE" \
    "$ROOT_DIR/scripts/switch-qwen-asr.sh" "$DEFAULT_MODEL" 9>&-; then
    # COMMITTED=1: the environment itself is verified and kept; only activation failed.
    echo "错误：环境与模型均已验收，但配置写入或桌面激活失败（见上方具体错误）；不能视为桌面语音就绪。" >&2
    echo "环境已保留，修复后单独重试：./scripts/switch-qwen-asr.sh $DEFAULT_MODEL" >&2
    exit 1
  fi
fi
if [[ "$GPU_STATE" == "ok" ]]; then
  echo "Qwen3-ASR 配置完成：CUDA/Triton、模型加载和短音频推理已验收。"
else
  echo "Qwen 环境和模型已准备，但语音未验证、未启用（GPU 不可用）。"
fi
[[ -z "$BACKUP" ]] || echo "原环境备份保留：$BACKUP"
echo "Python：$PY"
echo "依赖版本记录：$VENV/installed-requirements.txt"
