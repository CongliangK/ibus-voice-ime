"""Policy tests for secret persistence boundaries."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


class SecretPersistencePolicyTest(unittest.TestCase):
    def test_llm_cloud_example_template_has_no_real_key(self) -> None:
        example = json.loads(
            Path("examples/llm-cloud.json.example").read_text(encoding="utf-8")
        )
        api_key = example.get("api_key", "")
        # The committed template must only carry a CJK placeholder, never
        # anything that looks like a real provider key.
        self.assertIn("填入", api_key)
        self.assertIsNone(re.match(r"sk-[A-Za-z0-9_-]{16,}", api_key))

    def test_llm_cloud_user_config_is_gitignored(self) -> None:
        gitignore = Path(".gitignore").read_text(encoding="utf-8")
        self.assertIn("llm.json", gitignore)

    def test_llm_cloud_setup_script_writes_user_config_only(self) -> None:
        script = Path("scripts/setup-llm-cloud.sh").read_text(encoding="utf-8")
        # The generated config lives in the user's XDG config dir (0600),
        # never inside the repository or environment.d.
        self.assertNotIn("environment.d", script)
        self.assertIn("0o600", script)

    def test_mimo_cloud_switch_does_not_persist_raw_api_key(self) -> None:
        script = Path("scripts/switch-mimo-cloud-asr.sh").read_text(encoding="utf-8")
        forbidden = 'echo "VOICE_IME_MIMO_API_KEY=$API_KEY" >> "$ENV_FILE"'
        self.assertNotIn(forbidden, script)

    # 统一 JSON 配置时代的新策略：密钥名（不是密钥本身）持久化进
    # ~/.config/ibus-voice-ime/config.json 的 asr.<backend>.api_key_secret，
    # 由 `cfg set` 写入；environment.d 里不再出现 *_API_KEY_SECRET 行。

    def test_mimo_cloud_switch_persists_secret_name(self) -> None:
        script = Path("scripts/switch-mimo-cloud-asr.sh").read_text(encoding="utf-8")
        self.assertIn('cfg set asr.mimo_cloud.api_key_secret "$BWS_SECRET_NAME"', script)
        # 旧渠道（environment.d 行）不得残留。
        self.assertNotIn("VOICE_IME_MIMO_API_KEY_SECRET=$BWS_SECRET_NAME", script)

    def test_volc_switch_does_not_persist_raw_api_key(self) -> None:
        script = Path("scripts/switch-volc-bigmodel-asr.sh").read_text(encoding="utf-8")
        forbidden = 'echo "VOICE_IME_VOLC_API_KEY=$API_KEY" >> "$ENV_FILE"'
        self.assertNotIn(forbidden, script)

    def test_volc_switch_persists_secret_name(self) -> None:
        script = Path("scripts/switch-volc-bigmodel-asr.sh").read_text(encoding="utf-8")
        self.assertIn('cfg set asr.volc.api_key_secret "$BWS_SECRET_NAME"', script)
        self.assertNotIn("VOICE_IME_VOLC_API_KEY_SECRET=$BWS_SECRET_NAME", script)

    def test_siliconflow_switch_does_not_persist_raw_api_key(self) -> None:
        script = Path("scripts/switch-siliconflow-asr.sh").read_text(encoding="utf-8")
        forbidden = 'echo "VOICE_IME_SILICONFLOW_API_KEY=$API_KEY" >> "$ENV_FILE"'
        self.assertNotIn(forbidden, script)

    def test_siliconflow_switch_persists_secret_name(self) -> None:
        script = Path("scripts/switch-siliconflow-asr.sh").read_text(encoding="utf-8")
        self.assertIn('cfg set asr.siliconflow.api_key_secret "$BWS_SECRET_NAME"', script)
        self.assertNotIn("VOICE_IME_SILICONFLOW_API_KEY_SECRET=$BWS_SECRET_NAME", script)

    def test_local_switch_scripts_have_no_secret_persistence(self) -> None:
        # 本地后端（qwen / mimo）没有 API Key：switch 脚本不持久化任何密钥，
        # 也不得把 API Key（名）写进文件。CHANNEL_ENV_KEYS 清单里出现的
        # 纯键名（用于 environment.d 只删不写回）不算持久化。
        for name in ("switch-qwen-asr.sh", "switch-mimo-asr.sh"):
            script = Path(f"scripts/{name}").read_text(encoding="utf-8")
            self.assertNotIn('>> "$ENV_FILE"', script)
            self.assertNotRegex(script, r"cfg set \S*api_key")


if __name__ == "__main__":
    unittest.main()
