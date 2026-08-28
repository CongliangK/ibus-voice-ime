# -*- coding: utf-8 -*-
"""Unit tests for the engine's IPC command dispatch (toggle / toggle-raw).

engine.py needs PyGObject at import time; these tests install a minimal fake
``gi`` module (GLib/IBus stubs, no GTK, no IBus daemon) before importing the
module, then drive ``_serve_ipc_connection`` with in-memory socket-like
connections.  GLib.idle_add is recorded instead of running a main loop.
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 引擎里用到的非单字母 keyval 常量（值只需在进程内唯一且不与字母冲突）。
_FAKE_IBUS_KEYS = {
    name: 0x1000 + idx
    for idx, name in enumerate(
        (
            "KEY_Shift_L", "KEY_Shift_R",
            "KEY_Up", "KEY_Down", "KEY_Left", "KEY_Right",
            "KEY_KP_Up", "KEY_KP_Down", "KEY_KP_Left", "KEY_KP_Right",
            "KEY_Page_Up", "KEY_Page_Down", "KEY_KP_Page_Up", "KEY_KP_Page_Down",
            "KEY_Control_L", "KEY_Control_R", "KEY_Alt_L", "KEY_Alt_R",
            "KEY_Meta_L", "KEY_Meta_R",
        )
    )
}


def _make_fake_gi(idle_calls: list) -> types.ModuleType:
    gi = types.ModuleType("gi")
    gi.require_version = lambda *_args, **_kwargs: None

    class _GLib:
        PRIORITY_DEFAULT = 0

        @staticmethod
        def idle_add(callback, *args):
            idle_calls.append((callback, args))
            return len(idle_calls)

        @staticmethod
        def timeout_add(*_args, **_kwargs):
            return 0

    class _ModifierType:
        RELEASE_MASK = 1 << 0
        LOCK_MASK = 1 << 1
        CONTROL_MASK = 1 << 2
        MOD1_MASK = 1 << 3
        SHIFT_MASK = 1 << 4
        SUPER_MASK = 1 << 5
        META_MASK = 1 << 6
        HYPER_MASK = 1 << 7
        MOD4_MASK = 1 << 8

    class _Engine:
        props = types.SimpleNamespace()

    class _Factory:
        pass

    class _Text:
        @staticmethod
        def new_from_string(value):
            return value

    class _Orientation:
        VERTICAL = 0

    class _LookupTable:
        @staticmethod
        def new(**_kwargs):
            return _LookupTable()

    ibus = types.ModuleType("gi.repository.IBus")
    ibus.ModifierType = _ModifierType
    ibus.Engine = _Engine
    ibus.Factory = _Factory
    ibus.Text = _Text
    ibus.Orientation = _Orientation
    ibus.LookupTable = _LookupTable
    ibus.PATH_FACTORY = "/org/freedesktop/IBus/Factory"
    ibus.init = lambda: None
    ibus.keyval_to_unicode = lambda keyval: chr(keyval) if 0x20 <= keyval < 0x7F else ""

    def _ibus_getattr(name: str):
        if name.startswith("KEY_"):
            body = name[len("KEY_"):]
            if len(body) == 1 and body.isalpha():
                # 大小写分别对应真实 GDK/IBus 中不同的 keyval。
                return ord(body) if body.isupper() else ord(body.lower())
            if name not in _FAKE_IBUS_KEYS:
                _FAKE_IBUS_KEYS[name] = 0x2000 + len(_FAKE_IBUS_KEYS)
            return _FAKE_IBUS_KEYS[name]
        raise AttributeError(name)

    ibus.__getattr__ = _ibus_getattr  # PEP 562

    repository = types.ModuleType("gi.repository")
    repository.GLib = _GLib
    repository.IBus = ibus
    # 特意不提供 Gdk/Gtk：voice_overlay 的 try 导入会失败并走不可用降级路径。

    gi.repository = repository
    return gi


class _FakeConn:
    def __init__(self, payload: bytes):
        self._payload = payload
        self.sent: list[bytes] = []

    def recv(self, _size: int) -> bytes:
        return self._payload

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)


class _FakeEngineTarget:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def _handle_voice_hotkey(self, raw: bool = False) -> None:
        self.calls.append((raw,))


class IpcDispatchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.idle_calls: list = []
        cls._fake_gi = _make_fake_gi(cls.idle_calls)
        # sys.modules 快照/恢复：记录本测试关心的键（ibus_voice_ime* 与注入的
        # gi*），tearDownClass 精确还原。engine 的 import 链会带进
        # clipboard_paste / voice_overlay 等兄弟模块，若只 pop 个别模块，
        # 残留模块（例如持有 fake gi 的 clipboard_paste）会泄漏给后续测试。
        cls._modules_before = {
            name: sys.modules.get(name)
            for name in list(sys.modules)
            if name.startswith("ibus_voice_ime") or name in ("gi", "gi.repository")
        }
        sys.modules["gi"] = cls._fake_gi
        sys.modules["gi.repository"] = cls._fake_gi.repository
        import ibus_voice_ime.engine as engine_module  # noqa: E402

        cls.engine = engine_module

    @classmethod
    def tearDownClass(cls) -> None:
        # 精确还原：清掉测试期间新进入 sys.modules 的 ibus_voice_ime* / gi*
        # 键，并把快照中原本存在的模块对象放回原位（原本不存在的键移除），
        # 保证测试后 sys.modules 与测试前一致（含 fake gi 不外泄）。
        stale = [
            name
            for name in sys.modules
            if (name.startswith("ibus_voice_ime") or name in ("gi", "gi.repository"))
            and name not in cls._modules_before
        ]
        for name in stale:
            sys.modules.pop(name, None)
        for name, module in cls._modules_before.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def setUp(self) -> None:
        self.idle_calls.clear()
        self._saved_target = self.engine._FOCUSED_ENGINE
        self.target = _FakeEngineTarget()
        self.engine._FOCUSED_ENGINE = self.target

    def tearDown(self) -> None:
        self.engine._FOCUSED_ENGINE = self._saved_target

    # ------------------------------------------------------------- raw 命令

    def test_toggle_raw_dispatches_raw_true(self) -> None:
        conn = _FakeConn(b"toggle-raw\n")
        self.engine._serve_ipc_connection(conn)
        self.assertEqual(conn.sent, [b"OK\n"])
        self.assertEqual(len(self.idle_calls), 1)
        callback, args = self.idle_calls[0]
        self.assertEqual(callback, self.target._handle_voice_hotkey)
        self.assertEqual(args, (True,))
        callback(*args)  # idle 回调真的以 raw=True 落到引擎入口
        self.assertEqual(self.target.calls, [(True,)])

    def test_voice_raw_alias_dispatches_raw_true(self) -> None:
        conn = _FakeConn(b"voice-raw")
        self.engine._serve_ipc_connection(conn)
        self.assertEqual(conn.sent, [b"OK\n"])
        self.assertEqual(self.idle_calls[0][1], (True,))

    # ------------------------------------------------------------ 旧行为不变

    def test_legacy_toggle_dispatches_without_raw(self) -> None:
        for payload in (b"toggle\n", b"", b"hotkey\n", b"voice\n"):
            with self.subTest(payload=payload):
                self.idle_calls.clear()
                conn = _FakeConn(payload)
                self.engine._serve_ipc_connection(conn)
                self.assertEqual(conn.sent, [b"OK\n"])
                self.assertEqual(len(self.idle_calls), 1)
                callback, args = self.idle_calls[0]
                self.assertEqual(callback, self.target._handle_voice_hotkey)
                self.assertEqual(args, ())
                callback(*args)
                self.assertEqual(self.target.calls[-1], (False,))

    def test_unknown_command_rejected(self) -> None:
        conn = _FakeConn(b"toggle-raww\n")
        self.engine._serve_ipc_connection(conn)
        self.assertEqual(conn.sent, [b"ERROR unknown command\n"])
        self.assertEqual(self.idle_calls, [])

    def test_raw_command_without_focus_engine(self) -> None:
        self.engine._FOCUSED_ENGINE = None
        conn = _FakeConn(b"toggle-raw\n")
        self.engine._serve_ipc_connection(conn)
        self.assertEqual(conn.sent, [b"NO_FOCUS\n"])
        self.assertEqual(self.idle_calls, [])


if __name__ == "__main__":
    unittest.main()
