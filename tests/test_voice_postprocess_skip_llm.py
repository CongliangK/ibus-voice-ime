# -*- coding: utf-8 -*-
"""Unit tests for voice.postprocess(skip_llm=...) — the raw-hotkey LLM bypass."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ibus_voice_ime.asr import voice  # noqa: E402
from ibus_voice_ime.text import llm_postprocess  # noqa: E402

# 长文本（> min_chars=50）：正常路径会触发 LLM 分支，正好用于验证 skip_llm。
LONG_TEXT = "这是一段明显超过五十个字符的听写文本，" + "内容细节" * 20 + "，用来确认 LLM 后处理分支是否被调用。"
# 确定性规整可观察的变化：CJK 字符之间的空格会被合并。
SPACED_TEXT = "今天 晴朗"
NORMALIZED_SPACED = "今天晴朗"


class VoicePostprocessSkipLlmTest(unittest.TestCase):
    def setUp(self) -> None:
        # 干净基线：避免宿主环境影响 mode / 火山云端平滑开关。
        self._saved = {
            k: os.environ.get(k)
            for k in (
                "VOICE_IME_VOICE_MODE",
                "VOICE_IME_VOLC_BIGMODEL_ASR",
                "VOICE_IME_ASR_BACKEND",
                "VOICE_IME_VOLC_BIGMODEL_ENABLE_DDC",
                "VOICE_IME_REMOVE_FILLERS",
                "VOICE_IME_VOICE_COMMANDS",
            )
        }
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_skip_llm_true_never_calls_llm(self) -> None:
        def _explode(*_args, **_kwargs):
            raise AssertionError("LLM must not be called when skip_llm=True")

        with mock.patch.object(llm_postprocess, "enabled", return_value=True), \
             mock.patch.object(llm_postprocess, "refine_with_fallback", side_effect=_explode):
            result = voice.postprocess(LONG_TEXT, skip_llm=True)
        self.assertTrue(result.startswith("这是一段明显超过"))
        # 确定性规整照旧生效。
        self.assertEqual(voice.postprocess(SPACED_TEXT, skip_llm=True), NORMALIZED_SPACED)

    def test_skip_llm_false_calls_llm(self) -> None:
        polished = "大模型整理后的结果文本。"
        with mock.patch.object(llm_postprocess, "enabled", return_value=True), \
             mock.patch.object(
                 llm_postprocess, "refine_with_fallback", return_value=polished
             ) as refine:
            result = voice.postprocess(LONG_TEXT)
        refine.assert_called_once()
        self.assertEqual(result, polished)

    def test_default_arguments_keep_legacy_behavior(self) -> None:
        # 默认 skip_llm=False：与旧签名等价，LLM 分支照常参与。
        polished = "另一个整理结果。"
        with mock.patch.object(llm_postprocess, "enabled", return_value=True), \
             mock.patch.object(
                 llm_postprocess, "refine_with_fallback", return_value=polished
             ) as refine:
            self.assertEqual(voice.postprocess(LONG_TEXT, skip_llm=False), polished)
            self.assertEqual(voice.postprocess(LONG_TEXT), polished)
        self.assertEqual(refine.call_count, 2)

    def test_skip_llm_keeps_local_filler_removal_under_cloud_smoothing(self) -> None:
        # volc-bigmodel 后端 DDC 默认开（cloud_smoothing_on()=True）时，
        # 非 raw 路径会把本地去口水钉成 "0"（云端平滑优先）；raw 路径
        # （skip_llm=True，Ctrl+Alt+B）DDC 已被强制关闭，本地确定性去口水
        # 必须保留，不再被钉成 "0"。
        os.environ["VOICE_IME_ASR_BACKEND"] = "volc-bigmodel-asr"
        seen = {}
        real_normalize = voice.text_postprocess.normalize

        def spy(raw_text, *, mode=None):
            seen["remove_fillers"] = os.environ.get("VOICE_IME_REMOVE_FILLERS")
            return real_normalize(raw_text, mode=mode)

        with mock.patch.object(voice.text_postprocess, "normalize", side_effect=spy):
            voice.postprocess("嗯啊今天晴朗", skip_llm=True)
        self.assertNotEqual(seen.get("remove_fillers"), "0")
        # 对照：非 raw 路径该抑制仍然生效（证明条件分支没有误伤旧行为）。
        with mock.patch.object(voice.text_postprocess, "normalize", side_effect=spy):
            voice.postprocess("嗯啊今天晴朗")
        self.assertEqual(seen.get("remove_fillers"), "0")


if __name__ == "__main__":
    unittest.main()
