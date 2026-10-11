#!/usr/bin/env bash
# 选择本项目应使用的 Python 解释器，打印路径到 stdout（调用方捕获）。
#
# 背景问题：用户 shell 里激活的 conda/miniconda 会把 python3 遮蔽成
# 缺少 PyGObject(gi) 的发行版 Python，导致 check-environment 误报 FAIL、
# install.sh 拒绝执行。这里不再盲信 PATH 中的第一个 python3，而是：
#   1) 项目 venv 存在则直接用（venv 由本脚本体系创建，天然正确）；
#   2) 否则在候选列表里挑第一个「版本 >= 3.10 且能 import gi」的解释器；
#      候选含 PATH 的 python3 与系统 /usr/bin/python3*（dnf 的 gi 装在这里）。
#   3) 全部候选都没有 gi 时，退回第一个版本 >= 3.10 的候选，
#      让上层脚本按原有口径报「PyGObject 未安装」的可读错误。
# 约束：只读探测，不创建任何文件；输出仅一行路径；任何失败退出码非零。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  echo "$ROOT_DIR/.venv/bin/python"
  exit 0
fi

CANDIDATES=()
if command -v python3 >/dev/null 2>&1; then
  CANDIDATES+=("$(command -v python3)")
fi
# 系统 Python（dnf 的 python3-gobject 装在系统 site-packages）；按版本倒序
# 优先较新的（gi 与 IBus 绑定验证过的主路径）。
shopt -s nullglob
SYS_PYS=()
for cand in /usr/bin/python3 /usr/bin/python3.1[0-9]; do
  [[ -x "$cand" ]] && SYS_PYS+=("$cand")
done
for ((i=${#SYS_PYS[@]}-1; i>=0; i--)); do
  CANDIDATES+=("${SYS_PYS[$i]}")
done
shopt -u nullglob

ok_version() {  # 版本 >= 3.10
  "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}
has_gi() {
  "$1" -c 'import gi' >/dev/null 2>&1
}

FALLBACK=""
for cand in "${CANDIDATES[@]}"; do
  [[ -x "$cand" ]] || continue
  ok_version "$cand" || continue
  if has_gi "$cand"; then
    echo "$cand"
    exit 0
  fi
  [[ -z "$FALLBACK" ]] && FALLBACK="$cand"
done

if [[ -n "$FALLBACK" ]]; then
  echo "$FALLBACK"
  exit 0
fi

echo "错误：未找到版本 >= 3.10 的 Python 解释器" >&2
exit 1
