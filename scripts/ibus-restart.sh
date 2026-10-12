#!/usr/bin/env bash
# 统一的 IBus 重启入口：让组件/环境变更生效。
# GNOME 沿用完整替换配方（-drx --replace --panel disable）；
# 其他桌面只做 ibus restart——--replace 会杀掉 DE 自管的 ibus-daemon、
# --panel disable 会让依赖 kimpanel/ibus-panel 的桌面失去候选窗。
# 调用方通过环境变量前缀（VAR=... scripts/ibus-restart.sh）传递引擎配置，
# 本脚本 exec 保持该环境透传给 ibus-daemon。
#
# 组件路径必须自带（不能依赖调用方记得加前缀）：IBus 1.5.3x 的
# ibus_registry_load 在进程环境没有 IBUS_COMPONENT_PATH 时只扫系统组件目录
# （用户目录支持在源码中被 #if 0 禁用），而 GNOME/systemd 开机拉起的
# ibus-daemon 走 --cache auto：直接信任 ~/.cache/ibus/bus/registry 缓存，
# 只有缓存失效才重扫。因此任何一次无该变量的 daemon 重启（refresh 重扫
# 后覆写缓存，或 auto 重扫后写缓存）都会把用户组件从 registry 抹掉，
# 之后 auto daemon 认定缓存有效、永不再扫用户目录——症状是重启电脑后
# 输入法"未注册"且不自愈。故本脚本在组件 XML 存在时：推导
# IBUS_COMPONENT_PATH（调用方显式传入的值优先）→ 重启前先用
# ibus write-cache 原子重建注册缓存（该命令只替换缓存文件，不与运行中
# 的 daemon 交互，非 GNOME 的 ibus restart 路径同样受益）→ 再重启。
set -uo pipefail

COMPONENT_DIR="$HOME/.local/share/ibus/component"
if [[ -z "${IBUS_COMPONENT_PATH:-}" && -f "$COMPONENT_DIR/voice-custom.xml" ]]; then
  export IBUS_COMPONENT_PATH="$COMPONENT_DIR:/usr/share/ibus/component"
fi
if [[ -n "${IBUS_COMPONENT_PATH:-}" ]] && command -v ibus >/dev/null 2>&1; then
  # 固化正确的注册缓存；个别系统组件引擎 --xml 探测失败的 WARNING 无害，
  # 失败也不阻断重启（refresh 重启的 daemon 会再次重扫并写缓存）。
  ibus write-cache >/dev/null 2>&1 || true
fi

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
