"""Detached, bounded-memory stdout/stderr capture for an owned sidecar.

Only capture processes write this log. A persistent advisory lock serializes
append + retention across engines; producers hold pipes, never log inodes.
Retention is byte-preserving and LF-based (including a final partial line).
As with generic retention, a single unterminated line has no disk-size bound.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import threading
import time

CHUNK = 65536


class Sink:
    def __init__(self, path: Path, keep: int):
        self.path = path
        self.keep = max(0, keep)
        self.lock = path.with_name(path.name + '.capture-lock').open('ab')
        self.next_scan = self.keep * 128
        self.cancelled = lambda: False

    def close(self):
        self.lock.close()

    def append(self, data: bytes):
        # A failed/stuck sink must not leave a live inference process blocked on
        # a full pipe. Lock contention has a deadline, like other sink failures.
        deadline = time.monotonic() + 1
        while True:
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('sidecar log lock timed out')
                time.sleep(.01)
        try:
            with self.path.open('ab', buffering=0) as output:
                remaining = memoryview(data)
                while remaining:
                    written = output.write(remaining)
                    if not written:
                        raise OSError('short sidecar log write')
                    remaining = remaining[written:]
            if self.keep:
                self._retain()
        finally:
            fcntl.flock(self.lock, fcntl.LOCK_UN)

    def _retain(self):
        size = self.path.stat().st_size
        if size <= self.next_scan:
            return
        # Find a suffix boundary without reading a giant line into memory.
        with self.path.open('rb') as source:
            source.seek(-1, os.SEEK_END)
            trailing_lf = source.read(1) == b'\n'
            needed = self.keep + int(trailing_lf)
            count = 0
            boundary = 0
            pos = size
            while pos and count <= 2 * self.keep:
                if self.cancelled():
                    return
                start = max(0, pos - CHUNK)
                source.seek(start)
                block = source.read(pos - start)
                end = len(block)
                while True:
                    index = block.rfind(b'\n', 0, end)
                    if index < 0:
                        break
                    count += 1
                    if count == needed:
                        boundary = start + index + 1
                    end = index
                pos = start
            lines = count + (not trailing_lf)
            if lines <= 2 * self.keep or not boundary:
                # Amortize scanning an arbitrarily long partial line. A new
                # newline requests an immediate scan in the capture loop.
                self.next_scan = max(self.keep * 128, size * 2)
                return
            temp = None
            try:
                with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=self.path.name + '.', delete=False) as output:
                    temp = Path(output.name)
                    source.seek(boundary)
                    while True:
                        if self.cancelled():
                            return
                        block = source.read(CHUNK)
                        if not block:
                            break
                        output.write(block)
                os.replace(temp, self.path)
                temp = None
                self.next_scan = self.keep * 128
            finally:
                if temp is not None:
                    temp.unlink(missing_ok=True)


def run(path: Path, keep: int, command: list[str]) -> int:
    sink = Sink(path, keep)
    child = None
    stopping = False
    watcher = None
    finished = threading.Event()
    sink.cancelled = lambda: stopping

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    def watch_stop():
        # Signal handling cannot depend on the file owner's progress: scanning
        # a huge partial line (or a slow filesystem) may exceed the runtime's
        # supervisor-kill deadline. Only this bounded watchdog handles TERM
        # escalation, independently of retention. It never owns a log fd.
        while not finished.wait(.025):
            if stopping:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=.5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=.5)
                return

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        # All retention (including existing-file cleanup) belongs to this sink.
        sink.append(b'')
        if stopping:
            return 128 + signal.SIGTERM
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        watcher = threading.Thread(target=watch_stop, name='sidecar-stop', daemon=True)
        watcher.start()
        assert child.stdout is not None
        os.set_blocking(child.stdout.fileno(), False)
        deadline = None
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            while True:
                if stopping and deadline is None:
                    deadline = time.monotonic() + 1
                if child.poll() is not None and deadline is None:
                    deadline = time.monotonic() + .3
                if deadline is not None and time.monotonic() >= deadline:
                    if child.poll() is None:
                        child.kill()
                    break  # descendants may retain the pipe; never wait forever
                events = selector.select(.05)
                if not events:
                    continue
                data = os.read(child.stdout.fileno(), CHUNK)
                if not data:
                    break
                if b'\n' in data:
                    sink.next_scan = sink.keep * 128
                sink.append(data)
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                child.kill()
        return child.wait(timeout=.5)
    except Exception as exc:
        # Fail closed: never leave a healthy owned service with a dead reader.
        # stderr is the engine's diagnostic channel, not the failed sink.
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=.5)
        print(f'Qwen sidecar capture failed: {exc}', file=sys.stderr, flush=True)
        try:
            sink.append(('Qwen sidecar capture failed: ' + str(exc) + '\n').encode())
        except Exception:
            pass
        return 1
    finally:
        if child is not None:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=.5)
            if child.stdout is not None:
                child.stdout.close()
        finished.set()
        if watcher is not None:
            watcher.join(timeout=.1)
        sink.close()


if __name__ == '__main__':
    sys.exit(run(Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3:]))
