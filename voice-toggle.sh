#!/usr/bin/env bash
set -euo pipefail

python3 - <<'PY'
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ENGINE_NAME = "voice-custom"
COMMAND = b"toggle\n"
LOG_PATH = Path.home() / ".local/share/ibus-voice-ime/ipc-client.log"


def log(message: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] voice-toggle: {message}\n")
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
    # GNOME custom shortcuts are launched with a reduced environment on some
    # systems.  In that case XDG_RUNTIME_DIR may be missing even though the
    # standard per-user runtime directory exists.
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


def send_once() -> tuple[bool, str]:
    errors: list[str] = []
    for path in socket_candidates():
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(1.5)
                sock.connect(str(path))
                sock.sendall(COMMAND)
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


def activate_engine() -> None:
    try:
        subprocess.run(["ibus", "engine", ENGINE_NAME], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1.5)
        time.sleep(0.25)
    except Exception as exc:
        log(f"activate engine failed: {exc}")


ok, message = send_once()
if not ok:
    # If the engine was not running or not focused yet, ask IBus to activate it
    # and retry once.  This also recovers after an ibus restart leaves a stale
    # socket path behind.
    log(f"first send failed: {message}; activating engine and retrying")
    activate_engine()
    ok, message = send_once()

if not ok:
    log(f"failed: {message}")
    print(message, file=sys.stderr)
    sys.exit(1 if message == "NO_FOCUS" else 2)
PY
