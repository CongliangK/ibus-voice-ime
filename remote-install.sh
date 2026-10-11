#!/usr/bin/env bash
# 远程安装引导器：克隆或安全更新仓库，再运行 setup.sh；不替代手动克隆安装。
# 用法（调用方须已有 git 与 curl，本脚本绝不 sudo）：
#   curl -fsSL https://raw.githubusercontent.com/CongliangK/ibus-voice-ime/main/remote-install.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/CongliangK/ibus-voice-ime/main/remote-install.sh | bash -s -- --defaults
# 所有参数原样透传给 setup.sh，重复运行会更新仓库并重跑幂等安装。
# 环境变量（显式空值或含空白的值均拒绝）：
#   IBUS_VOICE_IME_DIR  默认 $HOME/.local/share/ibus-voice-ime-repo（克隆目录；
#                       不得使用运行时数据根 ~/.local/share/ibus-voice-ime——
#                       首次安装后 rime-user 等用户数据会让克隆变脏，下次更新被拒）
#   IBUS_VOICE_IME_REPO 默认 https://github.com/CongliangK/ibus-voice-ime.git（可信镜像也可）
#   IBUS_VOICE_IME_REF  默认 main（分支名）
# 注意：curl | bash 的 stdin 是脚本管道，setup.sh 的 -t 0 会因此自动采用默认值。
# 有可打开的 /dev/tty 时把 setup 的 stdin 接回终端，保留交互向导；节点存在但
# 没有控制终端也视为无人值守，保持原 stdin，由 setup 自行非终端降级，不追加 --yes。
set -euo pipefail

fail() { printf '错误：%s\n' "$1" >&2; exit 1; }
network_hint() {
  printf '%s\n' '请检查网络、仓库地址和分支；可设置 https_proxy，或将 IBUS_VOICE_IME_REPO 指向可信镜像后重试。' >&2
}

DIR="${IBUS_VOICE_IME_DIR-${HOME-}/.local/share/ibus-voice-ime-repo}"
REPO="${IBUS_VOICE_IME_REPO-https://github.com/CongliangK/ibus-voice-ime.git}"
REF="${IBUS_VOICE_IME_REF-main}"
# 去掉尾部斜杠与 /. 尾缀：/tmp/link/ 或 /tmp/link/. 的 -L 判定为假，
# 会绕过符号链接检查；数据根比对同理须用规范化路径（红队发现）。
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
[[ -n "${IBUS_VOICE_IME_DIR+x}" || -n "${HOME-}" ]] || fail 'HOME 未设置，请显式设置 IBUS_VOICE_IME_DIR。'
git check-ref-format --branch "$REF" >/dev/null 2>&1 || fail 'IBUS_VOICE_IME_REF 非法：请指定有效的分支名。'
# 运行时数据根（rime-user/词库/日志）与克隆必须分离：混用会让克隆在首次
# 安装后变脏，下一次引导器拒绝自动更新（红队 2026-10-11 发现）。
# 双侧 realpath -m 规范化后再比对，/./ 与 /../ 中缀不得绕过。
canon() { realpath -m -- "$1" 2>/dev/null || printf '%s' "$1"; }
[[ "$(canon "$DIR")" != "$(canon "${HOME-}/.local/share/ibus-voice-ime")" ]] ||
  fail 'IBUS_VOICE_IME_DIR 不得使用运行时数据根 ~/.local/share/ibus-voice-ime（安装数据会让克隆变脏）；请使用其他目录。'

if [[ -e "$DIR" || -L "$DIR" ]]; then
  [[ -d "$DIR" && ! -L "$DIR" && -e "$DIR/.git" ]] ||
    fail '安装目录已存在，但不是可更新的 Git 克隆（或是符号链接）；不会覆盖，请设置 IBUS_VOICE_IME_DIR 使用其他目录。'
  TOP="$(git -C "$DIR" rev-parse --show-toplevel 2>/dev/null)" || fail '安装目录不是有效的 Git 工作仓库，不会覆盖。'
  DIR="$(cd -- "$DIR" && pwd -P)"
  [[ "$TOP" == "$DIR" ]] || fail '安装目录不是 Git 仓库根目录，不会覆盖。'
  ORIGIN="$(git -C "$DIR" remote get-url origin 2>/dev/null)" || fail '现有克隆缺少 origin 远端，不会覆盖。'
  [[ "$ORIGIN" == "$REPO" ]] || fail '现有克隆的 origin 与 IBUS_VOICE_IME_REPO 不匹配，不会覆盖；请检查仓库地址或使用其他目录。'
  CHANGES="$(git -C "$DIR" status --porcelain --untracked-files=all)" || fail '无法检查本地变更，拒绝自动更新。'
  [[ -z "$CHANGES" ]] || fail '现有克隆有未提交变更（含未跟踪文件），拒绝自动覆盖；请把需要的文件备份到克隆目录之外，或删除整个克隆目录让引导器重新拉取（不要在克隆里提交：本地提交同样会被拒绝自动更新）。'
  printf '%s\n' '正在更新已有的干净克隆……'
  # 显式分支映射支持浅克隆切换分支，避免 reset 到过时的远端跟踪引用。
  if ! git -C "$DIR" fetch --depth 1 origin "+refs/heads/$REF:refs/remotes/origin/$REF"; then
    network_hint
    fail '获取远端分支失败，尚未重置本地文件。'
  fi
  TARGET_REF="refs/remotes/origin/$REF"
  # 标签/简写歧义防护：一律用全限定 ref，避免名为 origin/<ref> 的标签
  # 让 reset 指向旧版本（红队 2026-10-11 发现）。
  NEW_HEAD="$(git -C "$DIR" rev-parse --verify "$TARGET_REF^{commit}")" || fail '远端分支解析失败，尚未重置本地文件。'
  # ignored 文件碰撞预检：本地被忽略的文件若与新版本跟踪文件同名，
  # reset --hard 会静默覆盖（用户数据丢失）；先拒绝并点名文件。
  COLLISION="$(comm -12 \
    <(git -C "$DIR" ls-tree -r --name-only "$TARGET_REF") \
    <(git -C "$DIR" ls-files -o -i --exclude-standard))" || true
  [[ -z "$COLLISION" ]] || fail "本地被忽略的文件与新版本文件同名，拒绝覆盖：$(printf '%s ' $COLLISION)"
  # 本地提交保护：引导器用 .git 内的同步标记识别自己管理的克隆；HEAD
  # 与标记不一致说明用户提交过本地变更，浅克隆无法区分落后/分叉，
  # 一律拒绝自动重置（红队 2026-10-11 发现本地提交会被 reset 删除）。
  MARKER="$DIR/.git/ibus-voice-ime-bootstrap"
  HEAD_NOW="$(git -C "$DIR" rev-parse HEAD)"
  if [[ "$HEAD_NOW" != "$NEW_HEAD" ]]; then
    if [[ -f "$MARKER" && "$(cat -- "$MARKER")" == "$HEAD_NOW" ]]; then
      git -C "$DIR" reset --hard "$TARGET_REF" || fail '更新本地克隆失败，请检查目录权限与 Git 状态。'
    else
      fail '克隆的 HEAD 与引导器上次同步的不一致（可能存在本地提交）；为防数据丢失拒绝自动更新，请自行处理后再试。'
    fi
  fi
  printf '%s\n' "$NEW_HEAD" > "$MARKER"
else
  printf '%s\n' '正在克隆安装仓库……'
  if ! git clone --depth 1 -b "$REF" -- "$REPO" "$DIR"; then
    network_hint
    fail '克隆失败，安装尚未开始；若留下半成品目录，请检查后自行处理或指定其他目录。'
  fi
  DIR="$(cd -- "$DIR" && pwd -P)"
  printf '%s\n' "$(git -C "$DIR" rev-parse HEAD)" > "$DIR/.git/ibus-voice-ime-bootstrap"
fi

[[ -f "$DIR/setup.sh" ]] || fail '仓库缺少 setup.sh，请检查仓库地址与分支。'
cd -- "$DIR"
# /dev/tty 通常始终存在；须同时探测能否打开，避免真正无人值守时退出。
# 放在同一复合命令中：bash 必须先从脚本管道读完 exec，再切换 stdin，
# 否则 curl | bash 会在切换后去终端读取脚本的下一行，导致安装入口未执行。
{
  if [[ -e /dev/tty ]] && { true </dev/tty; } 2>/dev/null; then
    exec </dev/tty
  fi
  exec bash "$DIR/setup.sh" "$@"
}
