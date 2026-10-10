# -*- coding: utf-8 -*-
"""Manage the optional Qwen3-ASR sidecar server."""
from __future__ import annotations

import atexit
import json
import os
import shlex
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ibus_voice_ime import config
from ibus_voice_ime.log_trim import trim_to_last_lines
from ibus_voice_ime.memory import voice_terms
from ibus_voice_ime.asr.qwen_diagnostics import classify_error, error_detail

ROOT_DIR = Path(__file__).resolve().parents[3]
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18081
DEFAULT_MODEL_ALIAS = "1.7b"
_PROCESS: subprocess.Popen | None = None
_PROCESS_KEY: tuple[str, int, str] | None = None
# 录音开始的预热线程与停止后的识别线程可能并发首拉 sidecar；不加锁会双拉
# 进程，输家死于端口冲突并报出假错误（实际幸存方健康）。
_ENSURE_LOCK = threading.Lock()
# 拉起失败熔断：sidecar 依赖缺失/驱动不匹配等“稳定秒退”故障下，每次听写都会
# 重复支付 python+torch import（10-40s）后失败。冷却窗内直接抛缓存错误。
_FAIL_AT = 0.0
_FAIL_KEY: tuple[str, int, str] | None = None
_FAIL_MSG = ""


def _fail_cooldown() -> float:
    return max(0.0, config.env_float("VOICE_IME_QWEN_ASR_FAIL_COOLDOWN", 180.0))


def _record_spawn_failure(key: tuple[str, int, str], message: str) -> RuntimeError:
    global _FAIL_AT, _FAIL_KEY, _FAIL_MSG
    _FAIL_AT = time.monotonic()
    _FAIL_KEY = key
    _FAIL_MSG = message
    return RuntimeError(message)


def _raise_cached_failure(key: tuple[str, int, str]) -> None:
    cooldown = _fail_cooldown()
    if cooldown <= 0 or not _FAIL_MSG or _FAIL_KEY != key:
        return
    elapsed = time.monotonic() - _FAIL_AT
    if elapsed < cooldown:
        raise RuntimeError(
            f"Qwen3-ASR sidecar 刚刚启动失败，{int(cooldown - elapsed)}s 内不再重复拉起（熔断）。\n"
            f"上次错误：{_FAIL_MSG}\n"
            "修复后自动恢复；设 VOICE_IME_QWEN_ASR_FAIL_COOLDOWN=0 可禁用熔断。"
        )


def _clear_failure() -> None:
    global _FAIL_AT, _FAIL_KEY, _FAIL_MSG
    _FAIL_AT = 0.0
    _FAIL_KEY = None
    _FAIL_MSG = ""


def _terminate_process(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=1)
        except Exception:
            pass


def _env_bool(name: str, default: bool) -> bool:
    return config.env_bool(name, default)


def _env_int(name: str, default: int) -> int:
    return config.env_int(name, default)


def selected() -> bool:
    backend = config.env_str("VOICE_IME_ASR_BACKEND", "").strip().lower()
    return backend in {"qwen", "qwen3", "qwen3-asr", "qwen-asr"} or _env_bool("VOICE_IME_QWEN_ASR", False)


def _host() -> str:
    return config.env_str("VOICE_IME_QWEN_ASR_HOST", DEFAULT_HOST)


def _port() -> int:
    return _env_int("VOICE_IME_QWEN_ASR_PORT", DEFAULT_PORT)


def base_url() -> str:
    return f"http://{_host()}:{_port()}"


def _python() -> str:
    explicit = config.env_str("VOICE_IME_QWEN_ASR_PYTHON", "").strip()
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
    explicit_path = config.env_str("VOICE_IME_QWEN_ASR_MODEL_PATH", "").strip()
    if explicit_path:
        return str(Path(explicit_path).expanduser())

    raw = config.env_str("VOICE_IME_QWEN_ASR_MODEL", DEFAULT_MODEL_ALIAS).strip() or DEFAULT_MODEL_ALIAS
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
            info = json.loads(resp.read(65537).decode("utf-8"))
            if not isinstance(info, dict) or not isinstance(info.get("model"), str):
                return None
            if info.get("status") not in {"ok", "idle", "ready", "error"}:
                return None
            return info
    except Exception:
        return None


def is_ready() -> bool:
    """Whether the expected HTTP service is alive (not model readiness).

    Lazy loading/offloading is intentional: never kill an idle, healthy sidecar
    just because it is not holding GPU memory. Use is_model_ready for inference.
    """
    return _health() is not None


def is_model_ready() -> bool:
    info = _health()
    return bool(info and info.get("loaded") is True and not info.get("error"))


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


def _log_tail(log_file: Path, lines: int = 15) -> str:
    """最后几行日志，用于把失败原因带进用户可见的错误信息。"""
    try:
        content = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(content[-lines:]).strip()
    except Exception:
        return ""


def _humanize_transcribe_error(code: int, detail: str) -> str:
    """Classify before truncating: gcc's actual failure is often at the end."""
    if code not in {500, 503}:
        return f"Qwen3-ASR HTTP {code}: {detail}"
    category, guidance = classify_error(detail)
    message = error_detail(detail)
    if len(message) > 700:
        message = message[:250] + "\n…\n" + message[-450:]
    log_dir = Path(config.env_str("VOICE_IME_LOG_DIR", "~/.local/share/ibus-voice-ime")).expanduser()
    return (
        f"Qwen3-ASR 识别失败 [{category}]（HTTP {code}）：{message}\n{guidance}\n"
        f"完整日志：{log_dir / 'qwen-asr-server.log'}"
    )


def ensure_server() -> str:
    with _ENSURE_LOCK:
        return _ensure_server_locked()


def _ensure_server_locked() -> str:
    global _PROCESS, _PROCESS_KEY
    url = base_url()
    key = (_host(), _port(), model_id())
    log_dir = Path(config.env_str("VOICE_IME_LOG_DIR", "~/.local/share/ibus-voice-ime")).expanduser()
    log_file = log_dir / "qwen-asr-server.log"
    timeout = max(1.0, config.env_float("VOICE_IME_QWEN_ASR_START_TIMEOUT", 120.0))

    def death_error(code: int | None) -> RuntimeError:
        tail = _log_tail(log_file)
        hint = ""
        if "address already in use" in tail.lower() or "eaddrinuse" in tail.lower():
            hint = f"\n端口 {_port()} 已被其他进程占用：设置 VOICE_IME_QWEN_ASR_PORT 换端口后重启输入法。"
        return _record_spawn_failure(
            key,
            f"Qwen3-ASR sidecar 启动失败（退出码 {code}）。\n{tail}\n完整日志：{log_file}{hint}",
        )

    if _server_matches(key[2]) and (_PROCESS_KEY is None or _PROCESS_KEY == key):
        _clear_failure()
        return url
    if is_ready() and not _server_matches(key[2]):
        raise RuntimeError(
            "Qwen3-ASR sidecar 已在运行但模型与当前配置不同；"
            "请先执行 pkill -f qwen_asr_server.py，或使用 scripts/switch-qwen-asr.sh 切换。"
        )

    _raise_cached_failure(key)

    if _PROCESS is not None and _PROCESS.poll() is None:
        if _PROCESS_KEY != key:
            # 配置变了（模型/端口）：停掉自己拉起的旧 sidecar 再拉新的，避免端口冲突。
            _log("ASR 配置变更，重启 Qwen3-ASR sidecar")
            _terminate_process(_PROCESS)
            _PROCESS = None
            _PROCESS_KEY = None
        else:
            # 上一次 ensure 拉起的进程仍在启动中（如预热线程首拉）：等它就绪，
            # 而不是立即返回一个尚未监听的 URL。
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if _PROCESS.poll() is not None:
                    code = _PROCESS.returncode
                    _PROCESS = None
                    _PROCESS_KEY = None
                    raise death_error(code)
                if is_ready():
                    _log(f"ASR Qwen3-ASR sidecar ready: {url}")
                    _clear_failure()
                    return url
                time.sleep(0.5)
            # 超时：杀掉卡死的子进程再报错，不留占显存/端口的僵尸。
            proc, _PROCESS, _PROCESS_KEY = _PROCESS, None, None
            _terminate_process(proc)
            raise _record_spawn_failure(
                key,
                f"Qwen3-ASR sidecar 启动超时（{timeout:.0f}s），已终止进程。详见日志：{log_file}",
            )

    # 首装常见问题前置检查：模型目录不存在时 sidecar 会假装健康、识别时才
    # 报错；在这里早失败并给出补救命令。
    mid = key[2]
    mid_path = Path(mid).expanduser()
    if mid_path.is_absolute() and not mid_path.is_dir():
        raise RuntimeError(
            f"Qwen3-ASR 模型目录不存在：{mid}\n"
            "请先运行 ./scripts/setup-qwen-asr.sh 下载模型，"
            "或用 ./scripts/switch-mimo-cloud-asr.sh cn 切换云端后端（无需 GPU）。"
        )

    log_dir.mkdir(parents=True, exist_ok=True)
    trim_to_last_lines(log_file)  # 日志保留：spawn 前裁剪到最近 N 行
    cmd = [
        _python(),
        str(Path(__file__).resolve().parent / "qwen_asr_server.py"),
        "--model", model_id(),
        "--host", _host(),
        "--port", str(_port()),
    ]
    _log("ASR starting Qwen3-ASR sidecar: " + " ".join(shlex.quote(x) for x in cmd))
    log = log_file.open("a", encoding="utf-8")
    try:
        _PROCESS = subprocess.Popen(  # noqa: S603 - local sidecar command
            cmd,
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=str(ROOT_DIR),
            env=os.environ.copy(),
            start_new_session=True,
        )
    finally:
        log.close()  # 子进程持有 dup 出的 fd；父进程及时关闭，防反复 spawn 泄漏
    _PROCESS_KEY = key

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _PROCESS.poll() is not None:
            code = _PROCESS.returncode
            _PROCESS = None
            _PROCESS_KEY = None
            raise death_error(code)
        if is_ready():
            _log(f"ASR Qwen3-ASR sidecar ready: {url}")
            _clear_failure()
            return url
        time.sleep(0.5)
    proc, _PROCESS, _PROCESS_KEY = _PROCESS, None, None
    _terminate_process(proc)
    raise _record_spawn_failure(
        key,
        f"Qwen3-ASR sidecar 启动超时（{timeout:.0f}s），已终止进程。详见日志：{log_file}",
    )


def transcribe(wav_path: str) -> str:
    url = ensure_server()
    language = config.env_str("VOICE_IME_QWEN_ASR_LANGUAGE", None)
    if language is None:
        language = config.env_str("VOICE_IME_WHISPER_LANGUAGE", "zh")
    payload = {"audio": wav_path, "language": language}
    context = voice_terms.build_asr_context()
    if context:
        payload["context"] = context
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    timeout = config.env_float("VOICE_IME_QWEN_ASR_TIMEOUT", 120.0)
    req = urllib.request.Request(
        url + "/transcribe",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local endpoint
            raw = resp.read(1048577)
            if len(raw) > 1048576:
                raise RuntimeError("Qwen3-ASR 响应超过 1 MiB，拒绝解析异常响应。")
            body = raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read(65536).decode("utf-8", errors="replace")
        raise RuntimeError(_humanize_transcribe_error(exc.code, detail)) from exc
    except (urllib.error.URLError, socket.timeout) as exc:
        # 慢推理触发的读超时（socket.timeout 被 urllib 包成 URLError 或直接抛出）
        # 与“服务崩了”是两回事，混在一条文案里会把排障方向带偏。
        reason = getattr(exc, "reason", None)
        if isinstance(exc, socket.timeout) or isinstance(reason, (socket.timeout, TimeoutError)):
            raise RuntimeError(
                f"Qwen3-ASR 识别超时（>{timeout:.0f}s）：缩短录音长度，"
                "或调大 VOICE_IME_QWEN_ASR_TIMEOUT。"
            ) from exc
        raise RuntimeError(
            f"Qwen3-ASR sidecar 连接失败（{exc}）：服务可能已崩溃或未监听。"
            "详见 ~/.local/share/ibus-voice-ime/qwen-asr-server.log"
        ) from exc
    try:
        result = json.loads(body)
    except ValueError as exc:
        raise RuntimeError("Qwen3-ASR 返回无效 JSON；检查端口是否被其他服务占用及 sidecar 日志。") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Qwen3-ASR 返回格式无效（应为 JSON 对象）。")
    if result.get("error"):
        raise RuntimeError(_humanize_transcribe_error(500, body))
    if not isinstance(result.get("text"), str):
        raise RuntimeError("Qwen3-ASR 响应缺少有效 text 字段，不能当作空识别结果。")
    # 日志保留：sidecar 常驻进程的日志只在 spawn 时裁剪，重负载长会话期间
    # 由识别路径顺手兜底（廉价 stat，超阈值才重写）。
    log_dir = Path(config.env_str("VOICE_IME_LOG_DIR", "~/.local/share/ibus-voice-ime")).expanduser()
    trim_to_last_lines(log_dir / "qwen-asr-server.log")
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
    timeout = config.env_float("VOICE_IME_QWEN_ASR_START_TIMEOUT", 120.0)
    req = urllib.request.Request(
        url + "/warm",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local endpoint
            info = json.loads(resp.read(65536).decode("utf-8"))
            return 200 <= resp.status < 300 and isinstance(info, dict) and info.get("status") == "warm" and info.get("loaded") is True
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


__all__ = ["base_url", "ensure_server", "is_ready", "is_model_ready", "model_id", "selected", "shutdown", "transcribe", "warm"]
