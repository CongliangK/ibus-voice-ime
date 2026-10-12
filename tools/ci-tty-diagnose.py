#!/usr/bin/env python3
"""CI-only TTY diagnosis for config-wizard pty hang (temporary tool).

Probes which factor silences the wizard under a pty on GitHub runners:
terminal window size (0x0 vs 24x120) and interpreter (setup-python vs system).
"""
from __future__ import annotations

import argparse
import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def set_winsize(fd: int, rows: int, cols: int) -> None:
    try:
        termios.tcsetwinsize(fd, (rows, cols))  # Python >= 3.11
    except AttributeError:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def probe(wizard_python: str | None, winsize: bool, label: str) -> bool:
    tmp = tempfile.mkdtemp(prefix="tty-diag-")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VOICE_IME_", "XDG_", "STUB_"))}
    env.update(HOME=tmp, VOICE_IME_CONFIG=tmp + "/config.json",
               VOICE_IME_WIZARD_RESULT=tmp + "/result.json", TERM="xterm")
    if wizard_python:
        env["VOICE_IME_WIZARD_PYTHON"] = wizard_python
    master, slave = pty.openpty()
    if winsize:
        set_winsize(slave, 24, 120)
    process = subprocess.Popen(["bash", str(ROOT / "scripts/config-wizard.sh"),
                                "--reconfigure", "--voice-plan", "keyboard-only"],
                               stdin=slave, stdout=subprocess.PIPE, stderr=slave, env=env)
    start = time.monotonic()
    buf = b""
    got = False
    while time.monotonic() - start < 12:
        r, _, _ = select.select([master], [], [], 0.5)
        if r:
            chunk = os.read(master, 4096)
            if not chunk:
                print(f"[{label}] EOF after {time.monotonic()-start:.1f}s", flush=True)
                break
            buf += chunk
            if b"3)" in buf:
                print(f"[{label}] PROMPT after {time.monotonic()-start:.1f}s ({len(buf)}B)", flush=True)
                got = True
                break
    if not got:
        print(f"[{label}] NO PROMPT in 12s, captured {len(buf)}B: {buf[:200]!r}", flush=True)
        subprocess.run(["ps", "-ef", "--forest"], cwd=tmp)
        for pid in [process.pid]:
            try:
                print(Path(f"/proc/{pid}/wchan").read_text().strip(), flush=True)
            except OSError:
                pass
    process.kill()
    return got


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", default=None, help="VOICE_IME_WIZARD_PYTHON override")
    parser.add_argument("--no-winsize", action="store_true")
    parser.add_argument("--label", default="probe")
    args = parser.parse_args()
    ok = probe(args.python, not args.no_winsize, args.label)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
