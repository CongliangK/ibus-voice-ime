"""Security boundary tests for local ASR sidecars."""

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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ibus_voice_ime.asr import mimo_asr_server, qwen_asr_server  # noqa: E402


def post(handler: type, payload: dict[str, Any]) -> tuple[int, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        return post_to_port(server.server_port, payload)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def post_to_port(port: int, payload: dict[str, Any]) -> tuple[int, str]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/transcribe",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read().decode("utf-8")
        finally:
            exc.close()


def with_limit(handler: type) -> tuple[int, str]:
    old = os.environ.get("VOICE_IME_ASR_SIDECAR_MAX_BODY_BYTES")
    os.environ["VOICE_IME_ASR_SIDECAR_MAX_BODY_BYTES"] = "32"
    try:
        return post(handler, {"audio": "x" * 80})
    finally:
        restore_limit(old)


def restore_limit(old: str | None) -> None:
    if old is None:
        os.environ.pop("VOICE_IME_ASR_SIDECAR_MAX_BODY_BYTES", None)
        return
    os.environ["VOICE_IME_ASR_SIDECAR_MAX_BODY_BYTES"] = old


class SidecarSecurityTest(unittest.TestCase):
    def test_qwen_rejects_remote_audio_urls(self) -> None:
        status, body = post(qwen_asr_server.Handler, {"audio": "http://169.254.169.254/"})
        self.assertEqual(status, 400)
        self.assertIn("remote audio URLs are disabled", body)

    def test_qwen_rejects_oversized_request_before_model(self) -> None:
        status, body = with_limit(qwen_asr_server.Handler)
        self.assertEqual(status, 413)
        self.assertIn("request body too large", body)

    def test_mimo_rejects_oversized_request_before_model(self) -> None:
        status, body = with_limit(mimo_asr_server.Handler)
        self.assertEqual(status, 413)
        self.assertIn("request body too large", body)


if __name__ == "__main__":
    unittest.main()
