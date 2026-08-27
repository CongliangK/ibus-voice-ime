# -*- coding: utf-8 -*-
"""Tests for the Qwen3-ASR ``/warm`` pre-loading endpoint and client.

Hermetic (stdlib-only): the server test spawns a real ``ThreadingHTTPServer``
but stubs ``_MANAGER`` so no model is loaded; the client test mocks
``urllib.request.urlopen`` so no network is touched.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ibus_voice_ime.asr import qwen_asr_runtime  # noqa: E402
from ibus_voice_ime.asr import qwen_asr_server  # noqa: E402


# --------------------------------------------------------------------------- #
# Server side: the /warm endpoint routes to ModelManager acquire/release.
# --------------------------------------------------------------------------- #
class _FakeManager:
    """Records acquire/release calls; stands in for the module-level _MANAGER."""

    def __init__(self, active_id: str = "/m/Qwen3-ASR-1.7B", alias: str | None = "1.7b") -> None:
        self._active_id = active_id
        self._alias = alias
        self.acquire_calls = 0
        self.release_calls = 0

    def acquire_for_inference(self) -> None:
        self.acquire_calls += 1

    def release_after_inference(self) -> None:
        self.release_calls += 1

    @property
    def active_id(self) -> str:
        return self._active_id

    @property
    def active_alias(self) -> str | None:
        return self._alias


def _post_warm(port: int) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/warm",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:  # noqa: S310 - local endpoint
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class WarmEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_manager = qwen_asr_server._MANAGER
        self._fake = _FakeManager()
        qwen_asr_server._MANAGER = self._fake

    def tearDown(self) -> None:
        qwen_asr_server._MANAGER = self._prev_manager

    def _serve_once(self) -> tuple[int, dict[str, Any]]:
        server = ThreadingHTTPServer(("127.0.0.1", 0), qwen_asr_server.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            return _post_warm(server.server_port)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)

    def test_warm_calls_acquire_and_release(self) -> None:
        status, body = self._serve_once()
        self.assertEqual(status, 200)
        self.assertEqual(self._fake.acquire_calls, 1)
        self.assertEqual(self._fake.release_calls, 1)

    def test_warm_reports_active_model_and_alias(self) -> None:
        status, body = self._serve_once()
        self.assertEqual(body["status"], "warm")
        self.assertEqual(body["model"], "/m/Qwen3-ASR-1.7B")
        self.assertEqual(body["alias"], "1.7b")

    def test_warm_release_runs_even_on_error(self) -> None:
        # If building the response body raises, the handler should still
        # release the manager lock (the finally block must run).
        fake = _FakeManager()
        qwen_asr_server._MANAGER = fake
        # Make active_id raise to force the try-body in _handle_warm to error.
        p = mock.patch.object(type(fake), "active_id", new_callable=mock.PropertyMock)
        active_id_prop = p.start()
        active_id_prop.side_effect = RuntimeError("boom")
        try:
            status, body = self._serve_once()
        finally:
            p.stop()
        self.assertEqual(status, 500)
        self.assertIn("error", body)
        # acquire happened, and release must still have happened (finally block).
        self.assertEqual(fake.acquire_calls, 1)
        self.assertEqual(fake.release_calls, 1)


# --------------------------------------------------------------------------- #
# Client side: qwen_asr_runtime.warm() best-effort behavior.
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, status: int = 200, body: bytes = b'{"status":"warm"}') -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *_args: Any) -> None:
        return False


class WarmClientTest(unittest.TestCase):
    def setUp(self) -> None:
        # Force selected() True and ensure_server to a fixed URL.
        self._prev_env = (
            os.environ.get("VOICE_IME_ASR_BACKEND"),
            os.environ.get("VOICE_IME_QWEN_ASR"),
        )
        os.environ["VOICE_IME_ASR_BACKEND"] = "qwen3-asr"
        os.environ["VOICE_IME_QWEN_ASR"] = "1"
        # Patch ensure_server so warm() does not try to spawn a real process.
        patcher = mock.patch.object(qwen_asr_runtime, "ensure_server", return_value="http://127.0.0.1:18081")
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        backend, qwen = self._prev_env
        if backend is None:
            os.environ.pop("VOICE_IME_ASR_BACKEND", None)
        else:
            os.environ["VOICE_IME_ASR_BACKEND"] = backend
        if qwen is None:
            os.environ.pop("VOICE_IME_QWEN_ASR", None)
        else:
            os.environ["VOICE_IME_QWEN_ASR"] = qwen

    def test_warm_returns_true_on_200(self) -> None:
        with mock.patch("ibus_voice_ime.asr.qwen_asr_runtime.urllib.request.urlopen", return_value=_FakeResp(200)):
            self.assertTrue(qwen_asr_runtime.warm())

    def test_warm_returns_false_on_http_error(self) -> None:
        with mock.patch(
            "ibus_voice_ime.asr.qwen_asr_runtime.urllib.request.urlopen",
            side_effect=urllib.error.HTTPError("u", 500, "err", {}, None),  # type: ignore[arg-type]
        ):
            self.assertFalse(qwen_asr_runtime.warm())

    def test_warm_returns_false_on_connection_error(self) -> None:
        with mock.patch(
            "ibus_voice_ime.asr.qwen_asr_runtime.urllib.request.urlopen",
            side_effect=ConnectionError("refused"),
        ):
            self.assertFalse(qwen_asr_runtime.warm())

    def test_warm_skipped_when_not_selected(self) -> None:
        # Override selected() to False even though env says qwen3-asr.
        with mock.patch.object(qwen_asr_runtime, "selected", return_value=False):
            with mock.patch("ibus_voice_ime.asr.qwen_asr_runtime.urllib.request.urlopen") as urlopen:
                self.assertFalse(qwen_asr_runtime.warm())
                urlopen.assert_not_called()

    def test_warm_returns_false_if_ensure_server_raises(self) -> None:
        with mock.patch.object(qwen_asr_runtime, "ensure_server", side_effect=RuntimeError("no sidecar")):
            self.assertFalse(qwen_asr_runtime.warm())


if __name__ == "__main__":
    unittest.main()
