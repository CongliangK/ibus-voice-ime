# -*- coding: utf-8 -*-
"""Regression tests for process-level voice-recording ownership (2026-10-09 incident).

IBus creates one engine instance per input context and voice state lives on the
instance.  When the stop hotkey landed on a freshly created idle instance (focus
churn caused by the overlay stealing keyboard focus, amplified by __init__
hijacking _FOCUSED_ENGINE), "stop" was misread as "start": popups stacked,
arecord sessions leaked, dictation never completed.

These tests lock in the fix: the hotkey is forwarded to the owning instance
(_VOICE_OWNER), the constructor no longer hijacks focus routing, and the overlay
does not accept focus by default.

self.engine.py needs PyGObject at import time; the fake-gi bootstrap mirrors
tests/test_engine_ipc_dispatch.py.
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 复用 IPC 分发测试的 fake-gi（同一套 GLib/IBus 桩，避免两份 fake 分叉）。
from test_engine_ipc_dispatch import _make_fake_gi  # noqa: E402

_TRACKED_PREFIXES = ("ibus_voice_ime",)
_TRACKED_NAMES = ("gi", "gi.repository")


def _snapshot_tracked() -> dict:
    return {
        name: sys.modules.get(name)
        for name in list(sys.modules)
        if name.startswith(_TRACKED_PREFIXES) or name in _TRACKED_NAMES
    }


def _restore_tracked(snapshot: dict) -> None:
    for name in list(sys.modules):
        if (name.startswith(_TRACKED_PREFIXES) or name in _TRACKED_NAMES) and name not in snapshot:
            del sys.modules[name]
    for name, module in snapshot.items():
        if module is not None:
            sys.modules[name] = module


class _FakeEngine:
    """Duck-typed stand-in exposing only what the ownership helpers touch."""

    def __init__(self, name: str):
        self.name = name
        self._voice_state = "idle"
        self._voice_busy = False
        self.hotkey_calls: list[bool] = []

    def _handle_voice_hotkey(self, raw: bool = False) -> None:
        self.hotkey_calls.append(raw)


class VoiceToggleOwnershipTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # engine 的 import 链会绑定安装时的 gi；快照/驱逐/恢复与
        # test_engine_ipc_dispatch 完全同款，保证两个测试类在同一进程里
        # 先后运行时各自持有自己的 fake GLib（idle_add 落各自列表）。
        cls._snapshot = _snapshot_tracked()
        fake_gi = _make_fake_gi([])
        sys.modules["gi"] = fake_gi
        sys.modules["gi.repository"] = fake_gi.repository
        import ibus_voice_ime.engine as engine_module

        cls.engine = engine_module

    @classmethod
    def tearDownClass(cls) -> None:
        _restore_tracked(cls._snapshot)

    def setUp(self) -> None:
        self.engine._VOICE_OWNER = None

    def tearDown(self) -> None:
        self.engine._VOICE_OWNER = None

    def test_hotkey_forwards_to_recording_owner(self) -> None:
        # 事故场景：实例 A 在录音，焦点切换后新实例 B 收到热键。
        a, b = _FakeEngine("A"), _FakeEngine("B")
        a._voice_state = "recording"
        self.engine._claim_voice_owner(a)
        with mock.patch.object(self.engine, "log_error"):
            forwarded = self.engine._forward_voice_hotkey_to_owner(b, raw=False)
        self.assertTrue(forwarded)
        self.assertEqual(a.hotkey_calls, [False])  # 停止语义送达持有者
        self.assertEqual(b.hotkey_calls, [])       # B 自己绝不能再开始新录音

    def test_hotkey_forwards_while_processing(self) -> None:
        a, b = _FakeEngine("A"), _FakeEngine("B")
        a._voice_busy = True  # fixed 模式处理中（state 仍可能是 idle）
        self.engine._claim_voice_owner(a)
        with mock.patch.object(self.engine, "log_error"):
            self.assertTrue(self.engine._forward_voice_hotkey_to_owner(b, raw=True))
        self.assertEqual(a.hotkey_calls, [True])

    def test_no_forward_when_owner_idle_or_missing(self) -> None:
        a, b = _FakeEngine("A"), _FakeEngine("B")
        self.assertFalse(self.engine._forward_voice_hotkey_to_owner(b, raw=False))
        self.engine._claim_voice_owner(a)  # owner 存在但空闲
        self.assertFalse(self.engine._forward_voice_hotkey_to_owner(b, raw=False))
        self.assertEqual(a.hotkey_calls, [])

    def test_owner_receives_its_own_hotkey_directly(self) -> None:
        a = _FakeEngine("A")
        a._voice_state = "recording"
        self.engine._claim_voice_owner(a)
        self.assertFalse(self.engine._forward_voice_hotkey_to_owner(a, raw=False))

    def test_release_is_owned_only(self) -> None:
        a, b = _FakeEngine("A"), _FakeEngine("B")
        self.engine._claim_voice_owner(a)
        self.engine._release_voice_owner(b)  # 非 owner 的 release 不得清掉别人的所有权
        self.assertIs(self.engine._VOICE_OWNER, a)
        self.engine._release_voice_owner(a)
        self.assertIsNone(self.engine._VOICE_OWNER)

    def test_forward_to_dead_owner_releases(self) -> None:
        a, b = _FakeEngine("A"), _FakeEngine("B")
        a._voice_state = "recording"
        self.engine._claim_voice_owner(a)
        a._handle_voice_hotkey = None  # 模拟持有实例已被销毁

        class _Boom:
            def __getattr__(self, item):
                raise RuntimeError("dead object")

        dead = _Boom()
        dead._voice_state = "recording"
        self.engine._claim_voice_owner(dead)
        with mock.patch.object(self.engine, "log_error"):
            forwarded = self.engine._forward_voice_hotkey_to_owner(b, raw=False)
        self.assertTrue(forwarded)
        self.assertIsNone(self.engine._VOICE_OWNER)  # 失败路径必须释放，避免永久卡死

    def test_constructor_does_not_hijack_focused_engine(self) -> None:
        # __init__ 不再无条件设置 _FOCUSED_ENGINE（源码级守护，防回归）。
        source = Path(self.engine.__file__).read_text(encoding="utf-8")
        init_block = source.split("def __init__(self, bus: IBus.Bus, object_path: str):", 1)[1]
        init_block = init_block.split("def ", 1)[0]
        self.assertNotIn("_FOCUSED_ENGINE = self", init_block)

    def test_overlay_does_not_accept_focus_by_default(self) -> None:
        # 悬浮窗默认不抢焦点（源码级守护）。
        overlay_src = Path(self.engine.__file__).parent.joinpath("asr", "voice_overlay.py").read_text(encoding="utf-8")
        self.assertIn(
            'config.env_bool("VOICE_IME_OVERLAY_CATCH_HOTKEY", False)',
            overlay_src,
        )


if __name__ == "__main__":
    unittest.main()
