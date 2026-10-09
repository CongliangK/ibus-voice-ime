# -*- coding: utf-8 -*-
"""run-engine.sh 瘦身守卫：统一 JSON 配置时代脚本不再导出渠道/后端默认值。

渠道与行为默认统一由 config/defaults.json 在进程内生效（env > 用户
config.json > defaults.json > 调用点默认）。若 run-engine.sh 里重新出现
``${VOICE_IME_...:-默认值}`` 形式的渠道默认导出，env 层就永远压过
config.json 的渠道选择（asr.backend 轮换失效）——这些字面量必须保持缺席。

同时锁定必须保留的骨架：PYTHONPATH 导出、environment.d 的行级解析
（env-file-load.sh）、BWS 密钥注入段（渠道判定读 `config get asr.backend`）、
陈旧会话环境自愈段（_forget_stale_inherit）。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 渠道默认值字面量（代表性样本 ≥5）：任何 `${VOICE_IME_VAR:-默认}` 形式的
# 渠道/后端**设置类**键都不允许回来——否则 env 层永远压过 config.json 的
# 渠道选择。例外的 `*_ASR:-0}`（BWS 段的 legacy 渠道 flag 读取比较）与
# BWS 渠道判定段的内部变量兜底 `${ASR_BACKEND_LC:-qwen3-asr}`（二级回退
# 的最后一环，不是 VOICE_IME_ 键导出）是文档化例外，不在此列。
FORBIDDEN_DEFAULT_LITERALS = (
    "VOICE_IME_ASR_BACKEND:-",
    "VOICE_IME_QWEN_ASR:-",
    "VOICE_IME_QWEN_ASR_MODEL:-",
    "VOICE_IME_MIMO_ASR:-",
    "VOICE_IME_MIMO_CLOUD_BASE_URL:-",
    "VOICE_IME_MIMO_CLOUD_ASR_MODEL:-",
    "VOICE_IME_MIMO_CLOUD_LANGUAGE:-",
    "VOICE_IME_VOLC_BIGMODEL_BASE_URL:-",
    "VOICE_IME_VOLC_BIGMODEL_RESOURCE_ID:-",
    "VOICE_IME_VOLC_BIGMODEL_MODEL_NAME:-",
    "VOICE_IME_SILICONFLOW_BASE_URL:-",
    "VOICE_IME_SILICONFLOW_MODEL:-",
)

# `export VOICE_IME_*` 白名单：瘦身后仅剩 LLM 抑制与 LLM_CONFIG 机制。
_EXPORT_WHITELIST = {
    "VOICE_IME_LLM_POSTPROCESS",
    "VOICE_IME_LLM_INTERNAL",
    "VOICE_IME_LLM_CONFIG",
}


class RunEngineSlimTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = (ROOT / "run-engine.sh").read_text(encoding="utf-8")

    def test_no_channel_default_literals(self) -> None:
        for literal in FORBIDDEN_DEFAULT_LITERALS:
            self.assertNotIn(
                literal,
                self.script,
                f"run-engine.sh 不应再导出渠道默认值字面量 {literal!r}（会压过 config.json）",
            )

    def test_voice_ime_exports_limited_to_llm_whitelist(self) -> None:
        # 所有 `export VOICE_IME_*` 必须落在 LLM 白名单内——任何渠道/后端键
        # 的新导出都会让 env 层压过 config.json。
        exported = set(re.findall(r"^export (VOICE_IME_[A-Z0-9_]+)=", self.script, re.M))
        self.assertTrue(exported, "应至少导出白名单内的 LLM 键")
        self.assertEqual(exported, exported & _EXPORT_WHITELIST)

    def test_keeps_pythonpath_export(self) -> None:
        self.assertIn('export PYTHONPATH="$ROOT_DIR/src', self.script)
        self.assertIn('export LD_LIBRARY_PATH="$RUNTIME/lib', self.script)

    def test_keeps_env_file_line_parser(self) -> None:
        self.assertIn("env-file-load.sh", self.script)
        # environment.d 文件路径本身仍要被读取（兼容期：老渠道行作 env 覆盖）。
        self.assertIn("ibus-voice-ime.conf", self.script)

    def test_keeps_bws_section_driven_by_config(self) -> None:
        # 渠道判定读统一配置（env > config.json > defaults.json）。
        self.assertIn("config get asr.backend", self.script)
        self.assertIn("config get asr.mimo_cloud.api_key_secret", self.script)
        self.assertIn("config get asr.volc.api_key_secret", self.script)
        self.assertIn("config get asr.siliconflow.api_key_secret", self.script)
        self.assertIn("bws run --project-id", self.script)
        # 二级回退：config CLI 失败时读 environment.d 的渠道行，最后才 qwen3-asr。
        self.assertRegex(self.script, r"grep -m1 '\^VOICE_IME_ASR_BACKEND=' \"\$ENV_CONF\"")
        self.assertIn('${ASR_BACKEND_LC:-qwen3-asr}', self.script)

    def test_keeps_stale_inherit_self_healing(self) -> None:
        self.assertIn("_forget_stale_inherit", self.script)

    def test_keeps_engine_exec_and_llm_config_whitelist(self) -> None:
        # 白名单例外仍保留：LLM_CONFIG 机制 + llama.cpp 遗留路径的策略性压制。
        self.assertIn('exec "$PYTHON" "$ENGINE"', self.script)
        self.assertIn("VOICE_IME_LLM_CONFIG=", self.script)
        self.assertIn("export VOICE_IME_LLM_POSTPROCESS=0", self.script)
        self.assertIn("export VOICE_IME_LLM_INTERNAL=0", self.script)


if __name__ == "__main__":
    unittest.main()
