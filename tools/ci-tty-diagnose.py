#!/usr/bin/env python3
"""CI-only TTY diagnosis for config-wizard pty hang (temporary tool).

Runs scripts/config-wizard.sh under a pty exactly like tests/test_config_wizard.py
tty_wizard() does, but with bash -x tracing to a side file, generous timeouts,
and dumps where the process tree is blocked if the first prompt never appears.
"""
from __future__ import annotations

import os
import pty
import select
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def dump_blocked(procs: list[int]) -> None:
    for pid in procs:
        try:
            wchan = Path(f"/proc/{pid}/wchan").read_text().strip()
            stack = Path(f"/proc/{pid}/stack").read_text().strip()
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            print(f"[blocked] pid={pid} wchan={wchan} cmd={cmdline}\n{stack}", flush=True)
        except OSError as exc:
            print(f"[blocked] pid={pid} <{exc}>", flush=True)


def children(pid: int) -> list[int]:
    out = subprocess.run(["ps", "-o", "pid=", "--ppid", str(pid)], capture_output=True, text=True)
    return [int(x) for x in out.stdout.split()]


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="tty-diag-")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VOICE_IME_", "XDG_", "STUB_"))}
    env.update(HOME=tmp, VOICE_IME_CONFIG=tmp + "/config.json",
               VOICE_IME_WIZARD_RESULT=tmp + "/result.json", TERM="xterm")
    trace_path = tmp + "/xtrace.log"
    master, slave = pty.openpty()
    termios.tcsetwinsize(slave, (24, 120))
    trace = open(trace_path, "wb")
    process = subprocess.Popen(["bash", "-x", str(ROOT / "scripts/config-wizard.sh"),
                                "--reconfigure", "--voice-plan", "keyboard-only"],
                               stdin=slave, stdout=subprocess.PIPE, stderr=slave, env=env)
    start = time.monotonic()
    buf = b""
    got_prompt = False
    while time.monotonic() - start < 30:
        r, _, _ = select.select([master], [], [], 1.0)
        if r:
            chunk = os.read(master, 4096)
            if not chunk:
                print(f"[eof] after {time.monotonic()-start:.1f}s", flush=True)
                break
            buf += chunk
            if b"3)" in buf:
                print(f"[ok] first prompt after {time.monotonic()-start:.1f}s, {len(buf)} bytes", flush=True)
                got_prompt = True
                break
        if 10 < time.monotonic() - start < 11:
            print("[diag] 10s without prompt — dumping process tree", flush=True)
            dump_blocked([process.pid] + children(process.pid))
            for pid in children(process.pid):
                dump_blocked(children(pid))
    print(f"[bytes={len(buf)}] {buf[:400].decode(errors='replace')!r}", flush=True)
    process.kill()
    return 0 if got_prompt else 1


if __name__ == "__main__":
    raise SystemExit(main())
