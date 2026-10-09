# -*- coding: utf-8 -*-
"""Shared voice terminology / hotword helpers.

This module unifies two existing sources:

1. ``VOICE_IME_VOICE_DICTIONARY`` / ``voice-dictionary.txt`` for explicit
   speech terms and known ASR confusions.
2. ``english.json`` learned by keyboard input.  Frequently typed English tokens
   are useful speech hotwords because they often reflect the user's real working
   vocabulary.

The enhanced dictionary line format is intentionally simple and backward
compatible::

    Canonical | alias1, alias2 | confusion1, confusion2

A plain line such as ``Open Code`` is also accepted.
"""
from __future__ import annotations

import json
import math
import os

from ibus_voice_ime import config
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_WORD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+.#:/\-]{1,63}$")


@dataclass(frozen=True)
class VoiceTerm:
    canonical: str
    aliases: tuple[str, ...] = ()
    confusions: tuple[str, ...] = ()
    source: str = "dictionary"
    weight: float = 1.0
    meta: dict[str, Any] = field(default_factory=dict)


def _env_bool(name: str, default: bool) -> bool:
    return config.env_bool(name, default)


def _env_int(name: str, default: int) -> int:
    return config.env_int(name, default)


def _split_items(text: str) -> tuple[str, ...]:
    items: list[str] = []
    for part in re.split(r"[,，;；]", text):
        item = part.strip()
        if item:
            items.append(item)
    return tuple(dict.fromkeys(items))


def _voice_dictionary_path() -> Path:
    return Path(config.env_str("VOICE_IME_VOICE_DICTIONARY", "~/.local/share/ibus-voice-ime/voice-dictionary.txt")).expanduser()


def _english_memory_path() -> Path:
    return Path(config.env_str("VOICE_IME_ENGLISH_USER_DICT", "~/.local/share/ibus-voice-ime/english.json")).expanduser()


def _load_dictionary_terms() -> list[VoiceTerm]:
    path = _voice_dictionary_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except Exception:
        return []

    terms: list[VoiceTerm] = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        canonical = parts[0] if parts else ""
        if not canonical:
            continue
        aliases = _split_items(parts[1]) if len(parts) >= 2 else ()
        confusions = _split_items(parts[2]) if len(parts) >= 3 else ()
        terms.append(VoiceTerm(canonical=canonical, aliases=aliases, confusions=confusions, source="dictionary", weight=10.0))
    return terms


def _load_english_memory_terms() -> list[VoiceTerm]:
    if not _env_bool("VOICE_IME_VOICE_USE_ENGLISH_MEMORY", True):
        return []
    path = _english_memory_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception:
        return []
    if not isinstance(raw, dict):
        return []

    min_freq = _env_int("VOICE_IME_VOICE_ENGLISH_MEMORY_MIN_FREQ", 2)
    max_terms = _env_int("VOICE_IME_VOICE_ENGLISH_MEMORY_MAX_TERMS", 80)
    min_len = _env_int("VOICE_IME_VOICE_ENGLISH_MEMORY_MIN_LEN", 2)
    exclude_pinyin_like = _env_bool("VOICE_IME_VOICE_ENGLISH_MEMORY_EXCLUDE_PINYIN", True)

    rows: list[tuple[int, float, str, dict[str, Any]]] = []
    for key, item in raw.items():
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or key).strip()
        if len(text) < min_len or not _WORD_RE.fullmatch(text):
            continue
        freq = int(item.get("freq", 0) or 0)
        if freq < min_freq:
            continue
        if exclude_pinyin_like and bool(item.get("pinyin_like")):
            continue
        last = float(item.get("last", 0.0) or 0.0)
        rows.append((freq, last, text, item))

    rows.sort(key=lambda x: (-x[0], -x[1], x[2].lower()))
    now = time.time()
    terms: list[VoiceTerm] = []
    for freq, last, text, item in rows[:max_terms]:
        # Keep the weight bounded: high enough to be visible in prompts/rerank,
        # not high enough to dominate explicit dictionary terms.
        recency = max(0.0, 1.0 - max(0.0, now - last) / (60 * 60 * 24 * 90)) if last else 0.0
        weight = 1.0 + min(4.0, math.log1p(freq)) + recency
        aliases = tuple(dict.fromkeys([text.lower()] if text.lower() != text else []))
        terms.append(
            VoiceTerm(
                canonical=text,
                aliases=aliases,
                confusions=(),
                source="english_memory",
                weight=weight,
                meta={"freq": freq, "last": last},
            )
        )
    return terms


def load_terms() -> list[VoiceTerm]:
    """Load merged terms, explicit dictionary first, then learned English memory."""
    merged: dict[str, VoiceTerm] = {}
    for term in _load_dictionary_terms() + _load_english_memory_terms():
        key = term.canonical.lower()
        old = merged.get(key)
        if old is None:
            merged[key] = term
            continue
        aliases = tuple(dict.fromkeys([*old.aliases, *term.aliases]))
        confusions = tuple(dict.fromkeys([*old.confusions, *term.confusions]))
        source = old.source if old.source == "dictionary" else term.source
        merged[key] = VoiceTerm(
            canonical=old.canonical,
            aliases=aliases,
            confusions=confusions,
            source=source,
            weight=max(old.weight, term.weight),
            meta={**term.meta, **old.meta},
        )
    return sorted(merged.values(), key=lambda t: (0 if t.source == "dictionary" else 1, -t.weight, t.canonical.lower()))


def _contains(text: str, phrase: str) -> bool:
    if not phrase:
        return False
    return phrase.lower() in text.lower()


def _signals(term: VoiceTerm) -> tuple[str, ...]:
    return tuple(dict.fromkeys([term.canonical, *term.aliases, *term.confusions]))


def build_asr_context(max_chars: int | None = None) -> str:
    """Build compact context for Qwen3-ASR / ASR prompt biasing."""
    if max_chars is None:
        max_chars = _env_int("VOICE_IME_ASR_CONTEXT_MAX_CHARS", 3000)
    terms = load_terms()

    lines = [
        "这是一段中文夹杂英文技术词、命令、项目名的语音。请保留英文技术词原文和大小写，不要翻译成中文。",
        "请更积极地输出自然中文标点：根据语义加入逗号、句号、问号、顿号、分号或冒号，避免整段无标点。",
        "除识别文字和标点外，不要额外解释。",
    ]
    if not terms:
        return "\n".join(lines).strip()[:max_chars]

    lines.extend([
        "如果发音接近下列术语，请优先输出标准写法。",
        "显式语音词典：",
    ])
    for term in [t for t in terms if t.source == "dictionary"]:
        line = f"- {term.canonical}"
        if term.aliases:
            line += "；别名：" + ", ".join(term.aliases[:6])
        if term.confusions:
            line += "；常见误识别：" + ", ".join(term.confusions[:8])
        lines.append(line)

    memory_terms = [t for t in terms if t.source == "english_memory"]
    if memory_terms:
        lines.append("用户键盘输入中高频英文 token，语音里也可能出现，按频率排序：")
        chunk: list[str] = []
        for term in memory_terms:
            freq = term.meta.get("freq")
            chunk.append(f"{term.canonical}({freq})" if freq else term.canonical)
        lines.append(", ".join(chunk))

    out = "\n".join(lines).strip()
    return out[:max_chars]


def build_llm_context(max_chars: int | None = None) -> str:
    if max_chars is None:
        max_chars = _env_int("VOICE_IME_LLM_TERMS_MAX_CHARS", 4000)
    terms = load_terms()
    if not terms:
        return ""
    lines = ["用户术语/热词表。只在上下文和发音相近时修正，不要凭空插入热词。"]
    for term in terms:
        if term.source == "dictionary":
            line = f"- 标准：{term.canonical}"
            if term.aliases:
                line += "；别名：" + ", ".join(term.aliases[:6])
            if term.confusions:
                line += "；可能误识别为：" + ", ".join(term.confusions[:8])
            lines.append(line)
    memory_terms = [t for t in terms if t.source == "english_memory"]
    if memory_terms:
        lines.append("用户经常键入的英文 token，语音后处理应优先保留这些写法：")
        lines.append(", ".join(t.canonical for t in memory_terms))
    return "\n".join(lines).strip()[:max_chars]


def hotword_score(raw_text: str, candidate: str) -> tuple[float, list[str]]:
    """Return rerank score adjustments for hotword-aware correction."""
    score = 0.0
    reasons: list[str] = []
    for term in load_terms():
        cand_has = _contains(candidate, term.canonical) or any(_contains(candidate, a) for a in term.aliases)
        raw_has_canonical = _contains(raw_text, term.canonical) or any(_contains(raw_text, a) for a in term.aliases)
        raw_conf = [c for c in term.confusions if _contains(raw_text, c)]
        if cand_has and raw_conf:
            bonus = 28.0 if term.source == "dictionary" else 10.0
            score += bonus
            reasons.append(f"hotword_confusion({raw_conf[0]}->{term.canonical}):+{bonus:.0f}")
        elif cand_has and raw_has_canonical:
            bonus = 8.0 if term.source == "dictionary" else min(6.0, term.weight)
            score += bonus
            reasons.append(f"hotword_keep({term.canonical}):+{bonus:.0f}")
        elif cand_has and not raw_has_canonical and not raw_conf:
            # Explicit terms are allowed to appear when the LLM recovered them,
            # but unsupported insertion should not be too attractive.
            penalty = 14.0 if term.source == "dictionary" else 4.0
            score -= penalty
            reasons.append(f"unsupported_hotword_insert({term.canonical}):-{penalty:.0f}")
    return score, reasons


def is_allowed_hotword_replacement(raw_text: str, candidate: str, missing_token: str) -> bool:
    """Whether a protected raw token may disappear due to hotword correction."""
    token = missing_token.lower()
    if not token or token not in raw_text.lower():
        return False
    for term in load_terms():
        if not (_contains(candidate, term.canonical) or any(_contains(candidate, a) for a in term.aliases)):
            continue
        for phrase in _signals(term):
            p = phrase.lower()
            if token in p and _contains(raw_text, phrase):
                return True
        # Also allow replacing one token inside a multi-token confusion phrase,
        # e.g. raw "open cold" -> candidate "Open Code" lets "cold" vanish.
        for confusion in term.confusions:
            if token in re.findall(r"[A-Za-z][A-Za-z0-9_+.#:/\-]*", confusion.lower()) and _contains(raw_text, confusion):
                return True
    return False


__all__ = [
    "VoiceTerm",
    "build_asr_context",
    "build_llm_context",
    "hotword_score",
    "is_allowed_hotword_replacement",
    "load_terms",
]
