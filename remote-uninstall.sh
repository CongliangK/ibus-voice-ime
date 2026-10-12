#!/usr/bin/env bash
# 远程卸载引导器：在任何目录都能用 curl 管道彻底卸载本输入法（curl 安装的
# 默认布局：克隆在 ~/.local/share/ibus-voice-ime-repo，模型缓存在
# ~/.cache/ibus-voice-ime，用户数据/配置在 ~/.local/share|config/ibus-voice-ime）。
# 做法：定位克隆并调用其中的 uninstall-all.sh --purge --purge-models
# --purge-repo——注册/环境/热键清理解除、进程停止、用户数据与 API key、
# 模型共享缓存、整个克隆（.venv*、.python、.tools、vendor/ 下载产物）全部
# 删除，回到安装前的干净状态。绝不 sudo、不碰系统文件与跨项目共享缓存。
#
# 用法（调用方须已有 curl；本地克隆存在时只需 bash）：
#   curl -fsSL https://raw.githubusercontent.com/CongliangK/ibus-voice-ime/main/remote-uninstall.sh | bash
#   # 无人值守 / 无终端（SSH CI）必须显式确认：
#   curl -fsSL .../remote-uninstall.sh | bash -s -- --yes
#   # 保留个人词库与配置（其余照删）：
#   curl -fsSL .../remote-uninstall.sh | bash -s -- --keep-user-data
#
# 选项：
#   --yes / -y          跳过确认（无人值守必须）
#   --keep-user-data    保留个人词库/配置/热词（默认全部清除）
#   -h | --help         显示本帮助
#
# 环境变量（与 remote-install.sh 同名同义，显式空值或含空白均拒绝）：
#   IBUS_VOICE_IME_DIR  默认 $HOME/.local/share/ibus-voice-ime-repo（克隆目录）
#   IBUS_VOICE_IME_REPO 默认 https://github.com/CongliangK/ibus-voice-ime.git
#                       （仅在本地克隆不存在时用于推导 raw 下载地址）
#   IBUS_VOICE_IME_REF  默认 main（同上）
set -euo pipefail

usage() { awk 'NR==1{next} /^#/{sub(/^# ?/, ""); print; next} {exit}' "$0"; }

fail() { printf '错误：%s\n' "$1" >&2; exit 1; }

ASSUME_YES=0
KEEP_USER_DATA=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes|-y)          ASSUME_YES=1; shift ;;
    --keep-user-data)  KEEP_USER_DATA=1; shift ;;
    -h|--help)         usage; exit 0 ;;
    *) echo "未知参数：$1（查看 --help）" >&2; exit 2 ;;
  esac
done

if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
  fail '请勿以 root 运行（会操作错误用户的 HOME；本脚本也绝不使用 sudo）。'
fi
[[ -n "${HOME:-}" && -d "$HOME" ]] || fail 'HOME 未设置或不存在，请显式设置。'

DIR="${IBUS_VOICE_IME_DIR-${HOME-}/.local/share/ibus-voice-ime-repo}"
REPO="${IBUS_VOICE_IME_REPO-https://github.com/CongliangK/ibus-voice-ime.git}"
REF="${IBUS_VOICE_IME_REF-main}"
while [[ "$DIR" != "/" ]]; do
  case "$DIR" in
    */) DIR="${DIR%/}" ;;
    */.) DIR="${DIR%/.}" ;;
    *) break ;;
  esac
done
for name in IBUS_VOICE_IME_DIR IBUS_VOICE_IME_REPO IBUS_VOICE_IME_REF; do
  case "$name" in
    IBUS_VOICE_IME_DIR) value="$DIR" ;;
    IBUS_VOICE_IME_REPO) value="$REPO" ;;
    IBUS_VOICE_IME_REF) value="$REF" ;;
  esac
  [[ -n "$value" && ! "$value" =~ [[:space:][:cntrl:]] && "$value" != -* ]] ||
    fail "$name 非法：不得为空、含空白或控制字符，也不得以短横线开头。"
done

UNINSTALL_SCRIPT=''
HAD_CLONE=0
if [[ -e "$DIR" || -L "$DIR" ]]; then
  if [[ -f "$DIR/uninstall-all.sh" ]]; then
    UNINSTALL_SCRIPT="$DIR/uninstall-all.sh"
    HAD_CLONE=1
  else
    printf '警告：目录存在但缺少 uninstall-all.sh：%s\n' "$DIR" >&2
    printf '（可能是残缺克隆；注册足迹仍将清理，该目录请自行检查后删除。）\n' >&2
  fi
fi

# 本地没有可用脚本时从可信仓库下载（仅 github.com 布局可推导 raw 地址）。
if [[ -z "$UNINSTALL_SCRIPT" ]]; then
  RAW_URL=""
  case "$REPO" in
    https://github.com/*.git) RAW_URL="https://raw.githubusercontent.com/${REPO#https://github.com/}"; RAW_URL="${RAW_URL%.git}/$REF/uninstall-all.sh" ;;
    https://github.com/*)     RAW_URL="https://raw.githubusercontent.com/${REPO#https://github.com/}/$REF/uninstall-all.sh" ;;
    *) : ;;
  esac
  command -v curl >/dev/null 2>&1 || fail '本地克隆未找到且 curl 不可用，无法获取卸载脚本。'
  if [[ -n "$RAW_URL" ]] && command -v mktemp >/dev/null 2>&1; then
    TMP_SCRIPT="$(mktemp /tmp/ibus-voice-ime-uninstall.XXXXXX.sh)"
    if curl -fsSL -- "$RAW_URL" -o "$TMP_SCRIPT"; then
      UNINSTALL_SCRIPT="$TMP_SCRIPT"
      # 下载的脚本不在克隆内：没有仓库可删，--purge-repo 由克隆标记逻辑自然跳过。
    else
      rm -f -- "$TMP_SCRIPT"
    fi
  fi
  if [[ -z "$UNINSTALL_SCRIPT" ]]; then
    fail "找不到本地克隆（$DIR）也无法下载卸载脚本（$REPO @$REF）。请检查网络/代理，或手动克隆仓库后运行其中的 uninstall-all.sh。"
  fi
fi

ARGS=()
if [[ "$KEEP_USER_DATA" -eq 1 ]]; then ARGS+=(--keep-user-data); else ARGS+=(--purge); fi
ARGS+=(--purge-models --purge-repo)

printf '%s\n' '================ ibus-voice-ime 远程彻底卸载 ================'
printf '克隆目录：          %s\n' "$DIR"
printf '卸载脚本：          %s\n' "$UNINSTALL_SCRIPT"
printf '用户数据/配置：     %s\n' "$([[ "$KEEP_USER_DATA" -eq 1 ]] && echo '保留（--keep-user-data）' || echo '彻底删除（--purge）')"
printf '模型共享缓存：      删除（~/.cache/ibus-voice-ime 等）\n'
printf '仓库克隆本身：      删除（.venv*、.python、.tools、vendor/ 等全部下载产物）\n'
printf '%s\n' '跨项目共享缓存（~/.cache/pip、~/.triton/cache 等）与系统文件不会被触碰。'

if [[ "$ASSUME_YES" -ne 1 ]]; then
  ANSWER=''
  if [[ -e /dev/tty ]] && { true </dev/tty; } 2>/dev/null; then
    printf '%s' '确认执行以上彻底卸载？输入 yes 回车：'
    IFS= read -r ANSWER < /dev/tty || true
  fi
  [[ "$ANSWER" == "yes" ]] ||
    fail '未确认（无终端时请追加 --yes 重新运行）。尚未做任何删除。'
fi

RC=0
bash "$UNINSTALL_SCRIPT" "${ARGS[@]}" || RC=$?

if [[ "$HAD_CLONE" -eq 1 ]]; then
  if [[ -e "$DIR" ]]; then
    printf '警告：克隆目录仍存在：%s（若上方无拒绝说明，请手动检查后删除。）\n' "$DIR" >&2
    [[ "$RC" -ne 0 ]] || RC=1
  else
    printf '已确认克隆目录已删除：%s\n' "$DIR"
  fi
fi
if [[ "$UNINSTALL_SCRIPT" == /tmp/* ]]; then
  rm -f -- "$UNINSTALL_SCRIPT"
fi
if [[ "$RC" -ne 0 ]]; then
  printf '卸载未完全成功（退出码 %s）；请按上方 [残留] 提示人工处理。\n' "$RC" >&2
else
  printf '%s\n' '卸载完成：本机已回到安装前的干净状态（共享缓存除外）。'
fi
exit "$RC"
