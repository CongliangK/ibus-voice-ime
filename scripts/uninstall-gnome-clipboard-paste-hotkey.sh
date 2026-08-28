#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMMAND="$ROOT_DIR/scripts/clipboard-paste.sh"
NAME="自定义语音输入法输入法粘贴"

if ! command -v gsettings >/dev/null 2>&1; then
  echo "未找到 gsettings，跳过 GNOME 全局输入法粘贴快捷键移除。"
  exit 0
fi

python3 - "$COMMAND" "$NAME" <<'PY'
from __future__ import annotations

import ast
import subprocess
import sys

command, name = sys.argv[1:3]
MEDIA_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
CUSTOM_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"
KEY = "custom-keybindings"


def run(*args: str) -> str:
    return subprocess.check_output(["gsettings", *args], text=True).strip()


def setv(*args: str) -> None:
    subprocess.run(["gsettings", *args], check=True)


def parse_paths(raw: str) -> list[str]:
    raw = raw.strip()
    if raw.startswith("@as"):
        return []
    try:
        value = ast.literal_eval(raw)
    except Exception:
        return []
    return [str(x) for x in value]


def custom_get(path: str, key: str) -> str:
    try:
        return ast.literal_eval(run("get", f"{CUSTOM_SCHEMA}:{path}", key))
    except Exception:
        return ""

try:
    paths = parse_paths(run("get", MEDIA_SCHEMA, KEY))
except Exception as exc:
    print(f"无法读取 GNOME 自定义快捷键，已跳过：{exc}")
    raise SystemExit(0)

remove = [path for path in paths if custom_get(path, "command") == command or custom_get(path, "name") == name]
if not remove:
    print("GNOME 全局输入法粘贴快捷键不存在，无需移除。")
    raise SystemExit(0)

remaining = [path for path in paths if path not in set(remove)]
setv("set", MEDIA_SCHEMA, KEY, repr(remaining))
for path in remove:
    for key in ("binding", "command", "name"):
        try:
            setv("reset", f"{CUSTOM_SCHEMA}:{path}", key)
        except Exception:
            pass
print("已移除 GNOME 全局输入法粘贴快捷键；Ctrl+Alt+P 将直接交给 IBus 处理。")
PY
