#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SIZE="${1:-0.6b}"
case "${SIZE,,}" in
  0.6|0.6b|qwen3-asr-0.6b)
    MODEL=0.6b
    MODEL_PATH="$ROOT_DIR/vendor/models/qwen3-asr/Qwen3-ASR-0.6B"
    ;;
  1.7|1.7b|qwen3-asr-1.7b)
    MODEL=1.7b
    MODEL_PATH="$ROOT_DIR/vendor/models/qwen3-asr/Qwen3-ASR-1.7B"
    ;;
  *)
    echo "用法：$0 0.6b|1.7b" >&2
    exit 2
    ;;
esac
if [[ ! -d "$MODEL_PATH" ]]; then
  echo "模型目录不存在：$MODEL_PATH，请先运行 scripts/setup-qwen-asr.sh" >&2
  exit 1
fi
# expandable_segments lets torch.cuda.empty_cache() actually return reserved
# VRAM to the system after the idle watchdog offloads the model to CPU RAM.
# Without it, post-inference cached blocks (~3 GB on 1.7B) stay reserved.
PYTORCH_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}"
ENV_FILE="$HOME/.config/environment.d/ibus-voice-ime.conf"
mkdir -p "$(dirname "$ENV_FILE")"
if [[ -f "$ENV_FILE" ]]; then
  TMP="$(mktemp)"
  grep -vE '^(VOICE_IME_ASR_BACKEND|VOICE_IME_QWEN_ASR|VOICE_IME_MIMO_ASR|VOICE_IME_MIMO_CLOUD_ASR|PYTORCH_CUDA_ALLOC_CONF)' "$ENV_FILE" > "$TMP" || true
  mv "$TMP" "$ENV_FILE"
fi
cat >> "$ENV_FILE" <<EOF_ENV
VOICE_IME_ASR_BACKEND=qwen3-asr
VOICE_IME_QWEN_ASR=1
VOICE_IME_MIMO_ASR=0
VOICE_IME_QWEN_ASR_MODEL=$MODEL
VOICE_IME_QWEN_ASR_MODEL_PATH=$MODEL_PATH
VOICE_IME_QWEN_ASR_PYTHON=$ROOT_DIR/.venv-qwen-asr/bin/python
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
  VOICE_IME_ASR_BACKEND=qwen3-asr \
  VOICE_IME_QWEN_ASR=1 \
  VOICE_IME_MIMO_ASR=0 \
  "VOICE_IME_QWEN_ASR_MODEL=$MODEL" \
  "VOICE_IME_QWEN_ASR_MODEL_PATH=$MODEL_PATH" \
  "VOICE_IME_QWEN_ASR_PYTHON=$ROOT_DIR/.venv-qwen-asr/bin/python" \
  VOICE_IME_QWEN_ASR_HOST=127.0.0.1 \
  VOICE_IME_QWEN_ASR_PORT=18081 \
  VOICE_IME_QWEN_ASR_LANGUAGE=Chinese \
  VOICE_IME_QWEN_ASR_DTYPE=bfloat16 \
  VOICE_IME_QWEN_ASR_DEVICE_MAP=cuda:0 \
  VOICE_IME_QWEN_ASR_MAX_NEW_TOKENS=256 \
  VOICE_IME_QWEN_ASR_START_TIMEOUT=180 \
  VOICE_IME_QWEN_ASR_TIMEOUT=180 \
  "PYTORCH_CUDA_ALLOC_CONF=$PYTORCH_ALLOC_CONF" 2>/dev/null || true
pkill -f '[q]wen_asr_server.py' 2>/dev/null || true
pkill -f '[m]imo_asr_server.py' 2>/dev/null || true
COMPONENT_DIR="$HOME/.local/share/ibus/component"
IBUS_COMPONENT_PATH_VALUE="$COMPONENT_DIR:/usr/share/ibus/component"
# Start ibus-daemon with the new environment immediately; plain `ibus restart`
# may preserve the old daemon environment until next login.
IBUS_COMPONENT_PATH="$IBUS_COMPONENT_PATH_VALUE" \
LD_LIBRARY_PATH="$ROOT_DIR/vendor/rime/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
VOICE_IME_RIME_LIBRARY="$ROOT_DIR/vendor/rime/lib/librime.so.1" \
VOICE_IME_RIME_SHARED_DATA_DIR="$ROOT_DIR/vendor/rime/share/rime-data" \
VOICE_IME_RIME_STAGING_DIR="$ROOT_DIR/vendor/rime/build" \
VOICE_IME_RIME_USER_DATA_DIR="$HOME/.local/share/ibus-voice-ime/rime-user" \
VOICE_IME_ASR_BACKEND=qwen3-asr \
VOICE_IME_QWEN_ASR=1 \
VOICE_IME_MIMO_ASR=0 \
VOICE_IME_QWEN_ASR_MODEL="$MODEL" \
VOICE_IME_QWEN_ASR_MODEL_PATH="$MODEL_PATH" \
VOICE_IME_QWEN_ASR_PYTHON="$ROOT_DIR/.venv-qwen-asr/bin/python" \
VOICE_IME_QWEN_ASR_HOST=127.0.0.1 \
VOICE_IME_QWEN_ASR_PORT=18081 \
VOICE_IME_QWEN_ASR_LANGUAGE=Chinese \
VOICE_IME_QWEN_ASR_DTYPE=bfloat16 \
VOICE_IME_QWEN_ASR_DEVICE_MAP=cuda:0 \
VOICE_IME_QWEN_ASR_MAX_NEW_TOKENS=256 \
VOICE_IME_QWEN_ASR_START_TIMEOUT=180 \
VOICE_IME_QWEN_ASR_TIMEOUT=180 \
PYTORCH_CUDA_ALLOC_CONF="$PYTORCH_ALLOC_CONF" \
VOICE_IME_AUTO_PUNCTUATION="${VOICE_IME_AUTO_PUNCTUATION:-0}" \
VOICE_IME_LLM_POSTPROCESS="${VOICE_IME_SWITCH_LLM_POSTPROCESS:-0}" \
VOICE_IME_LLM_INTERNAL="${VOICE_IME_SWITCH_LLM_INTERNAL:-1}" \
VOICE_IME_LLM_TRUST_OUTPUT="${VOICE_IME_SWITCH_LLM_TRUST_OUTPUT:-1}" \
VOICE_IME_LLM_RERANK="${VOICE_IME_SWITCH_LLM_RERANK:-0}" \
VOICE_IME_LLM_CANDIDATES="${VOICE_IME_SWITCH_LLM_CANDIDATES:-1}" \
VOICE_IME_LLM_BASE_URL="${VOICE_IME_SWITCH_LLM_BASE_URL:-http://127.0.0.1:18080/v1}" \
VOICE_IME_LLM_API_KEY="${VOICE_IME_SWITCH_LLM_API_KEY:-local}" \
VOICE_IME_LLM_MODEL="${VOICE_IME_SWITCH_LLM_MODEL:-qwen3.5-0.8b}" \
VOICE_IME_LLM_LOG="${VOICE_IME_LLM_LOG:-$HOME/.local/share/ibus-voice-ime/llm.jsonl}" \
ibus-daemon -drx --replace --panel disable --cache refresh >/dev/null 2>&1 || ibus restart >/dev/null 2>&1 || true
sleep 1
ibus engine voice-custom >/dev/null 2>&1 || true
echo "已切换到 Qwen3-ASR $MODEL：$MODEL_PATH"
