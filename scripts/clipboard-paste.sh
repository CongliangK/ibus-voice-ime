#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export VOICE_IME_ROOT_DIR="$ROOT_DIR"

python3 - <<'PY'
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT_DIR = Path(os.environ["VOICE_IME_ROOT_DIR"])
sys.path.insert(0, str(ROOT_DIR / "src"))

from ibus_voice_ime import clipboard_paste  # noqa: E402

ENGINE_NAME = "voice-custom"
LOG_PATH = Path.home() / ".local/share/ibus-voice-ime/ipc-client.log"


def log(message: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] clipboard-paste: {message}\n")
    except Exception:
        pass


def socket_candidates() -> list[Path]:
    paths: list[Path] = []
    explicit = os.environ.get("VOICE_IME_IPC_SOCKET", "").strip()
    if explicit:
        paths.append(Path(explicit).expanduser())
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if runtime_dir:
        paths.append(Path(runtime_dir) / "ibus-voice-ime" / "voice.sock")
    paths.append(Path(f"/run/user/{os.getuid()}") / "ibus-voice-ime" / "voice.sock")
    paths.append(Path.home() / ".local/share/ibus-voice-ime/voice.sock")

    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def send_once(command: bytes) -> tuple[bool, str]:
    errors: list[str] = []
    for path in socket_candidates():
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(1.5)
                sock.connect(str(path))
                sock.sendall(command)
                response = sock.recv(128).decode("utf-8", "ignore").strip()
        except FileNotFoundError:
            errors.append(f"missing {path}")
            continue
        except (ConnectionRefusedError, socket.timeout, OSError) as exc:
            errors.append(f"{path}: {exc}")
            continue
        if not response or response == "OK":
            return True, "OK"
        return False, response
    return False, "; ".join(errors) or "no IPC socket candidates"


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def notify(message: str) -> None:
    # 诊断开关：GNOME 通知横幅疑似会在弹出瞬间引发输入上下文焦点抖动
    # （FocusOut/In 级联杀掉引擎实例），VOICE_IME_CLIPBOARD_NOTIFY=0 关闭验证。
    if not env_bool("VOICE_IME_CLIPBOARD_NOTIFY", True):
        return
    try:
        subprocess.Popen(
            ["notify-send", "-t", "1200", "自定义语音输入法", message],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def activate_engine() -> None:
    try:
        subprocess.run(["ibus", "engine", ENGINE_NAME], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1.5)
        time.sleep(max(0.0, env_float("VOICE_IME_CLIPBOARD_ACTIVATE_DELAY_SECONDS", 0.25)))
    except Exception as exc:
        log(f"activate engine failed: {exc}")


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}


runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "ibus-voice-ime"


try:
    content, source, details = clipboard_paste.read_clipboard_text_with_source()
except Exception as exc:
    log(f"clipboard read exception: {exc}")
    notify("读取剪贴板失败")
    print(f"clipboard read failed: {exc}", file=sys.stderr)
    sys.exit(2)

if not content:
    log(f"clipboard empty: {details}")
    notify("剪贴板没有文本")
    print(f"clipboard empty: {details}", file=sys.stderr)
    sys.exit(1)

runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "ibus-voice-ime"
runtime_dir.mkdir(parents=True, exist_ok=True)
text_path = runtime_dir / "clipboard-paste.txt"
text_path.write_text(content, encoding="utf-8")
os.chmod(text_path, 0o600)
# CP1 检查点：剪贴板→暂存文件。指纹与引擎侧 CP2/CP3a 对照可确认内容在
# 助手→引擎的传递中完好无损（诊断入口：scripts/diagnose-paste.sh）。
log(f"CP1 通过：剪贴板已写暂存文件 {clipboard_paste.content_fingerprint(content)} source={source}, details={details}, path={text_path}")

notify("📋 正在粘贴……")
# Return from the GNOME shortcut helper as quickly as possible.  The engine will
# do the Ctrl+Alt+V-style delayed commit after this process exits and focus has
# returned to the page.

command = f"paste-file {text_path}\n".encode("utf-8")
ok, message = send_once(command)
if not ok:
    log(f"paste-file send failed: {message}; activating engine and retrying")
    activate_engine()
    ok, message = send_once(command)

if not ok:
    log(f"failed: {message}")
    print(message, file=sys.stderr)
    sys.exit(1 if message == "NO_FOCUS" else 2)
PY
