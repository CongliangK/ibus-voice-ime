#!/usr/bin/env bash
# 给 GNOME 的 ibus systemd 用户单元写入 drop-in，追加 --cache refresh。
#
# 根因（2026-10 实测 + ibus 源码）：Fedora 会在 /var/cache/ibus/bus/registry
# 预置 root 生成的系统级注册缓存（ibus 包更新时刷新），而 ibus-daemon 默认
# --cache auto 的加载顺序是「系统缓存 → 用户缓存 → 重扫 IBUS_COMPONENT_PATH」，
# 系统目录不变则系统缓存永远"有效"——开机由 systemd 拉起的 daemon 在第一层
# 就被短路，用户组件目录（本项目的 IBUS_COMPONENT_PATH）永不被扫描，
# voice-custom 从引擎列表消失且不自愈（用户缓存里有没有它都一样）。
# 安装时手动 --cache refresh 重启的 daemon 能加载，纯属会话内存状态，
# 重启电脑后必然复发。
#
# 修复：用户级 drop-in 把 daemon 启动参数改为 --cache refresh——无视全部
# 缓存、每次启动强制重扫组件路径（daemon 环境的 IBUS_COMPONENT_PATH 由
# environment.d 提供）。纯用户目录操作、无 sudo、幂等；systemd 无此 unit
# 或非 systemd 会话时静默跳过（退出码 0，手动 ibus-restart.sh 路径不受影响）。
set -uo pipefail

IBUS_UNIT="org.freedesktop.IBus.session.GNOME.service"
DROPIN_NAME="ibus-voice-ime-cache-refresh.conf"

if ! command -v systemctl >/dev/null 2>&1 \
   || ! systemctl --user cat "$IBUS_UNIT" >/dev/null 2>&1; then
  # 无该 unit（非 Fedora-GNOME / 非 systemd）：不算错误，静默跳过。
  exit 0
fi

DROPIN_DIR="$HOME/.config/systemd/user/$IBUS_UNIT.d"
DROPIN_FILE="$DROPIN_DIR/$DROPIN_NAME"
mkdir -p "$DROPIN_DIR"
cat > "$DROPIN_FILE" <<'EOF_DROPIN'
# ibus-voice-ime：让 GNOME 的 ibus-daemon 强制重扫组件路径（--cache refresh）。
# 根因：系统级注册缓存 /var/cache/ibus/bus/registry 使 auto 模式在第一层短路，
# 用户组件目录（IBUS_COMPONENT_PATH）在开机时永不被扫描。
# 下方 ExecStart 复制自 Fedora unit 原始命令，仅追加 --cache refresh；
# 系统 unit 将来若变更形态，重跑 install.sh 会以本文件最新模板覆盖。
[Service]
ExecStart=
ExecStart=sh -c 'exec /usr/bin/ibus-daemon --panel disable --cache refresh $([ "$XDG_SESSION_TYPE" = "x11" ] && echo --xim)'
EOF_DROPIN

# daemon-reload 失败不阻断安装（下次登录时 systemd 自然重载）。
systemctl --user daemon-reload >/dev/null 2>&1 || true
echo "已写入 ibus 开机自愈 drop-in：$DROPIN_FILE（daemon 启动即强制重扫组件路径）"
