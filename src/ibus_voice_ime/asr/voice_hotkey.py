# -*- coding: utf-8 -*-
"""Shared voice hotkey parsing helpers."""
from __future__ import annotations

import os
import re
from typing import Any

_DEFAULT_HOTKEYS = ("v",)


def hotkey_letters() -> tuple[str, ...]:
    """Return configured Ctrl+Alt+letter voice hotkeys.

    Accepts values such as ``Ctrl+Alt+V`` or simply ``v`` from
    ``VOICE_IME_HOTKEYS``.  ``VOICE_IME_HOTKEY`` is kept as a single-hotkey
    compatibility alias.
    """
    raw = os.environ.get("VOICE_IME_HOTKEYS") or os.environ.get("VOICE_IME_HOTKEY") or ""
    letters: list[str] = []
    for item in re.split(r"[,;\s]+", raw):
        token = item.strip()
        if not token:
            continue
        key = token.rsplit("+", 1)[-1].strip().lower()
        if len(key) == 1 and "a" <= key <= "z" and key not in letters:
            letters.append(key)
    return tuple(letters or _DEFAULT_HOTKEYS)


def hotkey_label() -> str:
    return " / ".join(f"Ctrl+Alt+{letter.upper()}" for letter in hotkey_letters())


def matches_ctrl_alt_letter(namespace: Any, keyval: int, state: int, control_mask: int, alt_mask: int) -> bool:
    """Return whether a key event matches a configured Ctrl+Alt+letter hotkey.

    ``namespace`` is usually ``IBus`` or ``Gdk`` and must expose constants such
    as ``KEY_v`` / ``KEY_V``.
    """
    if not ((int(state) & int(control_mask)) and (int(state) & int(alt_mask))):
        return False
    for letter in hotkey_letters():
        lower = getattr(namespace, f"KEY_{letter}", None)
        upper = getattr(namespace, f"KEY_{letter.upper()}", None)
        if keyval == lower or keyval == upper:
            return True
    return False
