# -*- coding: utf-8 -*-
"""Shared voice hotkey parsing helpers.

热键规格支持完整修饰键组合：``Ctrl+Alt+V`` / ``Super+V`` / ``Ctrl+Shift+B``
等形式均可，修饰键可任意组合 ``Ctrl / Alt / Shift / Super``（Super 别名
Win / Meta / Cmd），主键目前仍限定单字母 a-z。裸字母（如 ``v``）按传统
含义解析为 ``Ctrl+Alt+v``，保持与旧配置的向后兼容。
"""
from __future__ import annotations

import os
import re

from ibus_voice_ime import config
from typing import Any, Iterable, NamedTuple

# Canonical modifier order used for labels and specs.
_MODIFIER_ORDER = ("ctrl", "alt", "shift", "super")
_MODIFIER_ALIASES = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "ctl": "ctrl",
    "alt": "alt",
    "option": "alt",
    "shift": "shift",
    "super": "super",
    "win": "super",
    "meta": "super",
    "cmd": "super",
}
_MODIFIER_LABELS = {
    "ctrl": "Ctrl",
    "alt": "Alt",
    "shift": "Shift",
    "super": "Super",
}
# 裸字母（无修饰键前缀）的传统语义：Ctrl+Alt+字母。
_BARE_LETTER_MODIFIERS = frozenset({"ctrl", "alt"})

_DEFAULT_HOTKEYS = ("v",)
# 「原文语音输入」热键：识别结果不做 LLM 后处理，只走确定性规整。
_DEFAULT_RAW_HOTKEYS = ("b",)


class HotkeyCombo(NamedTuple):
    """One parsed hotkey: a modifier set plus a single lowercase letter."""

    modifiers: frozenset[str]
    letter: str

    def label(self) -> str:
        parts = [_MODIFIER_LABELS[mod] for mod in _MODIFIER_ORDER if mod in self.modifiers]
        parts.append(self.letter.upper())
        return "+".join(parts)

    def canonical(self) -> tuple[frozenset[str], str]:
        return (self.modifiers, self.letter)


def _parse_one_combo(token: str) -> HotkeyCombo | None:
    """Parse one token like ``Ctrl+Alt+V`` / ``Super+b`` / ``v``."""
    token = token.strip()
    if not token:
        return None
    head, sep, key = token.rpartition("+")
    key = key.strip().lower()
    if len(key) != 1 or not "a" <= key <= "z":
        return None
    if not sep:
        # 裸字母：兼容旧的「仅 Ctrl+Alt+字母」语义。
        return HotkeyCombo(_BARE_LETTER_MODIFIERS, key)
    modifiers: set[str] = set()
    for part in head.split("+"):
        part = part.strip().lower()
        if not part:
            return None
        mapped = _MODIFIER_ALIASES.get(part)
        if mapped is None:
            return None
        modifiers.add(mapped)
    if not modifiers:
        return None
    return HotkeyCombo(frozenset(modifiers), key)


def parse_hotkey_combos(raw: str) -> tuple[HotkeyCombo, ...]:
    """Split a spec like ``Ctrl+Alt+V, Super+N`` into parsed combos.

    分隔符是逗号/分号；不含 ``+`` 的项还可用空白分隔多个裸字母。
    含 ``+`` 的项**不**按空白拆分（``Ctrl + Shift + B`` 是一个组合，
    加号周围的空白会被忽略），避免把组合误拆成裸字母后静默换键。
    """
    tokens: list[str] = []
    for item in re.split(r"[,;]+", raw):
        item = item.strip()
        if not item:
            continue
        if "+" in item:
            tokens.append(item)
        else:
            tokens.extend(part for part in re.split(r"\s+", item) if part)
    combos: list[HotkeyCombo] = []
    seen: set[tuple[frozenset[str], str]] = set()
    for token in tokens:
        combo = _parse_one_combo(token)
        if combo is None:
            continue
        if combo.canonical() not in seen:
            seen.add(combo.canonical())
            combos.append(combo)
    return tuple(combos)


def default_combos(letters: Iterable[str]) -> tuple[HotkeyCombo, ...]:
    """Bare-letter default combos (historic Ctrl+Alt+letter semantics)."""
    return tuple(HotkeyCombo(_BARE_LETTER_MODIFIERS, letter) for letter in letters)


def hotkey_combos() -> tuple[HotkeyCombo, ...]:
    """Return configured voice hotkey combos (full modifier support).

    Accepts values such as ``Ctrl+Alt+V`` / ``Super+V`` / bare ``v`` from
    ``VOICE_IME_HOTKEYS``.  ``VOICE_IME_HOTKEY`` is kept as a single-hotkey
    compatibility alias.  Bare letters keep the historic Ctrl+Alt+letter
    semantics.
    """
    raw = config.env_str("VOICE_IME_HOTKEYS", None) or config.env_str("VOICE_IME_HOTKEY", "") or ""
    return parse_hotkey_combos(raw) or default_combos(_DEFAULT_HOTKEYS)


def _clipboard_hotkey_combos() -> tuple[HotkeyCombo, ...]:
    """Ctrl+Alt+P 粘贴热键组合集合（用于 raw 热键去重）。

    延迟 import 并容错：clipboard_paste 顶层只依赖标准库，但 voice_hotkey
    被 engine/overlay/测试广泛导入，这里绝不能因为兄弟模块的意外问题而崩溃；
    import 失败时返回空元组（跳过该项过滤）。
    """
    try:
        from ibus_voice_ime import clipboard_paste  # local import to avoid import-time coupling

        return clipboard_paste.hotkey_combos()
    except Exception:
        return ()


def raw_hotkey_combos() -> tuple[HotkeyCombo, ...]:
    """Return configured "raw transcript" voice hotkey combos.

    Reads ``VOICE_IME_RAW_HOTKEYS`` with the same parsing rules as
    ``hotkey_combos``.  Combos colliding with the main hotkeys or the
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
    combos = parse_hotkey_combos(raw) or default_combos(_DEFAULT_RAW_HOTKEYS)
    blocked = {combo.canonical() for combo in hotkey_combos()}
    blocked.update(combo.canonical() for combo in _clipboard_hotkey_combos())
    return tuple(combo for combo in combos if combo.canonical() not in blocked)


def hotkey_letters() -> tuple[str, ...]:
    """Compat helper: letters of the configured main voice hotkeys."""
    return tuple(combo.letter for combo in hotkey_combos())


def raw_hotkey_letters() -> tuple[str, ...]:
    """Compat helper: letters of the configured raw voice hotkeys."""
    return tuple(combo.letter for combo in raw_hotkey_combos())


def combos_label(combos: Iterable[HotkeyCombo]) -> str:
    return " / ".join(combo.label() for combo in combos)


def hotkey_label() -> str:
    return combos_label(hotkey_combos())


def raw_hotkey_label() -> str:
    return combos_label(raw_hotkey_combos())


def stop_hotkey_combos() -> tuple[HotkeyCombo, ...]:
    """Every hotkey that stops an in-progress recording (main + raw)."""
    combos = list(hotkey_combos())
    seen = {combo.canonical() for combo in combos}
    for combo in raw_hotkey_combos():
        if combo.canonical() not in seen:
            seen.add(combo.canonical())
            combos.append(combo)
    return tuple(combos)


def stop_hotkey_label() -> str:
    """Label of every hotkey that stops an in-progress recording.

    While recording, pressing any voice hotkey (main or raw) stops the current
    session, so the "press X again to stop" hints show the combined set.
    """
    return combos_label(stop_hotkey_combos())


def modifier_masks(
    control_mask: int,
    alt_mask: int,
    shift_mask: int = 0,
    super_mask: int = 0,
) -> dict[str, int]:
    """Build the modifier-mask map consumed by :func:`matches_hotkey`.

    ``shift_mask`` / ``super_mask`` of 0 disable those modifiers in matching
    (callers on namespaces without the constant simply omit them).
    """
    return {
        "ctrl": int(control_mask),
        "alt": int(alt_mask),
        "shift": int(shift_mask),
        "super": int(super_mask),
    }


def resolve_super_mask(namespace: Any) -> int:
    """OR-combine every Super representation a namespace may expose.

    X/Wayland 工具链对 Super 有两种表示：``SUPER_MASK``（如 IBus/Gdk 的
    1 << 26，多用于快捷键声明）与 ``MOD4_MASK``（X 修饰键位 1 << 6，
    按键事件 state 里常见）。两者都接受，缺哪个就只用哪个；都没有返回 0
    （Super 热键在该命名空间下不匹配，也不崩溃）。
    """
    mask = 0
    for name in ("SUPER_MASK", "MOD4_MASK"):
        value = int(getattr(namespace, name, 0) or 0)
        mask |= value
    return mask


def _combo_matches(
    namespace: Any,
    keyval: int,
    active_modifiers: frozenset[str],
    combo: HotkeyCombo,
) -> bool:
    if not combo.modifiers <= active_modifiers:
        return False
    lower = getattr(namespace, f"KEY_{combo.letter}", None)
    upper = getattr(namespace, f"KEY_{combo.letter.upper()}", None)
    return keyval == lower or keyval == upper


def _active_modifiers(state: int, masks: dict[str, int]) -> frozenset[str]:
    return frozenset(name for name, mask in masks.items() if mask and (int(state) & mask))


def classify_hotkey(
    namespace: Any,
    keyval: int,
    state: int,
    masks: dict[str, int],
) -> str | None:
    """Decide whether a key event is the "main" or "raw" voice hotkey.

    匹配语义与 :func:`matches_hotkey` 一致（按下修饰键 ⊇ 组合修饰键），
    但主/raw 两组同时参与仲裁：取**修饰键最多（最特异）**的命中组合，
    同特异度时主热键优先。这避免了「主热键 Ctrl+B 吞掉 raw 的
    Ctrl+Alt+B」这类子集劫持——去重只剔除完全相同的组合，运行时
    路由必须自己处理子集关系。

    Returns "main" / "raw" / None.
    """
    active = _active_modifiers(state, masks)
    best: tuple[int, int, str] | None = None  # (specificity, priority, label)
    for priority, label, combos in ((1, "main", hotkey_combos()), (0, "raw", raw_hotkey_combos())):
        for combo in combos:
            if _combo_matches(namespace, keyval, active, combo):
                specificity = len(combo.modifiers)
                if best is None or specificity > best[0] or (specificity == best[0] and priority > best[1]):
                    best = (specificity, priority, label)
    return best[2] if best else None


def matches_hotkey(
    namespace: Any,
    keyval: int,
    state: int,
    masks: dict[str, int],
    combos: Iterable[HotkeyCombo] | None = None,
) -> bool:
    """Return whether a key event matches one of the configured hotkeys.

    ``namespace`` is usually ``IBus`` or ``Gdk`` and must expose constants such
    as ``KEY_v`` / ``KEY_V``.  ``combos`` defaults to the main voice hotkeys;
    pass ``raw_hotkey_combos()`` to match the raw-transcript hotkeys instead.
    A combo matches when every one of its modifiers is pressed (extra modifier
    bits in ``state`` do not block the match) and the keyval equals the letter.
    """
    active = _active_modifiers(state, masks)
    for combo in (hotkey_combos() if combos is None else combos):
        if _combo_matches(namespace, keyval, active, combo):
            return True
    return False


def matches_ctrl_alt_letter(
    namespace: Any,
    keyval: int,
    state: int,
    control_mask: int,
    alt_mask: int,
    letters: tuple[str, ...] | None = None,
) -> bool:
    """Legacy helper kept for compatibility: strict Ctrl+Alt+letter matching.

    Ignores configured modifiers entirely; new code should prefer
    :func:`matches_hotkey` with :func:`modifier_masks`.
    """
    if not ((int(state) & int(control_mask)) and (int(state) & int(alt_mask))):
        return False
    combo_letters = hotkey_letters() if letters is None else letters
    for letter in combo_letters:
        lower = getattr(namespace, f"KEY_{letter}", None)
        upper = getattr(namespace, f"KEY_{letter.upper()}", None)
        if keyval == lower or keyval == upper:
            return True
    return False
