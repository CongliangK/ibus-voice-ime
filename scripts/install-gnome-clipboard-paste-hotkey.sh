#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMMAND="$ROOT_DIR/scripts/clipboard-paste.sh"
NAME="自定义语音输入法输入法粘贴"

if ! command -v gsettings >/dev/null 2>&1; then
  echo "未找到 gsettings，跳过 GNOME 全局输入法粘贴快捷键注册。"
  exit 0
fi

# 键位从用户实际生效的配置（recording.clipboard_hotkeys，支持完整修饰键组合
# 与多组合；environment.d 的 ibus-voice-ime.conf > config.json > defaults，
# 与 run-engine.sh 同源同优先级）读取，读不到时回落默认 <Control><Alt>p。
# 每个组合一个槽位；重装按 (command, binding) 对齐，清理失效/重复槽位。
python3 - "$COMMAND" "$NAME" "$ROOT_DIR" <<'PY'
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys

command, name, root_dir = sys.argv[1:4]
MEDIA_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
CUSTOM_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"
KEY = "custom-keybindings"
PREFIX = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
DEFAULT_ACCEL = "<Control><Alt>p"

_ACCEL = {
    "ctrl": "<Control>",
    "alt": "<Alt>",
    "shift": "<Shift>",
    "super": "<Super>",
}
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def load_environment_d() -> None:
    """与 run-engine.sh / env-file-load.sh 同源同优先级（见语音热键安装器）。"""
    path = os.path.expanduser("~/.config/environment.d/ibus-voice-ime.conf")
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        if os.path.exists(path):
            print(f"警告：无法读取 {path}（{exc!r}），已跳过")
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not _ENV_KEY_RE.fullmatch(key):
            continue
        if key.startswith("VOICE_IME_"):
            try:
                os.environ[key] = value
            except ValueError:
                print(f"警告：环境键 {key!r} 含非法字符，已跳过")


def accel(combo) -> str:
    return "".join(
        _ACCEL[mod] for mod in ("ctrl", "alt", "shift", "super") if mod in combo.modifiers
    ) + combo.letter


def configured_bindings() -> list[tuple[str, str]]:
    """Return [(accel, name)] for every configured clipboard hotkey combo."""
    sys.path.insert(0, root_dir + "/src")
    load_environment_d()
    bindings: list[tuple[str, str]] = []
    try:
        from ibus_voice_ime import clipboard_paste

        for i, combo in enumerate(clipboard_paste.hotkey_combos()):
            label = name if i == 0 else f"{name}（{accel(combo)}）"
            bindings.append((accel(combo), label))
    except Exception as exc:  # noqa: BLE001 - 配置不可用时回落默认键位
        print(f"读取粘贴热键配置失败（{exc}），回落默认 Ctrl+Alt+P。")
    if not bindings:
        bindings = [(DEFAULT_ACCEL, name)]
    return bindings


BINDINGS = configured_bindings()
DESIRED_KEYS = {keys for keys, _ in BINDINGS}


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

# 对齐：本工具槽位中失效组合（含被裁剪的后续组合）与重复组合清出活动列表。
kept: list[str] = []
pair_slots: dict[str, str] = {}
pruned = 0
for path in paths:
    slot_command = custom_get(path, "command")
    slot_binding = custom_get(path, "binding")
    if slot_command == command:
        if slot_binding not in DESIRED_KEYS or slot_binding in pair_slots:
            pruned += 1
            continue
        pair_slots[slot_binding] = path
    kept.append(path)
if pruned:
    paths = kept
    setv("set", MEDIA_SCHEMA, KEY, repr(paths))
    print(f"已清理 {pruned} 个失效/重复的旧粘贴快捷键槽位")

for binding_keys, binding_name in BINDINGS:
    selected = pair_slots.get(binding_keys, "")
    if not selected:
        for path in paths:
            if custom_get(path, "name") == binding_name:
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
    setv("set", f"{CUSTOM_SCHEMA}:{selected}", "command", repr(command))
    setv("set", f"{CUSTOM_SCHEMA}:{selected}", "binding", repr(binding_keys))
    print(f"已注册 GNOME 全局输入法粘贴快捷键：{binding_keys} -> {command}")
PY
