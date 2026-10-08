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

ok()   { printf '  [OK]   %s\n' "$1"; }
warn() { printf '  [WARN] %s\n' "$1"; WARNINGS=$((WARNINGS + 1)); }
fail() { printf '  [FAIL] %s\n' "$1"; FAILURES=$((FAILURES + 1)); }
header() { printf '\n== %s ==\n' "$1"; }

# ---------------------------------------------------------------- 系统与桌面
header "操作系统与桌面"
. /etc/os-release 2>/dev/null || true
DISTRO="${PRETTY_NAME:-未知}"
echo "  系统：$DISTRO（$(uname -m)）"
case "${ID:-}" in
  fedora) ok "Fedora：开发者日常验证的发行版" ;;
  *) warn "非 Fedora 发行版未经验证。本项目仅在 Fedora 44 + GNOME 上测试过（详见 README「已测试环境」）" ;;
esac

if [[ -n "${XDG_CURRENT_DESKTOP:-}" || -n "${DESKTOP_SESSION:-}" ]]; then
  DESKTOP="${XDG_CURRENT_DESKTOP:-${DESKTOP_SESSION:-}}"
  echo "  桌面：$DESKTOP"
  if [[ "${DESKTOP,,}" == *gnome* ]]; then
    ok "GNOME：安装脚本的热键/输入源集成按 GNOME 设计"
  else
    warn "非 GNOME 桌面未经验证：install.sh 的 gsettings 输入源注册与全局热键脚本不适用，需要手工配置 IBus 组件与热键"
  fi
else
  warn "未检测到桌面会话（纯命令行环境无法使用本输入法）"
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
  PY_BIN="$(command -v python3 || true)"
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
  || warn "curl 未安装：init.sh 下载 rime-ice 词库与 RNNoise 模型会失败。Fedora: sudo dnf install curl；Debian/Ubuntu: sudo apt install curl"
command -v git >/dev/null 2>&1 \
  && ok "git 已安装：rime-ice 词库克隆" \
  || warn "git 未安装：init.sh 部署 rime-ice 词库会失败。Fedora: sudo dnf install git；Debian/Ubuntu: sudo apt install git"

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
  warn "vendor/rime/lib/librime.so.1 不存在（可能未完整 clone）；可用 scripts/vendor-rime-runtime.sh 重新生成"
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
  || warn "pw-record 未安装：arecord 失败时无兜底录音链路"
command -v ffmpeg >/dev/null 2>&1 \
  && ok "ffmpeg 已安装（兜底录音转换/RNNoise 降噪）" \
  || warn "ffmpeg 未安装：兜底录音转换与 rnnoise 降噪档不可用（自动降级 notch）"
command -v sox >/dev/null 2>&1 \
  && ok "sox 已安装：音频预处理（高通/陷波/归一化）" \
  || warn "sox 未安装：录音预处理全部跳过（识别仍可用）"
command -v gsettings >/dev/null 2>&1 \
  && ok "gsettings 已安装：GNOME 输入源/热键集成" \
  || warn "gsettings 未安装：install.sh 的 GNOME 集成步骤会跳过"
[[ -f /usr/share/opencc/t2s.json ]] \
  && ok "OpenCC 繁简转换数据可用" \
  || warn "/usr/share/opencc/t2s.json 不存在：繁→简兜底转换不可用（Fedora: sudo dnf install opencc；Debian/Ubuntu: sudo apt install opencc）"

# ---------------------------------------------------------------- 录音设备
header "录音设备"
if arecord -l >/dev/null 2>&1; then
  CARD_COUNT="$(arecord -l 2>/dev/null | grep -c '^card ' || true)"
  if [[ "${CARD_COUNT:-0}" -gt 0 ]]; then
    ok "检测到 ${CARD_COUNT} 块 ALSA 采集卡"
  else
    warn "未检测到 ALSA 硬件采集卡（USB/蓝牙设备可能未连接）"
  fi
fi
DEVICE="${VOICE_IME_ARECORD_DEVICE:-}"
if [[ "$DEVICE" == hw:* || "$DEVICE" == plughw:* ]]; then
  TOKEN="${DEVICE#*:}"; TOKEN="${TOKEN%%,*}"
  if arecord -l 2>/dev/null | grep -Eq "card [0-9]+: ${TOKEN}(\[|$)"; then
    ok "直采设备 $DEVICE 存在"
  else
    warn "直采设备 $DEVICE 在本机不存在：引擎会自动回退系统默认录音源；如需直采请用 'arecord -l' 里的卡名改写 VOICE_IME_ARECORD_DEVICE"
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
    ok "NVIDIA GPU：${GPU_BRIEF:-（型号未知）}"
    if [[ -d "$ROOT_DIR/vendor/models/qwen3-asr" ]]; then
      ok "Qwen3-ASR 模型已下载（vendor/models/qwen3-asr/）"
    else
      warn "Qwen3-ASR 模型未下载：运行 ./scripts/setup-qwen-asr.sh（约需数 GB 磁盘 + 对应显存）"
    fi
  elif [[ "$GPU_STATE" == "partial" ]]; then
    warn "检测到 NVIDIA 显卡，但 CUDA 驱动不可用：${GPU_REASON:-未知}"
    [[ -n "$GPU_ACTION" ]] && printf '         %s\n' "$GPU_ACTION"
  else
    warn "未检测到 NVIDIA GPU：本地 Qwen3-ASR / faster-whisper GPU 后端不可用"
    echo "         无 GPU 的替代方案（改用云端识别，零显存）："
    echo "         VOICE_IME_SILICONFLOW_API_KEY='sk-xxx' ./scripts/switch-siliconflow-asr.sh   # 硅基流动（大陆直连；SenseVoiceSmall 官方标注免费，Qwen3-ASR 按秒计费）"
    echo "         ./scripts/switch-mimo-cloud-asr.sh cn   # 小米 MiMo 云端（需 API Key）"
    echo "         ./scripts/switch-volc-bigmodel-asr.sh  # 火山引擎豆包（需 API Key）"
  fi
fi

# ---------------------------------------------------------------- 汇总
printf '\n== 汇总 ==\n'
echo "  FAIL: $FAILURES  WARN: $WARNINGS"
if [[ "$FAILURES" -gt 0 ]]; then
  echo "  存在必需组件缺失，请先解决 FAIL 项。"
  exit 1
fi
echo "  环境检查通过（WARN 项为可选/降级提示，不影响核心功能）。"
exit 0
