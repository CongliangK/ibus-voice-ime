#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Switch the voice IME to the Volcano Engine (豆包) bigmodel recording-file ASR
# (turbo, resource id volc.bigasr.auc_turbo, base64 直传本地音频).
# The API key (X-Api-Key) is injected at runtime from Bitwarden Secrets Manager
# (bws); only the secret NAME is persisted, never the raw key.

API_KEY="${VOICE_IME_VOLC_API_KEY:-${VOLC_BIGMODEL_API_KEY:-${VOLC_ASR_API_KEY:-}}}"
USE_BWS_KEY=0
BWS_ENV_FILE="${VOICE_IME_BWS_ENV_FILE:-${PI_BWS_ENV_FILE:-$HOME/.config/pi-secrets/bws.env}}"
BWS_SECRET_NAME="${VOICE_IME_VOLC_API_KEY_SECRET:-VOLC_BIGMODEL_ASR_API_KEY}"
if [[ -z "$API_KEY" && -r "$BWS_ENV_FILE" && -x "$ROOT_DIR/run-engine.sh" && $(command -v bws 2>/dev/null || true) ]]; then
  USE_BWS_KEY=1
elif [[ -z "$API_KEY" && -t 0 ]]; then
  read -rsp "请输入火山引擎 bigmodel ASR API Key（X-Api-Key）：" API_KEY
  echo
fi
if [[ -z "$API_KEY" && "$USE_BWS_KEY" != "1" ]]; then
  cat >&2 <<'EOF_ERR'
未提供火山引擎 ASR API Key，也没有找到可用的 Bitwarden Secrets Manager 配置。
请用以下任一方式运行：
  VOICE_IME_VOLC_API_KEY='xxxxx' ./scripts/switch-volc-bigmodel-asr.sh
或配置 ~/.config/pi-secrets/bws.env，并在项目中保存 VOLC_BIGMODEL_ASR_API_KEY。
EOF_ERR
  exit 1
fi

BASE_URL="${VOICE_IME_VOLC_BIGMODEL_BASE_URL:-https://openspeech.bytedance.com}"
RESOURCE_ID="${VOICE_IME_VOLC_BIGMODEL_RESOURCE_ID:-volc.bigasr.auc_turbo}"
MODEL_NAME="${VOICE_IME_VOLC_BIGMODEL_MODEL_NAME:-bigmodel}"
ENV_FILE="$HOME/.config/environment.d/ibus-voice-ime.conf"
mkdir -p "$(dirname "$ENV_FILE")"
if [[ -f "$ENV_FILE" ]]; then
  TMP="$(mktemp)"
  grep -vE '^(VOICE_IME_ASR_BACKEND|VOICE_IME_QWEN_ASR|VOICE_IME_MIMO_ASR|VOICE_IME_MIMO_CLOUD_ASR|VOICE_IME_VOLC_BIGMODEL_ASR|VOICE_IME_VOLC_API_KEY|VOICE_IME_VOLC_API_KEY_SECRET|VOICE_IME_VOLC_BIGMODEL_|VOICE_IME_VOLC_ASR)' "$ENV_FILE" > "$TMP" || true
  mv "$TMP" "$ENV_FILE"
fi
cat >> "$ENV_FILE" <<EOF_ENV
VOICE_IME_ASR_BACKEND=volc-bigmodel-asr
VOICE_IME_QWEN_ASR=0
VOICE_IME_MIMO_ASR=0
VOICE_IME_MIMO_CLOUD_ASR=0
VOICE_IME_VOLC_BIGMODEL_ASR=1
VOICE_IME_VOLC_BIGMODEL_BASE_URL=$BASE_URL
VOICE_IME_VOLC_BIGMODEL_RESOURCE_ID=$RESOURCE_ID
VOICE_IME_VOLC_BIGMODEL_MODEL_NAME=$MODEL_NAME
EOF_ENV
echo "VOICE_IME_VOLC_API_KEY_SECRET=$BWS_SECRET_NAME" >> "$ENV_FILE"
if [[ "$USE_BWS_KEY" != "1" ]]; then
  echo "提示：手动提供的 API Key 只注入当前运行环境，不写入 $ENV_FILE；建议配置 bws/$BWS_SECRET_NAME。"
fi
chmod 600 "$ENV_FILE" 2>/dev/null || true

systemctl --user set-environment \
  VOICE_IME_ASR_BACKEND=volc-bigmodel-asr \
  VOICE_IME_QWEN_ASR=0 \
  VOICE_IME_MIMO_ASR=0 \
  VOICE_IME_MIMO_CLOUD_ASR=0 \
  VOICE_IME_VOLC_BIGMODEL_ASR=1 \
  "VOICE_IME_VOLC_BIGMODEL_BASE_URL=$BASE_URL" \
  "VOICE_IME_VOLC_BIGMODEL_RESOURCE_ID=$RESOURCE_ID" \
  "VOICE_IME_VOLC_BIGMODEL_MODEL_NAME=$MODEL_NAME" 2>/dev/null || true
if [[ "$USE_BWS_KEY" == "1" ]]; then
  systemctl --user set-environment "VOICE_IME_VOLC_API_KEY_SECRET=$BWS_SECRET_NAME" 2>/dev/null || true
  systemctl --user unset-environment VOICE_IME_VOLC_API_KEY VOLC_BIGMODEL_API_KEY VOLC_ASR_API_KEY 2>/dev/null || true
else
  systemctl --user set-environment "VOICE_IME_VOLC_API_KEY=$API_KEY" 2>/dev/null || true
fi

pkill -f '[m]imo_asr_server.py' 2>/dev/null || true
pkill -f '[q]wen_asr_server.py' 2>/dev/null || true
if [[ "$USE_BWS_KEY" == "1" ]]; then
  unset VOICE_IME_VOLC_API_KEY VOLC_BIGMODEL_API_KEY VOLC_ASR_API_KEY
else
  export VOICE_IME_VOLC_API_KEY="$API_KEY"
fi
export VOICE_IME_VOLC_API_KEY_SECRET="$BWS_SECRET_NAME"
COMPONENT_DIR="$HOME/.local/share/ibus/component"
IBUS_COMPONENT_PATH_VALUE="$COMPONENT_DIR:/usr/share/ibus/component"
IBUS_COMPONENT_PATH="$IBUS_COMPONENT_PATH_VALUE" \
LD_LIBRARY_PATH="$ROOT_DIR/vendor/rime/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
VOICE_IME_RIME_LIBRARY="$ROOT_DIR/vendor/rime/lib/librime.so.1" \
VOICE_IME_RIME_SHARED_DATA_DIR="$ROOT_DIR/vendor/rime/share/rime-data" \
VOICE_IME_RIME_STAGING_DIR="$ROOT_DIR/vendor/rime/build" \
VOICE_IME_RIME_USER_DATA_DIR="$HOME/.local/share/ibus-voice-ime/rime-user" \
VOICE_IME_ASR_BACKEND=volc-bigmodel-asr \
VOICE_IME_QWEN_ASR=0 \
VOICE_IME_MIMO_ASR=0 \
VOICE_IME_MIMO_CLOUD_ASR=0 \
VOICE_IME_VOLC_BIGMODEL_ASR=1 \
VOICE_IME_VOLC_API_KEY_SECRET="$BWS_SECRET_NAME" \
VOICE_IME_VOLC_BIGMODEL_BASE_URL="$BASE_URL" \
VOICE_IME_VOLC_BIGMODEL_RESOURCE_ID="$RESOURCE_ID" \
VOICE_IME_VOLC_BIGMODEL_MODEL_NAME="$MODEL_NAME" \
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

if [[ "$USE_BWS_KEY" == "1" ]]; then
  echo "已切换到火山引擎 bigmodel ASR：$MODEL_NAME @ $BASE_URL ($RESOURCE_ID)（API Key 由 bws/$BWS_SECRET_NAME 运行时注入，不写入环境文件）"
else
  echo "已切换到火山引擎 bigmodel ASR：$MODEL_NAME @ $BASE_URL ($RESOURCE_ID)"
fi
