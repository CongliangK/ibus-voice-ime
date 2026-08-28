# -*- coding: utf-8 -*-
"""Unit tests for the main + raw voice hotkey parsing/matching helpers."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ibus_voice_ime.asr import voice_hotkey  # noqa: E402

_CTRL = 0x02
_ALT = 0x08


class _FakeNamespace:
    """Minimal IBus/Gdk-like keyval namespace (a=97 ... z=122, plus case)."""

    def __init__(self) -> None:
        self._keys = {
            f"KEY_{chr(code)}": code for code in range(ord("a"), ord("z") + 1)
        }
        self._keys.update({
            f"KEY_{chr(code).upper()}": ord(chr(code).upper())
            for code in range(ord("a"), ord("z") + 1)
        })

    def __getattr__(self, name: str) -> int:
        try:
            return self.__dict__["_keys"][name]
        except KeyError:
            raise AttributeError(name) from None


class VoiceHotkeyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            k: os.environ.get(k)
            for k in (
                "VOICE_IME_HOTKEYS",
                "VOICE_IME_HOTKEY",
                "VOICE_IME_RAW_HOTKEYS",
                # 剪贴板热键环境（raw 热键去重会读它）。
                "VOICE_IME_CLIPBOARD_HOTKEYS",
                "VOICE_IME_CLIPBOARD_HOTKEY",
            )
        }
        for k in self._saved:
            os.environ.pop(k, None)
        self.ns = _FakeNamespace()

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ------------------------------------------------------------ 默认值

    def test_raw_default_letter(self) -> None:
        self.assertEqual(voice_hotkey.raw_hotkey_letters(), ("b",))
        self.assertEqual(voice_hotkey.raw_hotkey_label(), "Ctrl+Alt+B")

    def test_main_default_unchanged(self) -> None:
        self.assertEqual(voice_hotkey.hotkey_letters(), ("v",))
        self.assertEqual(voice_hotkey.hotkey_label(), "Ctrl+Alt+V")

    # ------------------------------------------------------ 环境变量解析

    def test_raw_env_parsing(self) -> None:
        os.environ["VOICE_IME_RAW_HOTKEYS"] = "Ctrl+Alt+N, Ctrl+Alt+m"
        self.assertEqual(voice_hotkey.raw_hotkey_letters(), ("n", "m"))
        self.assertEqual(voice_hotkey.raw_hotkey_label(), "Ctrl+Alt+N / Ctrl+Alt+M")

    def test_raw_env_ignores_non_letters_and_duplicates(self) -> None:
        os.environ["VOICE_IME_RAW_HOTKEYS"] = "Ctrl+Alt+1 b,,b Ctrl+Alt+F1 zz"
        self.assertEqual(voice_hotkey.raw_hotkey_letters(), ("b",))

    # ---------------------------------------------------- 与主热键去重

    def test_raw_letters_drop_main_overlap(self) -> None:
        os.environ["VOICE_IME_HOTKEYS"] = "v, n"
        os.environ["VOICE_IME_RAW_HOTKEYS"] = "n, b"
        self.assertEqual(voice_hotkey.raw_hotkey_letters(), ("b",))

    def test_raw_all_overlapping_disables_raw_hotkeys(self) -> None:
        # 显式配置全部与主热键重叠 → 自动禁用（空元组），与文档一致；
        # 不再静默回落默认 b。
        os.environ["VOICE_IME_HOTKEYS"] = "v"
        os.environ["VOICE_IME_RAW_HOTKEYS"] = "v"
        self.assertEqual(voice_hotkey.raw_hotkey_letters(), ())
        self.assertEqual(voice_hotkey.raw_hotkey_label(), "")

    def test_raw_disabled_when_default_b_taken_by_main(self) -> None:
        os.environ["VOICE_IME_HOTKEYS"] = "Ctrl+Alt+B"
        self.assertEqual(voice_hotkey.raw_hotkey_letters(), ())
        self.assertEqual(voice_hotkey.raw_hotkey_label(), "")

    def test_raw_disabled_when_everything_overlaps(self) -> None:
        # 主热键占满默认 b 与用户显式配置的 v → 功能禁用（空元组，不崩溃）。
        os.environ["VOICE_IME_HOTKEYS"] = "v, b"
        os.environ["VOICE_IME_RAW_HOTKEYS"] = "v"
        self.assertEqual(voice_hotkey.raw_hotkey_letters(), ())

    # -------------------------------------------------- 与剪贴板热键去重

    def test_raw_letters_drop_clipboard_overlap(self) -> None:
        # 剪贴板热键（默认 Ctrl+Alt+P）优先：p 被从 raw 集合剔除。
        with mock.patch(
            "ibus_voice_ime.clipboard_paste.hotkey_letters", return_value=("p",)
        ):
            os.environ["VOICE_IME_RAW_HOTKEYS"] = "p, b"
            self.assertEqual(voice_hotkey.raw_hotkey_letters(), ("b",))

    def test_raw_disabled_when_only_clipboard_overlap(self) -> None:
        # 显式只配了 p 且 p 是剪贴板热键 → 过滤后为空 → 禁用（空元组）。
        with mock.patch(
            "ibus_voice_ime.clipboard_paste.hotkey_letters", return_value=("p",)
        ):
            os.environ["VOICE_IME_RAW_HOTKEYS"] = "p"
            self.assertEqual(voice_hotkey.raw_hotkey_letters(), ())

    def test_raw_disabled_when_default_b_taken_by_clipboard(self) -> None:
        # 未显式配置 raw 时默认 b，若剪贴板热键占了 b 同样禁用。
        with mock.patch(
            "ibus_voice_ime.clipboard_paste.hotkey_letters", return_value=("b",)
        ):
            self.assertEqual(voice_hotkey.raw_hotkey_letters(), ())

    def test_raw_dedup_survives_broken_clipboard_module(self) -> None:
        # clipboard_paste 导入失败时跳过该项过滤（不崩溃），默认 b 保留。
        import ibus_voice_ime as pkg

        had_attr = hasattr(pkg, "clipboard_paste")
        saved_module = getattr(pkg, "clipboard_paste", None)
        if had_attr:
            del pkg.clipboard_paste
        try:
            with mock.patch.dict(sys.modules, {"ibus_voice_ime.clipboard_paste": None}):
                self.assertEqual(voice_hotkey.raw_hotkey_letters(), ("b",))
        finally:
            if had_attr:
                pkg.clipboard_paste = saved_module

    # ------------------------------------------------------------ 匹配

    def test_matches_default_uses_main_letters(self) -> None:
        self.assertTrue(
            voice_hotkey.matches_ctrl_alt_letter(self.ns, ord("v"), _CTRL | _ALT, _CTRL, _ALT)
        )
        self.assertTrue(
            voice_hotkey.matches_ctrl_alt_letter(self.ns, ord("V"), _CTRL | _ALT, _CTRL, _ALT)
        )
        self.assertFalse(
            voice_hotkey.matches_ctrl_alt_letter(self.ns, ord("b"), _CTRL | _ALT, _CTRL, _ALT)
        )

    def test_matches_with_explicit_raw_letters(self) -> None:
        raw_letters = voice_hotkey.raw_hotkey_letters()
        self.assertTrue(
            voice_hotkey.matches_ctrl_alt_letter(
                self.ns, ord("b"), _CTRL | _ALT, _CTRL, _ALT, letters=raw_letters
            )
        )
        self.assertFalse(
            voice_hotkey.matches_ctrl_alt_letter(
                self.ns, ord("v"), _CTRL | _ALT, _CTRL, _ALT, letters=raw_letters
            )
        )

    def test_matches_requires_both_modifiers(self) -> None:
        for state in (0, _CTRL, _ALT):
            self.assertFalse(
                voice_hotkey.matches_ctrl_alt_letter(
                    self.ns, ord("b"), state, _CTRL, _ALT,
                    letters=voice_hotkey.raw_hotkey_letters(),
                )
            )
        # 额外的修饰位不阻断匹配（只要求 Ctrl 与 Alt 同时按下）。
        self.assertTrue(
            voice_hotkey.matches_ctrl_alt_letter(
                self.ns, ord("b"), _CTRL | _ALT | 0x04, _CTRL, _ALT,
                letters=voice_hotkey.raw_hotkey_letters(),
            )
        )

    def test_matches_with_empty_letters_never_matches(self) -> None:
        os.environ["VOICE_IME_HOTKEYS"] = "v, b"  # raw 功能被禁用
        self.assertEqual(voice_hotkey.raw_hotkey_letters(), ())
        self.assertFalse(
            voice_hotkey.matches_ctrl_alt_letter(
                self.ns, ord("b"), _CTRL | _ALT, _CTRL, _ALT,
                letters=voice_hotkey.raw_hotkey_letters(),
            )
        )

    # -------------------------------------------------------- 组合停止提示

    def test_stop_hotkey_label_combines_main_and_raw(self) -> None:
        label = voice_hotkey.stop_hotkey_label()
        self.assertIn("Ctrl+Alt+V", label)
        self.assertIn("Ctrl+Alt+B", label)
        self.assertEqual(label, "Ctrl+Alt+V / Ctrl+Alt+B")

    def test_stop_hotkey_label_without_duplicates(self) -> None:
        os.environ["VOICE_IME_HOTKEYS"] = "v, n"
        os.environ["VOICE_IME_RAW_HOTKEYS"] = "n, b"
        self.assertEqual(voice_hotkey.stop_hotkey_label(), "Ctrl+Alt+V / Ctrl+Alt+N / Ctrl+Alt+B")

    def test_stop_hotkey_label_when_raw_disabled(self) -> None:
        os.environ["VOICE_IME_HOTKEYS"] = "b"  # raw 默认 b 被占用 → 禁用
        self.assertEqual(voice_hotkey.stop_hotkey_label(), "Ctrl+Alt+B")


if __name__ == "__main__":
    unittest.main()
