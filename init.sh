#!/usr/bin/env bash
# 一次性初始化脚本：拿到仓库后的第一步。
#
# 它与 install.sh 的分工：
#   init.sh    —— 一次性、重量级：下载并配置基础资产（词库/语音模型/降噪模型），
#                 支持参数选择装什么，完成后自动调用 install.sh 完成注册激活。
#   install.sh —— 每次轻量注册：把引擎安装进当前电脑会话（IBus 组件/环境/热键/
#                 重启），不下载任何东西，可反复运行。
#
# 用法（所有步骤幂等，已就绪的资产自动跳过）：
#   ./init.sh                              # 默认全量：rime-ice 词库 + 本地语音 + 降噪
#   ./init.sh --no-voice                   # 不装语音输入（不下载 ASR 模型与 venv）
#   ./init.sh --no-denoise                 # 不要 RNNoise 降噪模型（录音降级 notch 档）
#   ./init.sh --no-rime-ice                # 跳过雾凇拼音大词库（键盘回退内置 luna）
#   ./init.sh --no-zhwiki --no-moegirl     # rime-ice 不带 zhwiki/moegirl 扩展词典
#   ./init.sh --proxy http://127.0.0.1:7890
#   ./init.sh --skip-install               # 只准备资产，不运行 install.sh（不注册/不重启）
#
# 语音说明：--voice（默认）安装本地 Qwen3-ASR（需 NVIDIA GPU，下载约 6GB）。
# 无 GPU 时自动跳过并打印云端替代命令，不算失败。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

WITH_VOICE=1
WITH_DENOISE=1
WITH_RIME_ICE=1
RIME_EXTRA_ARGS=()
PROXY=""
DO_INSTALL=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --voice)          WITH_VOICE=1; shift ;;
    --no-voice)       WITH_VOICE=0; shift ;;
    --denoise)        WITH_DENOISE=1; shift ;;
    --no-denoise)     WITH_DENOISE=0; shift ;;
    --rime-ice)       WITH_RIME_ICE=1; shift ;;
    --no-rime-ice)    WITH_RIME_ICE=0; shift ;;
    --no-zhwiki)      RIME_EXTRA_ARGS+=(--no-zhwiki); shift ;;
    --no-moegirl)     RIME_EXTRA_ARGS+=(--no-moegirl); shift ;;
    --proxy)          [[ $# -ge 2 ]] || { echo "--proxy 需要参数" >&2; exit 2; }; PROXY="$2"; shift 2 ;;
    --proxy=*)        PROXY="${1#--proxy=}"; shift ;;
    --skip-install)   DO_INSTALL=0; shift ;;
    -h|--help)        sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "未知参数：$1（查看 --help）" >&2; exit 2 ;;
  esac
done

if [[ -n "$PROXY" ]]; then
  export http_proxy="$PROXY" https_proxy="$PROXY" HTTP_PROXY="$PROXY" HTTPS_PROXY="$PROXY"
fi
PROXY_ARG=()
[[ -n "$PROXY" ]] && PROXY_ARG=(--proxy "$PROXY")

RIME_DATA="$ROOT_DIR/vendor/rime/share/rime-data"
RIME_BUILD="$ROOT_DIR/vendor/rime/build"
RIME_USER="$HOME/.local/share/ibus-voice-ime/rime-user"
QWEN_VENV="$ROOT_DIR/.venv-qwen-asr"
QWEN_MODEL="$ROOT_DIR/vendor/models/qwen3-asr/Qwen3-ASR-1.7B"
RNNOISE_MODEL="$ROOT_DIR/vendor/models/rnnoise/bd.rnnn"

ERRORS=0
DONE_STEPS=()
SKIP_STEPS=()

step() { printf '\n==> %s\n' "$1"; }

# ---------------------------------------------------------------- 0) 基础 --
step "脚本可执行位"
chmod +x "$ROOT_DIR"/*.sh "$ROOT_DIR"/scripts/*.sh 2>/dev/null || true
echo "OK"

step "环境预检（FAIL 项需按提示处理，此处不阻断）"
"$ROOT_DIR/scripts/check-environment.sh" || true

# ---------------------------------------------------------- 1) rime-ice --
if [[ $WITH_RIME_ICE -eq 1 ]]; then
  step "rime-ice（雾凇拼音）词库"
  if [[ -f "$RIME_DATA/cn_dicts/base.dict.yaml" ]] && \
     { [[ -f "$RIME_BUILD/rime_ice.table.bin" ]] || [[ -f "$RIME_USER/build/rime_ice.table.bin" ]]; }; then
    echo "已就绪，跳过。"
    SKIP_STEPS+=("rime-ice 词库")
  else
    echo "下载词库（约 50MB）并用内置 librime 编译（1-3 分钟）……"
    if "$ROOT_DIR/scripts/setup-rime-ice.sh" "${PROXY_ARG[@]}" "${RIME_EXTRA_ARGS[@]}"; then
      DONE_STEPS+=("rime-ice 词库")
    else
      echo "⚠ rime-ice 部署失败（网络？可 --proxy 重试）。键盘将回退 luna_pinyin_simp。" >&2
      ERRORS=$((ERRORS + 1))
    fi
  fi
else
  SKIP_STEPS+=("rime-ice 词库（--no-rime-ice）")
fi

# ------------------------------------------------------------ 2) 降噪 --
if [[ $WITH_DENOISE -eq 1 ]]; then
  step "RNNoise 降噪模型"
  if [[ -f "$RNNOISE_MODEL" ]]; then
    echo "已就绪，跳过。"
    SKIP_STEPS+=("RNNoise 模型")
  else
    if "$ROOT_DIR/scripts/fetch-rnnoise-model.sh" "${PROXY_ARG[@]}"; then
      DONE_STEPS+=("RNNoise 模型")
    else
      echo "⚠ RNNoise 模型下载失败（录音降噪自动降级 notch 档，不影响可用）。" >&2
      ERRORS=$((ERRORS + 1))
    fi
  fi
else
  SKIP_STEPS+=("RNNoise 模型（--no-denoise）")
fi

# ------------------------------------------------------------- 3) 语音 --
if [[ $WITH_VOICE -eq 1 ]]; then
  step "语音输入（本地 Qwen3-ASR 后端）"
  # 三态探测：ok=CUDA 链路可用；partial=有 N 卡硬件但驱动不可用（给出修复命令）；
  # none=无 N 卡。旧版只看 nvidia-smi，会把“驱动半装/内核升级后模块未编好”误报成
  # “无 GPU”，用户明明插着卡（2026-10 报障）。
  GPU_STATE="" GPU_REASON="" GPU_ACTION=""
  eval "$("$ROOT_DIR/scripts/gpu-probe.sh" --env)"
  if [[ "$GPU_STATE" == "ok" ]]; then
    if [[ -x "$QWEN_VENV/bin/pip" && ( -f "$QWEN_MODEL/model.safetensors" || -f "$QWEN_MODEL/model.safetensors.index.json" ) ]]; then
      echo "已就绪，跳过（模型 $QWEN_MODEL）。"
      SKIP_STEPS+=("Qwen3-ASR 本地后端")
    else
      echo "安装本地 Qwen3-ASR：创建 venv + 下载 0.6B/1.7B 模型（约 6GB，耗时较长）……"
      if "$ROOT_DIR/scripts/setup-qwen-asr.sh" "${PROXY_ARG[@]}"; then
        DONE_STEPS+=("Qwen3-ASR 本地后端")
      else
        echo "⚠ Qwen3-ASR 安装失败（网络/磁盘/Python 版本？）。可重试 ./init.sh 或改用云端后端。" >&2
        ERRORS=$((ERRORS + 1))
      fi
    fi
  elif [[ "$GPU_STATE" == "partial" ]]; then
    echo "检测到 NVIDIA 显卡，但本地语音所需的 CUDA 驱动不可用，跳过本地语音后端（不算失败）："
    echo "  原因：${GPU_REASON:-未知}"
    [[ -n "$GPU_ACTION" ]] && printf '  %s\n' "$GPU_ACTION"
    SKIP_STEPS+=("Qwen3-ASR 本地后端（NVIDIA 驱动不可用）")
  else
    echo "未检测到 NVIDIA GPU，跳过本地语音后端（不算失败）。"
    echo "云端替代（自备 API Key）："
    echo "  VOICE_IME_MIMO_API_KEY='tp-xxx' ./scripts/switch-mimo-cloud-asr.sh cn   # 小米 MiMo"
    echo "  VOICE_IME_VOLC_API_KEY='xxx'  ./scripts/switch-volc-bigmodel-asr.sh    # 火山豆包"
    SKIP_STEPS+=("Qwen3-ASR 本地后端（无 GPU）")
  fi
else
  SKIP_STEPS+=("语音输入（--no-voice）")
fi

# ------------------------------------------------------- 4) 注册激活 --
if [[ $DO_INSTALL -eq 1 ]]; then
  step "install.sh：注册 IBus 组件 / 环境 / 热键并重启输入法"
  "$ROOT_DIR/install.sh"
fi

# ------------------------------------------------------------- 汇总 --
printf '\n============================================\n'
printf '初始化完成。\n'
if [[ ${#DONE_STEPS[@]} -gt 0 ]]; then
  printf '本次安装：%s\n' "${DONE_STEPS[*]}"
fi
if [[ ${#SKIP_STEPS[@]} -gt 0 ]]; then
  printf '跳过项目：%s\n' "${SKIP_STEPS[*]}"
fi
if [[ $ERRORS -gt 0 ]]; then
  printf '失败 %s 项：按上方 ⚠ 提示重试，或运行 ./scripts/doctor.sh 诊断。\n' "$ERRORS"
  exit 1
fi
if [[ $DO_INSTALL -eq 1 ]]; then
  printf '下一步：Super+Space 切到「自定义语音输入法」；nihao+Space 出词；Ctrl+Alt+V 语音。\n'
  printf '健康检查：./scripts/doctor.sh（只检查）/ ./scripts/doctor.sh fix（自动修复）\n'
else
  printf '（--skip-install：未注册。需要时运行 ./install.sh。）\n'
fi
exit 0
