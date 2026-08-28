#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# 两个语音热键共用同一个脚本，第二个以参数 raw 区分「原文语音输入」。
COMMAND="$ROOT_DIR/scripts/voice-toggle.sh"
BINDING="<Control><Alt>v"
NAME="自定义语音输入法语音输入"
RAW_COMMAND="$ROOT_DIR/scripts/voice-toggle.sh raw"
RAW_BINDING="<Control><Alt>b"
RAW_NAME="自定义语音输入法语音输入（原文免整理）"

if ! command -v gsettings >/dev/null 2>&1; then
  echo "未找到 gsettings，跳过 GNOME 全局语音快捷键注册。"
  exit 0
fi

# 一次调用注册两个绑定；各自按 command 或 name 匹配复用槽位，command/name
# 互不相同，因此不会互相覆盖。
python3 - "$COMMAND" "$BINDING" "$NAME" "$RAW_COMMAND" "$RAW_BINDING" "$RAW_NAME" <<'PY'
from __future__ import annotations

import ast
import subprocess
import sys

command, binding, name, raw_command, raw_binding, raw_name = sys.argv[1:7]
BINDINGS = [
    (command, binding, name),
    (raw_command, raw_binding, raw_name),
]
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

for binding_command, binding_keys, binding_name in BINDINGS:
    selected = ""
    for path in paths:
        if custom_get(path, "command") == binding_command or custom_get(path, "name") == binding_name:
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

    setv("set", f"{CUSTOM_SCHEMA}:{selected}", "name", repr(binding_name))
    setv("set", f"{CUSTOM_SCHEMA}:{selected}", "command", repr(binding_command))
    setv("set", f"{CUSTOM_SCHEMA}:{selected}", "binding", repr(binding_keys))
    print(f"已注册 GNOME 全局语音快捷键：{binding_keys} -> {binding_command}")
PY
