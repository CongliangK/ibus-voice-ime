#!/usr/bin/env bash
# 把 environment.d 风格的 KEY=VALUE 文件转成可安全 eval 的 `export K=...` 行。
#
# 为什么不直接 `set -a; source <file>`：值里含空格（HOME 或仓库路径带空格，如
# /home/John Doe/ime）时，source 会把它当 shell 语法解析，第二段被当成命令执行，
# 引擎直接启动失败（exit 127）。systemd environment.d 本就不做 shell 展开/续行，
# 这里按行解析还原其语义；键名做白名单校验，防恶意/误写的行被 eval。
#
# 用法：eval "$(scripts/env-file-load.sh ~/.config/environment.d/ibus-voice-ime.conf)"
#       （文件不存在/不可读 → 输出空，eval 无副作用）
set -uo pipefail
FILE="${1:?用法：env-file-load.sh <env-file>}"
[[ -r "$FILE" ]] || exit 0
while IFS= read -r line || [[ -n "$line" ]]; do
  case "$line" in ''|'#'*) continue ;; esac
  line="${line#"${line%%[![:space:]]*}"}"      # 去行首空白
  [[ "$line" == *=* ]] || continue
  key="${line%%=*}"
  val="${line#*=}"
  [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
  printf 'export %s=%q\n' "$key" "$val"
done < "$FILE"
