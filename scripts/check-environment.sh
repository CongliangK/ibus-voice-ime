#!/usr/bin/env bash
# 环境自检：在一台新机器上评估 ibus-voice-ime 的运行条件。
#
# 用法：
#   ./scripts/check-environment.sh          # 全量检查
#   ./scripts/check-environment.sh --no-gpu # 跳过 GPU/模型检查（只想看键盘输入链路）
#
# 输出分三级：
#   [FAIL] 缺了必需组件，对应功能无法工作
#   [WARN] 缺了可选组件或仅在某类环境验证过，功能降级/未验证
#   [OK]   通过
# 任一 FAIL 时退出码为 1，否则为 0。WARN 不影响退出码。
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME="$ROOT_DIR/vendor/rime"

CHECK_GPU=1
for arg in "$@"; do
  case "$arg" in
    --no-gpu) CHECK_GPU=0 ;;
    *) echo "未知参数：$arg" >&2; exit 2 ;;
  esac
done

FAILURES=0
WARNINGS=0
WARN_TITLES=() WARN_IMPACTS=() WARN_GUIDANCE=()

ok()   { printf '  [OK]   %s\n' "$1"; }
warn() {
  WARN_TITLES+=("${1//$'\n'/ }"); WARN_IMPACTS+=("${2//$'\n'/ }"); WARN_GUIDANCE+=("${3//$'\n'/ }")
  WARNINGS=${#WARN_TITLES[@]}
  printf '  [WARN] %s —— %s；%s\n' "${WARN_TITLES[WARNINGS-1]}" "${WARN_IMPACTS[WARNINGS-1]}" "${WARN_GUIDANCE[WARNINGS-1]}"
}
fail() { printf '  [FAIL] %s\n' "$1"; FAILURES=$((FAILURES + 1)); }
header() { printf '\n== %s ==\n' "$1"; }

# ---------------------------------------------------------------- 系统与桌面
header "操作系统与桌面"
. /etc/os-release 2>/dev/null || true
DISTRO="${PRETTY_NAME:-未知}"
echo "  系统：$DISTRO（$(uname -m)）"
case "${ID:-}" in
  fedora) ok "Fedora：开发者日常验证的发行版" ;;
  *) warn "非 Fedora 发行版未经验证" "安装与运行兼容性未在此发行版验收" "若本机已验证可忽略；否则按 README「已测试环境」核对依赖后试用" ;;
esac

if [[ -n "${XDG_CURRENT_DESKTOP:-}" || -n "${DESKTOP_SESSION:-}" ]]; then
  DESKTOP="${XDG_CURRENT_DESKTOP:-${DESKTOP_SESSION:-}}"
  echo "  桌面：$DESKTOP"
  if [[ "${DESKTOP,,}" == *gnome* ]]; then
    ok "GNOME：安装脚本的热键/输入源集成按 GNOME 设计"
  else
    warn "非 GNOME 桌面未经验证" "GNOME 输入源/全局热键集成可能不适用" "按 README「非 GNOME 桌面」手工配置 IBus 与桌面热键；已配置可忽略"
  fi
else
  warn "未检测到桌面会话" "纯命令行环境不能使用桌面输入法" "请在已登录的桌面终端重跑 ./scripts/check-environment.sh；仅准备资产时可忽略"
fi

# ---------------------------------------------------------------- 必需组件
header "必需组件（缺失则无法运行）"

if command -v ibus >/dev/null 2>&1 || command -v ibus-daemon >/dev/null 2>&1; then
  ok "IBus 已安装"
else
  fail "IBus 未安装。Fedora: sudo dnf install ibus；其他发行版请用对应包管理器"
fi

# Python 选择逻辑与 run-engine.sh / install.sh 保持一致：项目 venv 优先。
PY_BIN="$ROOT_DIR/.venv/bin/python"
if [[ ! -x "$PY_BIN" ]]; then
  # 自动挑选可用解释器（conda/miniconda 遮蔽 PATH 时仍能找到带 gi 的系统 Python）。
  PY_BIN="$("$ROOT_DIR/scripts/python-select.sh" || true)"
fi

PY_OK=0
if [[ -n "${PY_BIN:-}" ]]; then
  echo "  检查用的 Python：$PY_BIN"
  PY_MINOR="$("$PY_BIN" -c 'import sys; print(sys.version_info[1])' 2>/dev/null || echo 0)"
  PY_MAJOR="$("$PY_BIN" -c 'import sys; print(sys.version_info[0])' 2>/dev/null || echo 0)"
  if [[ "$PY_MAJOR" == "3" && "$PY_MINOR" -ge 10 ]]; then
    ok "Python 3.${PY_MINOR}（要求 ≥ 3.10）"
    PY_OK=1
  else
    fail "Python 3.${PY_MAJOR}.${PY_MINOR} 过旧，代码要求 ≥ 3.10"
  fi
else
  fail "python3 未安装"
fi

if command -v arecord >/dev/null 2>&1; then
  ok "arecord（alsa-utils）已安装：主录音链路"
else
  fail "arecord 未安装。Fedora: sudo dnf install alsa-utils；Debian/Ubuntu: sudo apt install alsa-utils"
fi

# init.sh 的资产下载用到 curl（rime-ice/rnnoise）与 git（rime-ice）；缺失时错误
# 归因会变成误导性的“网络问题”，这里提前点名。
command -v curl >/dev/null 2>&1 \
  && ok "curl 已安装：词库/降噪模型下载" \
  || warn "curl 未安装" "词库和 RNNoise 模型下载会失败，已就绪资产不受影响" "Fedora: sudo dnf install curl；Debian/Ubuntu: sudo apt install curl；不下载可忽略"
command -v git >/dev/null 2>&1 \
  && ok "git 已安装：rime-ice 词库克隆" \
  || warn "git 未安装" "rime-ice 词库克隆会失败" "Fedora: sudo dnf install git；Debian/Ubuntu: sudo apt install git；不用雾凇可忽略"

# 内置 librime 能否在本机加载（glibc 版本 / CPU 架构不匹配会在这里暴露）。
if [[ -f "$RUNTIME/lib/librime.so.1" ]]; then
  RIME_LOAD="$(LD_LIBRARY_PATH="$RUNTIME/lib" python3 - <<'PY' 2>/dev/null
import ctypes
try:
    ctypes.CDLL("librime.so.1")
    print("OK")
except OSError as exc:
    print("FAIL:", exc)
PY
)"
  if [[ "$RIME_LOAD" == "OK" ]]; then
    ok "内置 librime 运行时可加载"
  else
    fail "内置 librime 无法加载：${RIME_LOAD#FAIL: }。内置库为 x86_64 Fedora 构建，"
    echo "         引擎会自动尝试回退系统 librime（安装 librime 后重启输入法即可）："
    echo "           Fedora: sudo dnf install librime   Debian/Ubuntu: sudo apt install librime"
    echo "         也可显式指定（写入 ~/.config/environment.d/ibus-voice-ime.conf 持久生效）："
    echo "           Fedora:   VOICE_IME_RIME_LIBRARY=/usr/lib64/librime.so.1"
    echo "           Debian系: VOICE_IME_RIME_LIBRARY=/usr/lib/x86_64-linux-gnu/librime.so.1"
    echo "           VOICE_IME_RIME_SHARED_DATA_DIR=/usr/share/rime-data"
  fi
else
  warn "内置 librime 运行时缺失" "键盘需要可加载的系统 librime 作为回退" "运行 ./scripts/vendor-rime-runtime.sh；已配置系统 librime 时可忽略"
fi

if [[ "$PY_OK" == "1" ]]; then
  if "$PY_BIN" -c "import gi" >/dev/null 2>&1; then
    if "$PY_BIN" - <<'PY' >/dev/null 2>&1
import gi
gi.require_version("IBus", "1.0")
from gi.repository import IBus
PY
    then
      ok "PyGObject + IBus GObject 内省可用"
    else
      fail "gi.repository.IBus 不可用。Fedora: sudo dnf install ibus python3-gobject；Debian/Ubuntu: sudo apt install ibus python3-gi"
    fi
  else
    fail "PyGObject 未安装（检查的 Python：$PY_BIN）。Fedora: sudo dnf install python3-gobject；"\
"若用项目 venv，请按 README 以 --system-site-packages 创建后再装 ASR 依赖"
  fi
fi

# ---------------------------------------------------------------- 可选组件
header "可选组件（缺失则相应功能降级）"

command -v pw-record >/dev/null 2>&1 \
  && ok "pw-record（PipeWire）已安装：录音兜底链路" \
  || warn "pw-record 未安装" "arecord 失败时没有 PipeWire 兜底录音" "Fedora: sudo dnf install pipewire-utils；Debian/Ubuntu: sudo apt install pipewire-bin；arecord 正常可忽略"
command -v ffmpeg >/dev/null 2>&1 \
  && ok "ffmpeg 已安装（兜底录音转换/RNNoise 降噪）" \
  || warn "ffmpeg 未安装" "兜底录音转换不可用，RNNoise 降噪降级 notch" "Fedora（需相应软件源）: sudo dnf install ffmpeg；Debian/Ubuntu: sudo apt install ffmpeg；接受降级可忽略"
command -v sox >/dev/null 2>&1 \
  && ok "sox 已安装：音频预处理（高通/陷波/归一化）" \
  || warn "sox 未安装" "录音预处理全部跳过，识别仍可用" "Fedora: sudo dnf install sox；Debian/Ubuntu: sudo apt install sox；接受原始录音可忽略"
command -v gsettings >/dev/null 2>&1 \
  && ok "gsettings 已安装：GNOME 输入源/热键集成" \
  || warn "gsettings 未安装" "GNOME 输入源/热键集成将跳过" "Fedora: sudo dnf install glib2；Debian/Ubuntu: sudo apt install libglib2.0-bin；手工集成时可忽略"
[[ -f /usr/share/opencc/t2s.json ]] \
  && ok "OpenCC 繁简转换数据可用" \
  || warn "OpenCC 繁简转换数据缺失" "语音繁→简兜底转换不可用" "Fedora: sudo dnf install opencc；Debian/Ubuntu: sudo apt install opencc；不需要转换可忽略"

# ---------------------------------------------------------------- 录音设备
header "录音设备"
if arecord -l >/dev/null 2>&1; then
  CARD_COUNT="$(arecord -l 2>/dev/null | grep -c '^card ' || true)"
  if [[ "${CARD_COUNT:-0}" -gt 0 ]]; then
    ok "检测到 ${CARD_COUNT} 块 ALSA 采集卡"
  else
    warn "未检测到 ALSA 硬件采集卡" "当前麦克风输入未经验证，USB/蓝牙设备可能未连接" "连接麦克风后运行 arecord -l；只用键盘可忽略"
  fi
fi
DEVICE="${VOICE_IME_ARECORD_DEVICE:-}"
if [[ "$DEVICE" == hw:* || "$DEVICE" == plughw:* ]]; then
  TOKEN="${DEVICE#*:}"; TOKEN="${TOKEN%%,*}"
  if arecord -l 2>/dev/null | grep -Eq "card [0-9]+: ${TOKEN}(\[|$)"; then
    ok "直采设备 $DEVICE 存在"
  else
    warn "直采设备 $DEVICE 在本机不存在" "录音自动回退系统默认源" "运行 arecord -l 并按卡名修改 VOICE_IME_ARECORD_DEVICE；默认源可用时可忽略"
  fi
else
  echo "  录音源：${DEVICE:-default}（系统默认源）"
fi

# ---------------------------------------------------------------- GPU 与模型
if [[ "$CHECK_GPU" == "1" ]]; then
  header "GPU 与本地模型（默认后端 = 本地 Qwen3-ASR）"
  GPU_STATE="" GPU_REASON="" GPU_ACTION="" GPU_BRIEF=""
  if [[ -x "$ROOT_DIR/scripts/gpu-probe.sh" ]]; then
    eval "$("$ROOT_DIR/scripts/gpu-probe.sh" --env)"
  fi
  if [[ "$GPU_STATE" == "ok" ]]; then
    ok "NVIDIA GPU 可被驱动列出：${GPU_BRIEF:-（型号未知）}（不是 CUDA/Triton 推理验收）"
    if [[ -x "$ROOT_DIR/.venv-qwen-asr/bin/python" ]]; then
      if "$PY_BIN" "$ROOT_DIR/scripts/qwen-preflight.py" --python "$ROOT_DIR/.venv-qwen-asr/bin/python" --stage static; then
        ok "Qwen 独立环境静态编译条件通过"
      else
        warn "Qwen 独立环境静态检查失败" "本地语音未就绪，键盘可独立使用" "运行 ./scripts/setup-qwen-asr.sh 修复并完整验收"
      fi
    else
      warn "Qwen 独立环境未创建" "本地语音未就绪，install.sh 仅注册不会准备资产" "运行 ./scripts/setup-qwen-asr.sh 准备并完整验收；不用本地语音可忽略"
    fi
    if [[ -d "$ROOT_DIR/vendor/models/qwen3-asr" ]]; then
      warn "Qwen 模型目录存在但不能证明推理可用" "这是分层验收设计：预检/体检不跑模型推理，目录存在不等于语音就绪" "本机最近验收过可用 ./scripts/setup-qwen-asr.sh --verify-only 复验；否则运行 ./scripts/setup-qwen-asr.sh 完整安装/验收"
    else
      warn "Qwen3-ASR 模型未下载" "本地语音不可用，需数 GB 磁盘与对应显存" "运行 ./scripts/setup-qwen-asr.sh；不用本地语音可忽略"
    fi
  elif [[ "$GPU_STATE" == "partial" ]]; then
    warn "NVIDIA 显卡 CUDA 驱动不可用" "本地 GPU 语音后端不可用：${GPU_REASON:-未知}" "${GPU_ACTION:-修复 NVIDIA 驱动后运行 nvidia-smi}；只用键盘/云端可忽略"
    [[ -n "$GPU_ACTION" ]] && printf '         %s\n' "$GPU_ACTION"
  else
    warn "未检测到 NVIDIA GPU" "本地 Qwen3-ASR / faster-whisper GPU 后端不可用" "自备 API Key 后运行 ./scripts/switch-mimo-cloud-asr.sh cn 切换云端；只用键盘可忽略"
    echo "         无 GPU 的替代方案（改用云端识别，零显存）："
    echo "         VOICE_IME_SILICONFLOW_API_KEY='sk-xxx' ./scripts/switch-siliconflow-asr.sh   # 硅基流动（大陆直连；SenseVoiceSmall 官方标注免费，Qwen3-ASR 按秒计费）"
    echo "         ./scripts/switch-mimo-cloud-asr.sh cn   # 小米 MiMo 云端（需 API Key）"
    echo "         ./scripts/switch-volc-bigmodel-asr.sh  # 火山引擎豆包（需 API Key）"
  fi
fi

# ---------------------------------------------------------------- 汇总
printf '\n== 汇总 ==\n'
for ((i=0; i<${#WARN_TITLES[@]}; i++)); do
  printf '  [WARN] %s —— %s；%s\n' "${WARN_TITLES[i]}" "${WARN_IMPACTS[i]}" "${WARN_GUIDANCE[i]}"
done
# 保留原计数行格式：doctor 与外层安装入口依赖此约定。
echo "  FAIL: $FAILURES  WARN: $WARNINGS"
if [[ "$FAILURES" -gt 0 ]]; then
  echo "  存在必需组件缺失，请先解决 FAIL 项。"
  echo "  EXIT: 1"
  exit 1
fi
echo "  环境检查通过（WARN 项为可选/降级提示，不改变退出码；实际影响见明细）。"
echo "  EXIT: 0"
exit 0
