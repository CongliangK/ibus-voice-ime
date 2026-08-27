# -*- coding: utf-8 -*-
"""Manage the optional Xiaomi MiMo-V2.5-ASR sidecar server."""
from __future__ import annotations

import atexit
import json
import os
import shlex
import subprocess
import time
import urllib.error
import urllib.request

from ibus_voice_ime.log_trim import trim_to_last_lines
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[3]
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18082
_PROCESS: subprocess.Popen | None = None
_PROCESS_KEY: tuple[str, int, str, str, str] | None = None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


def selected() -> bool:
    backend = os.environ.get("VOICE_IME_ASR_BACKEND", "").strip().lower()
    return backend in {"mimo", "mimo-asr", "mimo-v2.5-asr", "mimo-v25-asr"} or _env_bool("VOICE_IME_MIMO_ASR", False)


def _host() -> str:
    return os.environ.get("VOICE_IME_MIMO_ASR_HOST", DEFAULT_HOST)


def _port() -> int:
    return _env_int("VOICE_IME_MIMO_ASR_PORT", DEFAULT_PORT)


def base_url() -> str:
    return f"http://{_host()}:{_port()}"


def _python() -> str:
    explicit = os.environ.get("VOICE_IME_MIMO_ASR_PYTHON", "").strip()
    if explicit:
        return str(Path(explicit).expanduser())
    venv_python = ROOT_DIR / ".venv-mimo-asr" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return os.environ.get("PYTHON", "python3")


def _models_base() -> Path:
    return Path(os.environ.get("VOICE_IME_MIMO_ASR_MODELS_DIR", ROOT_DIR / "vendor" / "models" / "mimo-asr")).expanduser()


def _local_model_dir(name: str) -> Path | None:
    base = _models_base()
    candidates = [base / "MiMo-V2.5-ASR", base / "mimo-v2.5-asr", base / "MiMo-V25-ASR"]
    for path in candidates:
        if path.exists():
            return path
    if name and Path(name).expanduser().exists():
        return Path(name).expanduser()
    return None


def _local_tokenizer_dir() -> Path | None:
    base = _models_base()
    candidates = [base / "MiMo-Audio-Tokenizer", base / "mimo-audio-tokenizer"]
    for path in candidates:
        if path.exists():
            return path
    return None


def model_id() -> str:
    explicit_path = os.environ.get("VOICE_IME_MIMO_ASR_MODEL_PATH", "").strip()
    if explicit_path:
        return str(Path(explicit_path).expanduser())
    raw = os.environ.get("VOICE_IME_MIMO_ASR_MODEL", "XiaomiMiMo/MiMo-V2.5-ASR").strip()
    local = _local_model_dir(raw)
    if local is not None:
        return str(local)
    return raw or "XiaomiMiMo/MiMo-V2.5-ASR"


def tokenizer_id() -> str:
    explicit_path = os.environ.get("VOICE_IME_MIMO_ASR_TOKENIZER_PATH", "").strip()
    if explicit_path:
        return str(Path(explicit_path).expanduser())
    raw = os.environ.get("VOICE_IME_MIMO_ASR_TOKENIZER", "XiaomiMiMo/MiMo-Audio-Tokenizer").strip()
    local = _local_tokenizer_dir()
    if local is not None:
        return str(local)
    return raw or "XiaomiMiMo/MiMo-Audio-Tokenizer"


def source_dir() -> str:
    explicit = os.environ.get("VOICE_IME_MIMO_ASR_SOURCE", "").strip()
    if explicit:
        return str(Path(explicit).expanduser())
    local = ROOT_DIR / "vendor" / "MiMo-V2.5-ASR"
    if local.exists():
        return str(local)
    return ""


def _health() -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(base_url() + "/health", timeout=0.5) as resp:  # noqa: S310 - local endpoint
            if not (200 <= resp.status < 500):
                return None
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def is_ready() -> bool:
    return _health() is not None


def _server_matches(model: str, tokenizer: str) -> bool:
    info = _health()
    if not info:
        return False
    return str(info.get("model") or "") == model and str(info.get("tokenizer") or "") == tokenizer


def _log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def ensure_server() -> str:
    global _PROCESS, _PROCESS_KEY
    url = base_url()
    model = model_id()
    tokenizer = tokenizer_id()
    source = source_dir()
    key = (_host(), _port(), model, tokenizer, source)
    if _server_matches(model, tokenizer) and (_PROCESS_KEY is None or _PROCESS_KEY == key):
        return url
    if is_ready() and not _server_matches(model, tokenizer):
        raise RuntimeError(
            "MiMo-ASR sidecar 已在运行但模型与当前配置不同；"
            "请先执行 pkill -f mimo_asr_server.py，或使用 scripts/switch-mimo-asr.sh 切换。"
        )

    if _PROCESS is not None and _PROCESS.poll() is None and _PROCESS_KEY == key:
        return url

    log_dir = Path(os.environ.get("VOICE_IME_LOG_DIR", "~/.local/share/ibus-voice-ime")).expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "mimo-asr-server.log"
    trim_to_last_lines(log_file)  # 日志保留：spawn 前裁剪到最近 N 行
    cmd = [
        _python(),
        str(Path(__file__).resolve().parent / "mimo_asr_server.py"),
        "--model", model,
        "--tokenizer", tokenizer,
        "--host", _host(),
        "--port", str(_port()),
    ]
    if source:
        cmd.extend(["--source", source])
    _log("ASR starting MiMo-ASR sidecar: " + " ".join(shlex.quote(x) for x in cmd))
    log = log_file.open("a", encoding="utf-8")
    env = os.environ.copy()
    _PROCESS = subprocess.Popen(  # noqa: S603 - local sidecar command
        cmd,
        stdout=log,
        stderr=subprocess.STDOUT,
        cwd=str(ROOT_DIR),
        env=env,
        start_new_session=True,
    )
    _PROCESS_KEY = key

    timeout = float(os.environ.get("VOICE_IME_MIMO_ASR_START_TIMEOUT", "300"))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _PROCESS.poll() is not None:
            raise RuntimeError(f"MiMo-ASR sidecar 启动失败，详见日志：{log_file}")
        if is_ready():
            _log(f"ASR MiMo-ASR sidecar ready: {url}")
            return url
        time.sleep(0.5)
    raise RuntimeError(f"MiMo-ASR sidecar 启动超时，详见日志：{log_file}")


def transcribe(wav_path: str) -> str:
    url = ensure_server()
    language = os.environ.get("VOICE_IME_MIMO_ASR_LANGUAGE", "auto")
    audio_tag = os.environ.get("VOICE_IME_MIMO_ASR_AUDIO_TAG", "").strip()
    payload = {"audio": wav_path, "language": language}
    if audio_tag:
        payload["audio_tag"] = audio_tag
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    timeout = float(os.environ.get("VOICE_IME_MIMO_ASR_TIMEOUT", "300"))
    req = urllib.request.Request(
        url + "/transcribe",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local endpoint
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"MiMo-ASR HTTP {exc.code}: {detail}") from exc
    result: dict[str, Any] = json.loads(body)
    if result.get("error"):
        raise RuntimeError(str(result["error"]))
    return str(result.get("text") or "").strip()


def shutdown() -> None:
    global _PROCESS, _PROCESS_KEY
    proc = _PROCESS
    _PROCESS = None
    _PROCESS_KEY = None
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


atexit.register(shutdown)


__all__ = ["base_url", "ensure_server", "is_ready", "model_id", "selected", "shutdown", "tokenizer_id", "transcribe"]
