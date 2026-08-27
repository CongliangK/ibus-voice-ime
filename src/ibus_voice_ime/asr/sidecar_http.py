"""Shared HTTP request validation for local ASR sidecars."""

from __future__ import annotations

import json
import os
from typing import Any, BinaryIO, Mapping
from urllib.parse import urlparse

DEFAULT_MAX_BODY_BYTES = 1_048_576


class RequestError(ValueError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _max_body_bytes() -> int:
    raw = os.environ.get("VOICE_IME_ASR_SIDECAR_MAX_BODY_BYTES", "")
    if not raw.strip():
        return DEFAULT_MAX_BODY_BYTES
    try:
        return max(1, int(raw))
    except ValueError as exc:
        raise RequestError(500, "invalid sidecar body limit") from exc


def _content_length(headers: Mapping[str, str]) -> int:
    raw = str(headers.get("Content-Length", "0")).strip() or "0"
    try:
        length = int(raw)
    except ValueError as exc:
        raise RequestError(400, "invalid content length") from exc
    if length < 0:
        raise RequestError(400, "invalid content length")
    if length > _max_body_bytes():
        raise RequestError(413, "request body too large")
    return length


def read_json_payload(headers: Mapping[str, str], stream: BinaryIO) -> dict[str, Any]:
    length = _content_length(headers)
    try:
        payload = json.loads(stream.read(length).decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RequestError(400, "invalid json") from exc
    if not isinstance(payload, dict):
        raise RequestError(400, "json payload must be an object")
    return payload


def is_remote_url(value: str) -> bool:
    scheme = urlparse(value).scheme.lower()
    return scheme in {"http", "https"}
