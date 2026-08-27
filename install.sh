#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPONENT_DIR="$HOME/.local/share/ibus/component"
COMPONENT_XML="$COMPONENT_DIR/voice-custom.xml"
ENGINE_NAME="voice-custom"

# 环境预检：只提示不阻断（FAIL/WARN 明细由 check-environment.sh 输出）。
if [[ -x "$ROOT_DIR/scripts/check-environment.sh" ]]; then
  echo "== 环境预检 =="
  "$ROOT_DIR/scripts/check-environment.sh" || \
    echo "（预检发现问题，详见上方 FAIL/WARN；安装继续，但请先解决 FAIL 项再使用）"
  echo
fi

chmod +x "$ROOT_DIR/src/ibus_voice_ime/engine.py" "$ROOT_DIR/run-engine.sh" "$ROOT_DIR/voice-toggle.sh" "$ROOT_DIR/clipboard-paste.sh" "$ROOT_DIR/keyboard-paste.sh" "$ROOT_DIR/src/ibus_voice_ime/keyboard_type_clipboard.py" "$ROOT_DIR/scripts/install-gnome-voice-hotkey.sh" "$ROOT_DIR/scripts/install-gnome-clipboard-paste-hotkey.sh" "$ROOT_DIR/scripts/install-gnome-keyboard-paste-hotkey.sh" "$ROOT_DIR/scripts/uninstall-gnome-clipboard-paste-hotkey.sh" 2>/dev/null || chmod +x "$ROOT_DIR/src/ibus_voice_ime/engine.py"
PYTHON="$ROOT_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3)"
fi
mkdir -p "$COMPONENT_DIR"
PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON" "$ROOT_DIR/src/ibus_voice_ime/engine.py" --xml > "$COMPONENT_XML"

echo "已写入 IBus 组件：$COMPONENT_XML"

# Fedora/GNOME 的 IBus 默认只扫描 /usr/share/ibus/component；
# 这里把用户组件目录加入 IBus 扫描路径，并写入 environment.d 供下次登录继续生效。
IBUS_COMPONENT_PATH_VALUE="$COMPONENT_DIR:/usr/share/ibus/component"
mkdir -p "$HOME/.config/environment.d"
cat > "$HOME/.config/environment.d/ibus-voice-ime.conf" <<EOF_ENV
IBUS_COMPONENT_PATH=$IBUS_COMPONENT_PATH_VALUE
VOICE_IME_RIME_LIBRARY=$ROOT_DIR/vendor/rime/lib/librime.so.1
VOICE_IME_RIME_SHARED_DATA_DIR=$ROOT_DIR/vendor/rime/share/rime-data
VOICE_IME_RIME_STAGING_DIR=$ROOT_DIR/vendor/rime/build
VOICE_IME_RIME_USER_DATA_DIR=$HOME/.local/share/ibus-voice-ime/rime-user
VOICE_IME_ASR_BACKEND=qwen3-asr
VOICE_IME_QWEN_ASR=1
VOICE_IME_MIMO_ASR=0
VOICE_IME_MIMO_CLOUD_ASR=0
VOICE_IME_QWEN_ASR_MODEL=1.7b
VOICE_IME_QWEN_ASR_MODEL_PATH=$ROOT_DIR/vendor/models/qwen3-asr/Qwen3-ASR-1.7B
VOICE_IME_QWEN_ASR_PYTHON=$ROOT_DIR/.venv-qwen-asr/bin/python
VOICE_IME_QWEN_ASR_HOST=127.0.0.1
VOICE_IME_QWEN_ASR_PORT=18081
VOICE_IME_QWEN_ASR_LANGUAGE=Chinese
VOICE_IME_QWEN_ASR_DTYPE=bfloat16
VOICE_IME_QWEN_ASR_DEVICE_MAP=cuda:0
VOICE_IME_QWEN_ASR_MAX_NEW_TOKENS=256
VOICE_IME_QWEN_ASR_START_TIMEOUT=180
VOICE_IME_QWEN_ASR_TIMEOUT=180
VOICE_IME_MIMO_CLOUD_BASE_URL=https://token-plan-cn.xiaomimimo.com/v1
VOICE_IME_MIMO_CLOUD_ASR_MODEL=mimo-v2.5-asr
VOICE_IME_MIMO_CLOUD_ASR_LANGUAGE=auto
VOICE_IME_MIMO_CLOUD_AUTH_HEADER=api-key
VOICE_IME_MIMO_CLOUD_ASR_TIMEOUT=120
VOICE_IME_MIMO_API_KEY_SECRET=XIAOMI_TOKEN_PLAN_CN_API_KEY
VOICE_IME_WHISPER_MODEL=large-v3
VOICE_IME_WHISPER_DEVICE=cuda
VOICE_IME_WHISPER_DEVICE_INDEX=0
VOICE_IME_WHISPER_COMPUTE=float16
VOICE_IME_WHISPER_LANGUAGE=zh
VOICE_IME_REQUIRE_GPU_STT=1
VOICE_IME_LLM_POSTPROCESS=0
VOICE_IME_RECORD_SECONDS=5
VOICE_IME_HOTKEYS=Ctrl+Alt+V
VOICE_IME_CLIPBOARD_HOTKEYS=Ctrl+Alt+P
VOICE_IME_INTERNAL_CLIPBOARD_HOTKEY=0
VOICE_IME_CLIPBOARD_RECOVER_ENGINE=1
VOICE_IME_CLIPBOARD_RECOVER_DELAY_SECONDS=2.0
VOICE_IME_CANDIDATE_UI=popup
VOICE_IME_PREEDIT_MIRROR=off
EOF_ENV
systemctl --user set-environment \
  "IBUS_COMPONENT_PATH=$IBUS_COMPONENT_PATH_VALUE" \
  "VOICE_IME_RIME_LIBRARY=$ROOT_DIR/vendor/rime/lib/librime.so.1" \
  "VOICE_IME_RIME_SHARED_DATA_DIR=$ROOT_DIR/vendor/rime/share/rime-data" \
  "VOICE_IME_RIME_STAGING_DIR=$ROOT_DIR/vendor/rime/build" \
  "VOICE_IME_RIME_USER_DATA_DIR=$HOME/.local/share/ibus-voice-ime/rime-user" \
  VOICE_IME_ASR_BACKEND=qwen3-asr \
  VOICE_IME_QWEN_ASR=1 \
  VOICE_IME_MIMO_ASR=0 \
  VOICE_IME_MIMO_CLOUD_ASR=0 \
  "VOICE_IME_QWEN_ASR_MODEL=1.7b" \
  "VOICE_IME_QWEN_ASR_MODEL_PATH=$ROOT_DIR/vendor/models/qwen3-asr/Qwen3-ASR-1.7B" \
  "VOICE_IME_QWEN_ASR_PYTHON=$ROOT_DIR/.venv-qwen-asr/bin/python" \
  VOICE_IME_QWEN_ASR_HOST=127.0.0.1 \
  VOICE_IME_QWEN_ASR_PORT=18081 \
  VOICE_IME_QWEN_ASR_LANGUAGE=Chinese \
  VOICE_IME_QWEN_ASR_DTYPE=bfloat16 \
  VOICE_IME_QWEN_ASR_DEVICE_MAP=cuda:0 \
  VOICE_IME_QWEN_ASR_MAX_NEW_TOKENS=256 \
  VOICE_IME_QWEN_ASR_START_TIMEOUT=180 \
  VOICE_IME_QWEN_ASR_TIMEOUT=180 \
  VOICE_IME_MIMO_CLOUD_BASE_URL=https://token-plan-cn.xiaomimimo.com/v1 \
  VOICE_IME_MIMO_CLOUD_ASR_MODEL=mimo-v2.5-asr \
  VOICE_IME_MIMO_CLOUD_ASR_LANGUAGE=auto \
  VOICE_IME_MIMO_CLOUD_AUTH_HEADER=api-key \
  VOICE_IME_MIMO_CLOUD_ASR_TIMEOUT=120 \
  VOICE_IME_MIMO_API_KEY_SECRET=XIAOMI_TOKEN_PLAN_CN_API_KEY \
  VOICE_IME_WHISPER_MODEL=large-v3 \
  VOICE_IME_WHISPER_DEVICE=cuda \
  VOICE_IME_WHISPER_DEVICE_INDEX=0 \
  VOICE_IME_WHISPER_COMPUTE=float16 \
  VOICE_IME_WHISPER_LANGUAGE=zh \
  VOICE_IME_REQUIRE_GPU_STT=1 \
  VOICE_IME_LLM_POSTPROCESS=0 \
  VOICE_IME_RECORD_SECONDS=5 \
  "VOICE_IME_HOTKEYS=Ctrl+Alt+V" \
  "VOICE_IME_CLIPBOARD_HOTKEYS=Ctrl+Alt+P" \
  VOICE_IME_INTERNAL_CLIPBOARD_HOTKEY=0 \
  VOICE_IME_CLIPBOARD_RECOVER_ENGINE=1 \
  VOICE_IME_CLIPBOARD_RECOVER_DELAY_SECONDS=2.0 \
  VOICE_IME_CANDIDATE_UI=popup \
  VOICE_IME_PREEDIT_MIRROR=off 2>/dev/null || true
IBUS_COMPONENT_PATH="$IBUS_COMPONENT_PATH_VALUE" ibus write-cache >/dev/null 2>&1 || true

# Add to GNOME input sources without removing existing Rime.
python3 - <<'PY'
import ast
import subprocess
entry = ('ibus', 'voice-custom')
try:
    raw = subprocess.check_output([
        'gsettings', 'get', 'org.gnome.desktop.input-sources', 'sources'
    ], text=True).strip()
    sources = ast.literal_eval(raw)
except Exception:
    sources = []
if entry not in sources:
    sources.append(entry)
    subprocess.run([
        'gsettings', 'set', 'org.gnome.desktop.input-sources', 'sources', repr(sources)
    ], check=True)
    print('已加入 GNOME 输入源：', sources)
else:
    print('GNOME 输入源已存在：', entry)
PY

"$ROOT_DIR/scripts/install-gnome-voice-hotkey.sh" || true
# Ctrl+Alt+P uses a tiny external helper only to read the desktop clipboard and
# show the visible "正在粘贴" notification.  The helper passes prepared text back
# to the engine, which commits it through the same _commit_voice_result path as
# voice recognition results.
"$ROOT_DIR/scripts/install-gnome-clipboard-paste-hotkey.sh" || true

# Let ibus-daemon reload component list.  This briefly restarts input methods.
if command -v ibus-daemon >/dev/null 2>&1; then
  IBUS_COMPONENT_PATH="$IBUS_COMPONENT_PATH_VALUE" \
  LD_LIBRARY_PATH="$ROOT_DIR/vendor/rime/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
  VOICE_IME_RIME_LIBRARY="$ROOT_DIR/vendor/rime/lib/librime.so.1" \
  VOICE_IME_RIME_SHARED_DATA_DIR="$ROOT_DIR/vendor/rime/share/rime-data" \
  VOICE_IME_RIME_STAGING_DIR="$ROOT_DIR/vendor/rime/build" \
  VOICE_IME_RIME_USER_DATA_DIR="$HOME/.local/share/ibus-voice-ime/rime-user" \
  VOICE_IME_ASR_BACKEND=qwen3-asr \
  VOICE_IME_QWEN_ASR=1 \
  VOICE_IME_MIMO_ASR=0 \
  VOICE_IME_MIMO_CLOUD_ASR=0 \
  "VOICE_IME_QWEN_ASR_MODEL=1.7b" \
  "VOICE_IME_QWEN_ASR_MODEL_PATH=$ROOT_DIR/vendor/models/qwen3-asr/Qwen3-ASR-1.7B" \
  "VOICE_IME_QWEN_ASR_PYTHON=$ROOT_DIR/.venv-qwen-asr/bin/python" \
  VOICE_IME_QWEN_ASR_HOST=127.0.0.1 \
  VOICE_IME_QWEN_ASR_PORT=18081 \
  VOICE_IME_QWEN_ASR_LANGUAGE=Chinese \
  VOICE_IME_QWEN_ASR_DTYPE=bfloat16 \
  VOICE_IME_QWEN_ASR_DEVICE_MAP=cuda:0 \
  VOICE_IME_QWEN_ASR_MAX_NEW_TOKENS=256 \
  VOICE_IME_QWEN_ASR_START_TIMEOUT=180 \
  VOICE_IME_QWEN_ASR_TIMEOUT=180 \
  VOICE_IME_MIMO_CLOUD_BASE_URL=https://token-plan-cn.xiaomimimo.com/v1 \
  VOICE_IME_MIMO_CLOUD_ASR_MODEL=mimo-v2.5-asr \
  VOICE_IME_MIMO_CLOUD_ASR_LANGUAGE=auto \
  VOICE_IME_MIMO_CLOUD_AUTH_HEADER=api-key \
  VOICE_IME_MIMO_CLOUD_ASR_TIMEOUT=120 \
  VOICE_IME_MIMO_API_KEY_SECRET=XIAOMI_TOKEN_PLAN_CN_API_KEY \
  VOICE_IME_WHISPER_MODEL=large-v3 \
  VOICE_IME_WHISPER_DEVICE=cuda \
  VOICE_IME_WHISPER_DEVICE_INDEX=0 \
  VOICE_IME_WHISPER_COMPUTE=float16 \
  VOICE_IME_WHISPER_LANGUAGE=zh \
  VOICE_IME_REQUIRE_GPU_STT=1 \
  VOICE_IME_LLM_POSTPROCESS=0 \
  VOICE_IME_RECORD_SECONDS=5 \
  VOICE_IME_HOTKEYS=Ctrl+Alt+V \
  VOICE_IME_CLIPBOARD_HOTKEYS=Ctrl+Alt+P \
  VOICE_IME_INTERNAL_CLIPBOARD_HOTKEY=0 \
  VOICE_IME_CLIPBOARD_RECOVER_ENGINE=1 \
  VOICE_IME_CLIPBOARD_RECOVER_DELAY_SECONDS=2.0 \
  VOICE_IME_CANDIDATE_UI=popup \
  VOICE_IME_PREEDIT_MIRROR=off \
  ibus-daemon -drx --replace --panel disable --cache refresh || true
elif command -v ibus >/dev/null 2>&1; then
  IBUS_COMPONENT_PATH="$IBUS_COMPONENT_PATH_VALUE" ibus restart || true
fi

echo
cat <<EOF
安装完成。
切换方式：GNOME 顶栏输入法菜单，或 Super+Space，选择“自定义语音输入法”。
键盘输入示例：nihao + Space -> 你好
语音输入热键：Ctrl+Alt+V，默认后端为本地 Qwen3-ASR（1.7B）。
输入法粘贴热键：Ctrl+Alt+P，读取剪贴板并显示“正在粘贴”提示后，通过语音结果同款 commit_text 路径提交文本。
EOF

if [[ ! -d "$ROOT_DIR/vendor/models/qwen3-asr" ]]; then
  cat <<'EOF'
⚠ 尚未下载本地 ASR 模型（vendor/models/qwen3-asr/ 不存在）。语音输入前请二选一：
  1. 本地识别（需 NVIDIA GPU）：./scripts/setup-qwen-asr.sh
  2. 云端识别（无需 GPU，需 API Key）：./scripts/switch-mimo-cloud-asr.sh cn
键盘输入（Rime 拼音）不受影响，可直接使用。
EOF
fi

if ! command -v nvidia-smi >/dev/null 2>&1 || ! nvidia-smi -L >/dev/null 2>&1; then
  cat <<'EOF'
⚠ 未检测到 NVIDIA GPU：本地 Qwen3-ASR 后端无法运行，请改用云端后端
（./scripts/switch-mimo-cloud-asr.sh cn 或 ./scripts/switch-volc-bigmodel-asr.sh）。
EOF
fi
