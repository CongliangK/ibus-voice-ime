#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Switch the voice IME to the SiliconFlow (硅基流动) cloud ASR
# (POST /v1/audio/transcriptions, multipart 直传本地音频, 默认模型
# FunAudioLLM/SenseVoiceSmall，官方定价页标注免费——实际计费以控制台账单为准).
# 统一 JSON 配置时代：渠道与后端设置写入 ~/.config/ibus-voice-ime/config.json
# （asr.backend 是渠道唯一事实源）；environment.d 的渠道行只删不写回，
# systemd 用户管理器里的渠道键一律 unset，防止残留 env 压过 config.json。
# The API key (Authorization: Bearer) is injected at runtime from Bitwarden
# Secrets Manager (bws); only the secret NAME is persisted (config 的
# asr.siliconflow.api_key_secret)，never the raw key.

PYTHON="$ROOT_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3)"
fi
export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
cfg() { "$PYTHON" -m ibus_voice_ime.config "$@"; }

API_KEY="${VOICE_IME_SILICONFLOW_API_KEY:-${SILICONFLOW_API_KEY:-}}"
USE_BWS_KEY=0
BWS_ENV_FILE="${VOICE_IME_BWS_ENV_FILE:-${PI_BWS_ENV_FILE:-$HOME/.config/pi-secrets/bws.env}}"
BWS_SECRET_NAME="${VOICE_IME_SILICONFLOW_API_KEY_SECRET:-SILICONFLOW_API_KEY}"
if [[ -z "$API_KEY" && -r "$BWS_ENV_FILE" && -x "$ROOT_DIR/run-engine.sh" && $(command -v bws 2>/dev/null || true) ]]; then
  USE_BWS_KEY=1
elif [[ -z "$API_KEY" && -t 0 ]]; then
  read -rsp "请输入硅基流动 API Key（sk-...）：" API_KEY
  echo
fi
if [[ -z "$API_KEY" && "$USE_BWS_KEY" != "1" ]]; then
  cat >&2 <<'EOF_ERR'
未提供硅基流动 API Key，也没有找到可用的 Bitwarden Secrets Manager 配置。
请用以下任一方式运行：
  VOICE_IME_SILICONFLOW_API_KEY='sk-xxx' ./scripts/switch-siliconflow-asr.sh
或到 https://cloud.siliconflow.cn 创建 API Key，并配置 ~/.config/pi-secrets/bws.env，
在项目中保存 SILICONFLOW_API_KEY。
EOF_ERR
  exit 1
fi

BASE_URL="${VOICE_IME_SILICONFLOW_BASE_URL:-https://api.siliconflow.cn}"
MODEL="${VOICE_IME_SILICONFLOW_MODEL:-FunAudioLLM/SenseVoiceSmall}"
CONFIG_JSON="${VOICE_IME_CONFIG:-$HOME/.config/ibus-voice-ime/config.json}"
ENV_FILE="$HOME/.config/environment.d/ibus-voice-ime.conf"

# 渠道类 env 键（environment.d 清理与 systemctl unset 共用一份清单；
# 路径类键如 VOICE_IME_RIME_* / IBUS_COMPONENT_PATH 不在此列，照旧保留）。
CHANNEL_ENV_RE='^(VOICE_IME_ASR_BACKEND|VOICE_IME_QWEN_ASR|VOICE_IME_QWEN_ASR_[A-Z0-9_]+|VOICE_IME_MIMO_ASR|VOICE_IME_MIMO_ASR_[A-Z0-9_]+|VOICE_IME_MIMO_CLOUD[A-Z0-9_]*|VOICE_IME_MIMO_API_KEY[A-Z_]*|VOICE_IME_MIMO_BASE_URL|VOICE_IME_VOLC_[A-Z0-9_]+|VOICE_IME_SILICONFLOW[A-Z0-9_]*|PYTORCH_CUDA_ALLOC_CONF)='
CHANNEL_ENV_KEYS=(
  VOICE_IME_ASR_BACKEND
  VOICE_IME_QWEN_ASR VOICE_IME_QWEN_ASR_HOST VOICE_IME_QWEN_ASR_PORT VOICE_IME_QWEN_ASR_PYTHON
  VOICE_IME_QWEN_ASR_MODEL VOICE_IME_QWEN_ASR_MODEL_PATH VOICE_IME_QWEN_ASR_LANGUAGE
  VOICE_IME_QWEN_ASR_DTYPE VOICE_IME_QWEN_ASR_DEVICE_MAP VOICE_IME_QWEN_ASR_DEVICE_OFFLOAD_TARGET
  VOICE_IME_QWEN_ASR_MAX_BATCH VOICE_IME_QWEN_ASR_MAX_NEW_TOKENS VOICE_IME_QWEN_ASR_ATTN
  VOICE_IME_QWEN_ASR_START_TIMEOUT VOICE_IME_QWEN_ASR_TIMEOUT VOICE_IME_QWEN_ASR_FAIL_COOLDOWN
  VOICE_IME_QWEN_ASR_IDLE_TIMEOUT VOICE_IME_QWEN_ASR_IDLE_CHECK_INTERVAL
  VOICE_IME_QWEN_ASR_VRAM_MIN_MIB_1_7B VOICE_IME_QWEN_ASR_VRAM_MIN_MIB_0_6B
  VOICE_IME_MIMO_ASR VOICE_IME_MIMO_ASR_HOST VOICE_IME_MIMO_ASR_PORT VOICE_IME_MIMO_ASR_PYTHON
  VOICE_IME_MIMO_ASR_MODELS_DIR VOICE_IME_MIMO_ASR_MODEL VOICE_IME_MIMO_ASR_MODEL_PATH
  VOICE_IME_MIMO_ASR_MODEL_DEVICE_MAP VOICE_IME_MIMO_ASR_MODEL_MAX_MEMORY
  VOICE_IME_MIMO_ASR_MODEL_OFFLOAD_FOLDER VOICE_IME_MIMO_ASR_TOKENIZER
  VOICE_IME_MIMO_ASR_TOKENIZER_PATH VOICE_IME_MIMO_ASR_SOURCE VOICE_IME_MIMO_ASR_DEVICE
  VOICE_IME_MIMO_ASR_START_TIMEOUT VOICE_IME_MIMO_ASR_TIMEOUT VOICE_IME_MIMO_ASR_LANGUAGE
  VOICE_IME_MIMO_ASR_AUDIO_TAG
  VOICE_IME_MIMO_CLOUD_ASR VOICE_IME_MIMO_CLOUD_BASE_URL VOICE_IME_MIMO_CLOUD_ASR_MODEL
  VOICE_IME_MIMO_CLOUD_ASR_LANGUAGE VOICE_IME_MIMO_CLOUD_AUTH_HEADER
  VOICE_IME_MIMO_CLOUD_ASR_TIMEOUT VOICE_IME_MIMO_CLOUD_ASR_MAX_DATA_MB
  VOICE_IME_MIMO_API_KEY VOICE_IME_MIMO_API_KEY_SECRET VOICE_IME_MIMO_BASE_URL
  VOICE_IME_VOLC_BIGMODEL_ASR VOICE_IME_VOLC_BIGMODEL_BASE_URL
  VOICE_IME_VOLC_BIGMODEL_RESOURCE_ID VOICE_IME_VOLC_BIGMODEL_MODEL_NAME
  VOICE_IME_VOLC_BIGMODEL_LANGUAGE VOICE_IME_VOLC_BIGMODEL_ENABLE_ITN
  VOICE_IME_VOLC_BIGMODEL_ENABLE_PUNC VOICE_IME_VOLC_BIGMODEL_ENABLE_DDC
  VOICE_IME_VOLC_BIGMODEL_SHOW_UTTERANCES VOICE_IME_VOLC_BIGMODEL_HOTWORDS
  VOICE_IME_VOLC_BIGMODEL_HOTWORDS_MAX VOICE_IME_VOLC_BIGMODEL_BOOSTING_TABLE
  VOICE_IME_VOLC_BIGMODEL_CORRECT_TABLE VOICE_IME_VOLC_BIGMODEL_SUBMIT_TIMEOUT
  VOICE_IME_VOLC_BIGMODEL_QUERY_TIMEOUT VOICE_IME_VOLC_BIGMODEL_POLL_INTERVAL
  VOICE_IME_VOLC_BIGMODEL_TOTAL_TIMEOUT VOICE_IME_VOLC_BIGMODEL_MAX_DATA_MB
  VOICE_IME_VOLC_API_KEY VOICE_IME_VOLC_API_KEY_SECRET
  VOICE_IME_SILICONFLOW_ASR VOICE_IME_SILICONFLOW_BASE_URL VOICE_IME_SILICONFLOW_MODEL
  VOICE_IME_SILICONFLOW_TIMEOUT VOICE_IME_SILICONFLOW_MAX_DATA_MB
  VOICE_IME_SILICONFLOW_API_KEY VOICE_IME_SILICONFLOW_API_KEY_SECRET
)

# 渠道唯一事实源 = config.json 的 asr.backend：先把渠道选择与后端设置写进 JSON。
cfg set asr.backend siliconflow-asr
# 渠道互斥：显式写全五个 enabled 叶子。install.sh 迁移可能把旧
# VOICE_IME_QWEN_ASR=1 之类迁成 asr.<x>.enabled=true 残留；不清掉的话
# voice.py 渠道链（qwen 在最前，先命中先赢）会仍走旧渠道——复刻旧 env
# 互斥语义：目标渠道=1，其余=0。
cfg set asr.qwen3.enabled 0
cfg set asr.mimo.enabled 0
cfg set asr.mimo_cloud.enabled 0
cfg set asr.volc.enabled 0
cfg set asr.siliconflow.enabled 1
cfg set asr.siliconflow.base_url "$BASE_URL"
cfg set asr.siliconflow.model "$MODEL"
# 密钥名（不是密钥本身）迁入 config；BWS 运行时按该名字注入原始 Key。
cfg set asr.siliconflow.api_key_secret "$BWS_SECRET_NAME"

# environment.d 渠道行只删不写回；systemctl 用户环境同步 unset。
mkdir -p "$(dirname "$ENV_FILE")"
if [[ -f "$ENV_FILE" ]]; then
  TMP="$(mktemp)"
  grep -vE "$CHANNEL_ENV_RE" "$ENV_FILE" > "$TMP" || true
  mv "$TMP" "$ENV_FILE"
fi
chmod 600 "$ENV_FILE" 2>/dev/null || true
systemctl --user unset-environment "${CHANNEL_ENV_KEYS[@]}" 2>/dev/null || true
if [[ "$USE_BWS_KEY" != "1" ]]; then
  echo "提示：手动提供的 API Key 只注入当前运行环境，不写入任何文件；建议配置 bws/$BWS_SECRET_NAME。"
fi

pkill -f 'python.*[m]imo_asr_server\.py' 2>/dev/null || true
pkill -f 'python.*[q]wen_asr_server\.py' 2>/dev/null || true
if [[ "$USE_BWS_KEY" == "1" ]]; then
  unset VOICE_IME_SILICONFLOW_API_KEY SILICONFLOW_API_KEY
else
  # 手动提供的 Key 只注入本次 spawn 的 ibus-daemon（下方前缀）；
  # 不写入 systemd 用户管理器，避免明文 Key 对整个用户会话可见。
  export VOICE_IME_SILICONFLOW_API_KEY="$API_KEY"
fi
COMPONENT_DIR="$HOME/.local/share/ibus/component"
IBUS_COMPONENT_PATH_VALUE="$COMPONENT_DIR:/usr/share/ibus/component"
# 渠道/后端设置已入 config.json，由 run-engine.sh 进程内读取；这里只透传
# 路径类变量与 LLM 行为项（照抄原模式）。
IBUS_COMPONENT_PATH="$IBUS_COMPONENT_PATH_VALUE" \
LD_LIBRARY_PATH="$ROOT_DIR/vendor/rime/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
VOICE_IME_RIME_LIBRARY="$ROOT_DIR/vendor/rime/lib/librime.so.1" \
VOICE_IME_RIME_SHARED_DATA_DIR="$ROOT_DIR/vendor/rime/share/rime-data" \
VOICE_IME_RIME_STAGING_DIR="$ROOT_DIR/vendor/rime/build" \
VOICE_IME_RIME_USER_DATA_DIR="$HOME/.local/share/ibus-voice-ime/rime-user" \
VOICE_IME_AUTO_PUNCTUATION="${VOICE_IME_AUTO_PUNCTUATION:-0}" \
VOICE_IME_LLM_POSTPROCESS="${VOICE_IME_SWITCH_LLM_POSTPROCESS:-0}" \
VOICE_IME_LLM_INTERNAL="${VOICE_IME_SWITCH_LLM_INTERNAL:-0}" \
VOICE_IME_LLM_TRUST_OUTPUT="${VOICE_IME_SWITCH_LLM_TRUST_OUTPUT:-1}" \
VOICE_IME_LLM_RERANK="${VOICE_IME_SWITCH_LLM_RERANK:-0}" \
VOICE_IME_LLM_CANDIDATES="${VOICE_IME_SWITCH_LLM_CANDIDATES:-1}" \
VOICE_IME_LLM_BASE_URL="${VOICE_IME_SWITCH_LLM_BASE_URL:-http://127.0.0.1:18080/v1}" \
VOICE_IME_LLM_API_KEY="${VOICE_IME_SWITCH_LLM_API_KEY:-local}" \
VOICE_IME_LLM_MODEL="${VOICE_IME_SWITCH_LLM_MODEL:-qwen3.5-0.8b}" \
VOICE_IME_LLM_LOG="${VOICE_IME_LLM_LOG:-$HOME/.local/share/ibus-voice-ime/llm.jsonl}" \
"$ROOT_DIR/scripts/ibus-restart.sh" >/dev/null || true
sleep 1
ibus engine voice-custom >/dev/null 2>&1 || true

if [[ "$USE_BWS_KEY" == "1" ]]; then
  echo "已切换到硅基流动 ASR：$MODEL @ $BASE_URL（API Key 由 bws/$BWS_SECRET_NAME 运行时注入，不写入任何文件）"
else
  echo "已切换到硅基流动 ASR：$MODEL @ $BASE_URL"
fi
echo "配置已写入 config.json：$CONFIG_JSON"
echo "计费：SenseVoiceSmall 官方定价页标注免费；Qwen3-ASR 系按音频时长计费"
echo "      （如 Qwen/Qwen3-ASR-1.7B 约 ¥0.00022/秒 ≈ 0.0132 元/分钟）——一律以控制台账单为准（注册 https://cloud.siliconflow.cn）。"
echo "换模型：VOICE_IME_SILICONFLOW_MODEL='Qwen/Qwen3-ASR-1.7B' $0（注意 RPM/TPM 限流）。"
echo "提示：本后端为 Beta（未经深度测试），问题反馈/回退见 docs/asr-backends.md。"
