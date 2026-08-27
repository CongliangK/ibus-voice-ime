"""Policy tests for secret persistence boundaries."""

from __future__ import annotations

import unittest
from pathlib import Path


class SecretPersistencePolicyTest(unittest.TestCase):
    def test_mimo_cloud_switch_does_not_persist_raw_api_key(self) -> None:
        script = Path("scripts/switch-mimo-cloud-asr.sh").read_text(encoding="utf-8")
        forbidden = 'echo "VOICE_IME_MIMO_API_KEY=$API_KEY" >> "$ENV_FILE"'
        self.assertNotIn(forbidden, script)

    def test_mimo_cloud_switch_persists_secret_name(self) -> None:
        script = Path("scripts/switch-mimo-cloud-asr.sh").read_text(encoding="utf-8")
        self.assertIn("VOICE_IME_MIMO_API_KEY_SECRET=$BWS_SECRET_NAME", script)

    def test_volc_switch_does_not_persist_raw_api_key(self) -> None:
        script = Path("scripts/switch-volc-bigmodel-asr.sh").read_text(encoding="utf-8")
        forbidden = 'echo "VOICE_IME_VOLC_API_KEY=$API_KEY" >> "$ENV_FILE"'
        self.assertNotIn(forbidden, script)

    def test_volc_switch_persists_secret_name(self) -> None:
        script = Path("scripts/switch-volc-bigmodel-asr.sh").read_text(encoding="utf-8")
        self.assertIn("VOICE_IME_VOLC_API_KEY_SECRET=$BWS_SECRET_NAME", script)


if __name__ == "__main__":
    unittest.main()
