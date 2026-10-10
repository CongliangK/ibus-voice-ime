#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT_DIR/.venv-qwen-asr"
MODELS_DIR="${VOICE_IME_QWEN_ASR_MODELS_DIR:-$ROOT_DIR/vendor/models/qwen3-asr}"
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
      echo "默认选择独立 Python 3.12；uv 存在时可自动准备。--force 仅跳过 GPU 门控，无 GPU 时只准备不启用。"
      echo "--verify-only 不下载/不改配置：静态检查 + CUDA/Triton + 模型短音频推理。"
      exit 0 ;;
    *) echo "未知参数：$1（查看 --help）" >&2; exit 2 ;;
  esac
done
if [[ -n "$PROXY" ]]; then
  export http_proxy="$PROXY" https_proxy="$PROXY" HTTP_PROXY="$PROXY" HTTPS_PROXY="$PROXY"
fi
DEFAULT_MODEL="${VOICE_IME_QWEN_ASR_MODEL:-1.7b}"
case "${DEFAULT_MODEL,,}" in
  0.6|0.6b|qwen3-asr-0.6b) DEFAULT_MODEL=0.6b ;;
  1.7|1.7b|qwen3-asr-1.7b) DEFAULT_MODEL=1.7b ;;
  *) echo "无效的 VOICE_IME_QWEN_ASR_MODEL：$DEFAULT_MODEL（支持 0.6b / 1.7b）" >&2; exit 2 ;;
esac
DEFAULT_MODEL_DIR="$MODELS_DIR/Qwen3-ASR-$( [[ "$DEFAULT_MODEL" == "1.7b" ]] && echo 1.7B || echo 0.6B )"
TIMEOUT="${VOICE_IME_QWEN_ASR_VERIFY_TIMEOUT:-180}"
if [[ $VERIFY_ONLY -eq 1 ]]; then
  # Match the launcher's persisted overrides, without sourcing shell syntax or
  # ever modifying environment.d / JSON. Values containing spaces remain safe.
  if [[ -f "$ROOT_DIR/scripts/env-file-load.sh" ]]; then
    eval "$(bash "$ROOT_DIR/scripts/env-file-load.sh" "$HOME/.config/environment.d/ibus-voice-ime.conf" | grep -E '^export (VOICE_IME_[A-Z0-9_]+|PYTORCH_CUDA_ALLOC_CONF)=' || true)"
  fi
  TARGETS="$(PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" "$BOOTSTRAP" -c 'from ibus_voice_ime.asr import qwen_asr_runtime as r; print(r._python()); print(r.model_id())')"
  mapfile -t CHECK_TARGETS <<< "$TARGETS"
  [[ ${#CHECK_TARGETS[@]} -eq 2 ]] || { echo "无法解析实际 sidecar 配置" >&2; exit 1; }
  exec "$BOOTSTRAP" "$CHECK" --python "${CHECK_TARGETS[0]}" --stage smoke --model "${CHECK_TARGETS[1]}" --timeout "$TIMEOUT"
fi

# Single writer: init/doctor/manual installation must not mutate the same venv
# concurrently. Kernel lock is released even on interruption; no stale PID file.
command -v flock >/dev/null || { echo "缺少 flock（util-linux），无法安全串行安装。" >&2; exit 1; }
exec 9>"$ROOT_DIR/.qwen-asr-setup.lock"
flock -n 9 || { echo "另一个 Qwen 安装/修复正在运行，请等待完成。" >&2; exit 1; }

# pip index: torch CUDA wheels are multi-GB; the default TUNA mirror is a full
# PyPI replica and avoids crawling overseas PyPI. Override or disable per run:
#   VOICE_IME_PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple ./scripts/setup-qwen-asr.sh
#   VOICE_IME_PIP_INDEX_URL=default ./scripts/setup-qwen-asr.sh   # 官方 PyPI
PIP_INDEX_URL="${VOICE_IME_PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
PIP_INDEX_ARGS=()
if [[ "$PIP_INDEX_URL" != "default" ]]; then
  PIP_INDEX_ARGS=(--index-url "$PIP_INDEX_URL")
  echo "pip 镜像：$PIP_INDEX_URL（VOICE_IME_PIP_INDEX_URL=default 可切回官方源）"
fi

# Crash recovery journal: SIGKILL/power loss during an install can leave the
# user's original venv parked in a .bak- dir with a half-built replacement in
# place. The EXIT trap cannot help there, so the rename is journaled and the
# next run restores the original before doing anything else.
STATE_FILE="$ROOT_DIR/.qwen-asr-setup.state"
recover_interrupted() {
  [[ -f "$STATE_FILE" ]] || return 0
  local recorded
  recorded="$(sed -n 's/^backup=//p' "$STATE_FILE" | head -n1)"
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

# Explicit user choices are never silently replaced. Default is 3.12, NOT the
# system python3. uv's managed Python includes development headers and does not
# replace distro Python (needed by PyGObject/IBus).
EXPLICIT="${VOICE_IME_QWEN_ASR_SETUP_PYTHON:-}"
if [[ -z "$EXPLICIT" ]] && ! command -v python3.12 >/dev/null; then
  if command -v uv >/dev/null; then
    echo "用 uv 准备独立 Python 3.12（不修改系统 python3）……"
    uv python install 3.12
    EXPLICIT="$(uv python find --managed-python 3.12 2>/dev/null || uv python find 3.12 2>/dev/null || true)"
  fi
fi
PYTHON_BIN="$("$BOOTSTRAP" "$CHECK" --select-python --explicit "$EXPLICIT")"
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
  if [[ $REBUILD -eq 0 ]] && "$VENV/bin/python" -m pip check && \
    "$VENV/bin/python" - <<'PY'
import importlib.metadata as m
import sys
try:
    modelscope_version = tuple(int(part) for part in m.version('modelscope').split('.')[:2])
    ok = (m.version('qwen-asr') == '0.0.6' and m.version('transformers') == '4.57.6'
          and (1, 22) <= modelscope_version < (2, 0))
except (m.PackageNotFoundError, ValueError):
    ok = False
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
  # pip 自升级失败不致命：旧 pip 仍可能完成依赖安装，真正的安装错误由下一行报告
  if ! "$PY" -m pip install --upgrade pip "${PIP_INDEX_ARGS[@]}"; then
    echo "⚠ pip 自升级失败（网络？），继续使用现有 pip 安装依赖……" >&2
  fi
  if ! "$PY" -m pip install -r "$ROOT_DIR/requirements-qwen-asr.txt" "${PIP_INDEX_ARGS[@]}"; then
    echo "依赖安装失败：检查上方 pip 错误、网络/代理和目标解释器。旧环境如有备份会恢复。" >&2
    exit 1
  fi
fi
"$PY" -m pip check
"$BOOTSTRAP" "$CHECK" --python "$PY" --stage static
if [[ "$GPU_STATE" == "ok" ]]; then
  # Fail early, BEFORE downloading multi-GB assets.
  "$BOOTSTRAP" "$CHECK" --python "$PY" --stage runtime --timeout "$TIMEOUT"
fi

MS_CLI="$VENV/bin/modelscope"
[[ -x "$MS_CLI" ]] || { echo "venv 未安装 modelscope CLI。" >&2; exit 1; }
mkdir -p "$MODELS_DIR"
for size in 0.6B 1.7B; do
  model_dir="$MODELS_DIR/Qwen3-ASR-$size"
  if "$BOOTSTRAP" "$CHECK" --model-only --model "$model_dir" >/dev/null 2>&1; then
    echo "模型配置及全部分片已存在：$model_dir"
  else
    echo "下载/补齐 Qwen/Qwen3-ASR-$size（保留已有文件）……"
    "$MS_CLI" download --model "Qwen/Qwen3-ASR-$size" --local_dir "$model_dir"
    "$BOOTSTRAP" "$CHECK" --model-only --model "$model_dir"
  fi
done
if [[ "$GPU_STATE" == "ok" ]]; then
  VOICE_IME_QWEN_ASR_MAX_NEW_TOKENS=16 "$BOOTSTRAP" "$CHECK" --python "$PY" \
    --stage smoke --model "$DEFAULT_MODEL_DIR" --timeout "$TIMEOUT"
fi
# Preserve exact resolved dependency versions for issue reports; not a universal
# CUDA lock. No user configuration is changed before the gates above pass.
"$PY" -m pip freeze > "$VENV/installed-requirements.txt"
COMMITTED=1
if [[ "$ENABLE_NOW" != "0" ]]; then
  # Reuse JSON configuration/legacy-env cleanup rather than introducing a second
  # competing backend source. Custom model directories are explicitly retained.
  # Do NOT let ibus-daemon inherit fd 9 and hold the install lock indefinitely.
  if ! VOICE_IME_QWEN_SWITCH_MODEL_PATH="$DEFAULT_MODEL_DIR" \
    VOICE_IME_QWEN_SWITCH_DEVICE_MAP="$VOICE_IME_QWEN_ASR_DEVICE_MAP" \
    VOICE_IME_QWEN_SWITCH_DTYPE="$VOICE_IME_QWEN_ASR_DTYPE" \
    "$ROOT_DIR/scripts/switch-qwen-asr.sh" "$DEFAULT_MODEL" 9>&-; then
    # COMMITTED=1: the environment itself is verified and kept; only activation failed.
    echo "错误：环境与模型均已验收，但启用后端失败（config.json 损坏/权限不足？）。" >&2
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
