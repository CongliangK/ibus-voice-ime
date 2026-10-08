# -*- coding: utf-8 -*-
"""Hermetic regression tests for qwen_asr_runtime hardening (no torch, no GPU).

Locks in three fixes from the 2026-10 robustness pass:

  1. START_TIMEOUT expiry terminates the child instead of leaking it (and a
     second ensure waits for readiness instead of returning a stale URL).
  2. Repeated spawn failures trip a cooldown (circuit breaker) so a machine
     with a permanently broken sidecar does not pay python+torch import cost
     on every hotkey press.
  3. _humanize_transcribe_error branches by root cause (OOM / CPU-only torch /
     bf16-unsupported GPU / truncated safetensors) instead of one generic hint.
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ibus_voice_ime.asr import qwen_asr_runtime  # noqa: E402


class _SpawnRecorder:
    """Popen replacement that records created processes for later asserts."""

    def __init__(self) -> None:
        self.spawned: list = []

    def __call__(self, *args, **kwargs):
        import subprocess as real_subprocess

        proc = real_subprocess.Popen(*args, **kwargs)
        self.spawned.append(proc)
        return proc


def _patch_spawn(recorder: _SpawnRecorder):
    # 只替换 qwen_asr_runtime 模块里的 subprocess 名字绑定；patch 全局
    # subprocess.Popen 会让 recorder 内部的真 Popen 调用再次命中自己（无限递归）。
    import subprocess as real_subprocess

    fake = types.SimpleNamespace(
        Popen=recorder,
        STDOUT=real_subprocess.STDOUT,
        DEVNULL=real_subprocess.DEVNULL,
        PIPE=real_subprocess.PIPE,
    )
    return mock.patch.object(qwen_asr_runtime, "subprocess", fake)


class QwenRuntimeHardeningTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="qwen-runtime-test-")
        self.tmp = Path(self._tmp.name)
        # Stub "python": sleeps forever, pretending to be a slow-loading sidecar.
        self.sleeper = self.tmp / "fake-python"
        self.sleeper.write_text("#!/usr/bin/env bash\nexec sleep 300\n")
        self.sleeper.chmod(0o755)
        self.model_dir = self.tmp / "Qwen3-ASR-1.7B"
        self.model_dir.mkdir()
        self._env = {
            "VOICE_IME_QWEN_ASR_PYTHON": str(self.sleeper),
            "VOICE_IME_QWEN_ASR_MODEL_PATH": str(self.model_dir),
            "VOICE_IME_QWEN_ASR_PORT": "18999",
            "VOICE_IME_QWEN_ASR_START_TIMEOUT": "1",
            "VOICE_IME_QWEN_ASR_FAIL_COOLDOWN": "60",
            "VOICE_IME_LOG_DIR": str(self.tmp / "logs"),
        }
        self._patchers = []
        for key, value in self._env.items():
            patcher = mock.patch.dict(os.environ, {key: value})
            patcher.start()
            self._patchers.append(patcher)
        # None of these tests should ever reach the network.
        self._patchers.append(mock.patch.object(qwen_asr_runtime, "is_ready", return_value=False))
        self._patchers.append(mock.patch.object(qwen_asr_runtime, "_server_matches", return_value=False))

    def tearDown(self) -> None:
        for patcher in self._patchers:
            patcher.stop()
        qwen_asr_runtime._PROCESS = None
        qwen_asr_runtime._PROCESS_KEY = None
        qwen_asr_runtime._clear_failure()
        self._tmp.cleanup()

    def _reset_module_state(self) -> None:
        qwen_asr_runtime._PROCESS = None
        qwen_asr_runtime._PROCESS_KEY = None
        qwen_asr_runtime._clear_failure()

    def test_start_timeout_kills_child_and_resets_state(self) -> None:
        self._reset_module_state()
        recorder = _SpawnRecorder()
        with _patch_spawn(recorder):
            with self.assertRaises(RuntimeError) as ctx:
                qwen_asr_runtime.ensure_server()
        self.assertIn("启动超时", str(ctx.exception))
        # The child must be gone: no zombie holding VRAM/port/log fds.
        self.assertIsNone(qwen_asr_runtime._PROCESS)
        self.assertIsNone(qwen_asr_runtime._PROCESS_KEY)
        self.assertEqual(len(recorder.spawned), 1)
        proc = recorder.spawned[0]
        proc.poll()
        self.assertIsNotNone(proc.returncode, "timed-out sidecar must be terminated")

    def test_failure_cooldown_blocks_immediate_respawn(self) -> None:
        self._reset_module_state()
        recorder = _SpawnRecorder()
        with _patch_spawn(recorder):
            with self.assertRaises(RuntimeError):
                qwen_asr_runtime.ensure_server()
        first_spawns = len(recorder.spawned)
        # Second press within the cooldown window: cached error, no new spawn.
        with _patch_spawn(recorder):
            with self.assertRaises(RuntimeError) as ctx:
                qwen_asr_runtime.ensure_server()
        self.assertIn("熔断", str(ctx.exception))
        self.assertEqual(len(recorder.spawned), first_spawns)
        # Success path clears the breaker.
        qwen_asr_runtime._clear_failure()
        self.assertEqual(qwen_asr_runtime._FAIL_MSG, "")

    def test_cooldown_disabled_by_env_zero(self) -> None:
        self._reset_module_state()
        with mock.patch.dict(os.environ, {"VOICE_IME_QWEN_ASR_FAIL_COOLDOWN": "0"}):
            recorder = _SpawnRecorder()
            with _patch_spawn(recorder):
                with self.assertRaises(RuntimeError):
                    qwen_asr_runtime.ensure_server()
                self.assertEqual(len(recorder.spawned), 1)
                with self.assertRaises(RuntimeError) as ctx:
                    qwen_asr_runtime.ensure_server()
            self.assertNotIn("熔断", str(ctx.exception))
            self.assertEqual(len(recorder.spawned), 2)

    def test_config_change_restarts_our_live_sidecar(self) -> None:
        # A live child with a stale key (model switched) must be terminated
        # before spawning the new one, instead of racing for the port.
        import subprocess as real_subprocess

        self._reset_module_state()
        old = real_subprocess.Popen(
            [str(self.sleeper)], stdout=real_subprocess.DEVNULL, stderr=real_subprocess.DEVNULL
        )
        qwen_asr_runtime._PROCESS = old
        qwen_asr_runtime._PROCESS_KEY = ("127.0.0.1", 18999, "/old/model")
        recorder = _SpawnRecorder()
        with _patch_spawn(recorder):
            with self.assertRaises(RuntimeError):  # still times out (stub never listens)
                qwen_asr_runtime.ensure_server()
        self.assertIsNotNone(old.returncode, "old sidecar with stale config must be terminated")

    def test_humanize_oom(self) -> None:
        msg = qwen_asr_runtime._humanize_transcribe_error(
            500, "RuntimeError: CUDA out of memory. Tried to allocate 2.00 GiB"
        )
        self.assertIn("显存不足", msg)
        self.assertIn("0.6b", msg)

    def test_humanize_cpu_only_torch(self) -> None:
        msg = qwen_asr_runtime._humanize_transcribe_error(
            500, "AssertionError: Torch not compiled with CUDA enabled"
        )
        self.assertIn("CPU 版 PyTorch", msg)

    def test_humanize_bfloat16_gpu(self) -> None:
        msg = qwen_asr_runtime._humanize_transcribe_error(
            500, 'RuntimeError: "layer_norm_kernel" not implemented for \'BFloat16\''
        )
        self.assertIn("bfloat16", msg)
        self.assertIn("float16", msg)

    def test_humanize_truncated_model(self) -> None:
        msg = qwen_asr_runtime._humanize_transcribe_error(
            500, "SafetensorError: Error while deserializing header: HeaderTooLarge"
        )
        self.assertIn("不完整", msg)

    def test_humanize_non_500_passthrough(self) -> None:
        msg = qwen_asr_runtime._humanize_transcribe_error(502, "bad gateway")
        self.assertNotIn("常见原因", msg)


if __name__ == "__main__":
    unittest.main()
