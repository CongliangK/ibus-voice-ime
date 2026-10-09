# -*- coding: utf-8 -*-
"""Shared voice hotkey parsing helpers."""
from __future__ import annotations

import os
import re

from ibus_voice_ime import config
from typing import Any

_DEFAULT_HOTKEYS = ("v",)
# 「原文语音输入」热键：识别结果不做 LLM 后处理，只走确定性规整。
_DEFAULT_RAW_HOTKEYS = ("b",)


def _parse_hotkey_letters(raw: str) -> list[str]:
    """Split a hotkey spec like ``Ctrl+Alt+V, n`` into lowercase letters."""
    letters: list[str] = []
    for item in re.split(r"[,;\s]+", raw):
        token = item.strip()
        if not token:
            continue
        key = token.rsplit("+", 1)[-1].strip().lower()
        if len(key) == 1 and "a" <= key <= "z" and key not in letters:
            letters.append(key)
    return letters


def hotkey_letters() -> tuple[str, ...]:
    """Return configured Ctrl+Alt+letter voice hotkeys.

    Accepts values such as ``Ctrl+Alt+V`` or simply ``v`` from
    ``VOICE_IME_HOTKEYS``.  ``VOICE_IME_HOTKEY`` is kept as a single-hotkey
    compatibility alias.
    """
    raw = config.env_str("VOICE_IME_HOTKEYS", None) or config.env_str("VOICE_IME_HOTKEY", "") or ""
    return tuple(_parse_hotkey_letters(raw) or _DEFAULT_HOTKEYS)


def _clipboard_hotkey_letters() -> tuple[str, ...]:
    """Ctrl+Alt+P 粘贴热键字母集合（用于 raw 热键去重）。

    延迟 import 并容错：clipboard_paste 顶层只依赖标准库，但 voice_hotkey
    被 engine/overlay/测试广泛导入，这里绝不能因为兄弟模块的意外问题而崩溃；
    import 失败时返回空元组（跳过该项过滤）。
    """
    try:
        from ibus_voice_ime import clipboard_paste  # local import to avoid import-time coupling

        return clipboard_paste.hotkey_letters()
    except Exception:
        return ()


def raw_hotkey_letters() -> tuple[str, ...]:
    """Return configured Ctrl+Alt+letter "raw transcript" voice hotkeys.

    Reads ``VOICE_IME_RAW_HOTKEYS`` with the same parsing rules as
    ``hotkey_letters``.  Letters colliding with the main hotkeys or the
    clipboard-paste hotkeys are removed from the raw set (those win), so
    Ctrl+Alt+V / Ctrl+Alt+P behavior can never be shadowed by a raw binding.

    与文档一致的去重规则：

    - 用户**显式配置**了 ``VOICE_IME_RAW_HOTKEYS``：过滤主热键与剪贴板热键
      后若为空，返回空元组（原文热键自动禁用），**不**回落默认 ``b``——
      显式选择被完全占用时应尊重「禁用」而不是悄悄换键。
    - 用户**未配置**：使用默认 ``b``；若 ``b`` 恰好被主热键/剪贴板热键占用，
      同样返回空元组（禁用），绝不崩溃。
    """
    raw = config.env_str("VOICE_IME_RAW_HOTKEYS", "") or ""
    letters = _parse_hotkey_letters(raw) or list(_DEFAULT_RAW_HOTKEYS)
    blocked = set(hotkey_letters())
    blocked.update(_clipboard_hotkey_letters())
    return tuple(letter for letter in letters if letter not in blocked)


def hotkey_label() -> str:
    return " / ".join(f"Ctrl+Alt+{letter.upper()}" for letter in hotkey_letters())


def raw_hotkey_label() -> str:
    return " / ".join(f"Ctrl+Alt+{letter.upper()}" for letter in raw_hotkey_letters())


def stop_hotkey_label() -> str:
    """Label of every hotkey that stops an in-progress recording.

    While recording, pressing any voice hotkey (main or raw) stops the current
    session, so the "press X again to stop" hints show the combined set.
    """
    letters = list(hotkey_letters())
    letters.extend(letter for letter in raw_hotkey_letters() if letter not in letters)
    return " / ".join(f"Ctrl+Alt+{letter.upper()}" for letter in letters)


def matches_ctrl_alt_letter(
    namespace: Any,
    keyval: int,
    state: int,
    control_mask: int,
    alt_mask: int,
    letters: tuple[str, ...] | None = None,
) -> bool:
    """Return whether a key event matches a configured Ctrl+Alt+letter hotkey.

    ``namespace`` is usually ``IBus`` or ``Gdk`` and must expose constants such
    as ``KEY_v`` / ``KEY_V``.  ``letters`` defaults to the main voice hotkeys;
    pass ``raw_hotkey_letters()`` to match the raw-transcript hotkeys instead.
    """
    if not ((int(state) & int(control_mask)) and (int(state) & int(alt_mask))):
        return False
    for letter in (hotkey_letters() if letters is None else letters):
        lower = getattr(namespace, f"KEY_{letter}", None)
        upper = getattr(namespace, f"KEY_{letter.upper()}", None)
        if keyval == lower or keyval == upper:
            return True
    return False
