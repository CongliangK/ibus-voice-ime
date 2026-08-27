#!/usr/bin/env bash
# 安装/卸载蓝牙耳机 profile 切换后自动恢复播放的守护服务（systemd user）。
#
#   ./install-bt-play-restore.sh            安装并启用
#   ./install-bt-play-restore.sh uninstall  卸载
#
# 依赖：playerctl、python3 + pygobject (gi)、systemd --user。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_PY="$ROOT_DIR/scripts/bt-play-restore.py"

INSTALL_DIR="${HOME}/.local/lib/ibus-voice-ime"
SERVICE_NAME="ibus-voice-ime-bt-play-restore.service"
SERVICE_FILE="${HOME}/.config/systemd/user/${SERVICE_NAME}"
TARGET_PY="${INSTALL_DIR}/bt-play-restore.py"

ACTION="${1:-install}"

uninstall() {
  echo "正在卸载 ${SERVICE_NAME} ..."
  if systemctl --user list-unit-files "${SERVICE_NAME}" >/dev/null 2>&1; then
    systemctl --user disable --now "${SERVICE_NAME}" 2>/dev/null || true
  fi
  rm -f "${SERVICE_FILE}" "${TARGET_PY}"
  # 安装目录为空时顺手清掉，但不强删（可能有同目录其它文件）。
  rmdir "${INSTALL_DIR}" 2>/dev/null || true
  systemctl --user daemon-reload 2>/dev/null || true
  echo "已卸载。"
}

check_deps() {
  local missing=0

  if ! command -v playerctl >/dev/null 2>&1; then
    echo "✗ 未找到 playerctl。请先安装：sudo dnf install playerctl"
    missing=1
  fi

  if ! command -v pactl >/dev/null 2>&1; then
    echo "✗ 未找到 pactl。请安装：sudo dnf install pulseaudio-utils"
    missing=1
  fi

  if ! command -v python3 >/dev/null 2>&1; then
    echo "✗ 未找到 python3。"
    missing=1
  else
    if ! python3 -c "import gi; gi.require_version('GLib','2.0'); from gi.repository import GLib" >/dev/null 2>&1; then
      echo "✗ python3 缺少 pygobject (gi)。请安装：sudo dnf install python3-gobject"
      missing=1
    fi
  fi

  if ! command -v systemctl >/dev/null 2>&1; then
    echo "✗ 未找到 systemctl。"
    missing=1
  fi

  if [[ "${missing}" -ne 0 ]]; then
    exit 1
  fi
}

install_service() {
  check_deps

  mkdir -p "${INSTALL_DIR}" "${HOME}/.config/systemd/user"
  cp "${SRC_PY}" "${TARGET_PY}"
  chmod 0755 "${TARGET_PY}"

  # 用绝对 python3 路径，避免不同发行版解释器名差异。
  PYBIN="$(command -v python3)"

  cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=ibus-voice-ime: 蓝牙耳机 profile 切换后自动恢复播放
After=wireplumber.service pipewire.service pipewire-pulse.service
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=${PYBIN} ${TARGET_PY}
Restart=on-failure
RestartSec=5
# 用户级服务无 DISPLAY 也能跑（playerctl 走 DBus session 总线，systemd --user 自带）。

[Install]
WantedBy=default.target
EOF

  systemctl --user daemon-reload
  systemctl --user enable --now "${SERVICE_NAME}"

  echo ""
  echo "✓ 已安装并启动 ${SERVICE_NAME}"
  echo "  守护脚本：${TARGET_PY}"
  echo "  服务文件：${SERVICE_FILE}"
  echo ""
  echo "查看状态：systemctl --user status ${SERVICE_NAME}"
  echo "查看日志：journalctl --user -u ${SERVICE_NAME} -f"
  echo "停止服务：systemctl --user disable --now ${SERVICE_NAME}"
  echo "卸载：    ${0} uninstall"
}

case "${ACTION}" in
  install)
    install_service
    ;;
  uninstall)
    uninstall
    ;;
  *)
    echo "用法：$0 [install|uninstall]"
    exit 2
    ;;
esac
