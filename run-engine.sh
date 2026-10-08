#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME="$ROOT_DIR/vendor/rime"

# Python sources live under src/ as the ibus_voice_ime package.
export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
ENGINE="$ROOT_DIR/src/ibus_voice_ime/engine.py"

if [[ -d "$RUNTIME/lib" ]]; then
  export LD_LIBRARY_PATH="$RUNTIME/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

LOG_DIR="$HOME/.local/share/ibus-voice-ime"
mkdir -p "$LOG_DIR"

# 日志保留策略：每次引擎启动把日志裁剪到最近 N 行（默认 1000，防止日志洪水；
# 运行中的追加侧裁剪见 log_trim 模块）。VOICE_IME_LOG_KEEP_LINES=0 可关闭。
trim_log() {
  local file="$1" keep lines tmp
  [[ -f "$file" ]] || return 0
  keep="${VOICE_IME_LOG_KEEP_LINES:-1000}"
  [[ "$keep" =~ ^[0-9]+$ ]] || keep=1000
  [[ "$keep" -gt 0 ]] || return 0
  lines="$(wc -l < "$file")"
  [[ "$lines" -gt $((keep * 2)) ]] || return 0
  tmp="$(mktemp)"
  if tail -n "$keep" "$file" > "$tmp"; then
    mv "$tmp" "$file"
  else
    rm -f "$tmp"
  fi
}
trim_log "$LOG_DIR/engine.log"
trim_log "$LOG_DIR/error.log"

exec >> "$LOG_DIR/engine.log" 2>&1

# Source the persisted user environment (written by install.sh and the
# scripts/switch-*-asr.sh helpers) so the engine always reflects the user's
# chosen ASR/IME configuration, even when ibus-daemon was respawned with a
# stale session environment.  Lines are simple KEY=VALUE and safe to source.
# RIME 四键继承优先：调用方显式 export 的救援配置（如非 Fedora 机器改用系统
# librime：VOICE_IME_RIME_LIBRARY=/usr/lib64/librime.so.1）不被文件旧值覆盖。
_INHERITED_RIME_LIBRARY="${VOICE_IME_RIME_LIBRARY:-}"
_INHERITED_RIME_SHARED_DATA_DIR="${VOICE_IME_RIME_SHARED_DATA_DIR:-}"
_INHERITED_RIME_STAGING_DIR="${VOICE_IME_RIME_STAGING_DIR:-}"
_INHERITED_RIME_USER_DATA_DIR="${VOICE_IME_RIME_USER_DATA_DIR:-}"

# 陈旧会话环境自愈（2026-08-28 打字事故根因）：ibus-daemon 继承的是登录时
# 的会话环境；仓库被迁移/删除后，其中的绝对路径全部失效，而 environment.d
# 的新值要等下次登录才进入会话环境。继承的"救援值"只有指向真实存在的路径
# 时才有效——指向不存在路径的继承值视为陈旧并忽略，让 environment.d 与
# 本仓库默认值接管。
_forget_stale_inherit() {  # $1=_INHERITED_* 变量名：值非空且路径不存在时置空
  local -n var="$1"
  if [[ -n "$var" && ! -e "$var" ]]; then
    echo "[run-engine] 自愈：继承的 $1 指向不存在的路径（$var），已忽略" >&2
    var=""
  fi
  return 0
}
_forget_stale_inherit _INHERITED_RIME_LIBRARY
_forget_stale_inherit _INHERITED_RIME_SHARED_DATA_DIR
_forget_stale_inherit _INHERITED_RIME_STAGING_DIR
_forget_stale_inherit _INHERITED_RIME_USER_DATA_DIR

ENV_CONF="$HOME/.config/environment.d/ibus-voice-ime.conf"
if [[ -r "$ENV_CONF" ]]; then
  # 行级解析替代 `set -a; source`：值含空格（HOME/仓库路径带空格）时 source 会把
  # 值当 shell 语法执行导致引擎启动失败；systemd environment.d 也不做 shell 展开。
  if [[ -x "$ROOT_DIR/scripts/env-file-load.sh" ]]; then
    eval "$("$ROOT_DIR/scripts/env-file-load.sh" "$ENV_CONF")"
  else
    set -a
    # shellcheck disable=SC1090
    source "$ENV_CONF"
    set +a
  fi
fi
[[ -n "$_INHERITED_RIME_LIBRARY" ]] && export VOICE_IME_RIME_LIBRARY="$_INHERITED_RIME_LIBRARY"
[[ -n "$_INHERITED_RIME_SHARED_DATA_DIR" ]] && export VOICE_IME_RIME_SHARED_DATA_DIR="$_INHERITED_RIME_SHARED_DATA_DIR"
[[ -n "$_INHERITED_RIME_STAGING_DIR" ]] && export VOICE_IME_RIME_STAGING_DIR="$_INHERITED_RIME_STAGING_DIR"
[[ -n "$_INHERITED_RIME_USER_DATA_DIR" ]] && export VOICE_IME_RIME_USER_DATA_DIR="$_INHERITED_RIME_USER_DATA_DIR"

# Keep the keyboard engine private to this project.  Users may override these
# explicitly, but by default we do not read system/user Rime installations.
export VOICE_IME_RIME_LIBRARY="${VOICE_IME_RIME_LIBRARY:-$RUNTIME/lib/librime.so.1}"
export VOICE_IME_RIME_SHARED_DATA_DIR="${VOICE_IME_RIME_SHARED_DATA_DIR:-$RUNTIME/share/rime-data}"
export VOICE_IME_RIME_STAGING_DIR="${VOICE_IME_RIME_STAGING_DIR:-$RUNTIME/build}"
export VOICE_IME_RIME_USER_DATA_DIR="${VOICE_IME_RIME_USER_DATA_DIR:-$HOME/.local/share/ibus-voice-ime/rime-user}"

# Voice input defaults.  setup-asr.sh may also persist these, but keeping sane
# defaults here makes old component environments work after upgrades.
export VOICE_IME_TRIGGER_MODE="${VOICE_IME_TRIGGER_MODE:-toggle}"
export VOICE_IME_HOTKEYS="${VOICE_IME_HOTKEYS:-Ctrl+Alt+V}"
# 原文语音输入热键（跳过 LLM 后处理）；未设置时引擎内默认 b（voice_hotkey.py）。
export VOICE_IME_RAW_HOTKEYS="${VOICE_IME_RAW_HOTKEYS:-Ctrl+Alt+B}"
# 粘贴触发已收敛为唯一链路：GNOME 全局快捷键 → clipboard-paste.sh → paste-file IPC。
# 此变量仅用于 voice_hotkey 的 raw 热键去重（防止 B 键配置撞上粘贴快捷键）。
export VOICE_IME_CLIPBOARD_HOTKEYS="${VOICE_IME_CLIPBOARD_HOTKEYS:-Ctrl+Alt+P}"
export VOICE_IME_CLIPBOARD_MAX_CHARS="${VOICE_IME_CLIPBOARD_MAX_CHARS:-20000}"
export VOICE_IME_MAX_RECORD_SECONDS="${VOICE_IME_MAX_RECORD_SECONDS:-300}"
export VOICE_IME_OVERLAY="${VOICE_IME_OVERLAY:-0}"
export VOICE_IME_OVERLAY_POSITION="${VOICE_IME_OVERLAY_POSITION:-top-center}"
export VOICE_IME_OVERLAY_CATCH_HOTKEY="${VOICE_IME_OVERLAY_CATCH_HOTKEY:-1}"
export VOICE_IME_OVERLAY_BUTTONS="${VOICE_IME_OVERLAY_BUTTONS:-0}"
# Use the normal IBus/GNOME vertical candidate popup by default.
# Set VOICE_IME_CANDIDATE_UI=inline only when explicitly desired.
export VOICE_IME_CANDIDATE_UI="${VOICE_IME_CANDIDATE_UI:-popup}"
export VOICE_IME_PREEDIT_MIRROR="${VOICE_IME_PREEDIT_MIRROR:-off}"
export VOICE_IME_COMMIT_DELAY_MS="${VOICE_IME_COMMIT_DELAY_MS:-200}"
export VOICE_IME_TOGGLE_SILENCE_AUTO_STOP="${VOICE_IME_TOGGLE_SILENCE_AUTO_STOP:-1}"
export VOICE_IME_TOGGLE_SILENCE_SECONDS="${VOICE_IME_TOGGLE_SILENCE_SECONDS:-2.5}"
export VOICE_IME_TOGGLE_NO_SPEECH_TIMEOUT="${VOICE_IME_TOGGLE_NO_SPEECH_TIMEOUT:-8}"
# Audio preprocessing (sox) applied to the recorded WAV before ASR.  Master
# switch plus per-stage toggles; all fail safe (fall back to original audio).
export VOICE_IME_AUDIO_PREPROCESS="${VOICE_IME_AUDIO_PREPROCESS:-1}"
export VOICE_IME_AUDIO_NORMALIZE="${VOICE_IME_AUDIO_NORMALIZE:-1}"
export VOICE_IME_AUDIO_HIGHPASS="${VOICE_IME_AUDIO_HIGHPASS:-1}"
export VOICE_IME_AUDIO_DENOISE="${VOICE_IME_AUDIO_DENOISE:-0}"
export VOICE_IME_AUDIO_HIGHPASS_FREQ="${VOICE_IME_AUDIO_HIGHPASS_FREQ:-80}"
export VOICE_IME_AUDIO_NORMALIZE_HEADROOM="${VOICE_IME_AUDIO_NORMALIZE_HEADROOM:-3}"
export VOICE_IME_AUDIO_DENOISE_AMOUNT="${VOICE_IME_AUDIO_DENOISE_AMOUNT:-0.3}"
export VOICE_IME_AUDIO_NOISE_PROFILE_MS="${VOICE_IME_AUDIO_NOISE_PROFILE_MS:-400}"
# Denoise tier (2026-08-26 实测定案，与视频产线同源经验；详见
# src/ibus_voice_ime/asr/audio_preprocess.py 模块文档)：
#   none    = 旧行为（highpass + 可选 noisered + normalize）
#   notch   = 50/100/150Hz 工频哼声陷波（纯 sox，零新依赖）
#   rnnoise = 陷波 + RNNoise(bd)（ffmpeg arnndn，模型 vendor/models/rnnoise/，
#             由 scripts/fetch-rnnoise-model.sh 下载）；缺件自动降级 notch
export VOICE_IME_DENOISE_TIER="${VOICE_IME_DENOISE_TIER:-rnnoise}"
# 录音直采设备：默认走系统默认源（PipeWire/Pulse 兼容性最好）。想绕开默认源
# 漂移（蓝牙抢占等）可设为指定 ALSA 卡，如 plughw:M2,0（48k 设备须 plughw
# 前缀自动重采样 16k，不能写 hw:M2,0）；设备不存在时引擎会自动回退默认源。
export VOICE_IME_ARECORD_DEVICE="${VOICE_IME_ARECORD_DEVICE:-default}"
export VOICE_IME_VOICE_MODE="${VOICE_IME_VOICE_MODE:-dictation}"
export VOICE_IME_AUTO_PUNCTUATION="${VOICE_IME_AUTO_PUNCTUATION:-0}"
export VOICE_IME_AUTO_PUNCT_LEVEL="${VOICE_IME_AUTO_PUNCT_LEVEL:-aggressive}"
export VOICE_IME_AUTO_PUNCT_MIN_CJK="${VOICE_IME_AUTO_PUNCT_MIN_CJK:-6}"
export VOICE_IME_AUTO_PUNCT_MAX_CJK_PER_CLAUSE="${VOICE_IME_AUTO_PUNCT_MAX_CJK_PER_CLAUSE:-24}"
export VOICE_IME_CHINESE_SCRIPT="${VOICE_IME_CHINESE_SCRIPT:-simplified}"

# ASR backend selection: honor the persisted environment.d config (sourced
# above) or inherited values.  Default to the project default (local
# Qwen3-ASR 1.7B) only when nothing was configured.  The API key is not
# stored here; when MiMo cloud ASR is selected, the wrapper below can inject it
# just-in-time from BWS.
export VOICE_IME_ASR_BACKEND="${VOICE_IME_ASR_BACKEND:-qwen3-asr}"
export VOICE_IME_QWEN_ASR="${VOICE_IME_QWEN_ASR:-1}"
export VOICE_IME_MIMO_ASR="${VOICE_IME_MIMO_ASR:-0}"
export VOICE_IME_MIMO_CLOUD_ASR="${VOICE_IME_MIMO_CLOUD_ASR:-0}"
export VOICE_IME_QWEN_ASR_MODEL="${VOICE_IME_QWEN_ASR_MODEL:-1.7b}"
# QWEN 同类自愈：陈旧的继承路径（仓库迁移残留）不得压过 env 文件/默认值。
[[ -n "$VOICE_IME_QWEN_ASR_MODEL_PATH" && ! -e "$VOICE_IME_QWEN_ASR_MODEL_PATH" ]] && {
  echo "[run-engine] 自愈：继承的 VOICE_IME_QWEN_ASR_MODEL_PATH 指向不存在的路径，已忽略" >&2
  unset VOICE_IME_QWEN_ASR_MODEL_PATH
}
export VOICE_IME_QWEN_ASR_MODEL_PATH="${VOICE_IME_QWEN_ASR_MODEL_PATH:-$ROOT_DIR/vendor/models/qwen3-asr/Qwen3-ASR-1.7B}"
export VOICE_IME_QWEN_ASR_PYTHON="${VOICE_IME_QWEN_ASR_PYTHON:-$ROOT_DIR/.venv-qwen-asr/bin/python}"
export VOICE_IME_QWEN_ASR_HOST="${VOICE_IME_QWEN_ASR_HOST:-127.0.0.1}"
export VOICE_IME_QWEN_ASR_PORT="${VOICE_IME_QWEN_ASR_PORT:-18081}"
export VOICE_IME_QWEN_ASR_LANGUAGE="${VOICE_IME_QWEN_ASR_LANGUAGE:-Chinese}"
export VOICE_IME_QWEN_ASR_DTYPE="${VOICE_IME_QWEN_ASR_DTYPE:-bfloat16}"
export VOICE_IME_QWEN_ASR_DEVICE_MAP="${VOICE_IME_QWEN_ASR_DEVICE_MAP:-cuda:0}"
# 256 会把约 250~450 字的长听写硬截断（余下语音静默丢弃），与 server 侧默认对齐 1024。
export VOICE_IME_QWEN_ASR_MAX_NEW_TOKENS="${VOICE_IME_QWEN_ASR_MAX_NEW_TOKENS:-1024}"
export VOICE_IME_QWEN_ASR_START_TIMEOUT="${VOICE_IME_QWEN_ASR_START_TIMEOUT:-180}"
export VOICE_IME_QWEN_ASR_TIMEOUT="${VOICE_IME_QWEN_ASR_TIMEOUT:-180}"
# Use an expandable-segment CUDA allocator so that after the idle watchdog
# moves the model to CPU RAM, ``torch.cuda.empty_cache()`` can actually return
# the reserved VRAM to the system.  Without this, post-inference cached blocks
# stay reserved (~3 GB on the 1.7B model) and are never released.
export PYTORCH_CUDA_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}"
export VOICE_IME_MIMO_CLOUD_BASE_URL="${VOICE_IME_MIMO_CLOUD_BASE_URL:-https://token-plan-cn.xiaomimimo.com/v1}"
export VOICE_IME_MIMO_CLOUD_ASR_MODEL="${VOICE_IME_MIMO_CLOUD_ASR_MODEL:-mimo-v2.5-asr}"
export VOICE_IME_MIMO_CLOUD_ASR_LANGUAGE="${VOICE_IME_MIMO_CLOUD_ASR_LANGUAGE:-auto}"
export VOICE_IME_MIMO_CLOUD_AUTH_HEADER="${VOICE_IME_MIMO_CLOUD_AUTH_HEADER:-api-key}"
export VOICE_IME_MIMO_CLOUD_ASR_TIMEOUT="${VOICE_IME_MIMO_CLOUD_ASR_TIMEOUT:-120}"
export VOICE_IME_MIMO_API_KEY_SECRET="${VOICE_IME_MIMO_API_KEY_SECRET:-XIAOMI_TOKEN_PLAN_CN_API_KEY}"

# Volcano Engine (豆包) bigmodel recording-file ASR (turbo, base64 直传).
# Default off; enabled via scripts/switch-volc-bigmodel-asr.sh.  The API key is
# injected at runtime from BWS (VOICE_IME_VOLC_API_KEY_SECRET) below, never
# persisted in environment.d.
export VOICE_IME_VOLC_BIGMODEL_ASR="${VOICE_IME_VOLC_BIGMODEL_ASR:-0}"
export VOICE_IME_VOLC_BIGMODEL_BASE_URL="${VOICE_IME_VOLC_BIGMODEL_BASE_URL:-https://openspeech.bytedance.com}"
export VOICE_IME_VOLC_BIGMODEL_RESOURCE_ID="${VOICE_IME_VOLC_BIGMODEL_RESOURCE_ID:-volc.bigasr.auc_turbo}"
export VOICE_IME_VOLC_BIGMODEL_MODEL_NAME="${VOICE_IME_VOLC_BIGMODEL_MODEL_NAME:-bigmodel}"
export VOICE_IME_VOLC_API_KEY_SECRET="${VOICE_IME_VOLC_API_KEY_SECRET:-VOLC_BIGMODEL_ASR_API_KEY}"

# SiliconFlow (硅基流动) cloud ASR (multipart 直传, 默认免费模型 SenseVoiceSmall).
# Default off; enabled via scripts/switch-siliconflow-asr.sh.  The API key is
# injected at runtime from BWS (VOICE_IME_SILICONFLOW_API_KEY_SECRET) below,
# never persisted in environment.d.
export VOICE_IME_SILICONFLOW_ASR="${VOICE_IME_SILICONFLOW_ASR:-0}"
export VOICE_IME_SILICONFLOW_BASE_URL="${VOICE_IME_SILICONFLOW_BASE_URL:-https://api.siliconflow.cn}"
export VOICE_IME_SILICONFLOW_MODEL="${VOICE_IME_SILICONFLOW_MODEL:-FunAudioLLM/SenseVoiceSmall}"
export VOICE_IME_SILICONFLOW_API_KEY_SECRET="${VOICE_IME_SILICONFLOW_API_KEY_SECRET:-SILICONFLOW_API_KEY}"
export VOICE_IME_SILICONFLOW_TIMEOUT="${VOICE_IME_SILICONFLOW_TIMEOUT:-120}"

# LLM 后处理策略：云端 OpenAI 兼容接口是唯一受支持的开启方式，通过
# ~/.config/ibus-voice-ime/llm.json 显式配置（scripts/setup-llm-cloud.sh 生成，
# 含 base_url + api_key + 精确 model ID，不做模型列表查询）。该文件存在且合法时，
# 其配置优先于下面所有 VOICE_IME_LLM_* 环境变量。
# 本地 llama.cpp 小模型实测会改坏听写原文，遗留 env/sidecar 实验路径在此
# 一律压制：忽略继承的 VOICE_IME_LLM_POSTPROCESS/VOICE_IME_LLM_INTERNAL。
export VOICE_IME_LLM_CONFIG="${VOICE_IME_LLM_CONFIG:-$HOME/.config/ibus-voice-ime/llm.json}"
export VOICE_IME_LLM_POSTPROCESS=0
export VOICE_IME_LLM_INTERNAL=0
export VOICE_IME_LLM_RERANK=0
export VOICE_IME_LLM_TRUST_OUTPUT="${VOICE_IME_LLM_TRUST_OUTPUT:-1}"
export VOICE_IME_LLM_BASE_URL="${VOICE_IME_LLM_BASE_URL:-http://127.0.0.1:18080/v1}"
export VOICE_IME_LLM_API_KEY="${VOICE_IME_LLM_API_KEY:-local}"
export VOICE_IME_LLM_MODEL="${VOICE_IME_LLM_MODEL:-qwen3.5-0.8b}"
export VOICE_IME_LLM_CANDIDATES="${VOICE_IME_LLM_CANDIDATES:-1}"
export VOICE_IME_LLM_TIMEOUT="${VOICE_IME_LLM_TIMEOUT:-4}"
export VOICE_IME_LLM_TEMPERATURE="${VOICE_IME_LLM_TEMPERATURE:-0.1}"
export VOICE_IME_LLM_MIN_CHARS="${VOICE_IME_LLM_MIN_CHARS:-50}"
export VOICE_IME_LLM_FALLBACK_RAW="${VOICE_IME_LLM_FALLBACK_RAW:-1}"
export VOICE_IME_LLM_LOG="${VOICE_IME_LLM_LOG:-$LOG_DIR/llm.jsonl}"
export VOICE_IME_LLM_AGGRESSIVE="${VOICE_IME_LLM_AGGRESSIVE:-0}"
export VOICE_IME_LLM_STRICT_SAFETY="${VOICE_IME_LLM_STRICT_SAFETY:-1}"
export VOICE_IME_LLM_CONSERVATIVE_MARGIN="${VOICE_IME_LLM_CONSERVATIVE_MARGIN:-18}"
export VOICE_IME_LLM_CONSERVATIVE_MIN_SIMILARITY="${VOICE_IME_LLM_CONSERVATIVE_MIN_SIMILARITY:-0.88}"
export VOICE_IME_LLM_CONSERVATIVE_MIN_RATIO="${VOICE_IME_LLM_CONSERVATIVE_MIN_RATIO:-0.78}"
export VOICE_IME_LLM_CONSERVATIVE_MAX_RATIO="${VOICE_IME_LLM_CONSERVATIVE_MAX_RATIO:-1.18}"
export VOICE_IME_LLAMA_PORT="${VOICE_IME_LLAMA_PORT:-18080}"
export VOICE_IME_LLAMA_CTX_SIZE="${VOICE_IME_LLAMA_CTX_SIZE:-2048}"

# faster-whisper GPU STT diagnostic defaults.  Runtime transcription defaults
# to the local Qwen3-ASR sidecar above; these values are kept only for
# explicit diagnostic scripts/manual experiments outside the IBus engine.
export VOICE_IME_WHISPER_MODEL="${VOICE_IME_WHISPER_MODEL:-small}"
export VOICE_IME_WHISPER_DEVICE="${VOICE_IME_WHISPER_DEVICE:-cuda}"
export VOICE_IME_WHISPER_DEVICE_INDEX="${VOICE_IME_WHISPER_DEVICE_INDEX:-0}"
export VOICE_IME_WHISPER_COMPUTE="${VOICE_IME_WHISPER_COMPUTE:-float16}"
export VOICE_IME_WHISPER_LANGUAGE="${VOICE_IME_WHISPER_LANGUAGE:-zh}"
export VOICE_IME_REQUIRE_GPU_STT="${VOICE_IME_REQUIRE_GPU_STT:-1}"

PYTHON="$ROOT_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3)"
fi

# If CUDA runtime libraries are installed through NVIDIA pip wheels, make them
# visible to CTranslate2 before Python starts.  System CUDA installs continue to
# work through the regular loader paths.
CUDA_PIP_LIB_PATH="$("$PYTHON" - <<'PY' 2>/dev/null || true
import os
paths = []
for modname in ("nvidia.cublas.lib", "nvidia.cudnn.lib", "nvidia.cuda_nvrtc.lib"):
    try:
        mod = __import__(modname, fromlist=["__path__", "__file__"])
        candidates = list(getattr(mod, "__path__", []) or [])
        file = getattr(mod, "__file__", None)
        if file:
            candidates.append(os.path.dirname(file))
        for path in candidates:
            if path and os.path.isdir(path) and path not in paths:
                paths.append(path)
    except Exception:
        pass
print(":".join(paths))
PY
)"
if [[ -n "$CUDA_PIP_LIB_PATH" ]]; then
  export LD_LIBRARY_PATH="$CUDA_PIP_LIB_PATH${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

# If a cloud ASR backend is selected but its API key is not present in the
# environment, load it just-in-time from Bitwarden Secrets Manager.  This keeps
# keys out of persistent environment.d files; only this engine process
# receives the injected secret(s).  MiMo cloud, Volcano bigmodel and
# SiliconFlow ASR are all supported, and a single bws run wrapper can inject
# several if several are somehow configured.
ASR_BACKEND_LC="${VOICE_IME_ASR_BACKEND,,}"
MIMO_ACTIVE=0
VOLC_ACTIVE=0
SILICONFLOW_ACTIVE=0
if [[ -z "${VOICE_IME_MIMO_API_KEY:-${MIMO_API_KEY:-}}" ]] && \
   [[ "$ASR_BACKEND_LC" == "mimo-cloud" || "$ASR_BACKEND_LC" == "mimo-cloud-asr" || "$ASR_BACKEND_LC" == "mimo-api" || "$ASR_BACKEND_LC" == "mimo-api-asr" || "$ASR_BACKEND_LC" == "mimo-tokenplan-asr" || "${VOICE_IME_MIMO_CLOUD_ASR:-0}" == "1" ]]; then
  MIMO_ACTIVE=1
fi
if [[ -z "${VOICE_IME_VOLC_API_KEY:-${VOLC_BIGMODEL_API_KEY:-${VOLC_ASR_API_KEY:-}}}" ]] && \
   [[ "$ASR_BACKEND_LC" == "volc" || "$ASR_BACKEND_LC" == "volc-asr" || "$ASR_BACKEND_LC" == "volc-engine-asr" || "$ASR_BACKEND_LC" == "volc-bigmodel" || "$ASR_BACKEND_LC" == "volc-bigmodel-asr" || "$ASR_BACKEND_LC" == "doubao-asr" || "$ASR_BACKEND_LC" == "doubao-bigmodel-asr" || "${VOICE_IME_VOLC_BIGMODEL_ASR:-0}" == "1" ]]; then
  VOLC_ACTIVE=1
fi
if [[ -z "${VOICE_IME_SILICONFLOW_API_KEY:-${SILICONFLOW_API_KEY:-}}" ]] && \
   [[ "$ASR_BACKEND_LC" == "siliconflow" || "$ASR_BACKEND_LC" == "siliconflow-asr" || "$ASR_BACKEND_LC" == "sf-asr" || "${VOICE_IME_SILICONFLOW_ASR:-0}" == "1" ]]; then
  SILICONFLOW_ACTIVE=1
fi
if [[ "$MIMO_ACTIVE" == "1" || "$VOLC_ACTIVE" == "1" || "$SILICONFLOW_ACTIVE" == "1" ]]; then
  BWS_ENV_FILE="${VOICE_IME_BWS_ENV_FILE:-${PI_BWS_ENV_FILE:-$HOME/.config/pi-secrets/bws.env}}"
  if [[ -r "$BWS_ENV_FILE" ]] && command -v bws >/dev/null 2>&1; then
    if [[ -x "$ROOT_DIR/scripts/env-file-load.sh" ]]; then
      eval "$("$ROOT_DIR/scripts/env-file-load.sh" "$BWS_ENV_FILE")"
    else
      set -a
      # shellcheck disable=SC1090
      source "$BWS_ENV_FILE"
      set +a
    fi
    BWS_PROJECT_ID="${VOICE_IME_BWS_PROJECT_ID:-${BWS_PI_PROJECT_ID:-}}"
    if [[ -n "${BWS_ACCESS_TOKEN:-}" && -n "$BWS_PROJECT_ID" ]]; then
      PY_CMD=("$PYTHON" "$ENGINE" "$@")
      printf -v PY_CMD_Q '%q ' "${PY_CMD[@]}"
      # Build the in-wrapper env injection lines for each active backend.
      BWS_INJECT=""
      if [[ "$MIMO_ACTIVE" == "1" ]]; then
        MIMO_SECRET="${VOICE_IME_MIMO_API_KEY_SECRET:-XIAOMI_TOKEN_PLAN_CN_API_KEY}"
        BWS_INJECT+="if [[ -z \"\${VOICE_IME_MIMO_API_KEY:-}\" && -n \"\${${MIMO_SECRET}:-}\" ]]; then export VOICE_IME_MIMO_API_KEY=\"\${${MIMO_SECRET}}\"; fi; "
      fi
      if [[ "$VOLC_ACTIVE" == "1" ]]; then
        VOLC_SECRET="${VOICE_IME_VOLC_API_KEY_SECRET:-VOLC_BIGMODEL_ASR_API_KEY}"
        BWS_INJECT+="if [[ -z \"\${VOICE_IME_VOLC_API_KEY:-}\" && -n \"\${${VOLC_SECRET}:-}\" ]]; then export VOICE_IME_VOLC_API_KEY=\"\${${VOLC_SECRET}}\"; fi; "
      fi
      if [[ "$SILICONFLOW_ACTIVE" == "1" ]]; then
        SILICONFLOW_SECRET="${VOICE_IME_SILICONFLOW_API_KEY_SECRET:-SILICONFLOW_API_KEY}"
        BWS_INJECT+="if [[ -z \"\${VOICE_IME_SILICONFLOW_API_KEY:-}\" && -n \"\${${SILICONFLOW_SECRET}:-}\" ]]; then export VOICE_IME_SILICONFLOW_API_KEY=\"\${${SILICONFLOW_SECRET}}\"; fi; "
      fi
      exec bws run --project-id "$BWS_PROJECT_ID" --shell bash -- "
${BWS_INJECT}
exec $PY_CMD_Q
"
    fi
  fi
fi

exec "$PYTHON" "$ENGINE" "$@"
