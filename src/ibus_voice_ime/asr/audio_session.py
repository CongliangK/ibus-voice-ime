# -*- coding: utf-8 -*-
"""Toggle-style audio recording session for voice input.

Records 16 kHz / mono / signed 16-bit PCM with arecord, writes a WAV file, and
keeps a lightweight RMS level for the status overlay.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from array import array
from pathlib import Path


class AudioSessionError(RuntimeError):
    pass


def _pcm_rms_s16le(data: bytes) -> float:
    if not data:
        return 0.0
    samples = array("h")
    samples.frombytes(data[: len(data) - (len(data) % 2)])
    if not samples:
        return 0.0
    total = sum(int(x) * int(x) for x in samples)
    return (total / len(samples)) ** 0.5


class AudioSession:
    def __init__(self, *, max_seconds: int = 120, rms_full_scale: float = 3000.0):
        self.max_seconds = max(1, int(max_seconds))
        self.rms_full_scale = max(1.0, float(rms_full_scale))
        self._tmpdir_obj: tempfile.TemporaryDirectory[str] | None = None
        self.wav_path = ""
        self._proc: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._done_event = threading.Event()
        self._lock = threading.Lock()
        self._started_at = 0.0
        self._level = 0.0
        self._recording = False
        self._canceled = False
        self._error: str | None = None

    def start(self) -> None:
        if self._recording:
            return
        self._tmpdir_obj = tempfile.TemporaryDirectory(prefix="ibus-voice-ime-toggle-")
        self.wav_path = str(Path(self._tmpdir_obj.name) / "record.wav")
        cmd = ["arecord", "-q", "-t", "raw", "-f", "S16_LE", "-r", "16000", "-c", "1"]
        # VOICE_IME_ARECORD_DEVICE：直采指定设备（如 plughw:M2,0），不受系统默认源
        # 漂移影响（默认源被蓝牙抢占等，见 scripts/bt-play-restore.py）。
        device = os.environ.get("VOICE_IME_ARECORD_DEVICE", "").strip()
        if device:
            cmd[1:1] = ["-D", device]
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError as exc:
            self.cleanup()
            raise AudioSessionError("找不到 arecord，无法录音") from exc
        except Exception as exc:
            self.cleanup()
            raise AudioSessionError(f"启动录音失败：{exc}") from exc

        self._started_at = time.monotonic()
        self._recording = True
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def _reader(self) -> None:
        chunk_ms = 50
        chunk_bytes = int(16000 * 2 * chunk_ms / 1000)
        try:
            assert self._proc is not None and self._proc.stdout is not None
            with wave.open(self.wav_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                while not self._stop_event.is_set():
                    data = self._proc.stdout.read(chunk_bytes)
                    if not data:
                        break
                    wf.writeframes(data)
                    rms = _pcm_rms_s16le(data)
                    with self._lock:
                        # Smooth the level so the UI is readable.
                        self._level = min(1.0, max(0.0, (self._level * 0.65) + ((rms / self.rms_full_scale) * 0.35)))
                    if self.elapsed() >= self.max_seconds:
                        break
        except Exception as exc:
            self._error = str(exc)
        finally:
            self._recording = False
            if self._proc is not None and self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=1)
            self._done_event.set()

    def stop(self) -> str:
        self._stop_event.set()
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
        if self._thread is not None:
            self._done_event.wait(timeout=5)
        if self._error:
            raise AudioSessionError("录音失败：" + self._error)
        if self._canceled:
            raise AudioSessionError("录音已取消")
        if not self.wav_path or not Path(self.wav_path).exists() or Path(self.wav_path).stat().st_size <= 44:
            detail = ""
            try:
                if self._proc is not None and self._proc.stderr is not None:
                    detail = self._proc.stderr.read().decode("utf-8", errors="replace").strip()
            except Exception:
                detail = ""
            raise AudioSessionError("没有录到有效音频" + (f"：{detail}" if detail else ""))
        return self.wav_path

    def cancel(self) -> None:
        self._canceled = True
        self._stop_event.set()
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
        if self._thread is not None:
            self._done_event.wait(timeout=2)
        self.cleanup()

    def cleanup(self) -> None:
        if self._tmpdir_obj is not None:
            try:
                self._tmpdir_obj.cleanup()
            except Exception:
                # Last-resort cleanup if TemporaryDirectory was partially broken.
                try:
                    shutil.rmtree(Path(self.wav_path).parent, ignore_errors=True)
                except Exception:
                    pass
            self._tmpdir_obj = None

    def is_recording(self) -> bool:
        return self._recording

    def elapsed(self) -> float:
        if not self._started_at:
            return 0.0
        return max(0.0, time.monotonic() - self._started_at)

    def level(self) -> float:
        with self._lock:
            return self._level

    def error(self) -> str | None:
        return self._error
