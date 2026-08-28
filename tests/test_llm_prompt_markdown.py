# -*- coding: utf-8 -*-
"""Markdown 输出安全：提示词目标与后处理链不得破坏代码/结构。

云端 LLM 整理输出改为规范 Markdown（标题/分点/行内代码/代码块）后，两条
确定性清理路径可能误伤它：重复标点折叠会吃掉代码里的 ``::`` / ``...``，
首尾反引号剥离会砍掉合法围栏。这里锁住两条约束 + 提示词本身的目标。
"""
from __future__ import annotations

import unittest

from ibus_voice_ime.text import llm_postprocess, text_postprocess


class CleanupKeepCodeTest(unittest.TestCase):
    """cleanup_punctuation_keep_code 本体的行为测试（现为备用工具，LLM 路径不再调用）。"""

    def test_inline_code_punctuation_preserved(self) -> None:
        text = "用 `std::vector<int>` 和 `args...` 实现，，明白了吗"
        out = text_postprocess.cleanup_punctuation_keep_code(text)
        self.assertIn("`std::vector<int>`", out)
        self.assertIn("`args...`", out)
        # 代码之外的重复标点仍被清理
        self.assertNotIn("，，", out)

    def test_fenced_block_preserved(self) -> None:
        text = "步骤如下：\n\n```bash\npython main.py --wait...\nx=$HOME::bin\n```\n\n好的，，知道啦"
        out = text_postprocess.cleanup_punctuation_keep_code(text)
        self.assertIn("python main.py --wait...", out)
        self.assertIn("x=$HOME::bin", out)
        self.assertNotIn("，，", out)
        self.assertIn("```bash", out)

    def test_plain_text_behaves_like_original(self) -> None:
        text = "今天开会，，讨论新版本。然后吃饭。"
        self.assertEqual(
            text_postprocess.cleanup_punctuation_keep_code(text),
            text_postprocess.cleanup_punctuation(text),
        )

    def test_headings_and_lists_untouched(self) -> None:
        text = "# 标题\n\n## 子标题\n\n- 第一点\n- 第二点\n\n1. 步骤一\n2. 步骤二"
        self.assertEqual(text_postprocess.cleanup_punctuation_keep_code(text), text)


class StripWrappersTest(unittest.TestCase):
    def test_whole_output_fence_unwrapped(self) -> None:
        out = llm_postprocess._strip_wrappers("```markdown\n# 标题\n\n- 要点\n```")
        self.assertEqual(out, "# 标题\n\n- 要点")

    def test_leading_inline_code_not_stripped(self) -> None:
        out = llm_postprocess._strip_wrappers("# 用 `git clone` 克隆仓库")
        self.assertEqual(out, "# 用 `git clone` 克隆仓库")

    def test_leading_fenced_block_kept(self) -> None:
        text = "```bash\npip install -r requirements.txt\n```\n\n上面是安装命令。"
        self.assertEqual(llm_postprocess._strip_wrappers(text), text)

    def test_quotes_still_stripped(self) -> None:
        out = llm_postprocess._strip_wrappers('"整理后的文本。"')
        self.assertEqual(out, "整理后的文本。")

    def test_llm_output_trusted_no_mechanical_rewrite(self) -> None:
        # 信任模型输出：刻意的空格与标点不做机械重排（无重复标点折叠、
        # 无 CJK-拉丁空格删除）——那些规则只服务无 LLM 的确定性链路。
        text = "- 安装 `node 20` 之后 重启\n- 保留 这个空格"
        self.assertEqual(llm_postprocess._strip_wrappers(text), text)


class PromptContractTest(unittest.TestCase):
    """提示词必须声明 AI 任务简报目标（防回退的轻量守护）。"""

    def test_system_prompt_declares_agent_task_brief(self) -> None:
        prompt = llm_postprocess.SYSTEM_PROMPT
        self.assertIn("Markdown", prompt)
        self.assertIn("任务简报", prompt)
        self.assertIn("## 目标", prompt)
        self.assertIn("## 约束", prompt)
        self.assertIn("代码块", prompt)
        # 红线：不得编造用户没说过的要求
        self.assertIn("没说过的要求", prompt)

    def test_system_prompt_demands_aggressive_bullets(self) -> None:
        prompt = llm_postprocess.SYSTEM_PROMPT
        self.assertIn("积极分点", prompt)
        self.assertIn("能分点就分点", prompt)
        self.assertIn("`- `", prompt)

    def test_dictation_mode_instruction_mentions_brief(self) -> None:
        self.assertIn("任务简报", llm_postprocess.MODE_INSTRUCTIONS["dictation"])

    def test_user_message_carries_format_requirement(self) -> None:
        messages = llm_postprocess._build_messages("一段需要整理的长听写文本内容", "dictation")
        user_content = messages[1]["content"]
        self.assertIn("格式要求", user_content)
        self.assertIn("任务简报", user_content)


if __name__ == "__main__":
    unittest.main()
