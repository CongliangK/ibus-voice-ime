#!/usr/bin/env bash
set -euo pipefail

# 同时清理主语音热键（Ctrl+Alt+V）与原文语音热键（Ctrl+Alt+B）两个绑定。
NAME="自定义语音输入法语音输入"
RAW_NAME="自定义语音输入法语音输入（原文免整理）"

if ! command -v gsettings >/dev/null 2>&1; then
  exit 0
fi

python3 - "$NAME" "$RAW_NAME" <<'PY'
from __future__ import annotations

import ast
import subprocess
import sys

names = set(sys.argv[1:3])
MEDIA_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
CUSTOM_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"
KEY = "custom-keybindings"


def run(*args: str) -> str:
    return subprocess.check_output(["gsettings", *args], text=True).strip()


def setv(*args: str) -> None:
    subprocess.run(["gsettings", *args], check=True)


raw = run("get", MEDIA_SCHEMA, KEY)
if raw.startswith("@as"):
    raise SystemExit(0)
try:
    paths = [str(x) for x in ast.literal_eval(raw)]
except Exception:
    raise SystemExit(0)

kept: list[str] = []
removed = False
for path in paths:
    try:
        current_name = ast.literal_eval(run("get", f"{CUSTOM_SCHEMA}:{path}", "name"))
    except Exception:
        current_name = ""
    if current_name in names:
        removed = True
    else:
        kept.append(path)

if removed:
    setv("set", MEDIA_SCHEMA, KEY, repr(kept))
    print("已移除 GNOME 全局语音快捷键")
PY
