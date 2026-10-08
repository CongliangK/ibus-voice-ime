#!/usr/bin/env bash
set -euo pipefail
rm -f "$HOME/.local/share/ibus/component/voice-custom.xml"
rm -f "$HOME/.config/environment.d/ibus-voice-ime.conf"
systemctl --user unset-environment IBUS_COMPONENT_PATH VOICE_IME_HOTKEYS VOICE_IME_RAW_HOTKEYS VOICE_IME_IPC_SOCKET 2>/dev/null || true
"$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/uninstall-gnome-voice-hotkey.sh" || true
"$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/uninstall-gnome-clipboard-paste-hotkey.sh" || true
ibus write-cache >/dev/null 2>&1 || true
if command -v gsettings >/dev/null 2>&1; then
  python3 - <<'PY'
import ast
import subprocess
entry = ('ibus', 'voice-custom')
try:
    raw = subprocess.check_output([
        'gsettings', 'get', 'org.gnome.desktop.input-sources', 'sources'
    ], text=True).strip()
    sources = ast.literal_eval(raw)
except Exception:
    sources = []
new_sources = [x for x in sources if tuple(x) != entry]
if new_sources != sources:
    subprocess.run([
        'gsettings', 'set', 'org.gnome.desktop.input-sources', 'sources', repr(new_sources)
    ], check=True)
    print('已从 GNOME 输入源移除 voice-custom')
PY
fi
# 重启 IBus 让组件移除生效。GNOME 沿用完整替换配方；其他桌面走 ibus restart，
# 避免 --replace/--panel disable 影响 DE 自管的 ibus 会话。
if [[ "${XDG_CURRENT_DESKTOP:-}" == *GNOME* || "${XDG_CURRENT_DESKTOP:-}" == *gnome* ]]; then
  if command -v ibus-daemon >/dev/null 2>&1; then
    ibus-daemon -drx --replace --panel disable --cache refresh || true
  fi
elif command -v ibus >/dev/null 2>&1; then
  ibus restart || true
fi
echo "卸载完成。用户数据（~/.local/share/ibus-voice-ime/）保留，可手工删除。"
