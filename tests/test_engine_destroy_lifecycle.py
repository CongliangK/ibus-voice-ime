"""Destroyed input contexts must not retain recording or queued UI work.

Only fake IBus/GLib, AudioSession and worker calls are used. No desktop,
microphone, ASR service, model, subprocess or network access is needed.
"""
from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tests.test_engine_ipc_dispatch import _FakeConn, _make_fake_gi


class _Timers:
    def __init__(self):
        self.next_id = 0
        self.pending = {}
        self.removed = []

    def add(self, millis, callback, *args):
        self.next_id += 1
        self.pending[self.next_id] = (millis, callback, args)
        return self.next_id

    def remove(self, source):
        self.removed.append(source)
        return self.pending.pop(source, None) is not None

    def fire(self, source):
        _, callback, args = self.pending[source]
        repeat = callback(*args)
        if not repeat:
            self.pending.pop(source, None)
        return repeat


class _AudioSession:
    def __init__(self, **_kwargs):
        self.active = False
        self.cancel_calls = 0
        self.cleanup_calls = 0

    def start(self):
        self.active = True

    def cancel(self):
        self.cancel_calls += 1
        self.active = False

    def stop(self):
        self.active = False
        return "/fake-recording.wav"

    def cleanup(self):
        self.cleanup_calls += 1


class EngineDestroyLifecycleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = {
            name: module for name, module in sys.modules.copy().items()
            if name.startswith("ibus_voice_ime") or name in ("gi", "gi.repository")
        }
        for name in cls.snapshot:
            sys.modules.pop(name, None)
        cls.idle_calls = []
        fake_gi = _make_fake_gi(cls.idle_calls)
        sys.modules["gi"] = fake_gi
        sys.modules["gi.repository"] = fake_gi.repository
        cls.engine = importlib.import_module("ibus_voice_ime.engine")

    @classmethod
    def tearDownClass(cls):
        for name in list(sys.modules):
            if name.startswith("ibus_voice_ime") or name in ("gi", "gi.repository"):
                sys.modules.pop(name, None)
        sys.modules.update(cls.snapshot)

    def setUp(self):
        e = self.engine
        self.saved_globals = (e._FOCUSED_ENGINE, e._LAST_LIVE_ENGINE, e._VOICE_OWNER)
        self.graveyard_size = len(e._ENGINE_GRAVEYARD)
        e._FOCUSED_ENGINE = e._LAST_LIVE_ENGINE = e._VOICE_OWNER = None
        self.idle_calls.clear()
        self.timers = _Timers()
        self.sessions = []

        def new_session(**kwargs):
            session = _AudioSession(**kwargs)
            self.sessions.append(session)
            return session

        self.patchers = [
            mock.patch.object(e.GLib, "timeout_add", side_effect=self.timers.add),
            mock.patch.object(e.GLib, "source_remove", side_effect=self.timers.remove, create=True),
            mock.patch.object(e.audio_session, "AudioSession", side_effect=new_session),
            mock.patch.object(e.voice_overlay, "VoiceOverlay", side_effect=lambda **_: mock.Mock()),
            mock.patch.object(e, "log_error"),
            mock.patch.object(e.config, "env_str", side_effect=lambda _name, default="": default),
            mock.patch.object(e.llm_postprocess, "enabled", return_value=False),
            mock.patch.object(e.IBus.Engine, "do_destroy", create=True),
            # Even an unexpected worker must never reach real recording/ASR.
            mock.patch.object(e.threading, "Thread"),
            mock.patch.object(e.voice, "record_and_transcribe", return_value="fixed result"),
            mock.patch.object(e.voice, "transcribe", return_value="raw result"),
            mock.patch.object(e.voice, "postprocess", return_value="late result"),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        e = self.engine
        e._FOCUSED_ENGINE, e._LAST_LIVE_ENGINE, e._VOICE_OWNER = self.saved_globals
        del e._ENGINE_GRAVEYARD[self.graveyard_size:]

    def _engine(self):
        # Bypass construction of memory stores/Rime/IPC; retain real methods.
        owner = self.engine.VoiceCustomEngine.__new__(self.engine.VoiceCustomEngine)
        owner._destroyed = False
        owner._timeout_sources = set()
        owner._voice_generation = 0
        owner._voice_watchdog_source = None
        owner._voice_state = "idle"
        owner._voice_busy = False
        owner._voice_raw = False
        owner._audio_session = None
        owner._overlay = None
        owner._aux_generation = 0
        owner._rime = mock.Mock()
        owner.get_connection = mock.Mock(return_value=object())
        owner.commit_text = mock.Mock()
        owner.update_preedit_text = mock.Mock()
        owner.update_auxiliary_text = mock.Mock()
        owner._flush_composition_before_voice = mock.Mock()
        owner._warm_voice_asr = mock.Mock()
        self.engine._ENGINE_INSTANCES.add(owner)
        self.addCleanup(self.engine._ENGINE_INSTANCES.discard, owner)
        return owner

    def test_recording_destroy_then_hotkey_leaves_only_new_recording(self):
        e = self.engine
        old, new = self._engine(), self._engine()
        old._handle_voice_hotkey()
        session, overlay = old._audio_session, old._overlay
        _, callback, args = next(iter(self.timers.pending.values()))
        old.do_focus_in()
        generation = old._voice_generation

        old.do_destroy()
        self.assertFalse(session.active)
        self.assertEqual(session.cancel_calls, 1)
        overlay.destroy.assert_called_once_with()
        self.assertIsNone(old._audio_session)
        self.assertIsNone(old._overlay)
        self.assertFalse(old._voice_busy)
        self.assertEqual(old._voice_state, "idle")
        self.assertGreater(old._voice_generation, generation)
        self.assertIsNone(e._VOICE_OWNER)
        self.assertIsNone(e._FOCUSED_ENGINE)
        self.assertIsNone(e._LAST_LIVE_ENGINE)
        self.assertNotIn(old, e._ENGINE_INSTANCES)
        self.assertEqual(self.timers.pending, {})

        new.do_focus_in()
        new._handle_voice_hotkey(raw=True)
        old.update_auxiliary_text.reset_mock()
        self.assertFalse(callback(*args))  # removed timer already dequeued
        self.assertEqual(sum(s.active for s in self.sessions), 1)
        self.assertIs(e._VOICE_OWNER, new)
        self.assertTrue(new._audio_session.active)
        self.assertTrue(new._voice_raw)
        old.update_auxiliary_text.assert_not_called()

    def test_processing_destroy_discards_late_worker_and_watchdog(self):
        e = self.engine
        old, new = self._engine(), self._engine()
        old._handle_voice_hotkey()
        old._handle_voice_hotkey()  # stop queues fake worker
        worker = e.threading.Thread.call_args.kwargs["target"]
        token = old._voice_generation
        watchdog = old._voice_watchdog_source
        old.do_destroy()
        self.assertIn(watchdog, self.timers.removed)
        self.assertIsNone(old._voice_watchdog_source)
        new._handle_voice_hotkey()
        new_session = new._audio_session

        worker()  # fake ASR completes after destroy
        old.update_preedit_text.reset_mock()
        old.update_auxiliary_text.reset_mock()
        for callback, args in self.idle_calls:
            self.assertFalse(callback(*args))
        e.threading.Thread.reset_mock()
        self.assertFalse(old._voice_watchdog_expired(token, 1))
        e.threading.Thread.assert_not_called()
        old.commit_text.assert_not_called()
        old.update_preedit_text.assert_not_called()
        old.update_auxiliary_text.assert_not_called()
        self.assertIs(e._VOICE_OWNER, new)
        self.assertIs(new._audio_session, new_session)
        self.assertTrue(new_session.active)
        self.assertEqual(new_session.cancel_calls, 0)

    def test_fixed_worker_completion_is_discarded_after_destroy(self):
        owner = self._engine()
        owner._start_voice_input()
        worker = self.engine.threading.Thread.call_args.kwargs["target"]
        owner.do_destroy()
        worker()
        for callback, args in self.idle_calls:
            self.assertFalse(callback(*args))
        owner.commit_text.assert_not_called()
        self.assertEqual(self.timers.pending, {})

    def test_queued_commit_is_removed_and_dequeued_commit_is_inert(self):
        owner = self._engine()
        owner._finish_voice_input("accepted result", None)
        source, (_, callback, args) = next(iter(self.timers.pending.items()))
        owner.do_destroy()
        self.assertIn(source, self.timers.removed)
        self.assertFalse(callback(*args))
        self.assertFalse(owner._commit_voice_result("already dequeued"))
        self.assertFalse(owner._commit_clipboard_file_text("clipboard", False))
        owner.commit_text.assert_not_called()

    def test_destroy_is_idempotent_and_preserves_rime_and_new_owner(self):
        e = self.engine
        old, new = self._engine(), self._engine()
        old._handle_voice_hotkey()
        session, overlay, rime = old._audio_session, old._overlay, old._rime
        new.do_focus_in()
        new._begin_toggle_voice_recording()
        new_session = new._audio_session
        new_sources = new._timeout_sources.copy()
        old.do_destroy()
        generation = old._voice_generation
        removed = self.timers.removed.copy()
        old.do_destroy()
        self.assertEqual(old._voice_generation, generation)
        self.assertEqual(session.cancel_calls, 1)
        self.assertEqual(self.timers.removed, removed)
        overlay.destroy.assert_called_once_with()
        e.IBus.Engine.do_destroy.assert_called_once_with(old)
        self.assertIs(old._rime, rime)
        self.assertEqual(rime.mock_calls, [])
        self.assertEqual(e._ENGINE_GRAVEYARD.count(old), 1)
        self.assertIs(e._VOICE_OWNER, new)
        self.assertIs(e._FOCUSED_ENGINE, new)
        self.assertIs(e._LAST_LIVE_ENGINE, new)
        self.assertTrue(new_session.active)
        self.assertEqual(new_session.cancel_calls, 0)
        self.assertTrue(new_sources.issubset(self.timers.pending))

    def test_queued_ipc_and_late_focus_cannot_reactivate_destroyed_engine(self):
        e = self.engine
        old, new = self._engine(), self._engine()
        old.do_focus_in()
        conn = _FakeConn(b"toggle\n")
        e._serve_ipc_connection(conn)
        self.assertEqual(conn.sent, [b"OK\n"])
        callback, args = self.idle_calls.pop()
        old.do_destroy()
        new.do_focus_in()
        new._handle_voice_hotkey()
        callback(*args)
        old.do_focus_in()
        self.assertIs(e._FOCUSED_ENGINE, new)
        self.assertFalse(old.do_process_key_event(e.IBus.KEY_v, 0, 0))
        old._begin_toggle_voice_recording()
        old._start_voice_input()
        old._cancel_toggle_voice_recording()
        self.assertEqual(len(self.sessions), 1)
        self.assertTrue(new._audio_session.active)
        self.assertIs(e._VOICE_OWNER, new)
        self.assertIs(e._pick_ipc_target(), new)

    def test_accepted_commit_survives_new_generation_on_live_engine(self):
        owner = self._engine()
        owner._finish_voice_input("accepted result", None)
        source = next(iter(self.timers.pending))
        generation = owner._voice_generation
        owner._handle_voice_hotkey()
        self.assertGreater(owner._voice_generation, generation)
        self.assertFalse(self.timers.fire(source))
        owner.commit_text.assert_called_once_with("accepted result")
        self.assertTrue(owner._audio_session.active)
        self.assertIs(self.engine._VOICE_OWNER, owner)
        self.assertNotIn(source, owner._timeout_sources)
        owner.do_destroy()
        self.assertNotIn(source, self.timers.removed)

    def test_aux_and_error_overlay_timers_are_removed(self):
        owner = self._engine()
        owner._overlay = mock.Mock()
        overlay = owner._overlay
        owner._finish_voice_input("", "fake failure")
        pending = self.timers.pending.copy()
        self.assertEqual(len(pending), 2)
        owner.do_destroy()
        owner.update_auxiliary_text.reset_mock()
        for source, (_, callback, args) in pending.items():
            self.assertIn(source, self.timers.removed)
            self.assertFalse(callback(*args))
        owner.update_auxiliary_text.assert_not_called()
        overlay.destroy.assert_called_once_with()
        overlay.hide.assert_not_called()

    def test_cleanup_failures_do_not_skip_remaining_cleanup(self):
        owner = self._engine()
        owner._handle_voice_hotkey()
        session, overlay = owner._audio_session, owner._overlay
        session.cancel = mock.Mock(side_effect=RuntimeError("fake cancel failure"))
        self.engine.GLib.source_remove.side_effect = RuntimeError("fake timer failure")
        callbacks = list(self.timers.pending.values())
        owner.do_destroy()
        session.cancel.assert_called_once_with()
        overlay.destroy.assert_called_once_with()
        self.engine.IBus.Engine.do_destroy.assert_called_once_with(owner)
        self.assertIn(owner, self.engine._ENGINE_GRAVEYARD)
        self.assertIsNone(self.engine._VOICE_OWNER)
        for _, callback, args in callbacks:
            self.assertFalse(callback(*args))


if __name__ == "__main__":
    unittest.main()
