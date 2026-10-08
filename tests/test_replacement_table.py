"""Unit tests for the deterministic replacement-table layer (stdlib only)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ibus_voice_ime.text import text_postprocess  # noqa: E402


class ReplacementTableTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self._dict_path = os.path.join(self._tmp, "voice-dictionary.txt")
        with open(self._dict_path, "w", encoding="utf-8") as f:
            f.write(
                "Open Code | opencode | open cold\n"
                "Rime | | Rim\n"
                "pi\n"
                "DeepSeek Harness | deepseek harness | DeepSeek Honeys, DeepSeek Honey\n"
                "Harness | harness | Honeys, honeys\n"
            )
        self._saved_dict = os.environ.get("VOICE_IME_VOICE_DICTIONARY")
        self._saved_repl = os.environ.get("VOICE_IME_VOICE_REPLACEMENTS")
        os.environ["VOICE_IME_VOICE_DICTIONARY"] = self._dict_path
        # english memory off for determinism
        self._saved_mem = os.environ.get("VOICE_IME_VOICE_USE_ENGLISH_MEMORY")
        os.environ["VOICE_IME_VOICE_USE_ENGLISH_MEMORY"] = "0"

    def tearDown(self) -> None:
        for k, v in (
            ("VOICE_IME_VOICE_DICTIONARY", self._saved_dict),
            ("VOICE_IME_VOICE_REPLACEMENTS", self._saved_repl),
            ("VOICE_IME_VOICE_USE_ENGLISH_MEMORY", self._saved_mem),
        ):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_replaces_confusion_whole_word(self) -> None:
        out = text_postprocess.apply_replacement_table("我用 Rim 编辑")
        self.assertEqual(out, "我用 Rime 编辑")

    def test_replaces_cjk_adjacent(self) -> None:
        out = text_postprocess.apply_replacement_table("编辑Rim方案")
        self.assertEqual(out, "编辑Rime方案")

    def test_preserves_substring_in_longer_token(self) -> None:
        # English letters on either side are NOT a boundary.
        self.assertEqual(
            text_postprocess.apply_replacement_table("Rims 和 XRim 安全"),
            "Rims 和 XRim 安全",
        )

    def test_replaces_multiword_confusion(self) -> None:
        out = text_postprocess.apply_replacement_table("open cold 是误识别")
        self.assertEqual(out, "Open Code 是误识别")

    def test_replaces_partial_phrase_confusion(self) -> None:
        # Only the mis-heard tail word is replaced; the correct prefix survives.
        self.assertEqual(
            text_postprocess.apply_replacement_table("看看 DeepSeek Honeys 的日志"),
            "看看 DeepSeek Harness 的日志",
        )

    def test_replaces_bare_confusion_fallback(self) -> None:
        # ASR sometimes drops the prefix or lowercases: bare confusion still fixed.
        self.assertEqual(
            text_postprocess.apply_replacement_table("部署 Honeys 服务"),
            "部署 Harness 服务",
        )
        self.assertEqual(
            text_postprocess.apply_replacement_table("deepseek honeys 启动了吗"),
            "deepseek Harness 启动了吗",
        )

    def test_confusion_substring_in_longer_token_untouched(self) -> None:
        self.assertEqual(
            text_postprocess.apply_replacement_table("Honeysuckle 很香"),
            "Honeysuckle 很香",
        )

    def test_does_not_touch_canonical_already_present(self) -> None:
        out = text_postprocess.apply_replacement_table("Rime 和 Rim")
        self.assertEqual(out, "Rime 和 Rime")

    def test_disabled_by_env(self) -> None:
        os.environ["VOICE_IME_VOICE_REPLACEMENTS"] = "0"
        out = text_postprocess.apply_replacement_table("编辑 Rim")
        self.assertEqual(out, "编辑 Rim")


if __name__ == "__main__":
    unittest.main()
