#!/usr/bin/env bash
# 远程卸载引导器：在任何目录都能用 curl 管道彻底卸载本输入法（curl 安装的
# 默认布局：克隆在 ~/.local/share/ibus-voice-ime-repo，模型缓存在
# ~/.cache/ibus-voice-ime，用户数据/配置在 ~/.local/share|config/ibus-voice-ime）。
# 流程：① 若本地是引导器管理的克隆，先尝试把它更新到远端最新版（失败不阻断，
# 回退用本地版本——卸载脚本长期稳定，旧版也足以完成注册/环境/数据清理）；
# ② 只向卸载脚本传递它实际支持的参数（版本探测，避免新旧参数不匹配）；
# ③ 模型共享缓存与克隆目录本身由本引导器兜底删除（不依赖卸载脚本版本）。
# 绝不 sudo、不碰系统文件与跨项目共享缓存。
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
    else
      rm -f -- "$TMP_SCRIPT"
    fi
  fi
  if [[ -z "$UNINSTALL_SCRIPT" ]]; then
    fail "找不到本地克隆（$DIR）也无法下载卸载脚本（$REPO @$REF）。请检查网络/代理，或手动克隆仓库后运行其中的 uninstall-all.sh。"
  fi
fi

# 版本探测与参数构建在克隆更新之后进行（见下）：更新可能把旧版卸载脚本
# 换成新版，提前探测会用过期结论漏传 --purge-models。

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

# ① 确认后先尝试更新引导器管理的克隆到远端最新版：更新只为一件事——尽量用
# 上最新卸载脚本。克隆马上就要删除，内容安全要求低于 remote-install.sh，但
# 仅限带引导器标记的克隆（开发克隆有未提交工作，绝不 reset）。任何失败都回
# 退本地版本：卸载脚本长期向后兼容，旧版照样能完成注册/环境/数据清理。
update_clone() {
  [[ -d "$DIR/.git" && -f "$DIR/.git/ibus-voice-ime-bootstrap" ]] || return 1
  command -v git >/dev/null 2>&1 || return 1
  git -C "$DIR" fetch --depth 1 origin "+refs/heads/$REF:refs/remotes/origin/$REF" >/dev/null 2>&1 || return 1
  local new_head
  new_head="$(git -C "$DIR" rev-parse --verify "refs/remotes/origin/$REF^{commit}" 2>/dev/null)" || return 1
  git -C "$DIR" reset --hard "$new_head" >/dev/null 2>&1 || return 1
  git -C "$DIR" clean -fdq >/dev/null 2>&1 || true
  printf '%s\n' "$new_head" > "$DIR/.git/ibus-voice-ime-bootstrap" 2>/dev/null || true
  printf '已更新克隆到远端最新版（%s 的 %s）。\n' "$REF" "${new_head:0:12}"
}
if [[ "$HAD_CLONE" -eq 1 ]]; then
  printf '%s\n' '==> 尝试更新本地克隆到最新版（失败则回退本地版本，不阻断卸载）'
  update_clone || printf '%s\n' '（更新失败：继续使用克隆内的本地卸载脚本。）'
fi

# ② 版本探测（必须在更新之后）：只向卸载脚本传递它实际支持的参数。用
# `--help` 输出判定而非 grep 脚本正文——注释里提到参数不代表 case 分支真的
# 认识它。旧版脚本也完全兼容——模型缓存与克隆删除由引导器兜底完成。
script_supports() { bash "$UNINSTALL_SCRIPT" --help 2>/dev/null | grep -qF -- "$1"; }
ARGS=()
if [[ "$KEEP_USER_DATA" -eq 1 ]]; then ARGS+=(--keep-user-data); else ARGS+=(--purge); fi
MODELS_BY_SCRIPT=0
if script_supports --purge-models; then
  ARGS+=(--purge-models)
  MODELS_BY_SCRIPT=1
else
  printf '提示：本地卸载脚本为旧版（不支持 --purge-models）；模型缓存将由引导器兜底删除。\n'
fi

# ③ 兜底缓存候选提前抓取（卸载脚本随后会删除 environment.d 文件）：
# 当前 shell 环境 + environment.d 持久环境中的 VOICE_IME_MODEL_CACHE。
FALLBACK_CACHE_CANDIDATES=()
collect_fallback_cache_candidates() {
  local value
  [[ -z "${VOICE_IME_MODEL_CACHE:-}" ]] || FALLBACK_CACHE_CANDIDATES+=("$VOICE_IME_MODEL_CACHE")
  value="$(sed -n -E 's/^[[:space:]]*(export[[:space:]]+)?VOICE_IME_MODEL_CACHE=(.+)$/\2/p' \
    "$HOME/.config/environment.d/ibus-voice-ime.conf" 2>/dev/null | tail -n 1 || true)"
  value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
  [[ -z "$value" ]] || FALLBACK_CACHE_CANDIDATES+=("$value")
  FALLBACK_CACHE_CANDIDATES+=("${XDG_CACHE_HOME:-$HOME/.cache}/ibus-voice-ime")
}

RC=0
collect_fallback_cache_candidates
bash "$UNINSTALL_SCRIPT" "${ARGS[@]}" || RC=$?

# ② 模型缓存兜底：卸载脚本为旧版（未收到 --purge-models）时由引导器删除。
if [[ "$MODELS_BY_SCRIPT" -eq 0 ]]; then
  purge_model_cache_fallback() {
    local home_real raw path resolved
    home_real="$(realpath -e -- "$HOME" 2>/dev/null)" || return 1
    for raw in "${FALLBACK_CACHE_CANDIDATES[@]}"; do
      [[ -n "$raw" ]] || continue
      path="$(realpath -m -- "${raw/#\~/$HOME}")" || continue
      resolved="$(realpath -m -- "$path")" || continue
      # 只删 HOME 内、非 HOME 本身的路径，且不得包含 HOME 之外的解析目标。
      [[ "$path" == "$home_real"/* && "$resolved" == "$home_real"/* ]] || {
        printf '警告：模型缓存路径在 HOME 外或异常，请手动删除：%s\n' "$raw" >&2
        RC=1
        continue
      }
      if [[ -e "$path" ]]; then
        rm -rf -- "$path" && printf '已删除模型缓存（引导器兜底）：%s\n' "$path" || {
          printf '警告：无法删除模型缓存：%s\n' "$path" >&2; RC=1; }
      fi
    done
  }
  purge_model_cache_fallback
fi

# ③ 克隆目录兜底删除：不依赖卸载脚本版本。与 uninstall-all.sh --purge-repo
# 同款安全判据——仅引导器标记 + 身份文件；开发克隆拒绝删除，提示手动处理。
# 卸载脚本失败时保留克隆（残留排查可能还需要它），只警告。
if [[ "$HAD_CLONE" -eq 1 ]]; then
  if [[ "$RC" -ne 0 ]]; then
    printf '警告：卸载脚本未成功（退出码 %s），保留克隆目录供排查：%s\n' "$RC" "$DIR" >&2
  elif [[ -f "$DIR/.git/ibus-voice-ime-bootstrap" && -f "$DIR/setup.sh" && -f "$DIR/install.sh" ]] \
     && [[ "$DIR" != "$HOME" && "$HOME" != "$DIR" && "$HOME" != "$DIR/"* ]]; then
    if rm -rf -- "$DIR" 2>/dev/null; then
      printf '已删除仓库克隆（引导器兜底，含 .venv*、vendor/ 等全部下载产物）：%s\n' "$DIR"
    else
      printf '警告：无法删除克隆目录：%s（检查占用进程/权限后手动 rm -rf。）\n' "$DIR" >&2
      RC=1
    fi
  else
    printf '警告：拒绝删除克隆目录（无引导器标记或身份文件不全，可能是开发克隆）：\n%s\n请自行确认后手动删除。\n' "$DIR" >&2
    RC=1
  fi
  if [[ -e "$DIR" ]]; then
    printf '警告：克隆目录仍存在：%s\n' "$DIR" >&2
    [[ "$RC" -ne 0 ]] || RC=1
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
