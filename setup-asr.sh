#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT_DIR/.venv"
chmod +x "$ROOT_DIR/src/ibus_voice_ime/engine.py" "$ROOT_DIR/run-engine.sh" "$ROOT_DIR/voice-toggle.sh" "$ROOT_DIR/scripts/install-gnome-voice-hotkey.sh" 2>/dev/null || chmod +x "$ROOT_DIR/src/ibus_voice_ime/engine.py"
python3 -m venv --system-site-packages "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install -r "$ROOT_DIR/requirements-asr.txt"

# Prefer GPU STT.  CTranslate2/faster-whisper use CUDA on NVIDIA GPUs when the
# CUDA backend is visible; keep CPU as an explicit opt-out only.
read -r DETECTED_WHISPER_DEVICE DETECTED_WHISPER_COMPUTE < <("$VENV/bin/python" - <<'PY'
try:
    import ctranslate2  # type: ignore
    if ctranslate2.get_cuda_device_count() > 0:
        print("cuda float16")
    else:
        print("cpu int8")
except Exception:
    print("cpu int8")
PY
)
ASR_WHISPER_MODEL="${VOICE_IME_WHISPER_MODEL:-large-v3}"
ASR_WHISPER_DEVICE="${VOICE_IME_WHISPER_DEVICE:-$DETECTED_WHISPER_DEVICE}"
ASR_WHISPER_DEVICE_INDEX="${VOICE_IME_WHISPER_DEVICE_INDEX:-0}"
ASR_WHISPER_COMPUTE="${VOICE_IME_WHISPER_COMPUTE:-$DETECTED_WHISPER_COMPUTE}"
ASR_WHISPER_LANGUAGE="${VOICE_IME_WHISPER_LANGUAGE:-zh}"
ASR_REQUIRE_GPU="${VOICE_IME_REQUIRE_GPU_STT:-1}"
if [[ "$ASR_REQUIRE_GPU" != "0" && "$ASR_WHISPER_DEVICE" != "cuda" ]]; then
  cat >&2 <<'EOF_ERR'
未检测到可用 CUDA GPU，已按要求拒绝配置 CPU STT。
请确认：NVIDIA 驱动正常、nvidia-smi 可用、faster-whisper/ctranslate2 可看到 CUDA。
如确实要临时退回 CPU，可执行：VOICE_IME_REQUIRE_GPU_STT=0 VOICE_IME_WHISPER_DEVICE=cpu ./setup-asr.sh
EOF_ERR
  exit 1
fi

CUDA_PIP_LIB_PATH="$("$VENV/bin/python" - <<'PY' 2>/dev/null || true
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

# Re-generate component XML so IBus uses the venv Python that contains faster-whisper.
COMPONENT_DIR="$HOME/.local/share/ibus/component"
mkdir -p "$COMPONENT_DIR"
PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" "$VENV/bin/python" "$ROOT_DIR/src/ibus_voice_ime/engine.py" --xml > "$COMPONENT_DIR/voice-custom.xml"
IBUS_COMPONENT_PATH_VALUE="$COMPONENT_DIR:/usr/share/ibus/component"
mkdir -p "$HOME/.config/environment.d"
cat > "$HOME/.config/environment.d/ibus-voice-ime.conf" <<EOF_ENV
IBUS_COMPONENT_PATH=$IBUS_COMPONENT_PATH_VALUE
VOICE_IME_RIME_LIBRARY=$ROOT_DIR/vendor/rime/lib/librime.so.1
VOICE_IME_RIME_SHARED_DATA_DIR=$ROOT_DIR/vendor/rime/share/rime-data
VOICE_IME_RIME_STAGING_DIR=$ROOT_DIR/vendor/rime/build
VOICE_IME_RIME_USER_DATA_DIR=$HOME/.local/share/ibus-voice-ime/rime-user
VOICE_IME_ASR_BACKEND=mimo-cloud-asr
VOICE_IME_QWEN_ASR=0
VOICE_IME_MIMO_ASR=0
VOICE_IME_MIMO_CLOUD_ASR=1
VOICE_IME_MIMO_CLOUD_BASE_URL=https://token-plan-cn.xiaomimimo.com/v1
VOICE_IME_MIMO_CLOUD_ASR_MODEL=mimo-v2.5-asr
VOICE_IME_MIMO_CLOUD_ASR_LANGUAGE=auto
VOICE_IME_MIMO_CLOUD_AUTH_HEADER=api-key
VOICE_IME_MIMO_CLOUD_ASR_TIMEOUT=120
VOICE_IME_MIMO_API_KEY_SECRET=XIAOMI_TOKEN_PLAN_CN_API_KEY
VOICE_IME_WHISPER_MODEL=$ASR_WHISPER_MODEL
VOICE_IME_WHISPER_DEVICE=$ASR_WHISPER_DEVICE
VOICE_IME_WHISPER_DEVICE_INDEX=$ASR_WHISPER_DEVICE_INDEX
VOICE_IME_WHISPER_COMPUTE=$ASR_WHISPER_COMPUTE
VOICE_IME_WHISPER_LANGUAGE=$ASR_WHISPER_LANGUAGE
VOICE_IME_REQUIRE_GPU_STT=$ASR_REQUIRE_GPU
VOICE_IME_RECORD_SECONDS=5
VOICE_IME_TRIGGER_MODE=toggle
VOICE_IME_HOTKEYS=Ctrl+Alt+V
VOICE_IME_MAX_RECORD_SECONDS=120
VOICE_IME_OVERLAY=0
VOICE_IME_OVERLAY_CATCH_HOTKEY=1
VOICE_IME_OVERLAY_BUTTONS=0
VOICE_IME_COMMIT_DELAY_MS=200
VOICE_IME_TOGGLE_SILENCE_AUTO_STOP=1
VOICE_IME_TOGGLE_SILENCE_SECONDS=2.5
VOICE_IME_TOGGLE_NO_SPEECH_TIMEOUT=8
VOICE_IME_VAD_AUTO_STOP=0
VOICE_IME_VOICE_MODE=dictation
VOICE_IME_VOICE_COMMANDS=1
VOICE_IME_AUTO_PUNCTUATION=0
VOICE_IME_LLM_POSTPROCESS=0
VOICE_IME_LLM_INTERNAL=1
VOICE_IME_LLM_RERANK=1
VOICE_IME_LLM_BASE_URL=http://127.0.0.1:18080/v1
VOICE_IME_LLM_API_KEY=local
VOICE_IME_LLM_MODEL=qwen3.5-0.8b
VOICE_IME_LLM_CANDIDATES=3
EOF_ENV
if [[ -n "$CUDA_PIP_LIB_PATH" ]]; then
  echo "LD_LIBRARY_PATH=$CUDA_PIP_LIB_PATH${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" >> "$HOME/.config/environment.d/ibus-voice-ime.conf"
fi
systemctl --user set-environment \
  "IBUS_COMPONENT_PATH=$IBUS_COMPONENT_PATH_VALUE" \
  "VOICE_IME_RIME_LIBRARY=$ROOT_DIR/vendor/rime/lib/librime.so.1" \
  "VOICE_IME_RIME_SHARED_DATA_DIR=$ROOT_DIR/vendor/rime/share/rime-data" \
  "VOICE_IME_RIME_STAGING_DIR=$ROOT_DIR/vendor/rime/build" \
  "VOICE_IME_RIME_USER_DATA_DIR=$HOME/.local/share/ibus-voice-ime/rime-user" \
  VOICE_IME_ASR_BACKEND=mimo-cloud-asr \
  VOICE_IME_QWEN_ASR=0 \
  VOICE_IME_MIMO_ASR=0 \
  VOICE_IME_MIMO_CLOUD_ASR=1 \
  VOICE_IME_MIMO_CLOUD_BASE_URL=https://token-plan-cn.xiaomimimo.com/v1 \
  VOICE_IME_MIMO_CLOUD_ASR_MODEL=mimo-v2.5-asr \
  VOICE_IME_MIMO_CLOUD_ASR_LANGUAGE=auto \
  VOICE_IME_MIMO_CLOUD_AUTH_HEADER=api-key \
  VOICE_IME_MIMO_CLOUD_ASR_TIMEOUT=120 \
  VOICE_IME_MIMO_API_KEY_SECRET=XIAOMI_TOKEN_PLAN_CN_API_KEY \
  "VOICE_IME_WHISPER_MODEL=$ASR_WHISPER_MODEL" \
  "VOICE_IME_WHISPER_DEVICE=$ASR_WHISPER_DEVICE" \
  "VOICE_IME_WHISPER_DEVICE_INDEX=$ASR_WHISPER_DEVICE_INDEX" \
  "VOICE_IME_WHISPER_COMPUTE=$ASR_WHISPER_COMPUTE" \
  "VOICE_IME_WHISPER_LANGUAGE=$ASR_WHISPER_LANGUAGE" \
  "VOICE_IME_REQUIRE_GPU_STT=$ASR_REQUIRE_GPU" \
  VOICE_IME_RECORD_SECONDS=5 \
  VOICE_IME_TRIGGER_MODE=toggle \
  "VOICE_IME_HOTKEYS=Ctrl+Alt+V" \
  VOICE_IME_MAX_RECORD_SECONDS=120 \
  VOICE_IME_OVERLAY=0 \
  VOICE_IME_OVERLAY_CATCH_HOTKEY=1 \
  VOICE_IME_OVERLAY_BUTTONS=0 \
  VOICE_IME_COMMIT_DELAY_MS=200 \
  VOICE_IME_TOGGLE_SILENCE_AUTO_STOP=1 \
  VOICE_IME_TOGGLE_SILENCE_SECONDS=2.5 \
  VOICE_IME_TOGGLE_NO_SPEECH_TIMEOUT=8 \
  VOICE_IME_VAD_AUTO_STOP=0 \
  VOICE_IME_VOICE_MODE=dictation \
  VOICE_IME_VOICE_COMMANDS=1 \
  VOICE_IME_AUTO_PUNCTUATION=0 \
  VOICE_IME_LLM_POSTPROCESS=0 \
  VOICE_IME_LLM_INTERNAL=1 \
  VOICE_IME_LLM_RERANK=1 \
  VOICE_IME_LLM_BASE_URL=http://127.0.0.1:18080/v1 \
  VOICE_IME_LLM_API_KEY=local \
  VOICE_IME_LLM_MODEL=qwen3.5-0.8b \
  VOICE_IME_LLM_CANDIDATES=3 2>/dev/null || true
pkill -f '[q]wen_asr_server.py' 2>/dev/null || true
pkill -f '[m]imo_asr_server.py' 2>/dev/null || true
IBUS_COMPONENT_PATH="$IBUS_COMPONENT_PATH_VALUE" ibus write-cache >/dev/null 2>&1 || true
"$ROOT_DIR/scripts/install-gnome-voice-hotkey.sh" || true

cat <<EOF
ASR 依赖安装完成。可选环境变量：
  VOICE_IME_WHISPER_MODEL=tiny|base|small|medium|large-v3|large-v3-turbo（默认 large-v3，Whisper 最大通用模型）
  VOICE_IME_WHISPER_DEVICE=cuda                    GPU STT；如需 CPU 必须显式改成 cpu
  VOICE_IME_WHISPER_DEVICE_INDEX=0                 NVIDIA GPU 序号
  VOICE_IME_WHISPER_COMPUTE=float16                RTX 4080 推荐 float16；省显存可用 int8_float16
  VOICE_IME_REQUIRE_GPU_STT=1                      防止误回退到 CPU
  VOICE_IME_WHISPER_LANGUAGE=zh                    默认 zh，设为空可自动检测
  VOICE_IME_RECORD_SECONDS=5                       fixed 模式录音时长
  VOICE_IME_TRIGGER_MODE=toggle|fixed              默认 toggle：按一次开始，再按一次停止
  VOICE_IME_MAX_RECORD_SECONDS=120                 toggle 模式最长录音
  VOICE_IME_HOTKEYS=Ctrl+Alt+V                    语音热键；仅支持 Ctrl+Alt+字母
  VOICE_IME_OVERLAY=0                              默认关闭语音状态弹窗；设为 1 可重新开启
  VOICE_IME_OVERLAY_CATCH_HOTKEY=1                 弹窗开启且获得焦点时也能用同一语音热键结束
  VOICE_IME_OVERLAY_BUTTONS=0                      默认隐藏按钮，避免鼠标点击导致焦点丢失
  VOICE_IME_COMMIT_DELAY_MS=200                    隐藏弹窗后延迟提交，等待焦点恢复
  VOICE_IME_TOGGLE_SILENCE_AUTO_STOP=1             toggle 模式静音自动停止兜底
  VOICE_IME_TOGGLE_SILENCE_SECONDS=2.5             静音多久后自动停止
  VOICE_IME_VAD_AUTO_STOP=1                        fixed 模式启用简易静音自动停止
  VOICE_IME_VOICE_MODE=dictation|literal|markdown|prompt|command
  VOICE_IME_AUTO_PUNCTUATION=0                    本地自动补标点已禁用
  VOICE_IME_LLM_POSTPROCESS=1                      启用 LLM 后处理
  VOICE_IME_LLM_INTERNAL=1                          默认使用输入法内置 llama.cpp sidecar
  VOICE_IME_LLM_BASE_URL=http://127.0.0.1:18080/v1 内置 OpenAI-compatible 地址
  VOICE_IME_LLM_MODEL=qwen3.5-0.8b
默认 STT：MiMo 云端 ASR（mimo-v2.5-asr @ token-plan-cn）；本地 faster-whisper 兜底已安装为 $ASR_WHISPER_MODEL / $ASR_WHISPER_DEVICE:$ASR_WHISPER_DEVICE_INDEX / $ASR_WHISPER_COMPUTE；LLM 默认关闭，可运行 scripts/setup-llm.sh 启用。
EOF
IBUS_COMPONENT_PATH="$IBUS_COMPONENT_PATH_VALUE" \
LD_LIBRARY_PATH="$ROOT_DIR/vendor/rime/lib${CUDA_PIP_LIB_PATH:+:$CUDA_PIP_LIB_PATH}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
VOICE_IME_RIME_LIBRARY="$ROOT_DIR/vendor/rime/lib/librime.so.1" \
VOICE_IME_RIME_SHARED_DATA_DIR="$ROOT_DIR/vendor/rime/share/rime-data" \
VOICE_IME_RIME_STAGING_DIR="$ROOT_DIR/vendor/rime/build" \
VOICE_IME_RIME_USER_DATA_DIR="$HOME/.local/share/ibus-voice-ime/rime-user" \
VOICE_IME_ASR_BACKEND=mimo-cloud-asr \
VOICE_IME_QWEN_ASR=0 \
VOICE_IME_MIMO_ASR=0 \
VOICE_IME_MIMO_CLOUD_ASR=1 \
VOICE_IME_MIMO_CLOUD_BASE_URL=https://token-plan-cn.xiaomimimo.com/v1 \
VOICE_IME_MIMO_CLOUD_ASR_MODEL=mimo-v2.5-asr \
VOICE_IME_MIMO_CLOUD_ASR_LANGUAGE=auto \
VOICE_IME_MIMO_CLOUD_AUTH_HEADER=api-key \
VOICE_IME_MIMO_CLOUD_ASR_TIMEOUT=120 \
VOICE_IME_MIMO_API_KEY_SECRET=XIAOMI_TOKEN_PLAN_CN_API_KEY \
VOICE_IME_WHISPER_MODEL="$ASR_WHISPER_MODEL" \
VOICE_IME_WHISPER_DEVICE="$ASR_WHISPER_DEVICE" \
VOICE_IME_WHISPER_DEVICE_INDEX="$ASR_WHISPER_DEVICE_INDEX" \
VOICE_IME_WHISPER_COMPUTE="$ASR_WHISPER_COMPUTE" \
VOICE_IME_WHISPER_LANGUAGE="$ASR_WHISPER_LANGUAGE" \
VOICE_IME_REQUIRE_GPU_STT="$ASR_REQUIRE_GPU" \
VOICE_IME_RECORD_SECONDS=5 \
VOICE_IME_TRIGGER_MODE=toggle \
VOICE_IME_HOTKEYS=Ctrl+Alt+V \
VOICE_IME_MAX_RECORD_SECONDS=120 \
VOICE_IME_OVERLAY=0 \
VOICE_IME_OVERLAY_CATCH_HOTKEY=1 \
VOICE_IME_OVERLAY_BUTTONS=0 \
VOICE_IME_COMMIT_DELAY_MS=200 \
VOICE_IME_TOGGLE_SILENCE_AUTO_STOP=1 \
VOICE_IME_TOGGLE_SILENCE_SECONDS=2.5 \
VOICE_IME_TOGGLE_NO_SPEECH_TIMEOUT=8 \
VOICE_IME_VAD_AUTO_STOP=0 \
VOICE_IME_VOICE_MODE=dictation \
VOICE_IME_VOICE_COMMANDS=1 \
VOICE_IME_AUTO_PUNCTUATION=0 \
VOICE_IME_LLM_POSTPROCESS=0 \
VOICE_IME_LLM_INTERNAL=1 \
VOICE_IME_LLM_RERANK=1 \
VOICE_IME_LLM_BASE_URL=http://127.0.0.1:18080/v1 \
VOICE_IME_LLM_API_KEY=local \
VOICE_IME_LLM_MODEL=qwen3.5-0.8b \
VOICE_IME_LLM_CANDIDATES=3 \
ibus-daemon -drx --replace --panel disable --cache refresh || ibus restart || true
