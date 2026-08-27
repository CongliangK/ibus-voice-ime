# -*- coding: utf-8 -*-
"""Manage the optional Qwen3-ASR sidecar server."""
from __future__ import annotations

import atexit
import json
import os
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ibus_voice_ime.memory import voice_terms

ROOT_DIR = Path(__file__).resolve().parents[3]
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18081
DEFAULT_MODEL_ALIAS = "0.6b"
_PROCESS: subprocess.Popen | None = None
_PROCESS_KEY: tuple[str, int, str] | None = None


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
    return backend in {"qwen", "qwen3", "qwen3-asr", "qwen-asr"} or _env_bool("VOICE_IME_QWEN_ASR", False)


def _host() -> str:
    return os.environ.get("VOICE_IME_QWEN_ASR_HOST", DEFAULT_HOST)


def _port() -> int:
    return _env_int("VOICE_IME_QWEN_ASR_PORT", DEFAULT_PORT)


def base_url() -> str:
    return f"http://{_host()}:{_port()}"


def _python() -> str:
    explicit = os.environ.get("VOICE_IME_QWEN_ASR_PYTHON", "").strip()
    if explicit:
        return str(Path(explicit).expanduser())
    venv_python = ROOT_DIR / ".venv-qwen-asr" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return os.environ.get("PYTHON", "python3")


def _local_model_dir(name: str) -> Path | None:
    base = ROOT_DIR / "vendor" / "models" / "qwen3-asr"
    candidates = []
    if "1.7" in name:
        candidates.extend([base / "Qwen3-ASR-1.7B", base / "1.7B", base / "qwen3-asr-1.7b"])
    elif "0.6" in name or "0_6" in name:
        candidates.extend([base / "Qwen3-ASR-0.6B", base / "0.6B", base / "qwen3-asr-0.6b"])
    for path in candidates:
        if path.exists():
            return path
    return None


def model_id() -> str:
    explicit_path = os.environ.get("VOICE_IME_QWEN_ASR_MODEL_PATH", "").strip()
    if explicit_path:
        return str(Path(explicit_path).expanduser())

    raw = os.environ.get("VOICE_IME_QWEN_ASR_MODEL", DEFAULT_MODEL_ALIAS).strip() or DEFAULT_MODEL_ALIAS
    lower = raw.lower()
    local = _local_model_dir(lower)
    if local is not None:
        return str(local)
    if "1.7" in lower:
        return "Qwen/Qwen3-ASR-1.7B"
    if "0.6" in lower or "0_6" in lower:
        return "Qwen/Qwen3-ASR-0.6B"
    return raw


def _url_ok(url: str, timeout: float = 0.3) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - local endpoint
            return 200 <= resp.status < 500
    except Exception:
        return False


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


def _is_downgrade_of(running: str, desired: str) -> bool:
    """Whether ``running`` is a legitimate smaller-size downgrade of ``desired``.

    The Qwen3-ASR sidecar probes free VRAM at request time and may auto-load a
    smaller model (e.g. 0.6B when 1.7B would OOM).  From the runtime's perspective
    that sidecar is still healthy and serving the same family, so we treat the
    1.7B-requested / 0.6B-running case as a match and reuse the process instead
    of killing and respawning it (which would just OOM again).
    """
    if not running or not desired or running == desired:
        return False
    rl, dl = running.lower(), desired.lower()
    desired_is_17b = "1.7" in dl or "1_7" in dl
    running_is_06b = "0.6" in rl or "0_6" in rl
    return desired_is_17b and running_is_06b


def _server_matches(model: str) -> bool:
    info = _health()
    if not info:
        return False
    running = str(info.get("model") or "")
    return running == model or _is_downgrade_of(running, model)


def _log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def ensure_server() -> str:
    global _PROCESS, _PROCESS_KEY
    url = base_url()
    key = (_host(), _port(), model_id())
    if _server_matches(key[2]) and (_PROCESS_KEY is None or _PROCESS_KEY == key):
        return url
    if is_ready() and not _server_matches(key[2]):
        raise RuntimeError(
            "Qwen3-ASR sidecar 已在运行但模型与当前配置不同；"
            "请先执行 pkill -f qwen_asr_server.py，或使用 scripts/switch-qwen-asr.sh 切换。"
        )

    if _PROCESS is not None and _PROCESS.poll() is None and _PROCESS_KEY == key:
        return url

    log_dir = Path(os.environ.get("VOICE_IME_LOG_DIR", "~/.local/share/ibus-voice-ime")).expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "qwen-asr-server.log"
    cmd = [
        _python(),
        str(Path(__file__).resolve().parent / "qwen_asr_server.py"),
        "--model", model_id(),
        "--host", _host(),
        "--port", str(_port()),
    ]
    _log("ASR starting Qwen3-ASR sidecar: " + " ".join(shlex.quote(x) for x in cmd))
    log = log_file.open("a", encoding="utf-8")
    env = os.environ.copy()
    # Prefer ModelScope when the model id is remote and the user is in mainland China.
    env.setdefault("HF_ENDPOINT", os.environ.get("HF_ENDPOINT", ""))
    _PROCESS = subprocess.Popen(  # noqa: S603 - local sidecar command
        cmd,
        stdout=log,
        stderr=subprocess.STDOUT,
        cwd=str(ROOT_DIR),
        env=env,
        start_new_session=True,
    )
    _PROCESS_KEY = key

    timeout = float(os.environ.get("VOICE_IME_QWEN_ASR_START_TIMEOUT", "120"))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _PROCESS.poll() is not None:
            raise RuntimeError(f"Qwen3-ASR sidecar 启动失败，详见日志：{log_file}")
        if is_ready():
            _log(f"ASR Qwen3-ASR sidecar ready: {url}")
            return url
        time.sleep(0.5)
    raise RuntimeError(f"Qwen3-ASR sidecar 启动超时，详见日志：{log_file}")


def transcribe(wav_path: str) -> str:
    url = ensure_server()
    language = os.environ.get("VOICE_IME_QWEN_ASR_LANGUAGE", os.environ.get("VOICE_IME_WHISPER_LANGUAGE", "zh"))
    payload = {"audio": wav_path, "language": language}
    context = voice_terms.build_asr_context()
    if context:
        payload["context"] = context
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    timeout = float(os.environ.get("VOICE_IME_QWEN_ASR_TIMEOUT", "120"))
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
        raise RuntimeError(f"Qwen3-ASR HTTP {exc.code}: {detail}") from exc
    result: dict[str, Any] = json.loads(body)
    if result.get("error"):
        raise RuntimeError(str(result["error"]))
    return str(result.get("text") or "").strip()


def warm() -> bool:
    """Pre-load the Qwen3-ASR model onto the GPU without transcribing.

    Called when voice recording begins so the model is already resident on the
    GPU by the time the user stops talking, making the first ``/transcribe``
    after stop a near-instant cache hit.  Best-effort: any failure (sidecar not
    selected, not reachable, HTTP error) returns ``False`` and is logged by the
    caller — warmup must never break the recording/transcription flow.
    """
    if not selected():
        return False
    try:
        url = ensure_server()
    except Exception as exc:
        _log(f"ASR 预热跳过（ensure_server 失败）：{exc}")
        return False
    # Give warmup the full sidecar start window: the first warm may itself
    # trigger model loading (~10s from disk on first use, ~1s RAM->GPU after).
    timeout = float(os.environ.get("VOICE_IME_QWEN_ASR_START_TIMEOUT", "120"))
    req = urllib.request.Request(
        url + "/warm",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local endpoint
            return 200 <= resp.status < 300
    except Exception as exc:
        _log(f"ASR 预热失败（best-effort，忽略）：{exc}")
        return False


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


__all__ = ["base_url", "ensure_server", "is_ready", "model_id", "selected", "shutdown", "transcribe", "warm"]
