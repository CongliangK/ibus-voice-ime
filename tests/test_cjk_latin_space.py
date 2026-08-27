"""Unit tests for CJK<->Latin space handling (stdlib only)."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ibus_voice_ime.text import text_postprocess  # noqa: E402


class CjkLatinSpaceTest(unittest.TestCase):
    def setUp(self) -> None:
        # Default behavior is on (remove CJK<->Latin boundary spaces).
        self._saved = os.environ.get("VOICE_IME_CJK_LATIN_SPACE")

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop("VOICE_IME_CJK_LATIN_SPACE", None)
        else:
            os.environ[self._saved] = self._saved

    def test_removes_cjk_latin_boundary(self) -> None:
        self.assertEqual(
            text_postprocess.cleanup_punctuation("我用 pi 配合 opencode 编辑"),
            "我用pi配合opencode编辑",
        )

    def test_removes_latin_cjk_boundary(self) -> None:
        self.assertEqual(
            text_postprocess.cleanup_punctuation("opencode 编辑 Rime 方案"),
            "opencode编辑Rime方案",
        )

    def test_preserves_latin_latin_space(self) -> None:
        self.assertEqual(
            text_postprocess.cleanup_punctuation("hello world 你好"),
            "hello world你好",
        )

    def test_preserves_pure_english(self) -> None:
        self.assertEqual(
            text_postprocess.cleanup_punctuation("I love coding every day"),
            "I love coding every day",
        )

    def test_preserves_alnum_internal_space(self) -> None:
        # "GPT 5.5" is ASCII<->ASCII, preserved; "5.5 模型" boundary removed.
        self.assertEqual(
            text_postprocess.cleanup_punctuation("GPT 5.5 模型"),
            "GPT 5.5模型",
        )

    def test_disabled_by_env(self) -> None:
        os.environ["VOICE_IME_CJK_LATIN_SPACE"] = "0"
        self.assertEqual(
            text_postprocess.cleanup_punctuation("配合 opencode"),
            "配合 opencode",
        )


if __name__ == "__main__":
    unittest.main()
