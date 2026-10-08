# -*- coding: utf-8 -*-
"""SiliconFlow (硅基流动) cloud ASR backend.

Implements the ``POST /v1/audio/transcriptions`` API
(https://api.siliconflow.cn/docs/api/audio-transcriptions-post): a single
multipart/form-data request with exactly two fields — ``file`` (binary audio,
官方限额：单文件 <=50MB、时长 <=1h) and ``model`` (e.g.
``FunAudioLLM/SenseVoiceSmall``).  A 200 response is ``{"text": "..."}``.

默认模型 ``FunAudioLLM/SenseVoiceSmall`` 是免费模型（另有
``Qwen/Qwen3-ASR-1.7B`` 等可选，经 ``VOICE_IME_SILICONFLOW_MODEL`` 切换）；
免费档有 RPM/TPM 限流。``api.siliconflow.cn`` 大陆可直连，无需代理。

Authentication uses a single ``Authorization: Bearer {API_KEY}`` header.
The key must not be persisted in config files; it is injected at runtime
from Bitwarden Secrets Manager by ``run-engine.sh`` (see
``VOICE_IME_SILICONFLOW_API_KEY_SECRET``).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

DEFAULT_BASE_URL = "https://api.siliconflow.cn"
DEFAULT_MODEL = "FunAudioLLM/SenseVoiceSmall"
TRANSCRIBE_PATH = "/v1/audio/transcriptions"

# 上传音频的 Content-Type：引擎默认录音是 wav，其余按后缀映射
# （SiliconFlow 文档支持 wav/mp3/m4a/ogg 等常见格式）。
_MIME_BY_SUFFIX = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def selected() -> bool:
    backend = os.environ.get("VOICE_IME_ASR_BACKEND", "").strip().lower()
    return backend in {"siliconflow", "siliconflow-asr", "sf-asr"} or _env_bool(
        "VOICE_IME_SILICONFLOW_ASR", False
    )


def base_url() -> str:
    raw = (
        os.environ.get("VOICE_IME_SILICONFLOW_BASE_URL")
        or os.environ.get("SILICONFLOW_BASE_URL")
        or DEFAULT_BASE_URL
    ).strip()
    return raw.rstrip("/")


def model_id() -> str:
    """当前模型名（日志对齐用）。"""
    raw = (
        os.environ.get("VOICE_IME_SILICONFLOW_MODEL")
        or os.environ.get("SILICONFLOW_MODEL")
        or DEFAULT_MODEL
    ).strip()
    return raw or DEFAULT_MODEL


def _api_key() -> str:
    key = (
        os.environ.get("VOICE_IME_SILICONFLOW_API_KEY")
        or os.environ.get("SILICONFLOW_API_KEY")
        or ""
    )
    key = key.strip()
    if not key:
        raise RuntimeError(
            "未设置硅基流动 API Key：请到 https://cloud.siliconflow.cn 控制台创建 API Key，"
            "然后设置 VOICE_IME_SILICONFLOW_API_KEY，或运行 "
            "VOICE_IME_SILICONFLOW_API_KEY='sk-...' ./scripts/switch-siliconflow-asr.sh。"
        )
    return key


def _transcribe_url() -> str:
    url = base_url()
    if url.endswith(TRANSCRIBE_PATH):
        return url
    return url + TRANSCRIBE_PATH


def _mime_type(path: Path) -> str:
    # 引擎默认录音 wav；其余按后缀给 mime，未知后缀按二进制流兜底。
    return _MIME_BY_SUFFIX.get(path.suffix.lower(), "application/octet-stream")


def _multipart_body(model: str, audio_bytes: bytes, filename: str, mime: str) -> tuple[bytes, str]:
    """纯标准库手写 multipart/form-data（仅 model + file 两个字段）。

    boundary 用 uuid4().hex（无歧义字符），body 以 ``--{boundary}--\\r\\n``
    结尾；返回 (body, boundary)，boundary 供请求头 Content-Type 复用。
    filename 先做防御性清洗（``"``、``\\r``、``\\n`` 替换为下划线），防止
    恶意/异常文件名折断 Content-Disposition 头注入后续 multipart 段。
    """
    safe_name = filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
    boundary = uuid.uuid4().hex
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="model"\r\n'
        f"\r\n"
        f"{model}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
        f"Content-Type: {mime}\r\n"
        f"\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("ascii")
    return head + audio_bytes + tail, boundary


def _http_error_message(code: int, detail: str, retry_after: str = "") -> str:
    """按 HTTP 状态码给中文指引（detail 为响应 body 前 300 字符）。"""
    if code == 401:
        return (
            "硅基流动 ASR 认证失败（401 Invalid token）：API Key 无效或已过期，"
            "请到 https://cloud.siliconflow.cn 控制台重新生成 Key，"
            "并更新 VOICE_IME_SILICONFLOW_API_KEY。"
        )
    if code == 403:
        return (
            "硅基流动 ASR 拒绝访问（403）：该账号无权限或未完成实名认证，"
            "请到 https://cloud.siliconflow.cn 检查账号状态与权限。"
        )
    if code == 429:
        wait = f"（服务端 Retry-After：{retry_after} 秒）" if retry_after else ""
        return (
            f"硅基流动 ASR 触发限流（429，免费档 RPM/TPM 限额）{wait}：请稍后重试，"
            "或通过 VOICE_IME_SILICONFLOW_MODEL 换用其他模型。"
        )
    if code in {503, 504}:
        return f"硅基流动 ASR 模型服务过载/网关超时（HTTP {code}）：请稍后重试。{('响应：' + detail) if detail else ''}"
    if code == 400:
        return (
            f"硅基流动 ASR 请求被拒（400）：请检查音频格式（wav/mp3/m4a/ogg）"
            f"与模型名（当前 {model_id()}）。响应：{detail}"
        )
    return f"硅基流动 ASR 请求失败 HTTP {code}：{detail}"


def transcribe(wav_path: str) -> str:
    path = Path(wav_path)
    if not path.exists():
        raise RuntimeError(f"audio not found: {wav_path}")

    audio_bytes = path.read_bytes()
    # 官方限额单文件 <=50MB（时长 <=1h）；超限提前拒绝，避免整段上传后才报错。
    limit = int(float(os.environ.get("VOICE_IME_SILICONFLOW_MAX_DATA_MB", "50")) * 1024 * 1024)
    if len(audio_bytes) > limit:
        raise RuntimeError(
            f"硅基流动 ASR 音频过大（{len(audio_bytes) / (1024 * 1024):.1f}MB，"
            f"上限 {limit / (1024 * 1024):.0f}MB）：请缩短录音时长"
            "（如 VOICE_IME_MAX_RECORD_SECONDS=120）后重试。"
        )

    model = model_id()
    body, boundary = _multipart_body(model, audio_bytes, path.name, _mime_type(path))
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Authorization": f"Bearer {_api_key()}",
    }
    timeout = float(os.environ.get("VOICE_IME_SILICONFLOW_TIMEOUT", "120"))
    req = urllib.request.Request(_transcribe_url(), data=body, headers=headers, method="POST")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - user-configured API endpoint
            response_body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        # 429 限流时透传服务端 Retry-After（秒），便于用户判断等待时长。
        retry_after = ""
        if exc.code == 429 and exc.headers is not None:
            retry_after = (exc.headers.get("Retry-After") or "").strip()
        raise RuntimeError(_http_error_message(exc.code, detail, retry_after)) from exc
    except urllib.error.URLError as exc:
        # 非 HTTP 错误：DNS 解析失败/连接超时/SSL 握手失败等。
        raise RuntimeError(
            f"硅基流动 ASR 网络请求失败（{exc.reason}）：请检查网络/代理设置；"
            "api.siliconflow.cn 大陆可直连，一般无需代理。"
        ) from exc
    try:
        parsed = json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"硅基流动 ASR 返回非 JSON 结果：{response_body[:300]}") from exc
    text = str(parsed.get("text", "")).strip() if isinstance(parsed, dict) else ""
    if not text:
        raise RuntimeError("SiliconFlow ASR 返回空结果（可能录制到静音）")
    elapsed = time.monotonic() - started
    print(
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] SiliconFlow ASR model={model} "
        f"elapsed={elapsed:.3f}s chars={len(text)}",
        flush=True,
    )
    return text


__all__ = ["base_url", "model_id", "selected", "transcribe"]
