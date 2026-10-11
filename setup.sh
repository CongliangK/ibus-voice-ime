#!/usr/bin/env bash
# 一键安装入口：新机器拿到仓库后只需要跑这一个脚本。
#
# 它是现有脚本的编排层（不替代它们，高级用法仍可单独调用）：
#   setup.sh = check-environment（install.sh 内置预检）
#            + init.sh（词库/语音模型/降噪资产 + 自动调用 install.sh 注册）
#            + setup-qwen-asr.sh --verify-only（语音链路验收）
#            + doctor.sh（整体体检）
#
# 用法：
#   ./setup.sh                          # 全量默认：词库 + 本地 Qwen3-ASR + 降噪 + 验收
#   ./setup.sh --keyboard-only          # 只装键盘输入（Rime 拼音），不下载语音模型
#   ./setup.sh --cloud siliconflow      # 语音走云端（需 VOICE_IME_SILICONFLOW_API_KEY）
#   ./setup.sh --cloud mimo             # 语音走云端 MiMo（国内直连，需其 API Key）
#   ./setup.sh --verify-only            # 不安装，只做语音验收 + 体检
#   ./setup.sh --dry-run                # 只打印执行计划，不动任何东西
#
# 资产选项（透传给 init.sh）：--no-rime-ice --no-zhwiki --no-moegirl --no-denoise
# 其他：--proxy URL、--skip-verify、--skip-doctor、--yes（免确认）、-h/--help
#
# 所有子脚本幂等：重复运行 setup.sh 会跳过已就绪的资产，可用于断点续装。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CLOUD=""
VERIFY_ONLY=0
DRY_RUN=0
ASSUME_YES=0
SKIP_VERIFY=0
SKIP_DOCTOR=0
PROXY_ARGS=()
INIT_ARGS=()

usage() { sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --keyboard-only)   INIT_ARGS+=("--no-voice") ;;
    --no-voice)        INIT_ARGS+=("--no-voice") ;;
    --no-rime-ice|--no-zhwiki|--no-moegirl|--no-denoise)
                       INIT_ARGS+=("$1") ;;
    --cloud)
                       shift
                       [[ "${1:-}" == "siliconflow" || "${1:-}" == "mimo" ]] || {
                         echo "错误：--cloud 需要 siliconflow 或 mimo（收到：${1:-}）" >&2; exit 2; }
                       CLOUD="$1"; INIT_ARGS+=("--no-voice") ;;
    --verify-only)     VERIFY_ONLY=1 ;;
    --dry-run)         DRY_RUN=1 ;;
    -y|--yes)          ASSUME_YES=1 ;;
    --skip-verify)     SKIP_VERIFY=1 ;;
    --skip-doctor)     SKIP_DOCTOR=1 ;;
    --proxy)
                       shift; [[ -n "${1:-}" ]] || { echo "错误：--proxy 需要参数" >&2; exit 2; }
                       PROXY_ARGS+=("--proxy" "$1"); INIT_ARGS+=("--proxy" "$1") ;;
    -h|--help)         usage; exit 0 ;;
    *)                 echo "未知参数：$1（查看 --help）" >&2; exit 2 ;;
  esac
  shift
done

PLAN=()
if [[ "$VERIFY_ONLY" -eq 1 ]]; then
  PLAN+=("语音链路验收：scripts/setup-qwen-asr.sh --verify-only"
         "整体体检：scripts/doctor.sh")
else
  PLAN+=("环境预检 + 资产初始化 + 输入法注册：./init.sh ${INIT_ARGS[*]:-（全量默认）}")
  if [[ "$CLOUD" == "siliconflow" ]]; then
    PLAN+=("切换云端语音（SiliconFlow）：需已设置 VOICE_IME_SILICONFLOW_API_KEY")
  elif [[ "$CLOUD" == "mimo" ]]; then
    PLAN+=("切换云端语音（MiMo，国内直连）：scripts/switch-mimo-cloud-asr.sh cn")
  elif ! [[ "${INIT_ARGS[*]:-}" == *--no-voice* ]]; then
    PLAN+=("语音链路验收：scripts/setup-qwen-asr.sh --verify-only（本地 Qwen3-ASR，约 6GB 下载 + NVIDIA GPU）")
  fi
  [[ "$SKIP_DOCTOR" -eq 1 ]] || PLAN+=("整体体检：scripts/doctor.sh")
fi

echo "== 执行计划 =="
i=1; for p in "${PLAN[@]}"; do printf '  %d) %s\n' "$i" "$p"; i=$((i+1)); done

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "--dry-run：仅打印计划，未执行任何操作。"
  exit 0
fi

if [[ "$ASSUME_YES" -ne 1 && -t 0 ]]; then
  printf '继续执行？[y/N] '
  read -r REPLY
  [[ "$REPLY" =~ ^[Yy]$ ]] || { echo "已取消。"; exit 0; }
fi

step() { printf '\n==> %s\n' "$1"; }

if [[ "$VERIFY_ONLY" -eq 1 ]]; then
  step "语音链路验收"
  "$ROOT_DIR/scripts/setup-qwen-asr.sh" --verify-only "${PROXY_ARGS[@]+"${PROXY_ARGS[@]}"}"
  step "整体体检"
  "$ROOT_DIR/scripts/doctor.sh"
  echo "验收完成。"
  exit 0
fi

step "环境预检 + 资产初始化 + 输入法注册（init.sh）"
"$ROOT_DIR/init.sh" ${INIT_ARGS+"${INIT_ARGS[@]}"}

case "$CLOUD" in
  siliconflow)
    step "切换云端语音（SiliconFlow）"
    if [[ -z "${VOICE_IME_SILICONFLOW_API_KEY:-}" ]]; then
      echo "错误：未设置 VOICE_IME_SILICONFLOW_API_KEY。注册 cloud.siliconflow.cn 后：" >&2
      echo "  VOICE_IME_SILICONFLOW_API_KEY='sk-xxx' $ROOT_DIR/scripts/switch-siliconflow-asr.sh" >&2
      exit 1
    fi
    "$ROOT_DIR/scripts/switch-siliconflow-asr.sh"
    ;;
  mimo)
    step "切换云端语音（MiMo）"
    "$ROOT_DIR/scripts/switch-mimo-cloud-asr.sh" cn
    ;;
esac

if [[ "$SKIP_VERIFY" -ne 1 && -z "$CLOUD" ]] && ! [[ "${INIT_ARGS[*]:-}" == *--no-voice* ]]; then
  step "语音链路验收"
  "$ROOT_DIR/scripts/setup-qwen-asr.sh" --verify-only "${PROXY_ARGS[@]+"${PROXY_ARGS[@]}"}"
fi

if [[ "$SKIP_DOCTOR" -eq 1 ]]; then
  echo "安装完成（已按选项跳过体检）。"
else
  step "整体体检"
  "$ROOT_DIR/scripts/doctor.sh"
  echo "安装完成。切换方式：GNOME 顶栏输入法菜单或 Super+Space；Ctrl+Alt+V 语音，Ctrl+Alt+B 原文，Ctrl+Alt+P 粘贴。"
fi
