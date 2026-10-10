"""Liveness/readiness, retry cooling and cheap request validation regressions."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ibus_voice_ime.asr import qwen_asr_runtime as runtime, qwen_asr_server as server
from ibus_voice_ime.asr.sidecar_http import RequestError


class Response:
    status = 200
    def __init__(self, data):
        self.body = json.dumps(data).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self, size=-1):
        return self.body[:size] if size >= 0 else self.body


class HealthTest(unittest.TestCase):
    def test_invalid_health_is_not_a_qwen_service(self):
        for body in ([], None, {'status': 'ok'}, {'status': 'other', 'model': 'x'}, {'status': 'ok', 'model': 3}):
            with self.subTest(body=body), mock.patch.object(runtime.urllib.request, 'urlopen', return_value=Response(body)):
                self.assertFalse(runtime.is_ready())

    def test_idle_service_is_alive_not_model_ready(self):
        body = {'status': 'idle', 'model': 'x', 'loaded': False, 'error': ''}
        with mock.patch.object(runtime.urllib.request, 'urlopen', return_value=Response(body)):
            self.assertTrue(runtime.is_ready())
            self.assertFalse(runtime.is_model_ready())

    def test_failed_model_is_not_ready_even_if_stale_model_is_loaded(self):
        body = {'status': 'error', 'model': 'x', 'loaded': True, 'error': 'gcc failed'}
        with mock.patch.object(runtime.urllib.request, 'urlopen', return_value=Response(body)):
            self.assertTrue(runtime.is_ready())
            self.assertFalse(runtime.is_model_ready())

    def test_model_ready_is_distinct_from_liveness(self):
        body = {'status': 'ready', 'model': 'x', 'loaded': True, 'error': ''}
        with mock.patch.object(runtime.urllib.request, 'urlopen', return_value=Response(body)):
            self.assertTrue(runtime.is_model_ready())

    def test_error_classification_uses_end_of_long_detail(self):
        body = json.dumps({'error': 'gcc ' + 'x' * 2000 + '\nfatal error: Python.h: No such file or directory'})
        result = runtime._humanize_transcribe_error(500, body)
        self.assertIn('[python_headers]', result)
        self.assertIn('fatal error: Python.h', result)
        self.assertNotIn('模型未能在 GPU 上加载', result)

    def test_custom_log_directory_in_diagnostic(self):
        with mock.patch.dict(os.environ, {'VOICE_IME_LOG_DIR': '/custom/logs'}):
            self.assertIn('/custom/logs/qwen-asr-server.log', runtime._humanize_transcribe_error(500, 'gcc failed'))


class TranscribeResponseTest(unittest.TestCase):
    def call(self, response):
        with mock.patch.object(runtime, 'ensure_server', return_value='http://127.0.0.1:18081'), mock.patch.object(
            runtime.voice_terms, 'build_asr_context', return_value=''
        ), mock.patch.object(runtime.urllib.request, 'urlopen', return_value=response), mock.patch.object(runtime, 'trim_to_last_lines'):
            return runtime.transcribe('/fixture.wav')

    def test_invalid_response_is_not_silent_empty_transcription(self):
        for body in ([], {}, {'text': None}, {'text': 123}):
            with self.subTest(body=body), self.assertRaises(RuntimeError):
                self.call(Response(body))
        response = Response({})
        response.body = b'not json'
        with self.assertRaisesRegex(RuntimeError, '无效 JSON'):
            self.call(response)

    def test_oversized_response_rejected(self):
        response = Response({})
        response.body = b'x' * 1048577
        with self.assertRaisesRegex(RuntimeError, '1 MiB'):
            self.call(response)

    def test_200_error_response_is_classified(self):
        with self.assertRaisesRegex(RuntimeError, 'triton_compile'):
            self.call(Response({'error': 'gcc failed'}))

    def test_valid_empty_and_nonempty_text(self):
        self.assertEqual(self.call(Response({'text': ''})), '')
        self.assertEqual(self.call(Response({'text': ' 测试 '})), '测试')


class LoadFailureTest(unittest.TestCase):
    def manager(self):
        return server.ModelManager(primary_path='/m/Qwen3-ASR-1.7B', secondary_path=None,
                                   idle_timeout=0, check_interval=1)

    def acquire(self, manager):
        manager.acquire_for_inference()
        manager.release_after_inference()

    def test_stable_load_failure_is_not_repeated_on_every_request(self):
        manager = self.manager()
        with mock.patch.dict(os.environ, {'VOICE_IME_QWEN_ASR_FAIL_COOLDOWN': '180'}), mock.patch.object(
            manager, '_ensure_active_locked', side_effect=RuntimeError('gcc failed')
        ) as load:
            self.acquire(manager)
            self.acquire(manager)
            self.assertEqual(load.call_count, 1)
            self.assertIn('gcc failed', manager.load_error)

    def test_oom_is_immediately_retryable(self):
        manager = self.manager()
        with mock.patch.object(manager, '_ensure_active_locked', side_effect=RuntimeError('CUDA out of memory')) as load:
            self.acquire(manager)
            self.acquire(manager)
            self.assertEqual(load.call_count, 2)

    def test_success_after_cooldown_clears_failure(self):
        manager = self.manager()
        with mock.patch.dict(os.environ, {'VOICE_IME_QWEN_ASR_FAIL_COOLDOWN': '0'}), mock.patch.object(
            manager, '_ensure_active_locked', side_effect=[RuntimeError('gcc failed'), None]
        ) as load:
            self.acquire(manager)
            self.acquire(manager)
            self.assertEqual(load.call_count, 2)
            self.assertEqual(manager.load_error, '')


class ValidationBeforeLoadingTest(unittest.TestCase):
    def test_remote_or_oversized_input_never_acquires_model(self):
        manager = mock.Mock()
        for payload, status in (({'audio': 'https://example.com/audio.wav'}, 400), ({'audio': 'x' * 100}, 413)):
            body = json.dumps(payload).encode()
            handler = mock.Mock(headers={'Content-Length': str(len(body))}, rfile=io.BytesIO(body))
            limit = '32' if status == 413 else '1024'
            with self.subTest(status=status), mock.patch.object(server, '_MANAGER', manager), mock.patch.dict(
                os.environ, {'VOICE_IME_ASR_SIDECAR_MAX_BODY_BYTES': limit}
            ):
                with self.assertRaises(RequestError) as context:
                    server._handle_transcribe(handler)
                self.assertEqual(context.exception.status, status)
                manager.acquire_for_inference.assert_not_called()

    def test_invalid_warm_body_never_acquires_model(self):
        manager = mock.Mock()
        handler = mock.Mock(headers={'Content-Length': '1'}, rfile=io.BytesIO(b'['))
        with mock.patch.object(server, '_MANAGER', manager):
            with self.assertRaises(RequestError):
                server._handle_warm(handler)
            manager.acquire_for_inference.assert_not_called()


if __name__ == '__main__':
    unittest.main()
