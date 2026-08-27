#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT_DIR/.venv-qwen-asr"
MODELS_DIR="${VOICE_IME_QWEN_ASR_MODELS_DIR:-$ROOT_DIR/vendor/models/qwen3-asr}"
PYTHON_BIN="${VOICE_IME_QWEN_ASR_SETUP_PYTHON:-$(command -v python3)}"
ENABLE_NOW="${VOICE_IME_ENABLE_QWEN_ASR:-1}"
DEFAULT_MODEL="${VOICE_IME_QWEN_ASR_MODEL:-1.7b}"
case "${DEFAULT_MODEL,,}" in
  0.6|0.6b|qwen3-asr-0.6b) DEFAULT_MODEL=0.6b ;;
  1.7|1.7b|qwen3-asr-1.7b) DEFAULT_MODEL=1.7b ;;
  *)
    echo "无效的 VOICE_IME_QWEN_ASR_MODEL：$DEFAULT_MODEL（支持 0.6b / 1.7b）" >&2
    exit 2
    ;;
esac
DEFAULT_MODEL_DIR="$MODELS_DIR/Qwen3-ASR-$( [[ "${DEFAULT_MODEL}" == "1.7b" ]] && echo 1.7B || echo 0.6B )"

mkdir -p "$MODELS_DIR"

if [[ ! -x "$VENV/bin/python" ]]; then
  echo "创建 Qwen3-ASR 独立虚拟环境：$VENV"
  "$PYTHON_BIN" -m venv "$VENV"
fi

PY="$VENV/bin/python"
PIP="$VENV/bin/pip"
"$PY" -m pip install --upgrade pip

cat <<'EOF_NOTE'
安装 qwen-asr 依赖。注意：官方建议 Python 3.12；如果当前系统 Python 太新导致 PyTorch/qwen-asr 安装失败，
需要改用 Python 3.12 创建 .venv-qwen-asr，或使用官方 Docker/vLLM 部署。
EOF_NOTE
"$PIP" install -U qwen-asr modelscope

# Download both requested models from ModelScope.  They can also be used by
# qwen-asr through local paths, avoiding runtime network downloads.
MS_CLI="$VENV/bin/modelscope"
if [[ ! -x "$MS_CLI" ]]; then
  MS_CLI="$(command -v modelscope || true)"
fi
if [[ -z "$MS_CLI" ]]; then
  echo "未找到 modelscope 命令，无法下载 Qwen3-ASR 模型。" >&2
  exit 1
fi

if [[ ! -f "$MODELS_DIR/Qwen3-ASR-0.6B/model.safetensors" && ! -f "$MODELS_DIR/Qwen3-ASR-0.6B/model.safetensors.index.json" ]]; then
  echo "下载 Qwen/Qwen3-ASR-0.6B ..."
  "$MS_CLI" download --model Qwen/Qwen3-ASR-0.6B --local_dir "$MODELS_DIR/Qwen3-ASR-0.6B"
else
  echo "已存在：$MODELS_DIR/Qwen3-ASR-0.6B"
fi

if [[ ! -f "$MODELS_DIR/Qwen3-ASR-1.7B/model.safetensors" && ! -f "$MODELS_DIR/Qwen3-ASR-1.7B/model.safetensors.index.json" ]]; then
  echo "下载 Qwen/Qwen3-ASR-1.7B ..."
  "$MS_CLI" download --model Qwen/Qwen3-ASR-1.7B --local_dir "$MODELS_DIR/Qwen3-ASR-1.7B"
else
  echo "已存在：$MODELS_DIR/Qwen3-ASR-1.7B"
fi

# Verify the import.  Do not load model here; loading can take time and memory.
"$PY" - <<'PY'
import qwen_asr
from qwen_asr import Qwen3ASRModel
print('qwen_asr import ok:', getattr(qwen_asr, '__version__', 'unknown'))
PY

if [[ "$ENABLE_NOW" != "0" ]]; then
  COMPONENT_DIR="$HOME/.local/share/ibus/component"
  IBUS_COMPONENT_PATH_VALUE="$COMPONENT_DIR:/usr/share/ibus/component"
  ENV_FILE="$HOME/.config/environment.d/ibus-voice-ime.conf"
  mkdir -p "$(dirname "$ENV_FILE")"
  # expandable_segments lets torch.cuda.empty_cache() actually return reserved
  # VRAM after the idle watchdog offloads the model to CPU RAM.
  PYTORCH_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}"
  # Remove stale Qwen ASR lines then append current config.
  if [[ -f "$ENV_FILE" ]]; then
    TMP="$(mktemp)"
    grep -vE '^(VOICE_IME_ASR_BACKEND|VOICE_IME_QWEN_ASR|VOICE_IME_MIMO_ASR|VOICE_IME_MIMO_CLOUD_ASR|VOICE_IME_VOLC_BIGMODEL_ASR|PYTORCH_CUDA_ALLOC_CONF)' "$ENV_FILE" > "$TMP" || true
    mv "$TMP" "$ENV_FILE"
  fi
  cat >> "$ENV_FILE" <<EOF_ENV
VOICE_IME_ASR_BACKEND=qwen3-asr
VOICE_IME_QWEN_ASR=1
VOICE_IME_MIMO_ASR=0
VOICE_IME_MIMO_CLOUD_ASR=0
VOICE_IME_VOLC_BIGMODEL_ASR=0
VOICE_IME_QWEN_ASR_MODEL=$DEFAULT_MODEL
VOICE_IME_QWEN_ASR_MODEL_PATH=$DEFAULT_MODEL_DIR
VOICE_IME_QWEN_ASR_PYTHON=$VENV/bin/python
VOICE_IME_QWEN_ASR_HOST=127.0.0.1
VOICE_IME_QWEN_ASR_PORT=18081
VOICE_IME_QWEN_ASR_LANGUAGE=Chinese
VOICE_IME_QWEN_ASR_DTYPE=bfloat16
VOICE_IME_QWEN_ASR_DEVICE_MAP=cuda:0
VOICE_IME_QWEN_ASR_MAX_NEW_TOKENS=256
VOICE_IME_QWEN_ASR_START_TIMEOUT=180
VOICE_IME_QWEN_ASR_TIMEOUT=180
PYTORCH_CUDA_ALLOC_CONF=$PYTORCH_ALLOC_CONF
EOF_ENV
  systemctl --user set-environment \
    "IBUS_COMPONENT_PATH=$IBUS_COMPONENT_PATH_VALUE" \
    VOICE_IME_ASR_BACKEND=qwen3-asr \
    VOICE_IME_QWEN_ASR=1 \
    VOICE_IME_MIMO_ASR=0 \
    VOICE_IME_MIMO_CLOUD_ASR=0 \
    VOICE_IME_VOLC_BIGMODEL_ASR=0 \
    "VOICE_IME_QWEN_ASR_MODEL=$DEFAULT_MODEL" \
    "VOICE_IME_QWEN_ASR_MODEL_PATH=$DEFAULT_MODEL_DIR" \
    "VOICE_IME_QWEN_ASR_PYTHON=$VENV/bin/python" \
    VOICE_IME_QWEN_ASR_HOST=127.0.0.1 \
    VOICE_IME_QWEN_ASR_PORT=18081 \
    VOICE_IME_QWEN_ASR_LANGUAGE=Chinese \
    VOICE_IME_QWEN_ASR_DTYPE=bfloat16 \
    VOICE_IME_QWEN_ASR_DEVICE_MAP=cuda:0 \
    VOICE_IME_QWEN_ASR_MAX_NEW_TOKENS=256 \
    VOICE_IME_QWEN_ASR_START_TIMEOUT=180 \
    VOICE_IME_QWEN_ASR_TIMEOUT=180 \
    "PYTORCH_CUDA_ALLOC_CONF=$PYTORCH_ALLOC_CONF" 2>/dev/null || true
fi

cat <<EOF
Qwen3-ASR 配置完成。
  0.6B: $MODELS_DIR/Qwen3-ASR-0.6B
  1.7B: $MODELS_DIR/Qwen3-ASR-1.7B
  Python: $VENV/bin/python

当前启用 $DEFAULT_MODEL（已写入 environment.d）。切换档位：
  ./scripts/switch-qwen-asr.sh 1.7b    # 或 0.6b

切到云端/其他后端：./scripts/switch-mimo-cloud-asr.sh cn / ./scripts/switch-volc-bigmodel-asr.sh
EOF
