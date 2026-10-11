#!/usr/bin/env bash
# 桌面激活 voice-custom 引擎（供各 switch-*.sh 在重启 IBus 后调用）。
#
# 经验（2026-10 实测）：GNOME 会话由输入源管理引擎选择，`ibus engine voice-custom`
# 全局切换不稳定——daemon 重启后组件加载有 DBus/缓存时序，单次调用会间歇性失败。
# 判定语义：
#   1) 重试若干次 ibus engine voice-custom，成功即激活；
#   2) 仍失败时降级判定：IBus 组件 XML 已注册 + GNOME 输入源含 voice-custom
#      即视为桌面可用（用户用 Super+Space 切换到「自定义语音输入法」）；
#   3) 两者都不满足才失败（非零退出），由调用方走 activation_failed。
set -euo pipefail

ACTIVATED=0
if command -v ibus >/dev/null 2>&1; then
  for _ in 1 2 3 4 5; do
    if ibus engine voice-custom >/dev/null 2>&1; then ACTIVATED=1; break; fi
    sleep 2
  done
fi

if [[ "$ACTIVATED" -eq 1 ]]; then
  exit 0
fi

if command -v gsettings >/dev/null 2>&1 \
   && [[ -f "$HOME/.local/share/ibus/component/voice-custom.xml" ]] \
   && python3 - <<'PY'
import ast, subprocess, sys
try:
    raw = subprocess.check_output(
        ['gsettings', 'get', 'org.gnome.desktop.input-sources', 'sources'], text=True).strip()
    sources = ast.literal_eval(raw)
    sys.exit(0 if ('ibus', 'voice-custom') in [tuple(x) for x in sources if isinstance(x, (list, tuple))] else 1)
except Exception:
    sys.exit(1)
PY
then
  echo "提示：ibus engine 全局切换未成功（GNOME 输入源管理下常见）。"
  echo "组件与 GNOME 输入源均已注册 voice-custom：用 Super+Space 切换到「自定义语音输入法」即可，视为激活。"
  exit 0
fi

echo "激活失败：无法选择 voice-custom 引擎（IBus 全局切换失败且 GNOME 输入源未注册）" >&2
exit 1
