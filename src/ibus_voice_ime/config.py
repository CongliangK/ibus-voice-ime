# -*- coding: utf-8 -*-
"""Unified JSON configuration for ibus-voice-ime.

Configuration sources, per-key priority (first hit wins):

1. process environment ``VOICE_IME_*`` (rescue values / BWS injection / debug
   overrides — keeps full backward compatibility with the env-only era);
2. the user's sparse override file ``~/.config/ibus-voice-ime/config.json``
   (override its location with ``VOICE_IME_CONFIG``);
3. the versioned repository defaults ``config/defaults.json``;
4. the ``default`` argument passed by the call site (last resort; keep it in
   sync with defaults.json).

API keys and other secrets never live in these files; they are injected at
runtime via environment variables / Bitwarden Secrets Manager (see
run-engine.sh and llm_cloud_config.py for the llm.json connection file).

``null`` leaves in defaults.json mean "no static default": the value falls
through to the call-site default.  This is used where the historic default is
computed at runtime (e.g. ``ui.inline_candidates`` defaults to the candidate
page size) or where the variable participates in a legacy environment
fallback chain (e.g. ``asr.qwen3.language`` falls back to the whisper
language).  User config.json values still override normally.

``asr.backend`` is the single source of truth for channel selection (default
``qwen3-asr``); ``VOICE_IME_ASR_BACKEND`` and the legacy per-backend flags
(``VOICE_IME_QWEN_ASR`` etc.) remain env overrides with higher priority.

The engine process only needs the standard library (json/os/threading).

CLI (for the shell integration layer)::

    env python3 -m ibus_voice_ime.config get asr.backend
    env python3 -m ibus_voice_ime.config set asr.backend volc-bigmodel-asr
    env python3 -m ibus_voice_ime.config set llm.timeout 8 --float
    env python3 -m ibus_voice_ime.config set overlay.enabled 1 --bool
    env python3 -m ibus_voice_ime.config env
    env python3 -m ibus_voice_ime.config init
    env python3 -m ibus_voice_ime.config validate
    env python3 -m ibus_voice_ime.config migrate-env-file ~/.config/environment.d/ibus-voice-ime.conf
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

__all__ = [
    "ENV_MAP",
    "env_bool",
    "env_float",
    "env_int",
    "env_str",
    "get",
    "get_prompt",
    "load_defaults",
    "load_user_config",
    "reload",
    "save",
    "set",
    "user_config_path",
]

# src/ibus_voice_ime/config.py -> repo root (config/defaults.json lives there).
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULTS_PATH = _REPO_ROOT / "config" / "defaults.json"
_USER_CONFIG_ENV = "VOICE_IME_CONFIG"
CONFIG_VERSION = 1

# Boolean literals with the exact semantics of the historic per-module
# ``_env_bool`` helpers ("0/false/no/off/disabled" is false; anything else,
# including the empty string, is true).
_FALSY = {"0", "false", "no", "off", "disabled"}

# env variable name -> dotted config path.  Every leaf of config/defaults.json
# (except "version") must appear in the value set, and every path here must
# exist in defaults.json — the bidirectional lock tested in tests/test_config.py.
ENV_MAP: dict[str, str] = {
    # ---- asr: global / command / sidecar -----------------------------------
    "VOICE_IME_ASR_BACKEND": "asr.backend",
    "VOICE_IME_ASR_CMD": "asr.command",
    "VOICE_IME_ASR_SIDECAR_MAX_BODY_BYTES": "asr.sidecar_max_body_bytes",
    # ---- asr.qwen3 (local Qwen3-ASR sidecar) --------------------------------
    "VOICE_IME_QWEN_ASR": "asr.qwen3.enabled",
    "VOICE_IME_QWEN_ASR_HOST": "asr.qwen3.host",
    "VOICE_IME_QWEN_ASR_PORT": "asr.qwen3.port",
    "VOICE_IME_QWEN_ASR_PYTHON": "asr.qwen3.python",
    "VOICE_IME_QWEN_ASR_MODEL": "asr.qwen3.model",
    "VOICE_IME_QWEN_ASR_MODEL_PATH": "asr.qwen3.model_path",
    "VOICE_IME_QWEN_ASR_LANGUAGE": "asr.qwen3.language",
    "VOICE_IME_QWEN_ASR_DTYPE": "asr.qwen3.dtype",
    "VOICE_IME_QWEN_ASR_DEVICE_MAP": "asr.qwen3.device_map",
    "VOICE_IME_QWEN_ASR_DEVICE_OFFLOAD_TARGET": "asr.qwen3.device_offload_target",
    "VOICE_IME_QWEN_ASR_MAX_BATCH": "asr.qwen3.max_batch",
    "VOICE_IME_QWEN_ASR_MAX_NEW_TOKENS": "asr.qwen3.max_new_tokens",
    "VOICE_IME_QWEN_ASR_ATTN": "asr.qwen3.attn",
    "VOICE_IME_QWEN_ASR_START_TIMEOUT": "asr.qwen3.start_timeout",
    "VOICE_IME_QWEN_ASR_TIMEOUT": "asr.qwen3.timeout",
    "VOICE_IME_QWEN_ASR_FAIL_COOLDOWN": "asr.qwen3.fail_cooldown",
    "VOICE_IME_QWEN_ASR_IDLE_TIMEOUT": "asr.qwen3.idle_timeout",
    "VOICE_IME_QWEN_ASR_IDLE_CHECK_INTERVAL": "asr.qwen3.idle_check_interval",
    "VOICE_IME_QWEN_ASR_VRAM_MIN_MIB_1_7B": "asr.qwen3.vram_min_mib_1_7b",
    "VOICE_IME_QWEN_ASR_VRAM_MIN_MIB_0_6B": "asr.qwen3.vram_min_mib_0_6b",
    # ---- asr.mimo (local MiMo sidecar) --------------------------------------
    "VOICE_IME_MIMO_ASR": "asr.mimo.enabled",
    "VOICE_IME_MIMO_ASR_HOST": "asr.mimo.host",
    "VOICE_IME_MIMO_ASR_PORT": "asr.mimo.port",
    "VOICE_IME_MIMO_ASR_PYTHON": "asr.mimo.python",
    "VOICE_IME_MIMO_ASR_MODELS_DIR": "asr.mimo.models_dir",
    "VOICE_IME_MIMO_ASR_MODEL": "asr.mimo.model",
    "VOICE_IME_MIMO_ASR_MODEL_PATH": "asr.mimo.model_path",
    "VOICE_IME_MIMO_ASR_TOKENIZER": "asr.mimo.tokenizer",
    "VOICE_IME_MIMO_ASR_TOKENIZER_PATH": "asr.mimo.tokenizer_path",
    "VOICE_IME_MIMO_ASR_SOURCE": "asr.mimo.source",
    "VOICE_IME_MIMO_ASR_DEVICE": "asr.mimo.device",
    "VOICE_IME_MIMO_ASR_START_TIMEOUT": "asr.mimo.start_timeout",
    "VOICE_IME_MIMO_ASR_TIMEOUT": "asr.mimo.timeout",
    "VOICE_IME_MIMO_ASR_LANGUAGE": "asr.mimo.language",
    "VOICE_IME_MIMO_ASR_AUDIO_TAG": "asr.mimo.audio_tag",
    # ---- asr.mimo_cloud ------------------------------------------------------
    "VOICE_IME_MIMO_CLOUD_ASR": "asr.mimo_cloud.enabled",
    "VOICE_IME_MIMO_CLOUD_BASE_URL": "asr.mimo_cloud.base_url",
    "VOICE_IME_MIMO_CLOUD_ASR_MODEL": "asr.mimo_cloud.model",
    "VOICE_IME_MIMO_CLOUD_ASR_LANGUAGE": "asr.mimo_cloud.language",
    "VOICE_IME_MIMO_CLOUD_AUTH_HEADER": "asr.mimo_cloud.auth_header",
    "VOICE_IME_MIMO_CLOUD_ASR_TIMEOUT": "asr.mimo_cloud.timeout",
    "VOICE_IME_MIMO_CLOUD_ASR_MAX_DATA_MB": "asr.mimo_cloud.max_data_mb",
    "VOICE_IME_MIMO_API_KEY_SECRET": "asr.mimo_cloud.api_key_secret",
    # ---- asr.volc (Volcano bigmodel) -----------------------------------------
    "VOICE_IME_VOLC_BIGMODEL_ASR": "asr.volc.enabled",
    "VOICE_IME_VOLC_BIGMODEL_BASE_URL": "asr.volc.base_url",
    "VOICE_IME_VOLC_BIGMODEL_RESOURCE_ID": "asr.volc.resource_id",
    "VOICE_IME_VOLC_BIGMODEL_MODEL_NAME": "asr.volc.model_name",
    "VOICE_IME_VOLC_BIGMODEL_LANGUAGE": "asr.volc.language",
    "VOICE_IME_VOLC_BIGMODEL_ENABLE_ITN": "asr.volc.enable_itn",
    "VOICE_IME_VOLC_BIGMODEL_ENABLE_PUNC": "asr.volc.enable_punc",
    "VOICE_IME_VOLC_BIGMODEL_ENABLE_DDC": "asr.volc.enable_ddc",
    "VOICE_IME_VOLC_BIGMODEL_SHOW_UTTERANCES": "asr.volc.show_utterances",
    "VOICE_IME_VOLC_BIGMODEL_HOTWORDS": "asr.volc.hotwords",
    "VOICE_IME_VOLC_BIGMODEL_HOTWORDS_MAX": "asr.volc.hotwords_max",
    "VOICE_IME_VOLC_BIGMODEL_BOOSTING_TABLE": "asr.volc.boosting_table",
    "VOICE_IME_VOLC_BIGMODEL_CORRECT_TABLE": "asr.volc.correct_table",
    "VOICE_IME_VOLC_BIGMODEL_SUBMIT_TIMEOUT": "asr.volc.submit_timeout",
    "VOICE_IME_VOLC_BIGMODEL_QUERY_TIMEOUT": "asr.volc.query_timeout",
    "VOICE_IME_VOLC_BIGMODEL_POLL_INTERVAL": "asr.volc.poll_interval",
    "VOICE_IME_VOLC_BIGMODEL_TOTAL_TIMEOUT": "asr.volc.total_timeout",
    "VOICE_IME_VOLC_BIGMODEL_MAX_DATA_MB": "asr.volc.max_data_mb",
    "VOICE_IME_VOLC_API_KEY_SECRET": "asr.volc.api_key_secret",
    # ---- asr.siliconflow ------------------------------------------------------
    "VOICE_IME_SILICONFLOW_ASR": "asr.siliconflow.enabled",
    "VOICE_IME_SILICONFLOW_BASE_URL": "asr.siliconflow.base_url",
    "VOICE_IME_SILICONFLOW_MODEL": "asr.siliconflow.model",
    "VOICE_IME_SILICONFLOW_TIMEOUT": "asr.siliconflow.timeout",
    "VOICE_IME_SILICONFLOW_MAX_DATA_MB": "asr.siliconflow.max_data_mb",
    "VOICE_IME_SILICONFLOW_API_KEY_SECRET": "asr.siliconflow.api_key_secret",
    # ---- asr.whisper (diagnostic STT) / vosk ---------------------------------
    "VOICE_IME_WHISPER_MODEL": "asr.whisper.model",
    "VOICE_IME_WHISPER_DEVICE": "asr.whisper.device",
    "VOICE_IME_WHISPER_DEVICE_INDEX": "asr.whisper.device_index",
    "VOICE_IME_WHISPER_COMPUTE": "asr.whisper.compute",
    "VOICE_IME_WHISPER_LANGUAGE": "asr.whisper.language",
    "VOICE_IME_WHISPER_PROMPT": "asr.whisper.prompt",
    "VOICE_IME_REQUIRE_GPU_STT": "asr.whisper.require_gpu",
    "VOICE_IME_VOSK_MODEL": "asr.vosk.model",
    # ---- llm behaviour (non-secret) -------------------------------------------
    "VOICE_IME_LLM_POSTPROCESS": "llm.postprocess",
    "VOICE_IME_LLM_INTERNAL": "llm.internal",
    "VOICE_IME_LLM_RERANK": "llm.rerank",
    "VOICE_IME_LLM_TRUST_OUTPUT": "llm.trust_output",
    "VOICE_IME_LLM_AGGRESSIVE": "llm.aggressive",
    "VOICE_IME_LLM_BASE_URL": "llm.base_url",
    "VOICE_IME_LLM_MODEL": "llm.model",
    "VOICE_IME_LLM_MAX_TOKENS": "llm.max_tokens",
    "VOICE_IME_LLM_TIMEOUT": "llm.timeout",
    "VOICE_IME_LLM_TEMPERATURE": "llm.temperature",
    "VOICE_IME_LLM_MIN_CHARS": "llm.min_chars",
    "VOICE_IME_LLM_CANDIDATES": "llm.candidates",
    "VOICE_IME_LLM_PROTECT_TOKENS": "llm.protect_tokens",
    "VOICE_IME_LLM_STRICT_SAFETY": "llm.strict_safety",
    "VOICE_IME_LLM_MAX_EXPANSION_RATIO": "llm.max_expansion_ratio",
    "VOICE_IME_LLM_CONSERVATIVE_MARGIN": "llm.conservative_margin",
    "VOICE_IME_LLM_CONSERVATIVE_MIN_SIMILARITY": "llm.conservative_min_similarity",
    "VOICE_IME_LLM_CONSERVATIVE_MIN_RATIO": "llm.conservative_min_ratio",
    "VOICE_IME_LLM_CONSERVATIVE_MAX_RATIO": "llm.conservative_max_ratio",
    "VOICE_IME_LLM_FALLBACK_RAW": "llm.fallback_raw",
    "VOICE_IME_LLM_EXTRA_PROMPT": "llm.extra_prompt",
    "VOICE_IME_LLM_TERMS_MAX_CHARS": "llm.terms_max_chars",
    # ---- llm.llama_legacy (deprecated local llama.cpp sidecar) ----------------
    "VOICE_IME_LLAMA_HOST": "llm.llama_legacy.host",
    "VOICE_IME_LLAMA_PORT": "llm.llama_legacy.port",
    "VOICE_IME_LLAMA_SERVER": "llm.llama_legacy.server",
    "VOICE_IME_LLAMA_MODEL_PATH": "llm.llama_legacy.model_path",
    "VOICE_IME_LLAMA_CTX_SIZE": "llm.llama_legacy.ctx_size",
    "VOICE_IME_LLAMA_THREADS": "llm.llama_legacy.threads",
    "VOICE_IME_LLAMA_BATCH_SIZE": "llm.llama_legacy.batch_size",
    "VOICE_IME_LLAMA_N_GPU_LAYERS": "llm.llama_legacy.n_gpu_layers",
    "VOICE_IME_LLAMA_NO_WEBUI": "llm.llama_legacy.no_webui",
    "VOICE_IME_LLAMA_JINJA": "llm.llama_legacy.jinja",
    "VOICE_IME_LLAMA_REASONING": "llm.llama_legacy.reasoning",
    "VOICE_IME_LLAMA_CHAT_TEMPLATE_KWARGS": "llm.llama_legacy.chat_template_kwargs",
    "VOICE_IME_LLAMA_ARGS": "llm.llama_legacy.args",
    "VOICE_IME_LLAMA_START_TIMEOUT": "llm.llama_legacy.start_timeout",
    # ---- recording / hotkeys / VAD ---------------------------------------------
    "VOICE_IME_TRIGGER_MODE": "recording.trigger_mode",
    "VOICE_IME_HOTKEYS": "recording.hotkeys",
    "VOICE_IME_HOTKEY": "recording.hotkey",
    "VOICE_IME_RAW_HOTKEYS": "recording.raw_hotkeys",
    "VOICE_IME_CLIPBOARD_HOTKEYS": "recording.clipboard_hotkeys",
    "VOICE_IME_CLIPBOARD_HOTKEY": "recording.clipboard_hotkey",
    "VOICE_IME_CLIPBOARD_MAX_CHARS": "recording.clipboard_max_chars",
    "VOICE_IME_CLIPBOARD_PREPARE_DELAY_SECONDS": "recording.clipboard_prepare_delay_seconds",
    "VOICE_IME_CLIPBOARD_PREPARE_DELAY_MS": "recording.clipboard_prepare_delay_ms",
    "VOICE_IME_CLIPBOARD_PREPARE_HINT_MS": "recording.clipboard_prepare_hint_ms",
    "VOICE_IME_ARECORD_DEVICE": "recording.arecord_device",
    "VOICE_IME_VOICE_MODE": "recording.voice_mode",
    "VOICE_IME_RECORD_SECONDS": "recording.record_seconds",
    "VOICE_IME_MAX_RECORD_SECONDS": "recording.max_seconds",
    "VOICE_IME_MIN_RECORD_SECONDS": "recording.min_record_seconds",
    "VOICE_IME_SILENCE_STOP_MS": "recording.silence_stop_ms",
    "VOICE_IME_NO_SPEECH_TIMEOUT": "recording.no_speech_timeout",
    "VOICE_IME_VAD_AUTO_STOP": "recording.vad_auto_stop",
    "VOICE_IME_VAD_FALLBACK_FIXED": "recording.vad_fallback_fixed",
    "VOICE_IME_VAD_RMS_THRESHOLD": "recording.vad_rms_threshold",
    "VOICE_IME_TOGGLE_SILENCE_AUTO_STOP": "recording.toggle_silence_auto_stop",
    "VOICE_IME_TOGGLE_SOUND_LEVEL": "recording.toggle_sound_level",
    "VOICE_IME_TOGGLE_MIN_RECORD_SECONDS": "recording.toggle_min_record_seconds",
    "VOICE_IME_TOGGLE_SILENCE_SECONDS": "recording.toggle_silence_seconds",
    "VOICE_IME_TOGGLE_NO_SPEECH_TIMEOUT": "recording.toggle_no_speech_timeout",
    # ---- audio preprocessing ----------------------------------------------------
    "VOICE_IME_AUDIO_PREPROCESS": "audio.preprocess",
    "VOICE_IME_AUDIO_HIGHPASS": "audio.highpass",
    "VOICE_IME_AUDIO_HIGHPASS_FREQ": "audio.highpass_freq",
    "VOICE_IME_AUDIO_NOTCH": "audio.notch",
    "VOICE_IME_AUDIO_DENOISE": "audio.denoise",
    "VOICE_IME_AUDIO_DENOISE_AMOUNT": "audio.denoise_amount",
    "VOICE_IME_AUDIO_NOISE_PROFILE_MS": "audio.noise_profile_ms",
    "VOICE_IME_AUDIO_NORMALIZE": "audio.normalize",
    "VOICE_IME_AUDIO_NORMALIZE_HEADROOM": "audio.normalize_headroom",
    "VOICE_IME_DENOISE_TIER": "audio.denoise_tier",
    "VOICE_IME_AUDIO_RNNOISE_MODEL": "audio.rnnoise_model",
    # ---- overlay ------------------------------------------------------------------
    "VOICE_IME_OVERLAY": "overlay.enabled",
    "VOICE_IME_OVERLAY_POSITION": "overlay.position",
    "VOICE_IME_OVERLAY_CATCH_HOTKEY": "overlay.catch_hotkey",
    "VOICE_IME_OVERLAY_BUTTONS": "overlay.buttons",
    "VOICE_IME_OVERLAY_RMS_FULL_SCALE": "overlay.rms_full_scale",
    # ---- ui -------------------------------------------------------------------------
    "VOICE_IME_CANDIDATE_UI": "ui.candidate_ui",
    "VOICE_IME_CANDIDATE_PAGE_SIZE": "ui.candidate_page_size",
    "VOICE_IME_CANDIDATE_ADAPTIVE_ANCHOR": "ui.candidate_adaptive_anchor",
    "VOICE_IME_CANDIDATE_ROW_PX": "ui.candidate_row_px",
    "VOICE_IME_CANDIDATE_PADDING_PX": "ui.candidate_padding_px",
    "VOICE_IME_CANDIDATE_GAP_PX": "ui.candidate_gap_px",
    "VOICE_IME_PREEDIT_MIRROR": "ui.preedit_mirror",
    "VOICE_IME_PREEDIT_LINE_PX": "ui.preedit_line_px",
    "VOICE_IME_INLINE_CANDIDATES": "ui.inline_candidates",
    "VOICE_IME_INLINE_LABEL_CHARS": "ui.inline_label_chars",
    "VOICE_IME_INLINE_MAX_CHARS": "ui.inline_max_chars",
    "VOICE_IME_COMMIT_DELAY_MS": "ui.commit_delay_ms",
    "VOICE_IME_KEYBOARD_BACKEND": "ui.keyboard_backend",
    "VOICE_IME_START_ASCII": "ui.start_ascii",
    "VOICE_IME_SHIFT_TOGGLE_ASCII": "ui.shift_toggle_ascii",
    "VOICE_IME_ASCII_SLASH": "ui.ascii_slash",
    # ---- text ------------------------------------------------------------------------
    "VOICE_IME_CHINESE_SCRIPT": "text.chinese_script",
    "VOICE_IME_OPENCC_T2S_CONFIG": "text.opencc_t2s_config",
    "VOICE_IME_OPENCC_S2T_CONFIG": "text.opencc_s2t_config",
    "VOICE_IME_CJK_LATIN_SPACE": "text.cjk_latin_space",
    "VOICE_IME_VOICE_COMMANDS": "text.voice_commands",
    "VOICE_IME_INLINE_VOICE_COMMANDS": "text.inline_voice_commands",
    "VOICE_IME_REMOVE_FILLERS": "text.remove_fillers",
    "VOICE_IME_VOICE_REPLACEMENTS": "text.voice_replacements",
    "VOICE_IME_AUTO_PUNCT_MAX_CJK_PER_CLAUSE": "text.auto_punct_max_cjk_per_clause",
    "VOICE_IME_ASR_CONTEXT_MAX_CHARS": "text.asr_context_max_chars",
    "VOICE_IME_VOICE_DICTIONARY": "text.voice_dictionary",
    "VOICE_IME_CHINESE_USER_DICT": "text.chinese_user_dict",
    "VOICE_IME_ENGLISH_USER_DICT": "text.english_user_dict",
    "VOICE_IME_VOICE_USE_ENGLISH_MEMORY": "text.english_memory_enabled",
    "VOICE_IME_VOICE_ENGLISH_MEMORY_MIN_FREQ": "text.english_memory_min_freq",
    "VOICE_IME_VOICE_ENGLISH_MEMORY_MAX_TERMS": "text.english_memory_max_terms",
    "VOICE_IME_VOICE_ENGLISH_MEMORY_MIN_LEN": "text.english_memory_min_len",
    "VOICE_IME_VOICE_ENGLISH_MEMORY_EXCLUDE_PINYIN": "text.english_memory_exclude_pinyin",
    # ---- rime ---------------------------------------------------------------------------
    "VOICE_IME_RIME_SCHEMA": "rime.schema",
    "VOICE_IME_RIME_LIBRARY": "rime.library",
    "VOICE_IME_RIME_SHARED_DATA_DIR": "rime.shared_data_dir",
    "VOICE_IME_RIME_STAGING_DIR": "rime.staging_dir",
    "VOICE_IME_RIME_USER_DATA_DIR": "rime.user_data_dir",
    "VOICE_IME_RIME_ALLOW_SYSTEM": "rime.allow_system",
    "VOICE_IME_RIME_LOG_LEVEL": "rime.log_level",
    "VOICE_IME_RIME_LOG_DIR": "rime.log_dir",
    # ---- logging --------------------------------------------------------------------------
    "VOICE_IME_LOG_KEEP_LINES": "logging.keep_lines",
    "VOICE_IME_LOG_DIR": "logging.log_dir",
    "VOICE_IME_ERROR_LOG": "logging.error_log",
    "VOICE_IME_LLM_LOG": "logging.llm_log",
    # ---- ipc -------------------------------------------------------------------------------
    "VOICE_IME_IPC": "ipc.enabled",
    "VOICE_IME_IPC_SOCKET": "ipc.socket_path",
    # ---- prompt overrides (env escape hatch for the externalized prompts) ------------------
    "VOICE_IME_PROMPT_SYSTEM": "llm.prompts.system",
    "VOICE_IME_PROMPT_CANDIDATE_SYSTEM": "llm.prompts.candidate_system",
    "VOICE_IME_PROMPT_FORMAT_REQUIREMENT": "llm.prompts.format_requirement",
    "VOICE_IME_PROMPT_MODE_DICTATION": "llm.prompts.mode_instructions.dictation",
    "VOICE_IME_PROMPT_MODE_LITERAL": "llm.prompts.mode_instructions.literal",
    "VOICE_IME_PROMPT_MODE_MARKDOWN": "llm.prompts.mode_instructions.markdown",
    "VOICE_IME_PROMPT_MODE_PROMPT": "llm.prompts.mode_instructions.prompt",
    "VOICE_IME_PROMPT_MODE_COMMAND": "llm.prompts.mode_instructions.command",
    "VOICE_IME_PROMPT_WHISPER_PUNCTUATION": "asr.prompts.whisper_punctuation",
}

# Reverse map for deriving the env override of a prompt path.
_PATH_TO_ENV: dict[str, str] = {path: env for env, path in ENV_MAP.items()}

# 路径型叶子（名称以 config/defaults.json 实际叶子为准）：config 层拿到
# 「非空且磁盘上不存在」的值时视为未配置（回落下一层，通常到调用点默认），
# 把 2026-08-28 打字事故的"陈旧路径自愈"防线恢复到配置层——仓库被迁移/
# 删除后残留的绝对路径不得压过新值。环境层的值不做此检查（run-engine.sh
# 保留自己的 env 自愈段，env 值是救援输入）。
_PATH_KEYS = frozenset({
    "rime.library",
    "rime.shared_data_dir",
    "rime.staging_dir",
    "rime.user_data_dir",
    "asr.qwen3.model_path",
    "asr.qwen3.python",
    "asr.mimo.model_path",
    "asr.mimo.python",
})
_WARNED_STALE_PATHS: set[str] = set()

# 明文密钥指纹（值命中即视为疑似 API Key：validate 告警 / env 输出脱敏）。
_SECRET_VALUE_RE = re.compile(r"sk-[A-Za-z0-9_-]{16,}")

_LOCK = threading.RLock()
_DEFAULTS_CACHE: dict | None = None
_USER_CACHE: dict | None = None
_MERGED_CACHE: dict | None = None


def _warn(message: str) -> None:
    print(f"[ibus-voice-ime config] {message}", file=sys.stderr, flush=True)


def _stale_path_guard(path: str, value: Any) -> Any:
    """Treat a non-existent path-leaf value as unset (warn once per key)."""
    raw = value if isinstance(value, str) else _to_str(value)
    if not raw.strip():
        return value  # 显式空串保持原语义（如 rime.library="" 走系统默认）
    if Path(raw).expanduser().exists():
        return value
    if path not in _WARNED_STALE_PATHS:  # 每键每进程只警告一次，防日志洪水
        _WARNED_STALE_PATHS.add(path)
        _warn(f"配置 {path} 指向不存在的路径（{raw}），视为未配置并回落下一层（陈旧路径自愈）")
    return None


def user_config_path() -> Path:
    """Location of the user override file (env VOICE_IME_CONFIG wins)."""
    raw = os.environ.get(_USER_CONFIG_ENV, "").strip()
    if raw:
        return Path(raw).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "ibus-voice-ime" / "config.json"


def load_defaults() -> dict:
    """Load the versioned defaults; never raises (warns to stderr on problems)."""
    global _DEFAULTS_CACHE
    with _LOCK:
        if _DEFAULTS_CACHE is not None:
            return _DEFAULTS_CACHE
        try:
            data = json.loads(DEFAULTS_PATH.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("defaults.json 根节点必须是 JSON 对象")
        except Exception as exc:
            _warn(f"无法读取默认配置 {DEFAULTS_PATH}（{exc}），将只使用环境变量与调用点默认值")
            data = {}
        _DEFAULTS_CACHE = data
        return data


def load_user_config() -> dict:
    """Load the user override file; corrupt/absent files count as empty.

    A broken user file must never crash the engine, so any problem is reported
    on stderr and treated as "file does not exist".
    """
    global _USER_CACHE
    with _LOCK:
        if _USER_CACHE is not None:
            return _USER_CACHE
        path = user_config_path()
        data: dict = {}
        try:
            if path.is_file():
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict):
                    raise ValueError("根节点必须是 JSON 对象")
                data = loaded
        except Exception as exc:
            _warn(f"用户配置 {path} 无法解析（{exc}），已忽略并使用默认配置")
            data = {}
        _USER_CACHE = data
        return data


def _deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _effective() -> dict:
    global _MERGED_CACHE
    with _LOCK:
        if _MERGED_CACHE is None:
            _MERGED_CACHE = _deep_merge(load_defaults(), load_user_config())
        return _MERGED_CACHE


def _lookup(path: str) -> Any:
    node: Any = _effective()
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def get(path: str, env: str | None = None, default: Any = None) -> Any:
    """Resolve one config value by dotted path with the 4-layer priority.

    ``env`` (when given) is checked first and returns the raw string exactly
    as the historic ``os.environ.get`` did.  JSON ``null`` counts as unset so
    the call-site ``default`` applies.  Path-typed leaves additionally drop
    values whose target does not exist on disk (stale-path self-healing).
    """
    if env:
        raw = os.environ.get(env)
        if raw is not None:
            return raw
    value = _lookup(path)
    if value is not None and path in _PATH_KEYS:
        value = _stale_path_guard(path, value)
    if value is None:
        return default
    return value


def _path_for(env_name: str, path: str | None) -> str | None:
    return path if path is not None else ENV_MAP.get(env_name)


def _to_str(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return value if isinstance(value, str) else str(value)


def env_str(env_name: str, default: str | None = "", path: str | None = None) -> str | None:
    """String helper: env > user json > defaults.json > default.

    Passing ``default=None`` requests a tri-state result: ``None`` means
    "nothing configured" so the caller can continue a legacy fallback chain
    with ``or``.
    """
    value = get(_path_for(env_name, path) or "", env=env_name, default=None)
    if value is None:
        value = default
    if value is None:
        return None
    return _to_str(value)


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() not in _FALSY


def _json_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _parse_bool(value)
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def env_bool(env_name: str, default: bool, path: str | None = None) -> bool:
    """Bool helper with the exact semantics of the historic ``_env_bool``."""
    raw = os.environ.get(env_name)
    if raw is not None:
        return _parse_bool(raw)
    value = _json_bool(_lookup(_path_for(env_name, path) or ""))
    if value is None:
        return default
    return value


def env_int(env_name: str, default: int, path: str | None = None, minimum: int | float | None = None) -> int:
    """Int helper: an invalid env value falls back like the historic ``_env_int``."""
    raw = os.environ.get(env_name)
    if raw is not None:
        try:
            value = int(raw)
        except ValueError:
            value = default
    else:
        json_value = _lookup(_path_for(env_name, path) or "")
        if isinstance(json_value, bool):
            value = int(json_value)
        elif isinstance(json_value, (int, float)):
            try:
                value = int(json_value)
            except (TypeError, ValueError):
                value = default
        elif isinstance(json_value, str) and json_value.strip():
            try:
                value = int(json_value.strip())
            except ValueError:
                value = default
        else:
            value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def env_float(env_name: str, default: float, path: str | None = None, minimum: float | None = None) -> float:
    """Float helper mirroring the historic ``_env_float`` semantics."""
    raw = os.environ.get(env_name)
    if raw is not None:
        try:
            value = float(raw)
        except ValueError:
            value = default
    else:
        json_value = _lookup(_path_for(env_name, path) or "")
        if isinstance(json_value, bool):
            value = float(json_value)
        elif isinstance(json_value, (int, float)):
            value = float(json_value)
        elif isinstance(json_value, str) and json_value.strip():
            try:
                value = float(json_value.strip())
            except ValueError:
                value = default
        else:
            value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def get_prompt(name: str, env: str | None = None) -> str:
    """Read an externalized prompt (``llm.prompts.*`` / ``asr.prompts.*``).

    Accepts a short name ("system", "whisper_punctuation", ...) or a full
    dotted path ("llm.prompts.system").  Missing prompts return "" with a
    stderr warning instead of raising — a broken prompt entry must never
    take down dictation.
    """
    if "." not in name:
        candidates = (f"llm.prompts.{name}", f"asr.prompts.{name}")
    else:
        candidates = (name,)
    for path in candidates:
        env_name = env or _PATH_TO_ENV.get(path)
        value = get(path, env=env_name, default=None)
        if value is not None:
            return value if isinstance(value, str) else _to_str(value)
    _warn(f"提示词 {name!r} 不存在于配置中，返回空串")
    return ""


# ---------------------------------------------------------------------------
# Mutation (user layer only)
# ---------------------------------------------------------------------------

def set(path: str, value: Any) -> None:  # noqa: A001 - configured API name
    """Set one value in the in-memory user layer (call save() to persist)."""
    global _MERGED_CACHE
    with _LOCK:
        user = load_user_config()
        node = user
        parts = path.split(".")
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[parts[-1]] = value
        _MERGED_CACHE = None


def save() -> Path:
    """Atomically write the user layer to disk (tempfile + os.replace)."""
    with _LOCK:
        user = load_user_config()
        path = user_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(user, f, ensure_ascii=False, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return path


def reload() -> None:  # noqa: A001 - configured API name
    """Drop all caches and re-read both layers (tests / config changes)."""
    global _DEFAULTS_CACHE, _USER_CACHE, _MERGED_CACHE
    with _LOCK:
        _DEFAULTS_CACHE = None
        _USER_CACHE = None
        _MERGED_CACHE = None


# ---------------------------------------------------------------------------
# CLI for the shell integration layer
# ---------------------------------------------------------------------------

def _cli_get(path: str) -> int:
    value = get(path)
    if value is None:
        print(f"未配置：{path}", file=sys.stderr)
        return 1
    if isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, ensure_ascii=False))
    return 0


def _coerce_cli_value(raw: str, type_hint: str | None, path: str) -> Any:
    if type_hint == "bool":
        if raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}:
            return True
        return False
    if type_hint == "int":
        return int(raw)
    if type_hint == "float":
        return float(raw)
    # No explicit hint: coerce by the defaults.json leaf type so the user file
    # keeps native JSON types (bool/number/string).
    reference = load_defaults()
    node: Any = reference
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            node = None
            break
        node = node[part]
    if isinstance(node, bool):
        return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}
    if isinstance(node, int):
        return int(raw)
    if isinstance(node, float):
        return float(raw)
    return raw


def _cli_set(path: str, raw_value: str, type_hint: str | None) -> int:
    try:
        value = _coerce_cli_value(raw_value, type_hint, path)
    except ValueError as exc:
        print(f"值无法按指定类型解析：{exc}", file=sys.stderr)
        return 2
    set(path, value)
    saved = save()
    reload()
    print(f"已写入 {path} = {json.dumps(value, ensure_ascii=False)} → {saved}")
    return 0


def _cli_env() -> int:
    for env_name in sorted(ENV_MAP):
        if "API_KEY" in env_name or "SECRET" in env_name:
            continue
        if ".prompts." in ENV_MAP[env_name]:
            continue  # 提示词是多行文本，放不进 KEY=VALUE 行；走 config.json 覆盖
        value = get(ENV_MAP[env_name], env=env_name, default=None)
        if value is None:
            continue
        if isinstance(value, bool):
            text = "1" if value else "0"
        elif isinstance(value, str):
            text = value
        else:
            text = _to_str(value)
        if _SECRET_VALUE_RE.match(text):
            text = "sk-****"  # 疑似明文密钥一律脱敏输出
        print(f"{env_name}={text}")
    return 0


def _cli_init() -> int:
    path = user_config_path()
    if path.exists():
        print(f"已存在，保持不动：{path}")
        return 0
    skeleton = {
        "_说明": (
            "ibus-voice-ime 用户配置。这里只放需要覆盖默认值的键（参考 config/defaults.json）；"
            "删除某个键即回落默认值。以 _ 开头的键是注释性占位，会被程序忽略。"
            "API Key 等密钥不要写在这里，仍通过环境变量 / BWS 注入。"
        ),
        "asr": {"_可选": "backend: qwen3-asr / mimo-cloud / volc-bigmodel-asr / siliconflow / whisper ..."},
        "llm": {"_可选": "timeout / min_chars / prompts.system 等行为项；连接配置在 llm.json"},
        "recording": {"_可选": "trigger_mode / hotkeys / voice_mode ..."},
        "audio": {"_可选": "denoise_tier: none / notch / rnnoise"},
        "overlay": {"_可选": "enabled / position / buttons"},
        "ui": {"_可选": "candidate_ui / commit_delay_ms ..."},
        "text": {"_可选": "chinese_script / cjk_latin_space ..."},
    }
    set("_说明", skeleton["_说明"])
    for group, body in skeleton.items():
        if group == "_说明":
            continue
        set(f"{group}._可选", body["_可选"])
    save()
    reload()
    print(f"已生成用户配置骨架：{path}")
    return 0


def _iter_leaves(node: dict, prefix: str = ""):
    for key, value in node.items():
        if key.startswith("_"):
            continue
        if isinstance(value, dict):
            yield from _iter_leaves(value, f"{prefix}{key}.")
        else:
            yield f"{prefix}{key}"


def _type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"  # JSON 不区分 int/float，数字槽位互通
    if isinstance(value, str):
        return "str"
    if value is None:
        return "null"
    return type(value).__name__


def _cli_validate() -> int:
    problems: list[str] = []
    path = user_config_path()
    if not path.is_file():
        print(f"没有用户配置（{path}）——视为全部默认，校验通过")
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"问题 1/1：用户配置 JSON 损坏：{path}（{exc}）")
        return 1
    if not isinstance(data, dict):
        print(f"问题 1/1：根节点必须是 JSON 对象")
        return 1
    defaults = load_defaults()
    known_top = {key for key in defaults if key != "version"}
    for key in data:
        if key.startswith("_"):
            continue
        if key not in known_top:
            problems.append(f"未知顶层键：{key}（不在 config/defaults.json 的分组里）")
    default_leaves = {leaf: None for leaf in _iter_leaves(defaults)}
    for leaf in _iter_leaves(data):
        if leaf not in default_leaves:
            group = leaf.split(".", 1)[0]
            if group not in known_top:
                continue  # already reported as unknown top-level
            problems.append(f"未知配置键：{leaf}")
    for leaf in _iter_leaves(data):
        if leaf in default_leaves:
            user_value = _lookup_user(data, leaf)
            default_value = _lookup_user(defaults, leaf)
            if default_value is None or user_value is None:
                continue
            if _type_name(user_value) != _type_name(default_value):
                problems.append(
                    f"类型不符：{leaf} 期望 {_type_name(default_value)}，实际 {_type_name(user_value)}"
                )
    # 密钥防御纵深：任何键的值长得像明文 API Key 都要点名——config.json
    # 从不存放密钥本体（运行时走 env/BWS 注入，见 docs/configuration.md）。
    for leaf in _iter_leaves(data):
        value = _lookup_user(data, leaf)
        if isinstance(value, str) and _SECRET_VALUE_RE.match(value.strip()):
            problems.append(
                f"疑似明文 API Key：{leaf}（config.json 不存放密钥；请改走环境变量/BWS 注入）"
            )
    if problems:
        for index, problem in enumerate(problems, 1):
            print(f"问题 {index}/{len(problems)}：{problem}")
        return 1
    print(f"用户配置校验通过：{path}")
    return 0


def _lookup_user(data: dict, leaf: str) -> Any:
    node: Any = data
    for part in leaf.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _coerce_migrate_value(raw: str, reference: Any) -> Any:
    if isinstance(reference, bool):
        return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}
    if isinstance(reference, int) and not isinstance(reference, bool):
        try:
            return int(raw)
        except ValueError:
            return raw
    if isinstance(reference, float):
        try:
            return float(raw)
        except ValueError:
            return raw
    return raw


def _cli_migrate_env_file(file_name: str) -> int:
    """Move mapped KEY=VALUE lines from an env file into the user config.json.

    The "cleaned" env file (mapped keys removed, everything else preserved) is
    printed to stdout; the caller (shell integration) redirects it back over
    the original file if it looks good.  Line-by-line disposition goes to
    stderr so nothing is silently dropped:

    - mapped, non-path keys  → migrated into config.json;
    - mapped path-typed keys → NOT migrated (kept in the env file, where the
      env-layer stale-path self-healing in run-engine.sh keeps covering them);
    - raw API key lines       → kept in stdout but flagged (never migrated);
    - unmapped keys           → kept in stdout and flagged for manual review.
    """
    env_path = Path(file_name).expanduser()
    if not env_path.is_file():
        print(f"env 文件不存在：{env_path}", file=sys.stderr)
        return 1
    defaults = load_defaults()
    migrated: list[str] = []
    kept: list[str] = []
    report: list[str] = []
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            kept.append(line)
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key not in ENV_MAP:
            kept.append(line)
            if "API_KEY" in key and "API_KEY_SECRET" not in key:
                report.append(f"{key}：密钥本体不迁入 config（本就不该出现在 env 文件；请改走 BWS/运行时注入）")
            else:
                report.append(f"{key}：无对应配置键（保留原行，请人工确认）")
            continue
        path = ENV_MAP[key]
        if path in _PATH_KEYS:
            # 路径型键不迁移：留在 env 层保住 run-engine.sh 的陈旧路径自愈。
            kept.append(line)
            report.append(f"{key}：路径型键不迁移（保留在 env 文件，维持自愈层）")
            continue
        reference = _lookup_user(defaults, path)
        set(path, _coerce_migrate_value(value.strip(), reference))
        migrated.append(f"{key} → {path}")
    if migrated:
        save()
        reload()
    for entry in migrated:
        print(f"# 已迁移到 config.json：{entry}", file=sys.stderr)
    for entry in report:
        print(f"# 未迁移：{entry}", file=sys.stderr)
    sys.stdout.write("\n".join(kept) + ("\n" if kept else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ibus_voice_ime.config",
        description="ibus-voice-ime 统一 JSON 配置（读取/写入/校验/env 文件迁移）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_get = sub.add_parser("get", help="打印某个点路径的生效值")
    p_get.add_argument("path")

    p_set = sub.add_parser("set", help="写入用户层并原子保存")
    p_set.add_argument("path")
    p_set.add_argument("value")
    p_set.add_argument("--bool", dest="type_hint", action="store_const", const="bool")
    p_set.add_argument("--int", dest="type_hint", action="store_const", const="int")
    p_set.add_argument("--float", dest="type_hint", action="store_const", const="float")

    sub.add_parser("env", help="打印全部生效值（KEY=VALUE 行）")
    sub.add_parser("init", help="生成带注释性占位键的用户 config.json 骨架")
    sub.add_parser("validate", help="校验用户 JSON（供 doctor 使用）")

    p_migrate = sub.add_parser("migrate-env-file", help="把 env 文件中的映射键迁入 config.json 并输出清理后的文件")
    p_migrate.add_argument("file")

    args = parser.parse_args(argv)
    if args.command == "get":
        return _cli_get(args.path)
    if args.command == "set":
        return _cli_set(args.path, args.value, args.type_hint)
    if args.command == "env":
        return _cli_env()
    if args.command == "init":
        return _cli_init()
    if args.command == "validate":
        return _cli_validate()
    if args.command == "migrate-env-file":
        return _cli_migrate_env_file(args.file)
    parser.error(f"未知命令：{args.command}")
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:  # `... env | head` 等管道消费者提前退出不是错误
        try:
            sys.stdout.close()
        finally:
            raise SystemExit(0)
