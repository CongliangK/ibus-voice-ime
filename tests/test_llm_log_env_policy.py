# -*- coding: utf-8 -*-
"""VOICE_IME_LLM_LOG 环境注入策略守卫（2026-10 更新后假阳性事故回归）。

事故链：switch-*.sh 重启 ibus-daemon 时用 `${VOICE_IME_LLM_LOG:-默认路径}`
导出 llm.jsonl 默认路径 → 进入引擎 /proc/environ → LLM 未启用时文件永不
创建 → doctor 的陈旧路径检查 FAIL，且 fix 的"重启引擎"修不好（重启后
switch 脚本再次注入同样路径）。

策略（与 VOICE_IME_LLM_CONFIG 2026-09 修复同构）：
  1. shell 层（switch-*.sh）不导出默认值——Python 端 llm_postprocess 自带
     XDG 默认兜底；
  2. run-engine.sh 对父目录不存在的陈旧继承值自愈（日志文件按需创建，
     文件本身不存在合法）；
  3. doctor.sh 的陈旧路径检查跳过该变量（另有 test_doctor_stale_env.py）。

本测试静态守卫 1 与 2，抽取真实脚本内容而非复制品。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SWITCH_SCRIPTS = [
    "scripts/switch-qwen-asr.sh",
    "scripts/switch-volc-bigmodel-asr.sh",
    "scripts/switch-siliconflow-asr.sh",
    "scripts/switch-mimo-cloud-asr.sh",
    "scripts/switch-mimo-asr.sh",
]


class LlmLogEnvPolicyTest(unittest.TestCase):
    def test_switch_scripts_do_not_export_default_llm_log(self) -> None:
        # 任何形式的 VOICE_IME_LLM_LOG 默认导出（含 ${...:-默认} 模式）都会
        # 让 daemon 在 LLM 未启用时携带不存在的路径。
        for rel in SWITCH_SCRIPTS:
            with self.subTest(script=rel):
                text = (ROOT / rel).read_text(encoding="utf-8")
                self.assertNotIn(
                    "VOICE_IME_LLM_LOG=",
                    text,
                    f"{rel} 不得导出 VOICE_IME_LLM_LOG（含默认值）",
                )

    def test_run_engine_heals_stale_llm_log_parent_dir(self) -> None:
        # 自愈层必须存在：父目录不存在的继承值被 unset，父目录存在则保留。
        text = (ROOT / "run-engine.sh").read_text(encoding="utf-8")
        self.assertRegex(
            text,
            re.compile(
                r"unset VOICE_IME_LLM_LOG", re.S,
            ),
        )
        # 只允许"父目录"判据，不允许对文件本体做 -e 判断后 unset
        # （那会把"日志尚未创建"的合法状态误当陈旧值丢掉）。
        heal_block = re.search(
            r'if \[\[ -n "\$\{VOICE_IME_LLM_LOG:-\}".*?fi\nfi',
            text,
            re.S,
        )
        self.assertIsNotNone(heal_block, "run-engine.sh 缺少 LLM_LOG 自愈段")
        self.assertIn("dirname", heal_block.group(0))


if __name__ == "__main__":
    unittest.main()
