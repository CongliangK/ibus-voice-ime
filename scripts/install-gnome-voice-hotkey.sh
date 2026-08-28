#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMMAND="$ROOT_DIR/scripts/voice-toggle.sh"
BINDING="<Control><Alt>v"
NAME="自定义语音输入法语音输入"

if ! command -v gsettings >/dev/null 2>&1; then
  echo "未找到 gsettings，跳过 GNOME 全局语音快捷键注册。"
  exit 0
fi

python3 - "$COMMAND" "$BINDING" "$NAME" <<'PY'
from __future__ import annotations

import ast
import subprocess
import sys

command, binding, name = sys.argv[1:4]
MEDIA_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
CUSTOM_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"
KEY = "custom-keybindings"
PREFIX = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"


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

selected = ""
for path in paths:
    if custom_get(path, "command") == command or custom_get(path, "name") == name:
        selected = path
        break

if not selected:
    used = set(paths)
    idx = 0
    while True:
        candidate = f"{PREFIX}custom{idx}/"
        if candidate not in used:
            selected = candidate
            break
        idx += 1
    paths.append(selected)
    setv("set", MEDIA_SCHEMA, KEY, repr(paths))

setv("set", f"{CUSTOM_SCHEMA}:{selected}", "name", repr(name))
setv("set", f"{CUSTOM_SCHEMA}:{selected}", "command", repr(command))
setv("set", f"{CUSTOM_SCHEMA}:{selected}", "binding", repr(binding))
print(f"已注册 GNOME 全局语音快捷键：{binding} -> {command}")
PY
