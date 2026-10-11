#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# 两个语音热键共用同一个脚本，第二个以参数 raw 区分「原文语音输入」。
COMMAND="$ROOT_DIR/scripts/voice-toggle.sh"
NAME="自定义语音输入法语音输入"
RAW_COMMAND="$ROOT_DIR/scripts/voice-toggle.sh raw"
RAW_NAME="自定义语音输入法语音输入（原文免整理）"

if ! command -v gsettings >/dev/null 2>&1; then
  echo "未找到 gsettings，跳过 GNOME 全局语音快捷键注册。"
  exit 0
fi

# 键位从用户实际生效的配置读取（environment.d 的 ibus-voice-ime.conf >
# config.json > defaults，与 run-engine.sh 同源同优先级），读不到时回落默认
# <Control><Alt>v / <Control><Alt>b。每个组合键位一个槽位；重装按
# (command, binding) 对齐期望集合：清理失效槽位与重复活动槽位，重排组合
# 也不残留重复绑定，保证全局快捷键与引擎路由一致。
python3 - "$COMMAND" "$NAME" "$RAW_COMMAND" "$RAW_NAME" "$ROOT_DIR" <<'PY'
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys

command, name, raw_command, raw_name, root_dir = sys.argv[1:6]
MEDIA_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
CUSTOM_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"
KEY = "custom-keybindings"
PREFIX = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
OUR_COMMANDS = (command, raw_command)

_ACCEL = {
    "ctrl": "<Control>",
    "alt": "<Alt>",
    "shift": "<Shift>",
    "super": "<Super>",
}
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def load_environment_d() -> None:
    """与 run-engine.sh / env-file-load.sh 同源同优先级。

    引擎只加载 ~/.config/environment.d/ibus-voice-ime.conf，且 export 无条件
    覆盖继承的会话环境（仅 4 个 RIME 路径键例外，与热键无关）。这里复刻该
    语义：同一文件、同样覆盖、值不去引号（systemd/env-file-load 均不展开）。
    坏文件/非法键告警并跳过，绝不让安装器崩溃。
    """
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


def configured_bindings() -> list[tuple[str, str, str]]:
    """Return [(command, accel, name)] from the user's effective hotkey config."""
    sys.path.insert(0, root_dir + "/src")
    load_environment_d()
    bindings: list[tuple[str, str, str]] = []
    try:
        from ibus_voice_ime.asr import voice_hotkey

        for i, combo in enumerate(voice_hotkey.hotkey_combos()):
            label = name if i == 0 else f"{name}（{accel(combo)}）"
            bindings.append((command, accel(combo), label))
        for i, combo in enumerate(voice_hotkey.raw_hotkey_combos()):
            label = raw_name if i == 0 else f"{raw_name}（{accel(combo)}）"
            bindings.append((raw_command, accel(combo), label))
    except Exception as exc:  # noqa: BLE001 - 配置不可用时回落默认键位
        print(f"读取热键配置失败（{exc}），回落默认 Ctrl+Alt+V / Ctrl+Alt+B。")
    if not bindings:
        bindings = [
            (command, "<Control><Alt>v", name),
            (raw_command, "<Control><Alt>b", raw_name),
        ]
    return bindings


BINDINGS = configured_bindings()
DESIRED_PAIRS = {(cmd, keys) for cmd, keys, _ in BINDINGS}


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

# 一次遍历完成对齐：本工具的槽位里，失效组合（不在期望集合）与重复的
# 期望组合（保留首个）都清出活动列表；非本工具的槽位一律不动。
kept: list[str] = []
pair_slots: dict[tuple[str, str], str] = {}
pruned = 0
for path in paths:
    slot_command = custom_get(path, "command")
    slot_binding = custom_get(path, "binding")
    pair = (slot_command, slot_binding)
    if slot_command in OUR_COMMANDS:
        if pair not in DESIRED_PAIRS or pair in pair_slots:
            pruned += 1
            continue
        pair_slots[pair] = path
    kept.append(path)
if pruned:
    paths = kept
    setv("set", MEDIA_SCHEMA, KEY, repr(paths))
    print(f"已清理 {pruned} 个失效/重复的旧语音快捷键槽位")

for binding_command, binding_keys, binding_name in BINDINGS:
    # 复用优先级：(command, binding) 精确匹配（与槽位当前名字无关，
    # 重排组合后仍能复用原槽位）→ 唯一 name 匹配 → 分配新槽位。
    selected = pair_slots.get((binding_command, binding_keys), "")
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
    setv("set", f"{CUSTOM_SCHEMA}:{selected}", "command", repr(binding_command))
    setv("set", f"{CUSTOM_SCHEMA}:{selected}", "binding", repr(binding_keys))
    print(f"已注册 GNOME 全局语音快捷键：{binding_keys} -> {binding_command}")
PY
