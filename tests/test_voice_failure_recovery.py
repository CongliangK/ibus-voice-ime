"""No GPU/desktop: owned-child recovery, watchdog and stubborn recorder tests."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ibus_voice_ime.asr import qwen_asr_runtime as runtime
from ibus_voice_ime.asr.audio_session import AudioSession, AudioSessionError
from tests.test_engine_ipc_dispatch import IpcDispatchTest


class RuntimeRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.old = runtime._PROCESS, runtime._PROCESS_KEY
        runtime._PROCESS = None
        runtime._PROCESS_KEY = None
        runtime._clear_failure()

    def tearDown(self):
        runtime.shutdown()
        runtime._PROCESS, runtime._PROCESS_KEY = self.old
        runtime._clear_failure()

    def test_timeout_reaps_owned_child_and_clears_state(self):
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        runtime._PROCESS = proc
        runtime._PROCESS_KEY = ('127.0.0.1', 18081, 'model', 'python')
        with mock.patch.object(runtime, 'ensure_server', return_value='http://127.0.0.1:18081'), mock.patch.object(runtime.urllib.request, 'urlopen', side_effect=socket.timeout):
            with self.assertRaisesRegex(RuntimeError, '下次听写会重新启动'):
                runtime.transcribe('/fake.wav')
        self.assertIsNotNone(proc.poll())
        self.assertIsNone(runtime._PROCESS)
        self.assertIsNone(runtime._PROCESS_KEY)
        self.assertEqual(runtime._FAIL_MSG, '')

    def test_timeout_next_request_can_restart_and_succeed(self):
        import io
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        runtime._PROCESS = proc
        def ensure():
            if runtime._PROCESS is None:
                runtime._PROCESS = mock.Mock()
            return 'http://isolated'
        with mock.patch.object(runtime, 'ensure_server', side_effect=ensure), mock.patch.object(runtime.urllib.request, 'urlopen', side_effect=[socket.timeout(), io.BytesIO(b'{"text":"recovered"}')]):
            with self.assertRaises(RuntimeError):
                runtime.transcribe('/fake.wav')
            self.assertIsNone(runtime._PROCESS)
            self.assertEqual(runtime.transcribe('/fake.wav'), 'recovered')
        self.assertIsNotNone(proc.poll())
        runtime._PROCESS = None

    def test_warm_timeout_reaps_owned_process(self):
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        runtime._PROCESS = proc
        with mock.patch.object(runtime, 'selected', return_value=True), mock.patch.object(runtime, 'ensure_server', return_value='http://isolated'), mock.patch.object(runtime.urllib.request, 'urlopen', side_effect=socket.timeout()):
            self.assertFalse(runtime.warm())
        self.assertIsNone(runtime._PROCESS)
        self.assertIsNotNone(proc.poll())

    def test_healthy_owned_configuration_change_restarts_before_external_mismatch(self):
        old = mock.Mock()
        old.poll.return_value = None
        new = mock.Mock()
        new.poll.return_value = None
        runtime._PROCESS, runtime._PROCESS_KEY = old, ('old', 1, 'old-model', 'old-python')
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(runtime, 'model_id', return_value='Qwen/new-model'), mock.patch.object(runtime, '_python', return_value=sys.executable), mock.patch.object(runtime, '_server_matches', return_value=False), mock.patch.object(runtime, 'is_ready', return_value=True), mock.patch.object(runtime, '_terminate_process') as terminate, mock.patch.object(runtime.subprocess, 'Popen', return_value=new), mock.patch.dict(os.environ, {'VOICE_IME_LOG_DIR': directory}):
            runtime.ensure_server()
            terminate.assert_called_once_with(old)
            self.assertIs(runtime._PROCESS, new)
            self.assertIn(sys.executable, runtime._PROCESS_KEY[3])
        runtime._PROCESS = None

    def test_dtype_change_restarts_owned_same_model(self):
        old = mock.Mock(); old.poll.return_value = None
        new = mock.Mock(); new.poll.return_value = None
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(runtime, 'model_id', return_value='Qwen/same-model'), mock.patch.object(runtime, '_python', return_value=sys.executable), mock.patch.object(runtime, '_server_matches', side_effect=[False, True, True]), mock.patch.object(runtime, 'is_ready', side_effect=[False, True, True, True]), mock.patch.object(runtime, '_terminate_process') as terminate, mock.patch.object(runtime.subprocess, 'Popen', return_value=old) as spawn, mock.patch.dict(os.environ, {'VOICE_IME_LOG_DIR': directory, 'VOICE_IME_QWEN_ASR_DTYPE': 'bfloat16'}):
            runtime.ensure_server()
            first_key = runtime._PROCESS_KEY
            spawn.return_value = new
            with mock.patch.dict(os.environ, {'VOICE_IME_QWEN_ASR_DTYPE': 'float16'}):
                runtime.ensure_server()
            terminate.assert_called_once_with(old)
            self.assertNotEqual(runtime._PROCESS_KEY, first_key)
            self.assertIs(runtime._PROCESS, new)
        runtime._PROCESS = None

    def test_dead_owner_key_does_not_force_respawn_of_matching_external_service(self):
        old = mock.Mock(); old.poll.return_value = 0
        runtime._PROCESS, runtime._PROCESS_KEY = old, ('old', 1, 'old-model', 'old-python')
        with mock.patch.object(runtime, '_server_matches', return_value=True), mock.patch.object(runtime.subprocess, 'Popen') as spawn:
            runtime.ensure_server()
            spawn.assert_not_called()
        self.assertIsNone(runtime._PROCESS)
        self.assertIsNone(runtime._PROCESS_KEY)

    def test_failed_reap_preserves_ownership_and_reports_no_success(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        runtime._PROCESS = proc
        with mock.patch.object(runtime, '_terminate_process', side_effect=RuntimeError('cannot reap')):
            self.assertFalse(runtime.recover_timeout(proc))
        self.assertIs(runtime._PROCESS, proc)
        runtime._PROCESS = None

    def test_external_timeout_never_kills_process(self):
        with mock.patch.object(runtime, 'ensure_server', return_value='http://external'), mock.patch.object(runtime.urllib.request, 'urlopen', side_effect=socket.timeout), mock.patch.object(runtime, '_terminate_process') as terminate:
            with self.assertRaisesRegex(RuntimeError, '未终止外部服务'):
                runtime.transcribe('/fake.wav')
            terminate.assert_not_called()

    def test_late_timeout_does_not_kill_replacement(self):
        old, new = mock.Mock(), mock.Mock()
        runtime._PROCESS = new
        self.assertFalse(runtime.recover_timeout(old))
        self.assertIs(runtime._PROCESS, new)
        new.terminate.assert_not_called()
        runtime._PROCESS = None

    def test_contended_http_timeout_is_recovered_before_next_healthy_reuse(self):
        old, new = mock.Mock(), mock.Mock()
        old.poll.return_value = new.poll.return_value = None
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(runtime, 'model_id', return_value='Qwen/test-model'), \
             mock.patch.object(runtime, '_python', return_value=sys.executable), \
             mock.patch.object(runtime, '_server_matches', side_effect=lambda _: runtime._PROCESS is not None), \
             mock.patch.object(runtime, 'is_ready', side_effect=lambda: runtime._PROCESS is not None), \
             mock.patch.object(runtime.subprocess, 'Popen', side_effect=[old, new]) as spawn, \
             mock.patch.dict(os.environ, {'VOICE_IME_LOG_DIR': directory}):
            runtime.ensure_server()  # establish an exact healthy owner/key
            runtime._ENSURE_LOCK.acquire()
            try:
                with mock.patch.object(runtime, 'ensure_server', return_value='http://isolated'), \
                     mock.patch.object(runtime.urllib.request, 'urlopen', side_effect=socket.timeout):
                    with self.assertRaisesRegex(RuntimeError, '识别超时'):
                        runtime.transcribe('/fake.wav')
                self.assertIs(runtime._PENDING_RECOVERY, old)
                old.terminate.assert_not_called()
            finally:
                runtime._ENSURE_LOCK.release()
            runtime.ensure_server()
            old.terminate.assert_called_once()
            self.assertEqual(spawn.call_count, 2)
            self.assertIs(runtime._PROCESS, new)
            self.assertIsNone(runtime._PENDING_RECOVERY)
            self.assertEqual(runtime._FAIL_MSG, '')
        runtime._PROCESS = None

    def test_contended_recovery_never_kills_replacement_or_external(self):
        for replacement in (mock.Mock(), None):
            with self.subTest(external=replacement is None):
                old = mock.Mock(); old.poll.return_value = None
                runtime._PROCESS = old
                runtime._ENSURE_LOCK.acquire()
                try:
                    self.assertFalse(runtime.recover_timeout(old))
                finally:
                    runtime._ENSURE_LOCK.release()
                runtime._PROCESS, runtime._PROCESS_KEY = replacement, None
                if replacement is not None:
                    replacement.poll.return_value = None
                with mock.patch.object(runtime, '_server_matches', return_value=True), \
                     mock.patch.object(runtime, '_terminate_process') as terminate:
                    runtime.ensure_server()
                    terminate.assert_not_called()
                self.assertIs(runtime._PROCESS, replacement)
                self.assertIsNone(runtime._PENDING_RECOVERY)
                old.terminate.assert_not_called()
        runtime._PROCESS = None

    def test_pending_failed_reap_blocks_reuse_and_can_retry(self):
        proc = mock.Mock(); proc.poll.return_value = None
        runtime._PROCESS = proc
        with mock.patch.object(runtime, '_terminate_process', side_effect=RuntimeError('cannot reap')):
            self.assertFalse(runtime.recover_timeout(proc))
            with mock.patch.object(runtime, '_server_matches') as health:
                with self.assertRaisesRegex(RuntimeError, 'cannot reap'):
                    runtime.ensure_server()
                health.assert_not_called()
        self.assertIs(runtime._PENDING_RECOVERY, proc)
        self.assertTrue(runtime.recover_timeout(proc))
        self.assertIsNone(runtime._PROCESS)
        self.assertIsNone(runtime._PENDING_RECOVERY)

    def test_pending_dead_owner_clears_identity_without_terminating_external(self):
        proc = mock.Mock(); proc.poll.return_value = 0
        runtime._PROCESS, runtime._PROCESS_KEY = proc, ('old', 1, 'model', 'python')
        runtime._PENDING_RECOVERY = proc
        with mock.patch.object(runtime, '_server_matches', return_value=True), \
             mock.patch.object(runtime, '_terminate_process') as terminate:
            runtime.ensure_server()
            terminate.assert_not_called()
        self.assertIsNone(runtime._PROCESS)
        self.assertIsNone(runtime._PROCESS_KEY)
        self.assertIsNone(runtime._PENDING_RECOVERY)

    def test_shutdown_clears_pending_recovery(self):
        runtime._PENDING_RECOVERY = mock.Mock()
        runtime.shutdown()
        self.assertIsNone(runtime._PENDING_RECOVERY)

    def test_nonfinite_timeouts_have_finite_defaults(self):
        for invalid in ('nan', 'inf', '-1', '0'):
            with mock.patch.dict(os.environ, {'VOICE_IME_QWEN_ASR_TIMEOUT': invalid}):
                self.assertEqual(runtime._timeout('VOICE_IME_QWEN_ASR_TIMEOUT', 120), 120)

    def test_start_lock_wait_is_bounded(self):
        runtime._ENSURE_LOCK.acquire()
        try:
            with mock.patch.dict(os.environ, {'VOICE_IME_QWEN_ASR_START_TIMEOUT': '0.1'}):
                with self.assertRaisesRegex(RuntimeError, '启动锁超时'):
                    runtime.ensure_server()
        finally:
            runtime._ENSURE_LOCK.release()

    def test_missing_venv_never_returns_system_python(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(runtime, 'ROOT_DIR', Path(tmp)), mock.patch.object(runtime.config, 'env_str', return_value=''):
            self.assertEqual(runtime._python(), str(Path(tmp) / '.venv-qwen-asr/bin/python'))


class WatchdogTest(IpcDispatchTest):
    # Inherit isolated gi import/restoration, not a real IBus session.
    def _owner(self):
        cls = self.engine.VoiceCustomEngine
        owner = types.SimpleNamespace(
            _voice_generation=1, _voice_busy=True, _voice_state='processing',
            _voice_watchdog_source=41, _overlay=None, _audio_session=None,
            update_preedit_text=mock.Mock(), update_auxiliary_text=mock.Mock(),
            _show_aux=mock.Mock(), _commit_voice_result=mock.Mock(),
            _flush_composition_before_voice=mock.Mock(), _warm_voice_asr=mock.Mock(),
            _update_recording_status=mock.Mock(), _stop_toggle_voice_recording=mock.Mock(),
            _cancel_toggle_voice_recording=mock.Mock())
        for name in ('_clear_voice_watchdog', '_finish_voice_input', '_arm_voice_watchdog', '_voice_watchdog_expired'):
            setattr(owner, name, types.MethodType(getattr(cls, name), owner))
        return owner

    def test_success_then_new_recording_ignores_old_watchdog_and_keeps_commit(self):
        e = self.engine; cls = e.VoiceCustomEngine
        owner = self._owner()
        with mock.patch.object(e, '_release_voice_owner'), mock.patch.object(e, '_claim_voice_owner'), \
             mock.patch.object(e.audio_session, 'AudioSession'), \
             mock.patch.object(e.voice_overlay, 'VoiceOverlay'), \
             mock.patch.object(e.GLib, 'timeout_add', return_value=42) as timer, \
             mock.patch.object(e.GLib, 'source_remove', create=True) as remove, \
             mock.patch.object(e.threading, 'Thread') as thread:
            cls._voice_callback(owner, 1, owner._finish_voice_input, 'normal result', None)
            remove.assert_called_once_with(41)
            commit_call = timer.call_args
            self.assertIs(commit_call.args[1], owner._commit_voice_result)
            self.assertEqual(commit_call.args[2], 'normal result')
            cls._begin_toggle_voice_recording(owner, raw=True)
            session = owner._audio_session
            self.assertGreater(owner._voice_generation, 1)
            cls._voice_watchdog_expired(owner, 1, 600)
            self.assertEqual(owner._voice_state, 'recording')
            self.assertTrue(owner._voice_busy)
            self.assertIs(owner._audio_session, session)
            session.cancel.assert_not_called()
            thread.assert_not_called()
            # A's accepted commit remains independent of B's generation.
            commit_call.args[1](*commit_call.args[2:])
            owner._commit_voice_result.assert_called_once_with('normal result')

    def test_watchdog_requires_processing_even_with_matching_generation(self):
        owner = self._owner(); owner._voice_state = 'recording'
        with mock.patch.object(self.engine.threading, 'Thread') as thread:
            self.engine.VoiceCustomEngine._voice_watchdog_expired(owner, 1, 1)
            thread.assert_not_called()
        self.assertTrue(owner._voice_busy)
        self.assertEqual(owner._voice_watchdog_source, 41)

    def test_error_cancel_and_rearm_remove_timer(self):
        e = self.engine; cls = e.VoiceCustomEngine
        with mock.patch.object(e, '_release_voice_owner'), \
             mock.patch.object(e.GLib, 'source_remove', create=True) as remove, \
             mock.patch.object(e.GLib, 'timeout_add', return_value=42):
            for action in ('error', 'cancel', 'rearm'):
                with self.subTest(action=action):
                    owner = self._owner(); remove.reset_mock()
                    if action == 'error':
                        owner._finish_voice_input('', 'failure')
                    elif action == 'cancel':
                        cls._cancel_toggle_voice_recording(owner)
                    else:
                        owner._arm_voice_watchdog()
                    remove.assert_called_once_with(41)
                    self.assertGreater(owner._voice_generation, 1)
                    self.assertEqual(owner._voice_watchdog_source, 42 if action == 'rearm' else None)

    def test_stale_watchdog_does_not_clear_new_processing_timer(self):
        owner = self._owner(); owner._voice_generation = 2
        self.engine.VoiceCustomEngine._voice_watchdog_expired(owner, 1, 1)
        self.assertEqual(owner._voice_watchdog_source, 41)
        self.assertTrue(owner._voice_busy)

    def test_timeout_recovers_and_discards_old_completion(self):
        cls = self.engine.VoiceCustomEngine
        owner = types.SimpleNamespace(_voice_generation=1, _voice_busy=True, _voice_state='processing')
        owner._finish_voice_input = mock.Mock(side_effect=lambda *_: setattr(owner, '_voice_busy', False))
        with mock.patch.object(self.engine.threading, 'Thread') as thread:
            self.assertFalse(cls._voice_watchdog_expired(owner, 1, 0.1))
            thread.return_value.start.assert_called_once()
        self.assertFalse(owner._voice_busy)
        owner._voice_busy = True  # second dictation
        callback = mock.Mock()
        self.assertFalse(cls._voice_callback(owner, 1, callback, 'late text'))
        callback.assert_not_called()
        cls._voice_callback(owner, 2, callback, 'new text')
        callback.assert_called_once_with('new text')


class RecorderRecoveryTest(unittest.TestCase):
    def test_pcm_stderr_flood_and_inherited_writer_close_wav_bounded(self):
        import select
        import wave
        real_popen = subprocess.Popen
        ready_r, ready_w = os.pipe()
        child = holder = None
        def spawn(*args, **kwargs):
            nonlocal child, holder
            # Independent holder inherits the same stderr write FD; it remains
            # alive during stop. Both fake processes are directly waitable.
            holder = real_popen([sys.executable, '-c', 'import time; time.sleep(60)'], stderr=kwargs['stderr'])
            code = f'''import signal,time,os
signal.signal(signal.SIGTERM, signal.SIG_IGN)
os.write(1, b'\\x01\\x00' * 1600)
os.write(2, b'e' * 131072)
os.write({ready_w}, b'ready')
time.sleep(60)
'''
            child = real_popen([sys.executable, '-u', '-c', code], pass_fds=(ready_w,), **kwargs)
            return child
        session = AudioSession()
        try:
            with mock.patch('ibus_voice_ime.asr.audio_session.subprocess.Popen', side_effect=spawn), mock.patch('ibus_voice_ime.asr.voice._arecord_device_args', return_value=[]):
                session.start()
            self.assertTrue(select.select([ready_r], [], [], 3)[0])
            self.assertEqual(os.read(ready_r, 5), b'ready')
            # Wait until the reader consumed the PCM (without accessing devices).
            limit = time.monotonic() + 2
            while session.level() == 0 and time.monotonic() < limit:
                session._done_event.wait(0.01)
            started = time.monotonic()
            path = session.stop()
            self.assertLess(time.monotonic() - started, 4)
            self.assertIsNotNone(child.poll())
            self.assertTrue(session._done_event.is_set())
            session._thread.join(timeout=1)
            self.assertFalse(session._thread.is_alive())
            with wave.open(path) as wav:
                self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (1, 2, 16000))
                self.assertEqual(wav.getnframes(), 1600)
                self.assertEqual(len(wav.readframes(1600)), 3200)
        finally:
            os.close(ready_r); os.close(ready_w)
            for proc in (child, holder):
                if proc is not None:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=2)
            if session._thread:
                session._thread.join(timeout=1)
            session.cleanup()

    def test_early_exit_error_diagnostic_is_bounded(self):
        real_popen = subprocess.Popen
        session = AudioSession()
        with mock.patch('ibus_voice_ime.asr.audio_session.subprocess.Popen', side_effect=lambda *a, **kw: real_popen([sys.executable, '-c', 'import sys; sys.stderr.write("fake recorder error")'], **kw)), mock.patch('ibus_voice_ime.asr.voice._arecord_device_args', return_value=[]):
            session.start()
        try:
            self.assertTrue(session._done_event.wait(3))
            with self.assertRaisesRegex(AudioSessionError, 'fake recorder error'):
                session.stop()
        finally:
            session._thread.join(timeout=1)
            session.cleanup()

    def test_wait_failure_still_signals_completion_and_never_returns_wav(self):
        session = AudioSession()
        session._proc = mock.Mock()
        session._proc.poll.return_value = None
        session._proc.wait.side_effect = subprocess.TimeoutExpired('fake', 1)
        # Reader assertion/invalid WAV still must run finally and signal done.
        session._reader()
        self.assertTrue(session._done_event.is_set())
        with self.assertRaisesRegex(AudioSessionError, '停止/回收录音器失败'):
            session.stop()

    def test_no_output_recorder_ignoring_term_is_killed_bounded(self):
        import select
        real_popen = subprocess.Popen
        ready_r, ready_w = os.pipe()
        child = None
        def spawn(*args, **kwargs):
            nonlocal child
            child = real_popen([sys.executable, '-u', '-c',
                f'import signal,time,os; signal.signal(signal.SIGTERM, signal.SIG_IGN); os.write({ready_w}, b"ready"); time.sleep(60)'], pass_fds=(ready_w,), **kwargs)
            return child
        session = AudioSession()
        try:
            with mock.patch('ibus_voice_ime.asr.audio_session.subprocess.Popen', side_effect=spawn), mock.patch('ibus_voice_ime.asr.voice._arecord_device_args', return_value=[]):
                session.start()
            self.assertTrue(select.select([ready_r], [], [], 3)[0])
            self.assertEqual(os.read(ready_r, 5), b'ready')
            started = time.monotonic()
            with self.assertRaisesRegex(AudioSessionError, '没有录到有效音频'):
                session.stop()
            self.assertLess(time.monotonic() - started, 4)
            self.assertIsNotNone(child.poll())
            self.assertTrue(session._done_event.is_set())
        finally:
            os.close(ready_r); os.close(ready_w)
            if child is not None and child.poll() is None:
                child.kill(); child.wait(timeout=2)
            if session._thread is not None:
                session._thread.join(timeout=1)
            session.cleanup()
