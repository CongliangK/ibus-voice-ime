# -*- coding: utf-8 -*-
"""Manage the optional built-in llama.cpp sidecar for LLM post-processing.

The IBus engine process should stay small and stable, so we do not load a GGUF
model directly in-process.  Instead, when LLM post-processing is enabled, this
module can start a local ``llama-server`` child process and expose it through the
same OpenAI-compatible API already used by ``llm_postprocess.py``.
"""
from __future__ import annotations

import atexit
import os
import shlex
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[3]
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18080
DEFAULT_BASE_URL = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}/v1"
DEFAULT_MODEL_ID = "Qwen/Qwen3.5-0.8B"
DEFAULT_MODEL_ALIAS = "qwen3.5-0.8b"

_PROCESS: subprocess.Popen | None = None


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


def _log(message: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {message}", flush=True)


def internal_enabled() -> bool:
    """Whether llm_postprocess should try to manage the built-in sidecar.

    Default is auto: use the sidecar unless the user explicitly configured a
    different VOICE_IME_LLM_BASE_URL.  Users can force it with
    VOICE_IME_LLM_INTERNAL=1 or disable it with VOICE_IME_LLM_INTERNAL=0.
    A valid cloud JSON config (~/.config/ibus-voice-ime/llm.json) always takes
    precedence — the local sidecar must never shadow a configured cloud model.
    """
    from ibus_voice_ime.text import llm_cloud_config

    if llm_cloud_config.load() is not None:
        return False
    raw = os.environ.get("VOICE_IME_LLM_INTERNAL")
    if raw is not None:
        return _env_bool("VOICE_IME_LLM_INTERNAL", True)
    configured = os.environ.get("VOICE_IME_LLM_BASE_URL", "").strip()
    if not configured:
        return True
    return configured.rstrip("/") in {
        DEFAULT_BASE_URL.rstrip("/"),
        f"http://{DEFAULT_HOST}:{DEFAULT_PORT}".rstrip("/"),
    }


def base_url() -> str:
    if internal_enabled():
        host = os.environ.get("VOICE_IME_LLAMA_HOST", DEFAULT_HOST)
        port = _env_int("VOICE_IME_LLAMA_PORT", DEFAULT_PORT)
        return f"http://{host}:{port}/v1"
    return os.environ.get("VOICE_IME_LLM_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def model_alias() -> str:
    alias = os.environ.get("VOICE_IME_LLM_MODEL", DEFAULT_MODEL_ALIAS).strip() or DEFAULT_MODEL_ALIAS
    # Older setup versions used this Ollama example as a persisted default.  Do
    # not let that stale value rename the built-in Qwen3.5 sidecar.
    if alias == "qwen2.5:7b-instruct" and internal_enabled():
        return DEFAULT_MODEL_ALIAS
    return alias


def _server_bin() -> str:
    explicit = os.environ.get("VOICE_IME_LLAMA_SERVER", "").strip()
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.extend([
        ROOT_DIR / "vendor" / "llama.cpp" / "bin" / "llama-server",
        ROOT_DIR / "vendor" / "llama.cpp" / "llama-server",
        ROOT_DIR / "vendor" / "llama" / "llama-server",
    ])
    for path in candidates:
        if path.exists() and os.access(path, os.X_OK):
            return str(path)
    found = shutil.which("llama-server")
    if found:
        return found
    raise RuntimeError(
        "未找到 llama.cpp 的 llama-server。请运行 scripts/setup-llm.sh，"
        "或设置 VOICE_IME_LLAMA_SERVER=/path/to/llama-server。"
    )


def _find_gguf_in(directory: Path) -> Path | None:
    if not directory.exists():
        return None
    preferred_fragments = ("Q4_K_M", "q4_k_m", "Q5_K_M", "q5_k_m", "Q8_0", "q8_0")
    files = sorted(directory.rglob("*.gguf"), key=lambda p: p.stat().st_size if p.exists() else 0)
    if not files:
        return None
    for frag in preferred_fragments:
        for file in files:
            if frag in file.name:
                return file
    return files[0]


def model_path() -> Path:
    explicit = os.environ.get("VOICE_IME_LLAMA_MODEL_PATH", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if path.exists():
            return path
        raise RuntimeError(f"VOICE_IME_LLAMA_MODEL_PATH 指向的模型不存在：{path}")

    candidates = [
        ROOT_DIR / "vendor" / "models" / "qwen3.5-0.8b.gguf",
        ROOT_DIR / "vendor" / "models" / "Qwen3.5-0.8B.gguf",
        ROOT_DIR / "vendor" / "models" / "qwen3.5-0.8b-gguf",
        ROOT_DIR / "vendor" / "models",
    ]
    for candidate in candidates:
        if candidate.is_file() and candidate.suffix.lower() == ".gguf":
            return candidate
        found = _find_gguf_in(candidate) if candidate.is_dir() else None
        if found:
            return found

    raise RuntimeError(
        "未找到 Qwen3.5-0.8B 的 GGUF 模型文件。llama.cpp 需要 GGUF；"
        "请运行 scripts/setup-llm.sh 下载/配置模型，或设置 "
        "VOICE_IME_LLAMA_MODEL_PATH=/path/to/model.gguf。"
    )


def _url_ok(url: str, timeout: float = 0.3) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - local user-configured URL
            return 200 <= resp.status < 500
    except Exception:
        return False


def is_server_ready(url: str | None = None) -> bool:
    url = (url or base_url()).rstrip("/")
    root = url[:-3] if url.endswith("/v1") else url
    return _url_ok(root + "/health") or _url_ok(url + "/models")


def _default_threads() -> int:
    return max(2, min(os.cpu_count() or 4, 8))


def _build_command() -> list[str]:
    host = os.environ.get("VOICE_IME_LLAMA_HOST", DEFAULT_HOST)
    port = _env_int("VOICE_IME_LLAMA_PORT", DEFAULT_PORT)
    ctx_size = _env_int("VOICE_IME_LLAMA_CTX_SIZE", 2048)
    threads = _env_int("VOICE_IME_LLAMA_THREADS", _default_threads())
    batch_size = _env_int("VOICE_IME_LLAMA_BATCH_SIZE", 128)

    cmd = [
        _server_bin(),
        "-m", str(model_path()),
        "--alias", model_alias(),
        "--host", host,
        "--port", str(port),
        "--ctx-size", str(ctx_size),
        "--threads", str(threads),
        "--batch-size", str(batch_size),
    ]

    n_gpu_layers = os.environ.get("VOICE_IME_LLAMA_N_GPU_LAYERS", "").strip()
    if n_gpu_layers:
        cmd.extend(["--n-gpu-layers", n_gpu_layers])

    if _env_bool("VOICE_IME_LLAMA_NO_WEBUI", True):
        cmd.append("--no-webui")
    if _env_bool("VOICE_IME_LLAMA_JINJA", True):
        cmd.append("--jinja")

    # Qwen3.5 supports thinking mode, but an input method post-processor should
    # be fast and direct.  Current llama.cpp builds use --reasoning off.
    reasoning = os.environ.get("VOICE_IME_LLAMA_REASONING", "off").strip()
    if reasoning:
        cmd.extend(["--reasoning", reasoning])

    # Kept as an escape hatch for custom/older templates; empty by default.
    template_kwargs = os.environ.get("VOICE_IME_LLAMA_CHAT_TEMPLATE_KWARGS", "").strip()
    if template_kwargs:
        cmd.extend(["--chat-template-kwargs", template_kwargs])

    extra = os.environ.get("VOICE_IME_LLAMA_ARGS", "").strip()
    if extra:
        cmd.extend(shlex.split(extra))
    return cmd


def ensure_server() -> str:
    """Start the sidecar if needed and return its OpenAI-compatible base URL."""
    url = base_url()
    if not internal_enabled():
        return url
    if is_server_ready(url):
        return url

    global _PROCESS
    if _PROCESS is not None and _PROCESS.poll() is None:
        return url

    log_dir = Path(os.environ.get("VOICE_IME_LOG_DIR", "~/.local/share/ibus-voice-ime")).expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "llama-server.log"
    cmd = _build_command()
    _log("LLM starting llama.cpp sidecar: " + " ".join(shlex.quote(x) for x in cmd))
    log = log_file.open("a", encoding="utf-8")
    _PROCESS = subprocess.Popen(  # noqa: S603 - user-controlled local executable path by design
        cmd,
        stdout=log,
        stderr=subprocess.STDOUT,
        cwd=str(ROOT_DIR),
        start_new_session=True,
    )

    timeout = float(os.environ.get("VOICE_IME_LLAMA_START_TIMEOUT", "20"))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _PROCESS.poll() is not None:
            raise RuntimeError(f"llama-server 启动失败，详见日志：{log_file}")
        if is_server_ready(url):
            _log(f"LLM llama.cpp sidecar ready: {url}")
            return url
        time.sleep(0.2)

    raise RuntimeError(f"llama-server 启动超时，详见日志：{log_file}")


def shutdown() -> None:
    global _PROCESS
    proc = _PROCESS
    _PROCESS = None
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


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL_ALIAS",
    "DEFAULT_MODEL_ID",
    "base_url",
    "ensure_server",
    "internal_enabled",
    "is_server_ready",
    "model_alias",
    "model_path",
    "shutdown",
]
