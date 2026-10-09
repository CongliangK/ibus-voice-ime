#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small local HTTP sidecar for Xiaomi MiMo-V2.5-ASR.

MiMo-V2.5-ASR ships its inference code in the upstream GitHub repository and
uses a separate MiMo-Audio-Tokenizer model.  This sidecar keeps those heavy
runtime dependencies out of the IBus engine process and loads the model once for
repeated dictation requests.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ibus_voice_ime import config
from ibus_voice_ime.asr import sidecar_http

_MODEL = None
_MODEL_ID = ""
_TOKENIZER_ID = ""


def _language_tag(language: Any) -> str:
    raw = str(language or "").strip().lower()
    if not raw or raw in {"auto", "none", "detect", "automatic"}:
        return ""
    if raw in {"zh", "cn", "chinese", "mandarin", "中文", "汉语", "普通话"}:
        return "<chinese>"
    if raw in {"en", "english", "英语"}:
        return "<english>"
    # MiMo's public API documents only Chinese / English / Auto.  For Chinese
    # dialects such as Cantonese, Auto is usually preferable.
    if raw in {"yue", "cantonese", "粤语", "广东话"}:
        return ""
    return ""


def _load_model(model: str, tokenizer: str, source: str) -> Any:
    if source:
        sys.path.insert(0, str(Path(source).expanduser()))

    try:
        from src.mimo_audio.mimo_audio import MimoAudio  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on optional runtime
        raise RuntimeError(
            "无法导入 MiMo-V2.5-ASR 推理代码；请先运行 scripts/setup-mimo-asr.sh，"
            "或设置 VOICE_IME_MIMO_ASR_SOURCE 指向 XiaomiMiMo/MiMo-V2.5-ASR 源码目录。"
        ) from exc

    device = config.env_str("VOICE_IME_MIMO_ASR_DEVICE", "").strip() or None
    print(
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
        f"MiMo-ASR loading model={model} tokenizer={tokenizer} device={device or 'auto'}",
        flush=True,
    )
    started = time.monotonic()
    loaded = MimoAudio(model, tokenizer, device=device)
    print(
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
        f"MiMo-ASR loaded model={model} elapsed={time.monotonic() - started:.3f}s",
        flush=True,
    )
    return loaded


def _send_json(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _mimo_audio(payload: dict[str, Any]) -> str:
    audio = str(payload.get("audio") or payload.get("wav") or "").strip()
    if not audio:
        raise sidecar_http.RequestError(400, "missing audio")
    if not Path(audio).exists():
        raise sidecar_http.RequestError(400, f"audio not found: {audio}")
    return audio


def _handle_transcribe(handler: BaseHTTPRequestHandler) -> None:
    payload = sidecar_http.read_json_payload(handler.headers, handler.rfile)
    audio = _mimo_audio(payload)
    language = payload.get("language")
    audio_tag = str(payload.get("audio_tag") or "").strip() or _language_tag(language)
    started = time.monotonic()
    text = _MODEL.asr_sft(audio, audio_tag=audio_tag)
    elapsed = time.monotonic() - started
    item = {"text": str(text or "").strip(), "language": language, "audio_tag": audio_tag, "elapsed": elapsed}
    print(
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] MiMo-ASR transcribed "
        f"model={_MODEL_ID} audio_tag={audio_tag or 'auto'} elapsed={elapsed:.3f}s "
        f"chars={len(item['text'])}",
        flush=True,
    )
    _send_json(handler, 200, item)


class Handler(BaseHTTPRequestHandler):
    server_version = "MiMoASRSidecar/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {self.address_string()} {fmt % args}", flush=True)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in {"", "/health"}:
            _send_json(self, 200, {"status": "ok", "model": _MODEL_ID, "tokenizer": _TOKENIZER_ID})
            return
        _send_json(self, 404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/transcribe":
            _send_json(self, 404, {"error": "not found"})
            return
        try:
            _handle_transcribe(self)
        except sidecar_http.RequestError as exc:
            _send_json(self, exc.status, {"error": exc.message})
        except Exception as exc:
            import traceback

            traceback.print_exc()
            _send_json(self, 500, {"error": str(exc)})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--tokenizer", required=True)
    parser.add_argument("--source", default=config.env_str("VOICE_IME_MIMO_ASR_SOURCE", ""))
    parser.add_argument("--host", default=config.env_str("VOICE_IME_MIMO_ASR_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=config.env_int("VOICE_IME_MIMO_ASR_PORT", 18082))
    args = parser.parse_args()

    global _MODEL, _MODEL_ID, _TOKENIZER_ID
    _MODEL_ID = args.model
    _TOKENIZER_ID = args.tokenizer
    _MODEL = _load_model(args.model, args.tokenizer, args.source)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] MiMo-ASR server listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
