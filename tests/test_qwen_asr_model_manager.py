# -*- coding: utf-8 -*-
"""Hermetic tests for the Qwen3-ASR sidecar multi-model manager.

These tests do NOT load torch, CUDA, or the real Qwen3-ASR model.  They inject
lightweight fakes that reproduce the surface area the ``ModelManager`` touches
(``backend``, ``model.to()``, ``model.device``, ``forced_aligner``), and patch
out the disk/torch paths (``_load_to_ram_locked``, ``_probe_free_vram_mib``).
This keeps the validation gate stdlib-only, matching the convention in
``test_sidecar_security.py`` and ``test_volc_bigmodel_asr.py``.
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ibus_voice_ime.asr import qwen_asr_server  # noqa: E402
from ibus_voice_ime.asr import qwen_asr_runtime  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes reproducing the Qwen3ASRModel surface the manager touches.
# --------------------------------------------------------------------------- #
class _FakeInnerModel:
    """Stand-in for the inner transformers model (has .to() and .device)."""

    def __init__(self, device: str = "cuda:0") -> None:
        self._device = device
        self.to_calls: list[str] = []

    @property
    def device(self) -> str:
        return self._device

    def to(self, target: str) -> None:
        self.to_calls.append(target)
        self._device = target


class _FakeForcedAligner:
    def __init__(self, device: str = "cuda:0") -> None:
        self.model = _FakeInnerModel(device)
        self.device = device


class _FakeWrapper:
    """Stand-in for Qwen3ASRModel."""

    def __init__(self, device: str = "cuda:0", backend: str = "transformers") -> None:
        self.backend = backend
        self.model = _FakeInnerModel(device)
        self.forced_aligner = _FakeForcedAligner(device)
        self.device = device

    def transcribe(self, **_kwargs):
        return [{"text": "ok", "language": "Chinese"}]


def _make_manager(
    primary: str = "/models/Qwen3-ASR-1.7B",
    secondary: str | None = "/models/Qwen3-ASR-0.6B",
    vram_min_17b_mib: int = 5000,
    vram_min_06b_mib: int = 2000,
    idle_timeout: float = 0.0,
    check_interval: float = 5.0,
) -> "qwen_asr_server.ModelManager":
    return qwen_asr_server.ModelManager(
        primary_path=primary,
        secondary_path=secondary,
        idle_timeout=idle_timeout,
        check_interval=check_interval,
        vram_min_17b_mib=vram_min_17b_mib,
        vram_min_06b_mib=vram_min_06b_mib,
    )


# --------------------------------------------------------------------------- #
# Pure decision logic (no torch, no I/O).
# --------------------------------------------------------------------------- #
class PickTargetTest(unittest.TestCase):
    def test_prefers_17b_when_vram_sufficient(self) -> None:
        self.assertEqual(_make_manager()._pick_target(8000), "1.7b")

    def test_falls_back_to_06b_when_vram_low(self) -> None:
        self.assertEqual(_make_manager()._pick_target(3000), "0.6b")

    def test_probe_failure_is_conservative(self) -> None:
        # When we cannot read free VRAM, do not risk OOM on 1.7B; use 0.6B.
        self.assertEqual(_make_manager()._pick_target(None), "0.6b")

    def test_single_model_gate_on_vram(self) -> None:
        # 单模型配置也过 VRAM 门：探测成功且空闲不足时报“显存不足”，
        # 不再让 2-4GB 卡每次听写都付完整权重加载后 OOM。
        m = _make_manager(secondary=None)
        self.assertEqual(m._pick_target(6000), "1.7b")
        with self.assertRaises(RuntimeError) as ctx:
            m._pick_target(100)
        self.assertIn("显存不足", str(ctx.exception))

    def test_single_model_probe_failure_still_tries(self) -> None:
        # 探测失败（None）保持旧“照常尝试加载”语义，让真实加载错误浮出。
        m = _make_manager(secondary=None)
        self.assertEqual(m._pick_target(None), "1.7b")

    def test_single_06b_model(self) -> None:
        m = _make_manager(primary="/models/Qwen3-ASR-0.6B", secondary=None)
        self.assertEqual(m._pick_target(3000), "0.6b")
        self.assertEqual(m._pick_target(None), "0.6b")
        with self.assertRaises(RuntimeError):
            m._pick_target(100)


# --------------------------------------------------------------------------- #
# State-machine: _ensure_active_locked transitions.
# --------------------------------------------------------------------------- #
class EnsureActiveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[str] = []

    def _patched_manager(self, free_mib: int | None) -> "qwen_asr_server.ModelManager":
        m = _make_manager()

        def fake_probe(_self: Any) -> Any:
            self.calls.append(f"probe={free_mib}")
            return free_mib

        def fake_load(_self: Any, alias: str) -> Any:
            self.calls.append(f"load:{alias}")
            w = _FakeWrapper()
            _self._models[alias] = w
            return w

        def fake_to_gpu(_self: Any, alias: str) -> None:
            # Mirror the real _to_gpu_locked: load from disk if not resident,
            # then mark active.  This lets residency/upgrade assertions observe
            # both the load and the activation.
            if alias not in _self._models:
                self.calls.append(f"load:{alias}")
                _self._models[alias] = _FakeWrapper()
            self.calls.append(f"to_gpu:{alias}")
            _self._active = alias

        def fake_to_cpu(_self: Any, alias: str) -> None:
            self.calls.append(f"to_cpu:{alias}")
            _self._active = None

        for name, fn in [
            ("_probe_free_vram_mib", fake_probe),
            ("_load_to_ram_locked", fake_load),
            ("_to_gpu_locked", fake_to_gpu),
            ("_to_cpu_locked", fake_to_cpu),
        ]:
            p = mock.patch.object(qwen_asr_server.ModelManager, name, fn)
            p.start()
            self.addCleanup(p.stop)
        return m

    def test_first_request_low_vram_loads_06b(self) -> None:
        m = self._patched_manager(free_mib=3000)
        m._ensure_active_locked()
        self.assertIn("to_gpu:0.6b", self.calls)
        self.assertNotIn("to_gpu:1.7b", self.calls)
        self.assertEqual(m.active_alias, "0.6b")
        self.assertNotIn("to_cpu", " ".join(self.calls))  # nothing to evict

    def test_first_request_high_vram_loads_17b(self) -> None:
        m = self._patched_manager(free_mib=8000)
        m._ensure_active_locked()
        self.assertIn("to_gpu:1.7b", self.calls)
        self.assertNotIn("to_gpu:0.6b", self.calls)
        self.assertEqual(m.active_alias, "1.7b")

    def test_17b_active_skips_probe(self) -> None:
        # 1.7B already on GPU -> must NOT probe; serve directly.
        m = self._patched_manager(free_mib=3000)
        m._set_state_for_test("1.7b", gpu_device="cuda:0")
        m._ensure_active_locked()
        self.assertEqual(self.calls.count("probe=3000"), 0)
        self.assertEqual(m.active_alias, "1.7b")

    def test_06b_active_upgrades_when_vram_frees_up(self) -> None:
        m = self._patched_manager(free_mib=8000)
        m._set_state_for_test("0.6b", gpu_device="cuda:0")
        m._set_model_for_test("0.6b", _FakeWrapper())
        m._ensure_active_locked()
        # probe -> pick 1.7b -> evict 0.6b to cpu -> load + to_gpu 1.7b
        self.assertIn("to_cpu:0.6b", self.calls)
        self.assertIn("load:1.7b", self.calls)
        self.assertIn("to_gpu:1.7b", self.calls)
        self.assertEqual(m.active_alias, "1.7b")

    def test_06b_active_stays_when_vram_still_low(self) -> None:
        m = self._patched_manager(free_mib=3000)
        m._set_state_for_test("0.6b", gpu_device="cuda:0")
        m._set_model_for_test("0.6b", _FakeWrapper())
        m._ensure_active_locked()
        # pick still returns 0.6b -> active == want -> no transitions
        self.assertNotIn("to_cpu:0.6b", self.calls)
        self.assertNotIn("to_gpu", " ".join(self.calls))
        self.assertEqual(m.active_alias, "0.6b")


# --------------------------------------------------------------------------- #
# Residency: loaded models stay in _models after eviction.
# --------------------------------------------------------------------------- #
class ResidencyTest(unittest.TestCase):
    def test_evicted_model_stays_in_dict(self) -> None:
        m = _make_manager()
        w = _FakeWrapper()
        m._set_model_for_test("1.7b", w)
        m._set_state_for_test("1.7b", gpu_device="cuda:0")
        m._to_cpu_locked("1.7b")
        self.assertIsNone(m.active_alias)
        self.assertIn("1.7b", m._models)
        self.assertIs(m._models["1.7b"], w)
        self.assertEqual(w.model.to_calls, ["cpu"])

    def test_reload_from_ram_does_not_reload(self) -> None:
        m = _make_manager()
        w = _FakeWrapper(device="cpu")
        m._set_model_for_test("1.7b", w)
        m._set_state_for_test(None, gpu_device="cuda:0")
        with mock.patch.object(m, "_load_to_ram_locked") as load:
            m._to_gpu_locked("1.7b")
            load.assert_not_called()  # already resident, just .to(gpu)
        self.assertEqual(m.active_alias, "1.7b")
        self.assertEqual(w.model.to_calls, ["cuda:0"])

    def test_device_attr_resynced_after_migration(self) -> None:
        m = _make_manager()
        w = _FakeWrapper(device="cuda:0")
        m._set_model_for_test("1.7b", w)
        m._set_state_for_test("1.7b", gpu_device="cuda:0")
        self.assertEqual(w.device, "cuda:0")
        m._to_cpu_locked("1.7b")
        # resync updated the cached .device to match inner model's new device.
        self.assertEqual(w.device, "cpu")
        self.assertEqual(w.forced_aligner.device, "cpu")


# --------------------------------------------------------------------------- #
# Watchdog (uses real thread with short timeout).
# --------------------------------------------------------------------------- #
class WatchdogTest(unittest.TestCase):
    def test_idle_timeout_zero_disables_watchdog(self) -> None:
        m = _make_manager(idle_timeout=0.0)
        m.start()
        self.assertIsNone(m._thread)
        m.stop()

    def test_watchdog_evicts_active_model_when_idle(self) -> None:
        evicted: list[str] = []
        # check_interval is clamped to >=0.5s in the manager; idle timeout tiny
        # so the first tick (at ~0.5s) satisfies the idle condition.
        m = _make_manager(idle_timeout=0.01, check_interval=0.5)
        m._set_model_for_test("0.6b", _FakeWrapper())
        m._set_state_for_test("0.6b", gpu_device="cuda:0")
        # Force "last activity" into the past so the first tick evicts.
        m._last_activity = time.monotonic() - 10
        with mock.patch.object(m, "_to_cpu_locked", side_effect=lambda a: evicted.append(a)):
            m.start()
            time.sleep(1.2)  # wait past the first tick
            m.stop()
        self.assertIn("0.6b", evicted)


# --------------------------------------------------------------------------- #
# Runtime: downgrade-aware server matching.
# --------------------------------------------------------------------------- #
class RuntimeDowngradeMatchTest(unittest.TestCase):
    def test_is_downgrade_of_17b_desired_06b_running(self) -> None:
        self.assertTrue(
            qwen_asr_runtime._is_downgrade_of(
                "/p/Qwen3-ASR-0.6B", "/p/Qwen3-ASR-1.7B"
            )
        )

    def test_is_downgrade_of_reverse_is_false(self) -> None:
        self.assertFalse(
            qwen_asr_runtime._is_downgrade_of(
                "/p/Qwen3-ASR-1.7B", "/p/Qwen3-ASR-0.6B"
            )
        )

    def test_is_downgrade_of_equal_is_false(self) -> None:
        self.assertFalse(
            qwen_asr_runtime._is_downgrade_of(
                "/p/Qwen3-ASR-1.7B", "/p/Qwen3-ASR-1.7B"
            )
        )

    def test_is_downgrade_of_empty_is_false(self) -> None:
        self.assertFalse(qwen_asr_runtime._is_downgrade_of("", "/p/Qwen3-ASR-1.7B"))
        self.assertFalse(qwen_asr_runtime._is_downgrade_of("/p/Qwen3-ASR-0.6B", ""))

    def test_server_matches_accepts_legitimate_downgrade(self) -> None:
        with mock.patch.object(qwen_asr_runtime, "_health", return_value={"model": "/p/Qwen3-ASR-0.6B"}):
            self.assertTrue(qwen_asr_runtime._server_matches("/p/Qwen3-ASR-1.7B"))

    def test_server_matches_rejects_unrelated_model(self) -> None:
        with mock.patch.object(qwen_asr_runtime, "_health", return_value={"model": "/p/MiMo-V2.5-ASR"}):
            self.assertFalse(qwen_asr_runtime._server_matches("/p/Qwen3-ASR-1.7B"))


# --------------------------------------------------------------------------- #
# Server: _resolve_alias / _companion_path helpers.
# --------------------------------------------------------------------------- #
class AliasResolutionTest(unittest.TestCase):
    def test_resolve_alias(self) -> None:
        self.assertEqual(qwen_asr_server._resolve_alias("/p/Qwen3-ASR-1.7B"), "1.7b")
        self.assertEqual(qwen_asr_server._resolve_alias("/p/Qwen3-ASR-0.6B"), "0.6b")
        self.assertEqual(qwen_asr_server._resolve_alias("Qwen/Qwen3-ASR-0_6B"), "0.6b")

    def test_companion_path_missing_returns_none(self) -> None:
        self.assertIsNone(qwen_asr_server._companion_path("/nonexistent/Qwen3-ASR-1.7B"))

    def test_companion_path_finds_sibling(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as base:
            big = Path(base) / "Qwen3-ASR-1.7B"
            small = Path(base) / "Qwen3-ASR-0.6B"
            big.mkdir()
            small.mkdir()
            (big / "config.json").write_text("{}")
            (small / "config.json").write_text("{}")
            self.assertEqual(qwen_asr_server._companion_path(str(big)), str(small))
            self.assertEqual(qwen_asr_server._companion_path(str(small)), str(big))


# --------------------------------------------------------------------------- #
# Manager construction / active_id accessor.
# --------------------------------------------------------------------------- #
class ManagerBasicTest(unittest.TestCase):
    def test_requires_at_least_one_path(self) -> None:
        with self.assertRaises(ValueError):
            qwen_asr_server.ModelManager(
                primary_path="",
                secondary_path=None,
                idle_timeout=0,
                check_interval=5,
            )

    def test_active_id_falls_back_when_nothing_loaded(self) -> None:
        qwen_asr_server._MODEL_ID = "/primary/Qwen3-ASR-1.7B"
        try:
            self.assertEqual(_make_manager().active_id, "/primary/Qwen3-ASR-1.7B")
        finally:
            qwen_asr_server._MODEL_ID = ""

    def test_active_id_reports_active_path(self) -> None:
        m = _make_manager()
        m._set_state_for_test("0.6b")
        self.assertEqual(m.active_id, "/models/Qwen3-ASR-0.6B")


if __name__ == "__main__":
    unittest.main()
