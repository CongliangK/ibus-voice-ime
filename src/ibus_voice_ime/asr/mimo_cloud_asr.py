# -*- coding: utf-8 -*-
"""Cloud Xiaomi MiMo-V2.5-ASR backend using the OpenAI-compatible API."""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://token-plan-cn.xiaomimimo.com/v1"
DEFAULT_MODEL = "mimo-v2.5-asr"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def selected() -> bool:
    backend = os.environ.get("VOICE_IME_ASR_BACKEND", "").strip().lower()
    return backend in {"mimo-cloud", "mimo-cloud-asr", "mimo-api", "mimo-api-asr", "mimo-tokenplan-asr"} or _env_bool(
        "VOICE_IME_MIMO_CLOUD_ASR", False
    )


def base_url() -> str:
    raw = (
        os.environ.get("VOICE_IME_MIMO_CLOUD_BASE_URL")
        or os.environ.get("VOICE_IME_MIMO_BASE_URL")
        or os.environ.get("MIMO_BASE_URL")
        or os.environ.get("MIMO_API_BASE_URL")
        or DEFAULT_BASE_URL
    ).strip()
    return raw.rstrip("/")


def model_id() -> str:
    return os.environ.get("VOICE_IME_MIMO_CLOUD_ASR_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def _api_key() -> str:
    key = (
        os.environ.get("VOICE_IME_MIMO_API_KEY")
        or os.environ.get("MIMO_API_KEY")
        or os.environ.get("XIAOMI_TOKEN_PLAN_CN_API_KEY")
        or ""
    )
    key = key.strip()
    if not key:
        raise RuntimeError("未设置 MiMo API Key：请设置 VOICE_IME_MIMO_API_KEY、MIMO_API_KEY，或通过 bws 注入 XIAOMI_TOKEN_PLAN_CN_API_KEY。")
    return key


def _endpoint() -> str:
    url = base_url()
    if url.endswith("/chat/completions"):
        return url
    return url + "/chat/completions"


def _language() -> str:
    raw = (
        os.environ.get("VOICE_IME_MIMO_CLOUD_ASR_LANGUAGE")
        or os.environ.get("VOICE_IME_MIMO_ASR_LANGUAGE")
        or "auto"
    ).strip().lower()
    mapping = {
        "": "auto",
        "auto": "auto",
        "none": "auto",
        "detect": "auto",
        "zh": "zh",
        "cn": "zh",
        "chinese": "zh",
        "mandarin": "zh",
        "中文": "zh",
        "普通话": "zh",
        "en": "en",
        "english": "en",
        "英语": "en",
    }
    return mapping.get(raw, "auto")


def _mime_type(path: Path) -> str:
    lower = path.suffix.lower()
    if lower == ".wav":
        return "audio/wav"
    if lower == ".mp3":
        return "audio/mpeg"
    guessed, _ = mimetypes.guess_type(str(path))
    if guessed in {"audio/wav", "audio/mpeg", "audio/mp3"}:
        return guessed
    # The engine records wav by default.
    return "audio/wav"


def _extract_text(result: dict[str, Any]) -> str:
    choices = result.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                pieces: list[str] = []
                for item in content:
                    if isinstance(item, dict):
                        text = item.get("text") or item.get("content")
                        if isinstance(text, str):
                            pieces.append(text)
                    elif isinstance(item, str):
                        pieces.append(item)
                return "".join(pieces).strip()
        text = choices[0].get("text") if isinstance(choices[0], dict) else None
        if isinstance(text, str):
            return text.strip()
    if isinstance(result.get("text"), str):
        return str(result["text"]).strip()
    return ""


def transcribe(wav_path: str) -> str:
    path = Path(wav_path)
    if not path.exists():
        raise RuntimeError(f"audio not found: {wav_path}")

    audio_bytes = path.read_bytes()
    data_url = f"data:{_mime_type(path)};base64," + base64.b64encode(audio_bytes).decode("ascii")
    # Official docs say the Base64 data URL should not exceed 10 MB.
    limit = int(float(os.environ.get("VOICE_IME_MIMO_CLOUD_ASR_MAX_DATA_MB", "10")) * 1024 * 1024)
    if len(data_url.encode("utf-8")) > limit:
        raise RuntimeError(
            "MiMo 云端 ASR 音频超过 10MB data URL 限制；请缩短录音时长"
            "（如 VOICE_IME_MAX_RECORD_SECONDS=120）或改用本地后端。"
        )

    payload: dict[str, Any] = {
        "model": model_id(),
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {"data": data_url},
                    }
                ],
            }
        ],
        "asr_options": {"language": _language()},
        "stream": False,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    key = _api_key()
    auth_header = os.environ.get("VOICE_IME_MIMO_CLOUD_AUTH_HEADER", "api-key").strip().lower()
    if auth_header in {"authorization", "bearer"}:
        headers["Authorization"] = f"Bearer {key}"
    else:
        headers["api-key"] = key

    timeout = float(os.environ.get("VOICE_IME_MIMO_CLOUD_ASR_TIMEOUT", "120"))
    started = time.monotonic()
    req = urllib.request.Request(_endpoint(), data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - user-configured API endpoint
            response_body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"MiMo 云端 ASR HTTP {exc.code}: {detail}") from exc
    result = json.loads(response_body)
    if result.get("error"):
        raise RuntimeError(str(result["error"]))
    text = _extract_text(result)
    if not text:
        raise RuntimeError("MiMo 云端 ASR 返回空结果")
    elapsed = time.monotonic() - started
    print(
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] MiMo cloud ASR model={model_id()} "
        f"base_url={base_url()} language={_language()} elapsed={elapsed:.3f}s chars={len(text)}",
        flush=True,
    )
    return text


__all__ = ["base_url", "model_id", "selected", "transcribe"]
