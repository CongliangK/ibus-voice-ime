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
#   ./init.sh --strict-assets              # CI：请求的可降级资产失败也返回 exit 1
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
STRICT_ASSETS=0
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
    --strict-assets)  STRICT_ASSETS=1; shift ;;
    --skip-install)   DO_INSTALL=0; shift ;;
    -h|--help)        sed -n '2,23p' "$0"; exit 0 ;;
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

# 请求资产采用 ready / degraded / blocked 三态；主动跳过仍单列 SKIP。
READY_STEPS=()
DEGRADED_TITLES=() DEGRADED_IMPACTS=() DEGRADED_COMMANDS=()
BLOCKED_TITLES=() BLOCKED_IMPACTS=() BLOCKED_COMMANDS=()
SKIP_STEPS=()

# 输出可直接粘贴执行的命令：统一单引号包裹——单引号内没有历史扩展（!）、
# 参数展开（$）与命令替换（反引号），交互 bash 复制粘贴也安全；内嵌单引号
# 用 '\'' 转义（红队实测双引号风格在交互 bash 下遇 ! 报 event not found）。
shell_quote() {
  local value="$1"
  value="${value//\'/\'\\\'\'}"
  printf "'%s'" "$value"
}
retry_command() {
  local arg sep=""
  for arg in "$@"; do
    printf '%s' "$sep"; shell_quote "$arg"; sep=" "
  done
}
# 标题/影响/命令进入展示与 TSV 前剥离控制字符：换行可截断/伪造
# VOICE_IME_INIT_RESULT 标记行，制表符会错位字段（红队实测）。
sanitize_field() {
  local value="$1"
  value="${value//$'\t'/ }"
  value="${value//$'\n'/ }"
  value="${value//$'\r'/ }"
  printf '%s' "$value"
}
RIME_RETRY="$(retry_command "$ROOT_DIR/scripts/setup-rime-ice.sh" "${PROXY_ARG[@]}" "${RIME_EXTRA_ARGS[@]}")"
RNNOISE_RETRY="$(retry_command "$ROOT_DIR/scripts/fetch-rnnoise-model.sh" "${PROXY_ARG[@]}")"
QWEN_RETRY="$(retry_command "$ROOT_DIR/scripts/setup-qwen-asr.sh" "${PROXY_ARG[@]}")"
# 向导选择是本次进程环境；单独续装时也保留下载集合与默认模型。
for name in VOICE_IME_QWEN_ASR_MODEL VOICE_IME_QWEN_ASR_SIZES; do
  if [[ -v "$name" ]]; then QWEN_RETRY="$name=$(shell_quote "${!name}") $QWEN_RETRY"; fi
done
record_problem() {
  local status="$1"
  local title impact command
  title="$(sanitize_field "$2")"
  impact="$(sanitize_field "$3")"
  command="$(sanitize_field "$4")"
  if [[ "$status" == degraded ]]; then
    DEGRADED_TITLES+=("$title"); DEGRADED_IMPACTS+=("$impact"); DEGRADED_COMMANDS+=("$command")
  else
    BLOCKED_TITLES+=("$title"); BLOCKED_IMPACTS+=("$impact"); BLOCKED_COMMANDS+=("$command")
  fi
  printf '⚠ [%s] %s —— %s\n  续装命令：%s\n' "$status" "$title" "$impact" "$command" >&2
}

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
  # 就绪判据含请求的扩展词典：核心缓存存在但请求的 zhwiki/moegirl 缺失
  # 时不能记 ready（红队发现 strict 模式盲区），需重跑 helper 补齐。
  RIME_READY=1
  [[ -f "$RIME_DATA/cn_dicts/base.dict.yaml" ]] || RIME_READY=0
  { [[ -f "$RIME_BUILD/rime_ice.table.bin" ]] || [[ -f "$RIME_USER/build/rime_ice.table.bin" ]]; } || RIME_READY=0
  if [[ ! " ${RIME_EXTRA_ARGS[*]:-} " == *" --no-zhwiki "* ]] && [[ ! -f "$RIME_DATA/zhwiki.dict.yaml" ]]; then
    RIME_READY=0
  fi
  if [[ ! " ${RIME_EXTRA_ARGS[*]:-} " == *" --no-moegirl "* ]] && [[ ! -f "$RIME_DATA/moegirl.dict.yaml" ]]; then
    RIME_READY=0
  fi
  if [[ $RIME_READY -eq 1 ]]; then
    echo "已就绪，跳过。"
    READY_STEPS+=("rime-ice 词库（已就绪）")
  else
    echo "下载词库（约 50MB）并用内置 librime 编译（1-3 分钟）……"
    RIME_RC=0
    "$ROOT_DIR/scripts/setup-rime-ice.sh" "${PROXY_ARG[@]}" "${RIME_EXTRA_ARGS[@]}" || RIME_RC=$?
    case "$RIME_RC" in
      0) READY_STEPS+=("rime-ice 词库") ;;
      # 核心成功、请求的扩展词典缺失（脚本软失败专用码）：记降级，
      # --strict-assets 下视为失败（红队发现 strict 模式原盲区）。
      3) record_problem degraded "rime-ice 扩展词典（zhwiki/moegirl）" "核心词库已部署；扩展词典缺失，专有名词/网络词命中率略降" "$RIME_RETRY" ;;
      *) record_problem degraded "rime-ice 词库" "部署失败；键盘回退 luna_pinyin_simp，仍可打字但词库较小" "$RIME_RETRY" ;;
    esac
  fi
else
  SKIP_STEPS+=("rime-ice 词库（--no-rime-ice）")
fi

# ------------------------------------------------------------ 2) 降噪 --
if [[ $WITH_DENOISE -eq 1 ]]; then
  step "RNNoise 降噪模型"
  if [[ -f "$RNNOISE_MODEL" ]]; then
    echo "已就绪，跳过。"
    READY_STEPS+=("RNNoise 模型（已就绪）")
  else
    if "$ROOT_DIR/scripts/fetch-rnnoise-model.sh" "${PROXY_ARG[@]}"; then
      READY_STEPS+=("RNNoise 模型")
    else
      record_problem degraded "RNNoise 模型" "下载失败；录音降噪自动降级 notch 档，不影响语音可用" "$RNNOISE_RETRY"
    fi
  fi
else
  SKIP_STEPS+=("RNNoise 模型（--no-denoise）")
fi

# ------------------------------------------------------------- 3) 语音 --
if [[ $WITH_VOICE -eq 1 && $DO_INSTALL -eq 1 ]]; then
  # 语音安装末尾的"桌面激活"（ibus engine voice-custom）要求引擎已注册进当前
  # IBus 会话；全新机器上此刻尚未注册，激活必失败（时序缺陷，2026-10 实测）。
  # 因此先注册一次（幂等），语音步骤的激活检查才有意义；结尾仍会再注册一次刷新。
  step "install.sh（预注册，供语音步骤桌面激活）"
  if ! VOICE_IME_INSTALL_FROM_INIT=1 "$ROOT_DIR/install.sh"; then
    printf '⚠ 预注册失败：语音步骤的桌面激活可能失败，以语音验收和最终注册结果为准。\n  续装命令：%s\n' "$(retry_command "$ROOT_DIR/install.sh")" >&2
  fi
fi
if [[ $WITH_VOICE -eq 1 ]]; then
  step "语音输入（本地 Qwen3-ASR 后端）"
  # 三态探测：ok=CUDA 链路可用；partial=有 N 卡硬件但驱动不可用（给出修复命令）；
  # none=无 N 卡。旧版只看 nvidia-smi，会把“驱动半装/内核升级后模块未编好”误报成
  # “无 GPU”，用户明明插着卡（2026-10 报障）。
  GPU_STATE="" GPU_REASON="" GPU_ACTION=""
  eval "$("$ROOT_DIR/scripts/gpu-probe.sh" --env)"
  if [[ "$GPU_STATE" == "ok" ]]; then
    # File existence is not readiness. The setup script reuses matching packages
    # without upgrading, but always checks headers, CUDA/Triton and real inference.
    echo "准备并验收 Qwen3-ASR（独立解释器 / 依赖 / 全部分片 / 短音频推理）……"
    if "$ROOT_DIR/scripts/setup-qwen-asr.sh" "${PROXY_ARG[@]}"; then
      READY_STEPS+=("Qwen3-ASR 本地后端（推理验收通过）")
    else
      record_problem blocked "Qwen3-ASR 本地后端（安装/验收）" "所选本地语音未就绪；键盘可用，语音不能宣称就绪（具体阶段见上方）" "$QWEN_RETRY"
    fi
  elif [[ "$GPU_STATE" == "partial" ]]; then
    echo "检测到 NVIDIA 显卡，但本地语音所需的 CUDA 驱动不可用，跳过本地语音后端（不算失败）："
    echo "  原因：${GPU_REASON:-未知}"
    [[ -n "$GPU_ACTION" ]] && printf '  %s\n' "$GPU_ACTION"
    SKIP_STEPS+=("Qwen3-ASR 本地后端（NVIDIA 驱动不可用）")
  else
    echo "未检测到 NVIDIA GPU，跳过本地语音后端（不算失败）。"
    echo "云端替代（自备 API Key）："
    echo "  VOICE_IME_SILICONFLOW_API_KEY='sk-xxx' ./scripts/switch-siliconflow-asr.sh   # 硅基流动（无需 GPU/大陆直连；SenseVoiceSmall 官方标注免费，Qwen3-ASR 按秒计费）"
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
  if VOICE_IME_INSTALL_FROM_INIT=1 "$ROOT_DIR/install.sh"; then
    READY_STEPS+=("IBus 注册激活")
  else
    record_problem blocked "IBus 注册激活（install.sh）" "组件/桌面注册未完成；输入法不能宣称可用" "$(retry_command "$ROOT_DIR/install.sh")"
  fi
fi

# ------------------------------------------------------------- 汇总 --
printf '\n============================================\n'
printf '初始化完成。\n'
if [[ ${#READY_STEPS[@]} -gt 0 ]]; then
  printf '已就绪：%s\n' "${READY_STEPS[*]}"
fi
if [[ ${#SKIP_STEPS[@]} -gt 0 ]]; then
  printf '跳过项目：%s\n' "${SKIP_STEPS[*]}"
fi
# 固定前缀 TSV 仅在汇总发出；setup 按数据读取，不 source/eval 命令。
print_problems() {
  local status="$1" i
  local -n titles="$2" impacts="$3" commands="$4"
  for ((i=0; i<${#titles[@]}; i++)); do
    printf '  [%s] %s —— %s；续装命令：%s\n' "$status" "${titles[i]}" "${impacts[i]}" "${commands[i]}"
    printf 'VOICE_IME_INIT_RESULT\t%s\t%s\t%s\t%s\n' "$status" "${titles[i]}" "${impacts[i]}" "${commands[i]}"
  done
}
if [[ ${#DEGRADED_TITLES[@]} -gt 0 ]]; then
  echo '⚠ 降级项（核心功能可用，可按需续装）：'
  print_problems degraded DEGRADED_TITLES DEGRADED_IMPACTS DEGRADED_COMMANDS
fi
if [[ ${#BLOCKED_TITLES[@]} -gt 0 ]]; then
  echo '✗ 阻断项（安装失败）：'
  print_problems blocked BLOCKED_TITLES BLOCKED_IMPACTS BLOCKED_COMMANDS
fi
EXIT_CODE=0
if [[ ${#BLOCKED_TITLES[@]} -gt 0 ]]; then EXIT_CODE=1; fi
if [[ $STRICT_ASSETS -eq 1 && ${#DEGRADED_TITLES[@]} -gt 0 ]]; then
  echo '--strict-assets：请求资产存在降级，按 CI 严格模式返回失败。'
  EXIT_CODE=1
fi
printf 'ready: %s  degraded: %s  blocked: %s  SKIP: %s  EXIT: %s\n' "${#READY_STEPS[@]}" "${#DEGRADED_TITLES[@]}" "${#BLOCKED_TITLES[@]}" "${#SKIP_STEPS[@]}" "$EXIT_CODE"
if [[ $EXIT_CODE -ne 0 ]]; then
  echo '请执行对应续装命令；子步骤幂等，续装不重跑向导/不重复下载已就绪资产。'
  exit "$EXIT_CODE"
fi
if [[ $DO_INSTALL -eq 1 ]]; then
  printf '下一步：Super+Space 切到「自定义语音输入法」；nihao+Space 出词。Ctrl+Alt+V 语音需后端验收通过（无 GPU/跳过语音时不可据此认为就绪）。\n'
  printf '健康检查：./scripts/doctor.sh（只检查）/ ./scripts/doctor.sh fix（自动修复）\n'
else
  printf '（--skip-install：未注册。需要时运行 ./install.sh。）\n'
fi
exit 0
