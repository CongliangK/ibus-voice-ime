#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small local HTTP sidecar for Qwen3-ASR."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import inspect
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ibus_voice_ime import config
from ibus_voice_ime.asr import sidecar_http
from ibus_voice_ime.asr.qwen_diagnostics import classify_error, missing_python_header_hint

_MODEL = None
_MODEL_ID = ""

def _dtype(name: str):
    import torch  # type: ignore

    name = (name or "bfloat16").lower()
    if name in {"bf16", "bfloat16"}:
        return torch.bfloat16
    if name in {"fp16", "float16", "half"}:
        return torch.float16
    if name in {"fp32", "float32"}:
        return torch.float32
    return torch.bfloat16

def _build_model_kwargs() -> dict[str, Any]:
    """Assemble the Qwen3ASRModel.from_pretrained kwargs from env vars.

    Mirrors the kwargs the old ``_load_model`` produced, kept here so the
    multi-model manager can build fresh kwargs for each model it loads.
    """
    dtype_name = (config.env_str("VOICE_IME_QWEN_ASR_DTYPE", "bfloat16") or "bfloat16").lower()
    device_map = config.env_str("VOICE_IME_QWEN_ASR_DEVICE_MAP", "cuda:0")
    # pre-Ampere（GTX 10xx/16xx、RTX 20xx，sm<80）没有原生 bfloat16：默认 bf16 会在
    # 首个 kernel 报 "not implemented for 'BFloat16'"。这些卡 fp16 完全可用，自动降档
    # 并打日志，用户无感。torch 只在 sidecar venv 里存在，import 失败则保持原值。
    if dtype_name in {"bf16", "bfloat16"} and device_map.startswith("cuda"):
        try:
            import torch  # type: ignore

            if torch.cuda.is_available() and not torch.cuda.is_bf16_supported():
                print(
                    f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                    "本机 GPU 不支持 bfloat16，自动降级 dtype=bfloat16 -> float16"
                    "（如需固定可设 VOICE_IME_QWEN_ASR_DTYPE=float16）",
                    flush=True,
                )
                dtype_name = "float16"
        except Exception:
            pass
    dtype = _dtype(dtype_name)
    max_batch = config.env_int("VOICE_IME_QWEN_ASR_MAX_BATCH", 1)
    # 256 会把约 250~450 字的长听写硬截断（生成到上限即停、余下语音静默丢弃）；
    # 1024 覆盖 5 分钟录音的典型字数量级。代价仅是长文本极端生成时间变长。
    max_tokens = config.env_int("VOICE_IME_QWEN_ASR_MAX_NEW_TOKENS", 1024)
    kwargs: dict[str, Any] = {
        "dtype": dtype,
        "device_map": device_map,
        "max_inference_batch_size": max_batch,
        "max_new_tokens": max_tokens,
    }
    attn = config.env_str("VOICE_IME_QWEN_ASR_ATTN", "").strip()
    if attn:
        kwargs["attn_implementation"] = attn
    return kwargs


def _resolve_alias(path: str) -> str:
    """Map a model path/id to its size alias ("1.7b" | "0.6b" | "1.7b")."""
    lower = (path or "").lower()
    if "0.6" in lower or "0_6" in lower:
        return "0.6b"
    return "1.7b"


def _companion_path(model_path: str) -> str | None:
    """Given a local model dir, return the other size's dir if it exists.

    e.g. ``.../Qwen3-ASR-1.7B`` -> ``.../Qwen3-ASR-0.6B`` when present.
    """
    p = Path(model_path)
    base = p.parent
    if not p.exists():
        return None
    alias = _resolve_alias(model_path)
    if alias == "1.7b":
        for cand in [base / "Qwen3-ASR-0.6B", base / "0.6B"]:
            if (cand / "config.json").exists():
                return str(cand)
    # Explicit 0.6B is a ceiling, not permission to silently upgrade to 1.7B.
    return None


class ModelManager:
    """Multi-model GPU/RAM resident manager with VRAM-aware selection.

    Replaces the old single-model ``IdleOffloader``.  Loaded ``Qwen3ASRModel``
    wrappers are held in ``self._models`` and *never released to disk* once
    loaded; only their location moves between GPU and CPU RAM.  At most one
    model is on the GPU at a time (``self._active``); the rest sit in CPU RAM.

    Selection state machine (called before every inference):

      active == "1.7b"  -> no probe; the model is healthy, serve directly.
      active == "0.6b"  -> probe ``torch.cuda.mem_get_info``; if free VRAM now
                           exceeds the 1.7b threshold, upgrade: evict 0.6b to
                           RAM and load 1.7b onto the GPU.  Otherwise keep 0.6b.
      active is None    -> probe; load the largest model that fits.  Models
                           already in ``self._models`` move RAM->GPU (fast);
                           first use goes disk->RAM->GPU (slow, ~10s — this is
                           the cost the probe exists to avoid).

    An idle watchdog moves the active model to CPU RAM after ``idle_timeout``
    seconds, freeing VRAM; the next request re-probes (state -> None branch).

    Only the ``transformers`` backend participates.  ``Qwen3ASRModel`` and
    ``Qwen3ForcedAligner`` cache ``self.device`` in ``__init__``, so after each
    migration the cached attribute is resynced to ``model.device``.
    """

    def __init__(
        self,
        primary_path: str,
        secondary_path: str | None,
        idle_timeout: float,
        check_interval: float,
        vram_min_17b_mib: int = 5000,
        vram_min_06b_mib: int = 2000,
        offload_target: str = "cpu",
    ) -> None:
        # Build alias -> path map; drop missing/empty entries.
        paths: dict[str, str] = {_resolve_alias(primary_path): primary_path}
        if secondary_path and _resolve_alias(primary_path) == "1.7b":
            paths["0.6b"] = secondary_path
        # Normalize: ensure each path is filed under the alias matching its name.
        self._paths: dict[str, str] = {}
        for path in paths.values():
            if not path:
                continue
            alias = _resolve_alias(path)
            if alias not in self._paths:
                self._paths[alias] = path
        if not self._paths:
            raise ValueError("ModelManager requires at least one model path")

        self._models: dict[str, Any] = {}   # alias -> loaded Qwen3ASRModel (resident)
        self._active: str | None = None     # alias currently on GPU
        self._gpu_device: Any = None        # observed GPU device, set on first activation
        self._vram_min = {"1.7b": vram_min_17b_mib, "0.6b": vram_min_06b_mib}
        self._offload_target = offload_target
        self._idle_timeout = float(idle_timeout)
        self._interval = max(0.5, float(check_interval))
        self._lock = threading.RLock()
        self._last_activity = time.monotonic()
        self._load_error: str = ""  # last acquire_for_inference failure, exposed via /health
        self._load_failed_at = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._idle_timeout <= 0:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="qwen-asr-model-manager", daemon=True)
        self._thread.start()
        print(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
            f"Qwen3-ASR model manager armed: models={list(self._paths)} "
            f"idle_timeout={self._idle_timeout:.0f}s vram_min={self._vram_min}",
            flush=True,
        )

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

    def touch(self) -> None:
        self._last_activity = time.monotonic()

    @property
    def load_error(self) -> str:
        """Last model-load failure (empty string when the last acquire succeeded)."""
        return self._load_error

    # ---- request path ----------------------------------------------------
    def acquire_for_inference(self) -> None:
        """Make the best model live on the GPU and ready to infer.

        Holds the lock so the watchdog cannot migrate mid-request.  Must be
        paired with ``release_after_inference``.
        """
        wait = config.env_float("VOICE_IME_QWEN_ASR_TIMEOUT", 120.0)
        import math
        wait = min(1800.0, max(0.1, wait)) if math.isfinite(wait) and wait > 0 else 120.0
        if not self._lock.acquire(timeout=wait):
            raise sidecar_http.RequestError(503, "模型正在处理上一请求，等待超时；请稍后重试或重启 sidecar。")
        try:
            cooldown = max(0.0, config.env_float("VOICE_IME_QWEN_ASR_FAIL_COOLDOWN", 180.0))
            if self._load_error and self._load_failed_at > 0 and time.monotonic() - self._load_failed_at < cooldown:
                return  # keep lock/error: caller reports failure, never a fake warm success
            self._ensure_active_locked()
            self._last_activity = time.monotonic()
            self._load_error = ""
            self._load_failed_at = 0.0
        except Exception as exc:  # never let model management crash a request
            import traceback

            # Full traceback (and gcc stderr) belongs in the log. Keep a bounded
            # but useful HTTP detail; do not cut off the end of compiler errors.
            self._load_error = f"{type(exc).__name__}: {exc}"[-8192:]
            category, _ = classify_error(self._load_error)
            self._load_failed_at = time.monotonic() if category != "oom" else 0.0
            traceback.print_exc()
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                  f"Qwen3-ASR acquire_for_inference failed [{category}]: {exc}", flush=True)

    def release_after_inference(self) -> None:
        try:
            self.touch()
        finally:
            try:
                self._lock.release()
            except RuntimeError:
                pass

    # ---- state machine core ---------------------------------------------
    def _ensure_active_locked(self) -> None:
        """Decide which model should be on the GPU and make it so.

        See the class docstring for the state machine.  When the active alias
        is the largest configured model we trust its existing allocation.
        Cold activation (including RAM->GPU after idle offload) still probes;
        a downgraded model in a dual-model configuration probes for upgrades.
        """
        if self._active in self._paths and (len(self._paths) == 1 or self._active == "1.7b"):
            return  # already serving the largest model; do not re-probe
        free_mib = self._probe_free_vram_mib()
        want = self._pick_target(free_mib)
        if self._active == want:
            return
        if self._active is not None:
            self._to_cpu_locked(self._active)
        self._to_gpu_locked(want)

    def _pick_target(self, free_mib: int | None) -> str:
        """Choose the alias to activate given free VRAM (MiB) or None.

        Prefer the largest model that fits; fall back conservatively when the
        probe failed.  With a single configured model we still gate on VRAM
        when the probe succeeded: a 2-4GB card would otherwise pay the full
        disk->RAM weight load on every dictation just to OOM with a generic
        error.  Probe failure (None) keeps the old try-anyway behavior so the
        load surfaces the real underlying error.
        """
        if len(self._paths) == 1:
            only = next(iter(self._paths))
            need = self._vram_min[only]
            if free_mib is not None and free_mib < need:
                raise RuntimeError(
                    f"显存不足：{only} 模型约需 {need} MiB 空闲显存，当前仅 {free_mib} MiB。"
                    "关闭占用显存的程序后重试，或安装 0.6B 模型"
                    "（./scripts/setup-qwen-asr.sh 会同时下载两档）以获得自动降级。"
                )
            return only
        if "1.7b" in self._paths and free_mib is not None and free_mib >= self._vram_min["1.7b"]:
            return "1.7b"
        if "0.6b" in self._paths:
            return "0.6b"
        return next(iter(self._paths))

    # ---- migration primitives -------------------------------------------
    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                with self._lock:
                    if self._active is None:
                        continue
                    if time.monotonic() - self._last_activity < self._idle_timeout:
                        continue
                    self._to_cpu_locked(self._active)
            except Exception as exc:  # watchdog must never die
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                      f"Qwen3-ASR model manager watchdog check failed: {exc}", flush=True)

    def _probe_free_vram_mib(self) -> int | None:
        """Return free GPU VRAM in MiB, or None if torch/CUDA is unavailable.

        Reads the device selected by VOICE_IME_QWEN_ASR_DEVICE_MAP (default
        cuda:0): on multi-GPU boxes the free memory of card 0 is irrelevant
        when the model is about to be placed on card 1.
        """
        try:
            import torch  # type: ignore

            index = 0
            device_map = config.env_str("VOICE_IME_QWEN_ASR_DEVICE_MAP", "cuda:0")
            if ":" in device_map:
                try:
                    index = int(device_map.rsplit(":", 1)[1])
                except ValueError:
                    index = 0
            free, _total = torch.cuda.mem_get_info(index)
            return int(free) // (1024 * 1024)
        except Exception:
            return None

    def _resolve_gpu_device(self) -> Any:
        """Resolve the cuda device object/string from the device_map env var."""
        device_map = config.env_str("VOICE_IME_QWEN_ASR_DEVICE_MAP", "cuda:0")
        try:
            import torch  # type: ignore

            return torch.device(device_map)
        except Exception:
            return device_map

    def _load_to_ram_locked(self, alias: str) -> Any:
        """Load a model from disk via ``from_pretrained`` (disk -> RAM/VRAM).

        Called on first use of an alias.  The kwargs use the configured
        ``device_map`` (default cuda:0); if VRAM is insufficient the caller
        (``_ensure_active_locked``) will have picked a smaller alias, so we do
        not retry here.  The loaded wrapper is cached in ``self._models``.
        """
        from qwen_asr import Qwen3ASRModel  # type: ignore

        path = self._paths[alias]
        kwargs = _build_model_kwargs()
        started = time.monotonic()
        print(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
            f"Qwen3-ASR loading model={path} device_map={kwargs.get('device_map')} "
            f"dtype={kwargs.get('dtype')} max_tokens={kwargs.get('max_new_tokens')}",
            flush=True,
        )
        loaded = Qwen3ASRModel.from_pretrained(path, **kwargs)
        print(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
            f"Qwen3-ASR loaded model={path} elapsed={time.monotonic() - started:.3f}s",
            flush=True,
        )
        self._models[alias] = loaded
        return loaded

    def _resync_device(self, wrapper: Any) -> None:
        """Refresh cached ``device`` attributes after a ``.to()`` migration."""
        inner = getattr(wrapper, "model", None)
        if inner is not None:
            wrapper.device = getattr(inner, "device", getattr(wrapper, "device", None))
        aligner = getattr(wrapper, "forced_aligner", None)
        aligner_model = getattr(aligner, "model", None) if aligner is not None else None
        if aligner is not None and aligner_model is not None:
            aligner.device = getattr(aligner_model, "device", getattr(aligner, "device", None))

    def _to_gpu_locked(self, alias: str) -> None:
        wrapper = self._models.get(alias)
        if wrapper is None:
            wrapper = self._load_to_ram_locked(alias)
        inner = getattr(wrapper, "model", None)
        if inner is None:
            raise RuntimeError(f"model {alias} has no inner .model to migrate")
        if self._gpu_device is None:
            self._gpu_device = getattr(inner, "device", None) or self._resolve_gpu_device()
        target = self._gpu_device
        started = time.monotonic()
        inner.to(target)
        aligner = getattr(wrapper, "forced_aligner", None)
        aligner_model = getattr(aligner, "model", None) if aligner is not None else None
        if aligner_model is not None:
            aligner_model.to(target)
        self._resync_device(wrapper)
        self._active = alias
        # Keep the module-level globals in sync so the transcribe path that
        # reads ``_MODEL`` / ``_MODEL_ID`` sees the now-active model.
        _sync_module_model(alias, wrapper, self._paths[alias])
        print(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
            f"Qwen3-ASR model on GPU: alias={alias} path={self._paths[alias]} "
            f"elapsed={time.monotonic() - started:.3f}s",
            flush=True,
        )

    def _to_cpu_locked(self, alias: str) -> None:
        wrapper = self._models.get(alias)
        if wrapper is None:
            return  # never loaded; nothing to evict
        inner = getattr(wrapper, "model", None)
        if inner is None:
            return
        started = time.monotonic()
        inner.to(self._offload_target)
        aligner = getattr(wrapper, "forced_aligner", None)
        aligner_model = getattr(aligner, "model", None) if aligner is not None else None
        if aligner_model is not None:
            aligner_model.to(self._offload_target)
        self._resync_device(wrapper)
        # Release VRAM.  torch is only available in the real sidecar venv; keep
        # the import local so the module stays importable under stdlib-only CI.
        try:
            import torch  # type: ignore

            torch.cuda.empty_cache()
        except Exception as exc:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                  f"Qwen3-ASR empty_cache after offload skipped: {exc}", flush=True)
        self._active = None
        print(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
            f"Qwen3-ASR model offloaded to {self._offload_target} (VRAM freed): "
            f"alias={alias} elapsed={time.monotonic() - started:.3f}s",
            flush=True,
        )

    # ---- accessors -------------------------------------------------------
    @property
    def active_alias(self) -> str | None:
        return self._active

    @property
    def active_id(self) -> str:
        """The path of the currently-active model, for ``/health`` reporting."""
        if self._active is not None and self._active in self._paths:
            return self._paths[self._active]
        return _MODEL_ID  # fallback to whatever was last loaded

    def active_model(self) -> Any:
        """Return the wrapper currently on the GPU, or None."""
        if self._active is None:
            return None
        return self._models.get(self._active)

    # ---- test helpers ----------------------------------------------------
    def _set_state_for_test(self, active: str | None, gpu_device: Any = None) -> None:
        """Used by hermetic tests to force device state without torch."""
        self._active = active
        self._gpu_device = gpu_device

    def _state_for_test(self) -> str | None:
        return self._active

    def _set_model_for_test(self, alias: str, wrapper: Any) -> None:
        """Inject a fake loaded wrapper without touching disk."""
        self._models[alias] = wrapper


def _sync_module_model(alias: str, wrapper: Any, path: str) -> None:
    """Keep module-level ``_MODEL`` / ``_MODEL_ID`` pointing at the active model."""
    global _MODEL, _MODEL_ID
    _MODEL = wrapper
    _MODEL_ID = path


_MANAGER: ModelManager | None = None


def _language() -> str | None:
    raw = config.env_str("VOICE_IME_QWEN_ASR_LANGUAGE", None)
    if raw is None:
        raw = config.env_str("VOICE_IME_WHISPER_LANGUAGE", "zh")
    raw = (raw or "").strip().lower()
    if not raw or raw in {"auto", "none"}:
        return None
    mapping = {
        "zh": "Chinese",
        "cn": "Chinese",
        "chinese": "Chinese",
        "mandarin": "Chinese",
        "yue": "Cantonese",
        "cantonese": "Cantonese",
        "en": "English",
        "english": "English",
        "ja": "Japanese",
        "japanese": "Japanese",
        "ko": "Korean",
        "korean": "Korean",
    }
    return mapping.get(raw, raw)

def _result_to_dict(item: Any) -> dict[str, Any]:
    text = getattr(item, "text", "")
    language = getattr(item, "language", None)
    data = {"text": str(text or ""), "language": language}
    if hasattr(item, "time_stamps"):
        try:
            data["time_stamps"] = getattr(item, "time_stamps")
        except Exception:
            pass
    return data

def _transcribe_with_optional_context_model(model: Any, audio: str, language: Any, context: str | None) -> Any:
    """Call ``model.transcribe`` with context when supported."""
    base_kwargs: dict[str, Any] = {"audio": audio, "language": language}
    context = (context or "").strip()
    if context:
        try:
            params = inspect.signature(model.transcribe).parameters
            has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        except Exception:
            params = {}
            has_var_kw = True
        for key in ("context", "prompt", "hotwords", "text_context", "context_text"):
            if params and key not in params and not has_var_kw:
                continue
            try:
                return model.transcribe(**base_kwargs, **{key: context})
            except TypeError:
                continue
    return model.transcribe(**base_kwargs)

def _send_json(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)

def _qwen_audio(payload: dict[str, Any]) -> str:
    audio = str(payload.get("audio") or payload.get("wav") or "").strip()
    if not audio:
        raise sidecar_http.RequestError(400, "missing audio")
    if sidecar_http.is_remote_url(audio):
        raise sidecar_http.RequestError(400, "remote audio URLs are disabled")
    if not Path(audio).is_file():
        raise sidecar_http.RequestError(400, f"audio not found or not a regular file: {audio}")
    return audio

def _qwen_language(payload: dict[str, Any]) -> Any:
    language = payload.get("language", _language())
    if isinstance(language, str) and not language.strip():
        return None
    return language

def _classified_error_payload(detail: str) -> dict[str, str]:
    """Build the 500 body, upgrading generic triton/compiler errors.

    Triton swallows gcc's stderr (the ``fatal error: Python.h`` line only
    lands in the server log), so the HTTP-visible exception is a bare
    ``CalledProcessError: ... returned non-zero exit status``.  When that
    generic text is all we have, probe the running interpreter for Python.h
    and, if missing, reclassify as ``python_headers`` with a version-matched
    fix command instead of a multi-cause guess.
    """
    category = classify_error(detail)[0]
    if category in {"triton_compile", "compiler"}:
        hint = missing_python_header_hint()
        if hint:
            category = "python_headers"
            detail = f"{detail}\n{hint}"
    return {"error": detail, "error_code": category}


def _handle_transcribe(handler: BaseHTTPRequestHandler) -> None:
    # Reject malformed/oversized/remote input BEFORE importing/loading the GPU
    # model or taking its lock. Invalid requests must never trigger heavy work.
    payload = sidecar_http.read_json_payload(handler.headers, handler.rfile)
    audio = _qwen_audio(payload)
    if _MANAGER is not None:
        _MANAGER.acquire_for_inference()
    try:
        # ``_MODEL`` is kept in sync by ``ModelManager._to_gpu_locked`` (via
        # ``_sync_module_model``) so it always points at the wrapper currently
        # on the GPU.  Read it fresh here after acquire.
        active = _MANAGER.active_model() if _MANAGER is not None else _MODEL
        if active is None or (_MANAGER is not None and _MANAGER.load_error):
            # acquire_for_inference 把失败原因存进 load_error（仅日志可见）；
            # 这里必须带出来，否则用户只看到自引用的泛化句，真实原因
            # （OOM/CPU torch/驱动不匹配/模型文件损坏）全被吞掉。
            detail = _MANAGER.load_error if _MANAGER is not None else ""
            raise RuntimeError(
                "no Qwen3-ASR model is active on the GPU"
                + (f": {detail}" if detail else "")
            )
        context = payload.get("context") if isinstance(payload.get("context"), str) else None
        started = time.monotonic()
        language = _qwen_language(payload)
        results = _transcribe_with_optional_context_model(active, audio, language, context)
        first = results[0] if results else None
        item = _result_to_dict(first) if first is not None else {"text": "", "language": language}
        item["elapsed"] = time.monotonic() - started
        active_id = _MANAGER.active_id if _MANAGER is not None else _MODEL_ID
        print(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Qwen3-ASR transcribed "
            f"model={active_id} language={item.get('language')} elapsed={item['elapsed']:.3f}s "
            f"chars={len(item.get('text') or '')}",
            flush=True,
        )
        _send_json(handler, 200, item)
    finally:
        if _MANAGER is not None:
            _MANAGER.release_after_inference()

def _handle_warm(handler: BaseHTTPRequestHandler) -> None:
    """Pre-load the best model onto the GPU without transcribing.

    Mirrors the model-management half of ``_handle_transcribe`` (acquire then
    release the manager lock) but skips the actual inference.  The runtime
    calls this when voice recording *begins* so the model is already resident
    on the GPU by the time the user stops talking, turning the first
    ``/transcribe`` after stop into a near-instant cache hit.  Safe to call
    repeatedly; ``acquire_for_inference`` no-ops when the wanted model is
    already active.
    """
    # Consume the (empty) request body so the connection can be reused.
    sidecar_http.read_json_payload(handler.headers, handler.rfile)
    if _MANAGER is not None:
        _MANAGER.acquire_for_inference()
    try:
        active = _MANAGER.active_model() if _MANAGER is not None else _MODEL
        error = _MANAGER.load_error if _MANAGER is not None else ""
        if active is None or error:
            raise RuntimeError("no Qwen3-ASR model is active on the GPU" + (f": {error}" if error else ""))
        active_id = _MANAGER.active_id if _MANAGER is not None else _MODEL_ID
        active_alias = _MANAGER.active_alias if _MANAGER is not None else None
        print(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Qwen3-ASR warm: "
            f"alias={active_alias} model={active_id}",
            flush=True,
        )
        _send_json(handler, 200, {"status": "warm", "model": active_id, "alias": active_alias, "loaded": True})
    finally:
        if _MANAGER is not None:
            _MANAGER.release_after_inference()

class Handler(BaseHTTPRequestHandler):
    server_version = "QwenASRSidecar/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {self.address_string()} {fmt % args}", flush=True)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in {"", "/health"}:
            if _MANAGER is not None:
                _MANAGER.touch()
            active_id = _MANAGER.active_id if _MANAGER is not None else _MODEL_ID
            loaded = (_MANAGER.active_model() if _MANAGER is not None else _MODEL) is not None
            error = _MANAGER.load_error if _MANAGER is not None else ""
            category, _ = classify_error(error) if error else ("", "")
            _send_json(self, 200, {
                "status": "error" if error else ("ready" if loaded else "idle"),
                "model": active_id, "loaded": loaded, "ready": loaded and not error,
                "error": error, "error_code": category,
            })
            return
        _send_json(self, 404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.rstrip("/")
        if path == "/warm":
            try:
                _handle_warm(self)
            except sidecar_http.RequestError as exc:
                _send_json(self, exc.status, {"error": exc.message})
            except Exception as exc:
                import traceback

                traceback.print_exc()
                _send_json(self, 500, _classified_error_payload(str(exc)))
            return
        if path != "/transcribe":
            _send_json(self, 404, {"error": "not found"})
            return
        try:
            _handle_transcribe(self)
        except sidecar_http.RequestError as exc:
            _send_json(self, exc.status, {"error": exc.message})
        except Exception as exc:
            import traceback

            traceback.print_exc()
            _send_json(self, 500, _classified_error_payload(str(exc)))


def _env_float(name: str, default: float) -> float:
    return config.env_float(name, default)


def _env_int(name: str, default: int) -> int:
    return config.env_int(name, default)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--host", default=config.env_str("VOICE_IME_QWEN_ASR_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=config.env_int("VOICE_IME_QWEN_ASR_PORT", 18081))
    args = parser.parse_args()

    global _MODEL_ID, _MANAGER
    _MODEL_ID = args.model

    # 首装常见问题前置检查：本地模型路径不存在 / venv 缺 qwen_asr 依赖。
    # 早失败 + 可读错误，让 runtime 的 poll 分支给出正确指引，而不是服务
    # 假装健康、识别时才报英文 500。
    model_path = Path(args.model).expanduser()
    if model_path.is_absolute() and not model_path.is_dir():
        print(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 错误：模型目录不存在：{args.model}\n"
            "请先运行 ./scripts/setup-qwen-asr.sh 下载模型，"
            "或用 ./scripts/switch-mimo-cloud-asr.sh cn 切换云端后端。",
            flush=True,
        )
        sys.exit(2)
    try:
        import qwen_asr  # noqa: F401
    except Exception as exc:
        print(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 错误：当前 Python 无法导入 qwen_asr（{exc}）。\n"
            "请用 setup-qwen-asr.sh 创建的 .venv-qwen-asr 运行本服务"
            "（VOICE_IME_QWEN_ASR_PYTHON 指向该 venv 的 python）。",
            flush=True,
        )
        sys.exit(2)
    # Early, loud warning when the interpreter lacks Python.h: Triton will try
    # to compile its CUDA driver helper on the first kernel launch and fail
    # with a generic CalledProcessError.  Do not exit — a warm ~/.triton cache
    # may already hold a prebuilt helper, in which case everything still works.
    header_hint = missing_python_header_hint()
    if header_hint:
        print(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 警告 [python_headers]：{header_hint}",
            flush=True,
        )
    # Resolve the primary model and, if its sibling size is also on disk, a
    # secondary so the manager can auto-downgrade on VRAM pressure.  Loading is
    # deferred to the first request so the manager can probe free VRAM first
    # (avoiding a wasteful ~10s disk->VRAM load that would OOM anyway).
    primary = args.model
    secondary = _companion_path(primary)
    if secondary:
        print(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
            f"Qwen3-ASR primary={primary} secondary(downgrade target)={secondary}",
            flush=True,
        )

    # Default idle timeout is short (5s): RAM->GPU reload is ~1s, so releasing
    # VRAM aggressively between dictations keeps the GPU free for other work
    # (e.g. ComfyUI) with only a small reload cost on the next utterance.  Set
    # VOICE_IME_QWEN_ASR_IDLE_TIMEOUT=0 to disable the watchdog entirely.
    idle_timeout = _env_float("VOICE_IME_QWEN_ASR_IDLE_TIMEOUT", 5.0)
    interval = _env_float("VOICE_IME_QWEN_ASR_IDLE_CHECK_INTERVAL", 1.0)
    vram_min_17b = _env_int("VOICE_IME_QWEN_ASR_VRAM_MIN_MIB_1_7B", 5000)
    vram_min_06b = _env_int("VOICE_IME_QWEN_ASR_VRAM_MIN_MIB_0_6B", 2000)
    offload_target = config.env_str("VOICE_IME_QWEN_ASR_DEVICE_OFFLOAD_TARGET", "cpu").strip() or "cpu"
    _MANAGER = ModelManager(
        primary_path=primary,
        secondary_path=secondary,
        idle_timeout=idle_timeout,
        check_interval=interval,
        vram_min_17b_mib=vram_min_17b,
        vram_min_06b_mib=vram_min_06b,
        offload_target=offload_target,
    )
    _MANAGER.start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Qwen3-ASR server listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    finally:
        _MANAGER.stop()


if __name__ == "__main__":
    main()
