# -*- coding: utf-8 -*-
"""Chinese script normalization for voice input.

ASR backends usually expose only a broad "Chinese" language option and may
occasionally return Traditional Chinese.  This module provides a final,
deterministic guard before committing voice text.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import re
from pathlib import Path

_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_OPENCC_LIB = None
_OPENCC_HANDLES: dict[str, int] = {}
_OPENCC_ERROR = ctypes.c_void_p(-1).value

_SIMPLIFIED_ALIASES = {"simplified", "simp", "simple", "zh-hans", "hans", "cn", "zh_cn", "简体", "簡體"}
_TRADITIONAL_ALIASES = {"traditional", "trad", "zh-hant", "hant", "tw", "hk", "zh_tw", "zh_hk", "繁体", "繁體"}
_DISABLED_ALIASES = {"", "none", "off", "0", "false", "disabled", "disable"}


def _mode() -> str:
    raw = os.environ.get("VOICE_IME_CHINESE_SCRIPT", "simplified").strip().lower()
    if raw in _DISABLED_ALIASES:
        return "none"
    if raw in _TRADITIONAL_ALIASES:
        return "traditional"
    # Prefer simplified for unknown non-empty values because this option is a
    # user-facing guard against accidental Traditional Chinese output.
    return "simplified"


def _config_for_mode(mode: str) -> str | None:
    if mode == "simplified":
        configured = os.environ.get("VOICE_IME_OPENCC_T2S_CONFIG", "").strip()
        if configured:
            return configured
        return "/usr/share/opencc/t2s.json"
    if mode == "traditional":
        configured = os.environ.get("VOICE_IME_OPENCC_S2T_CONFIG", "").strip()
        if configured:
            return configured
        return "/usr/share/opencc/s2t.json"
    return None


def _load_lib():
    global _OPENCC_LIB
    if _OPENCC_LIB is not None:
        return _OPENCC_LIB
    name = ctypes.util.find_library("opencc") or "libopencc.so.1.1"
    lib = ctypes.CDLL(name)
    lib.opencc_open.argtypes = [ctypes.c_char_p]
    lib.opencc_open.restype = ctypes.c_void_p
    lib.opencc_close.argtypes = [ctypes.c_void_p]
    lib.opencc_close.restype = ctypes.c_int
    lib.opencc_convert_utf8.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
    lib.opencc_convert_utf8.restype = ctypes.c_void_p
    lib.opencc_convert_utf8_free.argtypes = [ctypes.c_void_p]
    lib.opencc_convert_utf8_free.restype = None
    _OPENCC_LIB = lib
    return lib


def _open_handle(config: str) -> int | None:
    config = str(Path(config).expanduser())
    if config in _OPENCC_HANDLES:
        return _OPENCC_HANDLES[config]
    if not Path(config).exists():
        return None
    try:
        lib = _load_lib()
        handle = lib.opencc_open(config.encode("utf-8"))
    except Exception:
        return None
    if not handle or handle == _OPENCC_ERROR:
        return None
    _OPENCC_HANDLES[config] = int(handle)
    return int(handle)


def _convert_opencc(text: str, config: str) -> str | None:
    handle = _open_handle(config)
    if not handle:
        return None
    raw = text.encode("utf-8")
    lib = _load_lib()
    out_ptr = lib.opencc_convert_utf8(ctypes.c_void_p(handle), raw, len(raw))
    if not out_ptr or out_ptr == _OPENCC_ERROR:
        return None
    try:
        return ctypes.string_at(out_ptr).decode("utf-8")
    finally:
        lib.opencc_convert_utf8_free(out_ptr)


def normalize(text: str, *, mode: str | None = None) -> str:
    """Normalize Chinese script according to VOICE_IME_CHINESE_SCRIPT.

    Defaults to Simplified Chinese.  If OpenCC is unavailable, returns the input
    unchanged rather than breaking voice input.
    """
    if not text or not _CJK_RE.search(text):
        return text
    selected = (mode or _mode()).strip().lower()
    if selected in _DISABLED_ALIASES or selected == "none":
        return text
    if selected in _TRADITIONAL_ALIASES:
        selected = "traditional"
    else:
        selected = "simplified"
    config = _config_for_mode(selected)
    if not config:
        return text
    converted = _convert_opencc(text, config)
    return converted if converted is not None else text


__all__ = ["normalize"]
