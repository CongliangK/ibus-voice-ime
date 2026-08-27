#!/usr/bin/env bash
# 统一的 IBus 重启入口：让组件/环境变更生效。
# GNOME 沿用完整替换配方（-drx --replace --panel disable）；
# 其他桌面只做 ibus restart——--replace 会杀掉 DE 自管的 ibus-daemon、
# --panel disable 会让依赖 kimpanel/ibus-panel 的桌面失去候选窗。
# 调用方通过环境变量前缀（VAR=... scripts/ibus-restart.sh）传递引擎配置，
# 本脚本 exec 保持该环境透传给 ibus-daemon。
set -uo pipefail

DESKTOP="${XDG_CURRENT_DESKTOP:-}"
if [[ "${DESKTOP,,}" == *gnome* ]] && command -v ibus-daemon >/dev/null 2>&1; then
  exec ibus-daemon -drx --replace --panel disable --cache refresh
fi
if command -v ibus >/dev/null 2>&1; then
  echo "非 GNOME 桌面：使用 ibus restart；若输入法未刷新，请重新登录一次。" >&2
  exec ibus restart
fi
echo "未找到 ibus / ibus-daemon，请手动重启输入法服务。" >&2
exit 1
