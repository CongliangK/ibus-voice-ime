# -*- coding: utf-8 -*-
"""Volcano Engine (火山引擎/豆包) bigmodel recording-file ASR backend.

Implements the "大模型录音文件识别 极速版" API
(https://www.volcengine.com/docs/6561/1631584, resource id
``volc.bigasr.auc_turbo``).  Unlike the standard version (1354868), the turbo
version accepts ``audio.data`` as base64-encoded audio content, so a local
recording from this input method can be transcribed without a public audio URL.

Flow: submit task (returns X-Api-Request-Id) -> poll query until the response
header ``X-Api-Status-Code`` is ``20000000`` -> return ``result.text``.

Authentication uses a single ``X-Api-Key``.  The key must not be persisted in
config files; it is injected at runtime from Bitwarden Secrets Manager by
``run-engine.sh`` (see ``VOICE_IME_VOLC_API_KEY_SECRET``).
"""
from __future__ import annotations

import base64
import json
import os

from ibus_voice_ime import config
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://openspeech.bytedance.com"
DEFAULT_RESOURCE_ID = "volc.bigasr.auc_turbo"
DEFAULT_MODEL_NAME = "bigmodel"
# X-Api-Status-Code values shared by the standard/turbo bigmodel APIs.
STATUS_SUCCESS = "20000000"
STATUS_PROCESSING = "20000001"
STATUS_QUEUED = "20000002"
# 20000003 = silent audio; per the standard-version docs the client should not
# re-query, it should re-submit.  For an input-method clip we treat it as empty.
STATUS_SILENT = "20000003"


def _env_bool(name: str, default: bool) -> bool:
    return config.env_bool(name, default)


def _env_int(name: str, default: int) -> int:
    return config.env_int(name, default)


def selected() -> bool:
    backend = config.env_str("VOICE_IME_ASR_BACKEND", "").strip().lower()
    return backend in {
        "volc",
        "volc-asr",
        "volc-engine-asr",
        "volc-bigmodel",
        "volc-bigmodel-asr",
        "doubao-asr",
        "doubao-bigmodel-asr",
    } or _env_bool("VOICE_IME_VOLC_BIGMODEL_ASR", False)


def base_url() -> str:
    raw = (
        config.env_str("VOICE_IME_VOLC_BIGMODEL_BASE_URL", None)
        or os.environ.get("VOLC_BIGMODEL_BASE_URL")
        or os.environ.get("VOLC_ASR_BASE_URL")
        or DEFAULT_BASE_URL
    ).strip()
    return raw.rstrip("/")


def resource_id() -> str:
    return config.env_str("VOICE_IME_VOLC_BIGMODEL_RESOURCE_ID", DEFAULT_RESOURCE_ID).strip() or DEFAULT_RESOURCE_ID


def model_name() -> str:
    return config.env_str("VOICE_IME_VOLC_BIGMODEL_MODEL_NAME", DEFAULT_MODEL_NAME).strip() or DEFAULT_MODEL_NAME


def model_id() -> str:
    """Stable identifier exposed for logging parity with other backends."""
    return f"{resource_id()}:{model_name()}"


def _api_key() -> str:
    key = (
        os.environ.get("VOICE_IME_VOLC_API_KEY")
        or os.environ.get("VOLC_BIGMODEL_API_KEY")
        or os.environ.get("VOLC_ASR_API_KEY")
        or ""
    )
    key = key.strip()
    if not key:
        raise RuntimeError(
            "未设置火山引擎 ASR API Key：请设置 VOICE_IME_VOLC_API_KEY，或通过 bws 注入"
            " VOLC_BIGMODEL_ASR_API_KEY（运行时由 run-engine.sh 的 BWS 包装注入）。"
        )
    return key


def _app_key_for_uid() -> str:
    """Optional App Key used only as ``user.uid`` (not used for auth on turbo)."""
    return (
        os.environ.get("VOICE_IME_VOLC_APP_KEY")
        or os.environ.get("VOLC_BIGMODEL_APP_KEY")
        or os.environ.get("VOICE_IME_VOLC_API_KEY")
        or ""
    ).strip() or "voice-ime"


def _submit_url() -> str:
    url = base_url()
    if url.endswith("/api/v3/auc/bigmodel/submit"):
        return url
    return url + "/api/v3/auc/bigmodel/submit"


def _query_url() -> str:
    url = base_url()
    if url.endswith("/api/v3/auc/bigmodel/query"):
        return url
    return url + "/api/v3/auc/bigmodel/query"


def _audio_format(path: Path) -> str:
    lower = path.suffix.lower().lstrip(".")
    if lower in {"wav", "mp3", "m4a", "aac", "ogg", "opus", "pcm"}:
        return "wav" if lower == "wav" else lower
    # The engine records wav by default.
    return "wav"


def _language() -> str | None:
    """Resolve the audio.language field.

    The bigmodel API documents that leaving ``language`` empty enables the
    widest recognition coverage (Mandarin + English + several dialects), which
    is exactly what a Chinese-English mixed dictation IME wants.  Setting it to
    ``zh-CN`` would restrict to Mandarin and degrade English mixing.  We keep
    the default empty (do not send the field at all) and only override via the
    VOICE_IME_VOLC_BIGMODEL_LANGUAGE env var for users who want a single
    language pinned.
    """
    raw = config.env_str("VOICE_IME_VOLC_BIGMODEL_LANGUAGE", "").strip()
    if not raw or raw.lower() in {"auto", "none", "mixed", "zh-mix"}:
        return None  # empty -> best Chinese/English/dialect mixing
    mapping = {
        "zh": "zh-CN",
        "cn": "zh-CN",
        "zh-cn": "zh-CN",
        "chinese": "zh-CN",
        "mandarin": "zh-CN",
        "en": "en-US",
        "english": "en-US",
        "ja": "ja-JP",
        "ja-jp": "ja-JP",
        "japanese": "ja-JP",
        "ko": "ko-KR",
        "yue": "yue-CN",
        "cantonese": "yue-CN",
    }
    return mapping.get(raw.lower(), raw)


def _hotwords() -> list[str]:
    """Build the hotword list for ``corpus.context`` from the shared voice terms.

    Reuses the same explicit dictionary (``voice-dictionary.txt``) and learned
    English memory that Qwen3-ASR / Whisper already bias on.  The bigmodel API
    accepts up to 5000 hotwords per request via direct pass-through, with no
    console word-table setup required.
    """
    if not _env_bool("VOICE_IME_VOLC_BIGMODEL_HOTWORDS", True):
        return []
    try:
        from ibus_voice_ime.memory import voice_terms  # local import to avoid hard dependency at import time
    except Exception:
        return []
    terms = voice_terms.load_terms()
    words: list[str] = []
    seen: set[str] = set()
    for term in terms:
        signals = [term.canonical, *term.aliases, *term.confusions]
        for sig in signals:
            sig = sig.strip()
            if not sig or len(sig) > 32:
                continue
            key = sig.lower()
            if key in seen:
                continue
            seen.add(key)
            words.append(sig)
            if len(words) >= _env_int("VOICE_IME_VOLC_BIGMODEL_HOTWORDS_MAX", 5000):
                return words
    return words


def _corpus() -> dict[str, str] | None:
    """Build the ``corpus`` field: hotword direct-pass + optional word tables.

    Priority (per Volcano docs): hotword direct-pass (context) executes before
    the console word tables (boosting_table_name / correct_table_name).
    """
    corpus: dict[str, str] = {}
    hotwords = _hotwords()
    if hotwords:
        # Documented format: {"hotwords":[{"word":"..."}, ...]}
        context = json.dumps({"hotwords": [{"word": w} for w in hotwords]}, ensure_ascii=False)
        corpus["context"] = context
    boosting = config.env_str("VOICE_IME_VOLC_BIGMODEL_BOOSTING_TABLE", "").strip()
    if boosting:
        corpus["boosting_table_name"] = boosting
    correct = config.env_str("VOICE_IME_VOLC_BIGMODEL_CORRECT_TABLE", "").strip()
    if correct:
        corpus["correct_table_name"] = correct
    return corpus or None


def _request_options(*, skip_llm: bool = False) -> dict[str, Any]:
    """Build the ``request`` field of the submit body from env toggles.

    Defaults are tuned for a Chinese-English mixed dictation IME:
    - enable_punc + enable_itn on (punctuation + number normalization)
    - enable_ddc on by default (cloud-side semantic smoothing; voice.py
      suppresses the local filler layer when DDC is active)
    - no language pin (empty = best Mandarin/English/dialect mixing)
    - corpus.context hotwords from the shared voice dictionary

    ``skip_llm=True``（原文语音输入 Ctrl+Alt+B）强制 ``enable_ddc=False``：
    云端语义平滑也是 LLM 改写的一种，原文模式必须拿到未经改动的识别结果，
    该覆盖优先于 VOICE_IME_VOLC_BIGMODEL_ENABLE_DDC 环境变量。
    """
    options: dict[str, Any] = {
        "model_name": model_name(),
        "enable_itn": _env_bool("VOICE_IME_VOLC_BIGMODEL_ENABLE_ITN", True),
        "enable_punc": _env_bool("VOICE_IME_VOLC_BIGMODEL_ENABLE_PUNC", True),
    }
    if _env_bool("VOICE_IME_VOLC_BIGMODEL_SHOW_UTTERANCES", True):
        options["show_utterances"] = True
    # DDC (semantic smoothing/dislfluency removal) defaults ON for this backend:
    # the user prefers the cloud bigmodel's smoothing over the local
    # text_postprocess filler removal.  When DDC is on, the local
    # VOICE_IME_REMOVE_FILLERS path is auto-suppressed in voice.py for this
    # backend to avoid double-processing.  The raw-transcript path (skip_llm)
    # forces it off so the transcript stays unmodified.
    options["enable_ddc"] = False if skip_llm else _env_bool("VOICE_IME_VOLC_BIGMODEL_ENABLE_DDC", True)
    corpus = _corpus()
    if corpus is not None:
        options["corpus"] = corpus
    return options


def _submit(task_id: str, audio_b64: str, audio_format: str, *, skip_llm: bool = False) -> str:
    """Submit the recognition task; return the X-Tt-Logid for query tracing."""
    headers = {
        "Content-Type": "application/json",
        "X-Api-Key": _api_key(),
        "X-Api-Resource-Id": resource_id(),
        "X-Api-Request-Id": task_id,
        "X-Api-Sequence": "-1",
    }
    audio_block: dict[str, Any] = {
        "data": audio_b64,
        "format": audio_format,
        "codec": "raw",
    }
    lang = _language()
    if lang:
        audio_block["language"] = lang
    body = {
        "user": {"uid": _app_key_for_uid()},
        "audio": audio_block,
        "request": _request_options(skip_llm=skip_llm),
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(_submit_url(), data=data, headers=headers, method="POST")
    timeout = config.env_float("VOICE_IME_VOLC_BIGMODEL_SUBMIT_TIMEOUT", 30.0)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - user-configured API endpoint
            status = resp.headers.get("X-Api-Status-Code", "")
            message = resp.headers.get("X-Api-Message", "")
            logid = resp.headers.get("X-Tt-Logid", "")
            resp.read()  # drain
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
        except Exception:
            pass
        logid = exc.headers.get("X-Tt-Logid", "") if exc.headers else ""
        raise RuntimeError(
            f"火山引擎 ASR 提交失败 HTTP {exc.code} (X-Api-Status-Code={exc.headers.get('X-Api-Status-Code', '') if exc.headers else ''} "
            f"logid={logid}): {detail}"
        ) from exc
    if status != STATUS_SUCCESS:
        raise RuntimeError(f"火山引擎 ASR 提交未成功：X-Api-Status-Code={status} message={message} logid={logid}")
    return logid


def _query(task_id: str, logid: str) -> tuple[str, str, str]:
    """Poll once; return (status_code, message, response_body)."""
    headers = {
        "Content-Type": "application/json",
        "X-Api-Key": _api_key(),
        "X-Api-Resource-Id": resource_id(),
        "X-Api-Request-Id": task_id,
    }
    if logid:
        headers["X-Tt-Logid"] = logid
    data = json.dumps({}).encode("utf-8")
    req = urllib.request.Request(_query_url(), data=data, headers=headers, method="POST")
    timeout = config.env_float("VOICE_IME_VOLC_BIGMODEL_QUERY_TIMEOUT", 30.0)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - user-configured API endpoint
            status = resp.headers.get("X-Api-Status-Code", "")
            message = resp.headers.get("X-Api-Message", "")
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
        except Exception:
            pass
        qlogid = exc.headers.get("X-Tt-Logid", logid) if exc.headers else logid
        raise RuntimeError(
            f"火山引擎 ASR 查询失败 HTTP {exc.code} (X-Api-Status-Code={exc.headers.get('X-Api-Status-Code', '') if exc.headers else ''} "
            f"logid={qlogid}): {detail}"
        ) from exc
    return status, message, body


def _extract_text(parsed: dict[str, Any]) -> str:
    result = parsed.get("result")
    if isinstance(result, dict):
        text = result.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
        utterances = result.get("utterances")
        if isinstance(utterances, list):
            pieces: list[str] = []
            for utt in utterances:
                if isinstance(utt, dict):
                    t = utt.get("text")
                    if isinstance(t, str):
                        pieces.append(t)
            joined = "".join(pieces).strip()
            if joined:
                return joined
    if isinstance(parsed.get("text"), str):
        return str(parsed["text"]).strip()
    return ""


def transcribe(wav_path: str, *, skip_llm: bool = False) -> str:
    """Submit the audio file and poll until the transcript is ready.

    ``skip_llm=True``（原文语音输入 Ctrl+Alt+B）强制关闭云端 DDC 语义平滑；
    参数一路以函数形参传递到 ``_request_options``，不用环境变量（线程安全）。
    """
    path = Path(wav_path)
    if not path.exists():
        raise RuntimeError(f"audio not found: {wav_path}")

    audio_bytes = path.read_bytes()
    audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
    # The turbo API does not document a hard base64 size cap, but keep a sane
    # guard so a runaway recording does not produce an enormous single request.
    limit = int(config.env_float("VOICE_IME_VOLC_BIGMODEL_MAX_DATA_MB", 25.0) * 1024 * 1024)
    if len(audio_b64.encode("utf-8")) > limit:
        raise RuntimeError("火山引擎 ASR 音频过大；请缩短录音时长或改用本地后端。")

    task_id = str(uuid.uuid4())
    started = time.monotonic()
    logid = _submit(task_id, audio_b64, _audio_format(path), skip_llm=skip_llm)

    interval = max(0.3, config.env_float("VOICE_IME_VOLC_BIGMODEL_POLL_INTERVAL", 1.0))
    max_wait = max(5.0, config.env_float("VOICE_IME_VOLC_BIGMODEL_TOTAL_TIMEOUT", 120.0))
    deadline = started + max_wait
    last_message = ""
    while True:
        status, message, body = _query(task_id, logid)
        last_message = message
        if status == STATUS_SUCCESS:
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"火山引擎 ASR 返回非 JSON 结果：{body[:500]}") from exc
            text = _extract_text(parsed)
            if not text:
                raise RuntimeError("火山引擎 ASR 返回空结果")
            elapsed = time.monotonic() - started
            print(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 火山引擎 bigmodel ASR resource={resource_id()} "
                f"model={model_name()} elapsed={elapsed:.3f}s chars={len(text)}",
                flush=True,
            )
            return text
        if status == STATUS_SILENT:
            raise RuntimeError("火山引擎 ASR 判定为静音音频（20000003）")
        if status in {STATUS_PROCESSING, STATUS_QUEUED}:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"火山引擎 ASR 轮询超时：最后状态 X-Api-Status-Code={status} message={last_message}"
                )
            time.sleep(interval)
            continue
        raise RuntimeError(
            f"火山引擎 ASR 任务失败：X-Api-Status-Code={status} message={message} body={body[:500]}"
        )


def cloud_smoothing_on() -> bool:
    """Whether this backend is currently performing cloud-side semantic smoothing.

    When True, the caller (voice.py) should skip the local filler-removal pass
    so the same disfluencies are not processed twice.  Only meaningful when the
    Volcano backend is actually selected.
    """
    return selected() and _env_bool("VOICE_IME_VOLC_BIGMODEL_ENABLE_DDC", True)


__all__ = ["base_url", "cloud_smoothing_on", "model_id", "resource_id", "selected", "transcribe"]
