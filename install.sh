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

chmod +x "$ROOT_DIR/src/ibus_voice_ime/engine.py" "$ROOT_DIR/run-engine.sh" "$ROOT_DIR/scripts/voice-toggle.sh" "$ROOT_DIR/scripts/clipboard-paste.sh" "$ROOT_DIR/scripts/install-gnome-voice-hotkey.sh" "$ROOT_DIR/scripts/install-gnome-clipboard-paste-hotkey.sh" "$ROOT_DIR/scripts/uninstall-gnome-clipboard-paste-hotkey.sh" 2>/dev/null || chmod +x "$ROOT_DIR/src/ibus_voice_ime/engine.py"
PYTHON="$ROOT_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3)"
fi

# engine.py --xml 依赖 PyGObject（gi）；缺失时给可读提示而不是裸 traceback。
if ! "$PYTHON" -c 'import gi' >/dev/null 2>&1; then
  cat >&2 <<'EOF_GI'
错误：当前 Python（见上方路径）缺少 PyGObject（gi），无法生成 IBus 组件。
Fedora:        sudo dnf install python3-gobject
Debian/Ubuntu: sudo apt install python3-gobject
（若用项目 venv，请按 README 以 --system-site-packages 创建）
EOF_GI
  exit 1
fi

mkdir -p "$COMPONENT_DIR"
PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON" "$ROOT_DIR/src/ibus_voice_ime/engine.py" --xml > "$COMPONENT_XML"

echo "已写入 IBus 组件：$COMPONENT_XML"

# Fedora/GNOME 的 IBus 默认只扫描 /usr/share/ibus/component；
# 这里把用户组件目录加入 IBus 扫描路径，并写入 environment.d 供下次登录继续生效。
IBUS_COMPONENT_PATH_VALUE="$COMPONENT_DIR:/usr/share/ibus/component"
ENV_DIR="$HOME/.config/environment.d"
ENV_FILE="$ENV_DIR/ibus-voice-ime.conf"

# 重跑安装时保留用户已用 switch-*/setup-* 脚本做出的后端/密钥/CUDA 配置；
# install.sh 只负责组件注册与路径修正，不把后端静默重置回默认。
BACKEND_LINES=""
if [[ -f "$ENV_FILE" ]]; then
  BACKEND_LINES="$(grep -E '^(VOICE_IME_ASR_BACKEND|VOICE_IME_QWEN_ASR|VOICE_IME_MIMO_ASR|VOICE_IME_MIMO_CLOUD_|VOICE_IME_MIMO_API_KEY|VOICE_IME_MIMO_BASE_URL|VOICE_IME_VOLC_|VOICE_IME_SILICONFLOW|VOICE_IME_WHISPER_|VOICE_IME_REQUIRE_GPU_STT|PYTORCH_CUDA_ALLOC_CONF|LD_LIBRARY_PATH)' "$ENV_FILE" || true)"
  CURRENT_BACKEND="$(grep -m1 '^VOICE_IME_ASR_BACKEND=' "$ENV_FILE" | cut -d= -f2- || true)"
  echo "检测到已有配置：保留当前语音后端设置（${CURRENT_BACKEND:-qwen3-asr}）"
fi

mkdir -p "$ENV_DIR"
cat > "$ENV_FILE" <<EOF_ENV
IBUS_COMPONENT_PATH=$IBUS_COMPONENT_PATH_VALUE
VOICE_IME_RIME_LIBRARY=$ROOT_DIR/vendor/rime/lib/librime.so.1
VOICE_IME_RIME_SHARED_DATA_DIR=$ROOT_DIR/vendor/rime/share/rime-data
VOICE_IME_RIME_STAGING_DIR=$ROOT_DIR/vendor/rime/build
VOICE_IME_RIME_USER_DATA_DIR=$HOME/.local/share/ibus-voice-ime/rime-user
VOICE_IME_LLM_POSTPROCESS=0
VOICE_IME_RECORD_SECONDS=5
VOICE_IME_HOTKEYS=Ctrl+Alt+V
VOICE_IME_RAW_HOTKEYS=Ctrl+Alt+B
VOICE_IME_CLIPBOARD_HOTKEYS=Ctrl+Alt+P
VOICE_IME_CANDIDATE_UI=popup
VOICE_IME_PREEDIT_MIRROR=off
EOF_ENV
if [[ -n "$BACKEND_LINES" ]]; then
  printf '%s\n' "$BACKEND_LINES" >> "$ENV_FILE"
else
  # 全新安装的默认后端：本地 Qwen3-ASR 1.7B。
  cat >> "$ENV_FILE" <<'EOF_DEFAULT_BACKEND'
VOICE_IME_ASR_BACKEND=qwen3-asr
VOICE_IME_QWEN_ASR=1
VOICE_IME_MIMO_ASR=0
VOICE_IME_MIMO_CLOUD_ASR=0
VOICE_IME_QWEN_ASR_MODEL=1.7b
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
EOF_DEFAULT_BACKEND
fi

# 会话级环境（当前登录立即可用；下次登录由 environment.d 接管）。
if ! systemctl --user set-environment \
  "IBUS_COMPONENT_PATH=$IBUS_COMPONENT_PATH_VALUE" \
  "VOICE_IME_RIME_LIBRARY=$ROOT_DIR/vendor/rime/lib/librime.so.1" \
  "VOICE_IME_RIME_SHARED_DATA_DIR=$ROOT_DIR/vendor/rime/share/rime-data" \
  "VOICE_IME_RIME_STAGING_DIR=$ROOT_DIR/vendor/rime/build" \
  "VOICE_IME_RIME_USER_DATA_DIR=$HOME/.local/share/ibus-voice-ime/rime-user" \
  "VOICE_IME_HOTKEYS=Ctrl+Alt+V" \
  "VOICE_IME_RAW_HOTKEYS=Ctrl+Alt+B" \
  "VOICE_IME_CLIPBOARD_HOTKEYS=Ctrl+Alt+P" 2>/dev/null; then
  echo "提示：systemctl --user 不可用（SSH 无用户会话/非 systemd？），配置将在下次登录时由 environment.d 生效。"
fi
if [[ -n "$BACKEND_LINES" ]]; then
  while IFS= read -r line; do
    [[ -n "$line" ]] && systemctl --user set-environment "$line" 2>/dev/null || true
  done <<< "$BACKEND_LINES"
fi
IBUS_COMPONENT_PATH="$IBUS_COMPONENT_PATH_VALUE" ibus write-cache >/dev/null 2>&1 || true

# Add to GNOME input sources without removing existing Rime.
if command -v gsettings >/dev/null 2>&1; then
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
    try:
        subprocess.run([
            'gsettings', 'set', 'org.gnome.desktop.input-sources', 'sources', repr(sources)
        ], check=True)
        print('已加入 GNOME 输入源：', sources)
    except Exception as exc:
        print(f'警告：无法写入 GNOME 输入源（{exc}）；请手动把 {entry} 加入输入源。')
else:
    print('GNOME 输入源已存在：', entry)
PY
fi

"$ROOT_DIR/scripts/install-gnome-voice-hotkey.sh" || true
# Ctrl+Alt+P uses a tiny external helper only to read the desktop clipboard and
# show the visible "正在粘贴" notification.  The helper passes prepared text back
# to the engine, which commits it through the same _commit_voice_result path as
# voice recognition results.
"$ROOT_DIR/scripts/install-gnome-clipboard-paste-hotkey.sh" || true

# 词库就绪性提示（只警告、不下载——install.sh 定位是轻量的每次注册/激活；
# 真正的资产初始化由一次性脚本 ./init.sh 完成）。
if [[ ! -f "$ROOT_DIR/vendor/rime/share/rime-data/cn_dicts/base.dict.yaml" ]]; then
  echo "⚠ rime-ice 词库未部署：键盘将回退 luna_pinyin_simp（可打字但词库小）。" >&2
  echo "  一次性初始化（默认部署词库/语音模型/降噪）：./init.sh" >&2
fi

# Let ibus-daemon reload component list.  This briefly restarts input methods.
# 后端/密钥等 ASR 配置以 environment.d 文件为单一事实源，透传给重启后的 daemon。
SPAWN_BACKEND_ENV=()
if [[ -n "$BACKEND_LINES" ]]; then
  while IFS= read -r line; do
    [[ -n "$line" ]] && SPAWN_BACKEND_ENV+=("$line")
  done <<< "$BACKEND_LINES"
else
  SPAWN_BACKEND_ENV+=(
    "VOICE_IME_ASR_BACKEND=qwen3-asr"
    "VOICE_IME_QWEN_ASR=1"
    "VOICE_IME_MIMO_ASR=0"
    "VOICE_IME_MIMO_CLOUD_ASR=0"
    "VOICE_IME_QWEN_ASR_MODEL=1.7b"
  )
fi
if command -v ibus >/dev/null 2>&1 || command -v ibus-daemon >/dev/null 2>&1; then
  env \
    IBUS_COMPONENT_PATH="$IBUS_COMPONENT_PATH_VALUE" \
    LD_LIBRARY_PATH="$ROOT_DIR/vendor/rime/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
    VOICE_IME_RIME_LIBRARY="$ROOT_DIR/vendor/rime/lib/librime.so.1" \
    VOICE_IME_RIME_SHARED_DATA_DIR="$ROOT_DIR/vendor/rime/share/rime-data" \
    VOICE_IME_RIME_STAGING_DIR="$ROOT_DIR/vendor/rime/build" \
    VOICE_IME_RIME_USER_DATA_DIR="$HOME/.local/share/ibus-voice-ime/rime-user" \
    "${SPAWN_BACKEND_ENV[@]}" \
    "$ROOT_DIR/scripts/ibus-restart.sh" || true
fi

echo
cat <<EOF
安装完成。
切换方式：GNOME 顶栏输入法菜单，或 Super+Space，选择“自定义语音输入法”。
键盘输入示例：nihao + Space -> 你好
语音输入热键：Ctrl+Alt+V，默认后端为本地 Qwen3-ASR（1.7B）。
原文语音输入热键：Ctrl+Alt+B，识别结果不做 LLM 后处理，只做确定性规整后上屏。
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

GPU_STATE="" GPU_REASON="" GPU_ACTION=""
if [[ -x "$ROOT_DIR/scripts/gpu-probe.sh" ]]; then
  eval "$("$ROOT_DIR/scripts/gpu-probe.sh" --env)"
fi
if [[ "$GPU_STATE" != "ok" ]]; then
  cat <<EOF
⚠ 本机 NVIDIA GPU 不可用（${GPU_REASON:-未检测到}）：默认语音后端（本地 Qwen3-ASR）将无法运行。
键盘输入（Rime 拼音）不受影响。语音三选一：
  1. 修复 GPU 环境后安装本地识别：$GPU_ACTION
  2. 云端识别（无需 GPU/大陆直连，注册 cloud.siliconflow.cn；SenseVoiceSmall 官方标注免费，Qwen3-ASR 按秒计费）：VOICE_IME_SILICONFLOW_API_KEY='sk-xxx' ./scripts/switch-siliconflow-asr.sh
  3. 云端识别（需 API Key）：./scripts/switch-mimo-cloud-asr.sh cn
EOF
fi
