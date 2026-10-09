# -*- coding: utf-8 -*-
"""Persistent user priority for Chinese candidates.

Rime has its own user dictionary, but this small overlay gives our custom IME a
predictable rule: once the user commits a Chinese candidate for a raw code, it
is remembered and ranked before the default Rime candidates next time.
"""
from __future__ import annotations

import json
import os

from ibus_voice_ime import config
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ChineseCandidate:
    text: str
    comment: str = ""
    freq: int = 0
    last: float = 0.0


CJK_RE = re.compile(r"[\u3400-\u9fff]")
CODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9']{0,63}$")


def has_cjk(text: str) -> bool:
    return bool(CJK_RE.search(text))


def starts_with_cjk(text: str) -> bool:
    return bool(text and CJK_RE.match(text[0]))


class ChineseMemory:
    def __init__(self, path: str | Path | None = None):
        if path is None:
            path = config.env_str("VOICE_IME_CHINESE_USER_DICT", "~/.local/share/ibus-voice-ime/chinese.json")
        self.path = Path(path).expanduser()
        self.items: dict[str, dict[str, dict[str, Any]]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.items = {
                    str(code).lower(): {str(text): meta for text, meta in entries.items() if isinstance(meta, dict)}
                    for code, entries in raw.items()
                    if isinstance(entries, dict)
                }
        except FileNotFoundError:
            self.items = {}
        except Exception:
            self.items = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.items, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def learn(self, code: str, text: str) -> bool:
        code = code.strip().lower()
        text = text.strip()
        if not code or not text or not CODE_RE.fullmatch(code) or not starts_with_cjk(text):
            return False
        entries = self.items.setdefault(code, {})
        meta = entries.get(text, {})
        meta["freq"] = int(meta.get("freq", 0)) + 1
        meta["last"] = time.time()
        entries[text] = meta
        self._save()
        return True

    def suggest(self, code: str, limit: int = 3) -> list[ChineseCandidate]:
        code = code.strip().lower()
        entries = self.items.get(code)
        if not entries:
            return []
        ranked = []
        for text, meta in entries.items():
            freq = int(meta.get("freq", 0))
            last = float(meta.get("last", 0.0))
            ranked.append((-freq, -last, text, freq))
        ranked.sort()
        result = []
        for _freq_rank, _last_rank, text, freq in ranked[:limit]:
            last = float(entries.get(text, {}).get("last", 0.0))
            result.append(ChineseCandidate(text, "", freq, last))
        return result
