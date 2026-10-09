# -*- coding: utf-8 -*-
"""Channel selection is JSON-driven: ``asr.backend`` in the unified config is
the single source of truth for which ASR backend ``selected()`` fires.

验收点（统一 JSON 配置升级）：
- 纯 config 环境（无任何 VOICE_IME_*/MIMO/VOLC/SILICONFLOW 环境变量）下，
  defaults.json 的 asr.backend=qwen3-asr 只让 qwen_asr_runtime.selected()
  为 True，其余（本地 mimo / mimo-cloud / volc / siliconflow）均为 False；
- config.json 里 set asr.backend 为某个渠道后，只有对应后端 selected() 为
  True（渠道互斥、可轮换）；
- VOICE_IME_ASR_BACKEND 环境变量仍然最高优先（env > config.json > defaults）。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ibus_voice_ime import config  # noqa: E402
from ibus_voice_ime.asr import (  # noqa: E402
    mimo_asr_runtime,
    mimo_cloud_asr,
    qwen_asr_runtime,
    siliconflow_asr,
    volc_bigmodel_asr,
)

# 渠道判定可能读到的全部环境变量前缀（selected() 的输入除了 asr.backend
# 还有各后端的 legacy flag，如 VOICE_IME_QWEN_ASR → asr.qwen3.enabled）。
_ENV_PREFIXES = ("VOICE_IME_", "MIMO", "VOLC", "SILICONFLOW")


def _selected_map() -> dict[str, bool]:
    return {
        "qwen3-asr": qwen_asr_runtime.selected(),
        "mimo-asr": mimo_asr_runtime.selected(),
        "mimo-cloud-asr": mimo_cloud_asr.selected(),
        "volc-bigmodel-asr": volc_bigmodel_asr.selected(),
        "siliconflow-asr": siliconflow_asr.selected(),
    }


class _CleanConfigEnv(unittest.TestCase):
    """Scrub channel env vars, point VOICE_IME_CONFIG at a temp file."""

    def setUp(self) -> None:
        # 真实进程环境里可能残留 VOICE_IME_*（开发机/CI），先摘掉并登记恢复。
        scrubbed: dict[str, str] = {}
        for key in list(os.environ):
            if key.startswith(_ENV_PREFIXES):
                scrubbed[key] = os.environ.pop(key)
        self.addCleanup(lambda: os.environ.update(scrubbed))

        self._tmp = tempfile.TemporaryDirectory(prefix="ime-channel-test-")
        self.addCleanup(self._tmp.cleanup)
        self.config_path = Path(self._tmp.name) / "config.json"
        os.environ["VOICE_IME_CONFIG"] = str(self.config_path)
        config.reload()
        self.addCleanup(config.reload)

    def _set_user_backend(self, backend: str) -> None:
        self.config_path.write_text(
            json.dumps({"asr": {"backend": backend}}, ensure_ascii=False),
            encoding="utf-8",
        )
        config.reload()


class DefaultsChannelTest(_CleanConfigEnv):
    def test_defaults_select_only_qwen(self) -> None:
        # 无用户覆盖、无 env：defaults.json 的 asr.backend=qwen3-asr 生效，
        # 且所有 cloud 后端默认 enabled=false，不被误选。
        self.assertFalse(self.config_path.exists())
        selected = _selected_map()
        self.assertTrue(selected["qwen3-asr"])
        for name, value in selected.items():
            if name != "qwen3-asr":
                self.assertFalse(value, f"{name} 不应被默认选中")


class ConfigChannelRotationTest(_CleanConfigEnv):
    def test_backend_rotation_is_exclusive(self) -> None:
        for backend in ("mimo-cloud-asr", "volc-bigmodel-asr", "siliconflow-asr", "mimo-asr"):
            with self.subTest(backend=backend):
                self._set_user_backend(backend)
                selected = _selected_map()
                for name, value in selected.items():
                    self.assertEqual(
                        value,
                        name == backend,
                        f"asr.backend={backend} 时 {name}.selected() 应为 {name == backend}",
                    )

    def test_back_to_qwen_via_config(self) -> None:
        self._set_user_backend("siliconflow-asr")
        self.assertTrue(siliconflow_asr.selected())
        self._set_user_backend("qwen3-asr")
        self.assertTrue(qwen_asr_runtime.selected())
        self.assertFalse(siliconflow_asr.selected())


class EnvOverrideBeatsConfigTest(_CleanConfigEnv):
    def test_env_backend_wins_over_config(self) -> None:
        # config.json 选了 qwen3-asr，但 env 层优先级更高：
        # VOICE_IME_ASR_BACKEND=siliconflow-asr 必须胜出（救援/临时切换语义）。
        self._set_user_backend("qwen3-asr")
        os.environ["VOICE_IME_ASR_BACKEND"] = "siliconflow-asr"
        try:
            self.assertTrue(siliconflow_asr.selected())
            self.assertFalse(qwen_asr_runtime.selected())
        finally:
            del os.environ["VOICE_IME_ASR_BACKEND"]

    def test_legacy_flag_env_can_force_backend(self) -> None:
        # legacy 渠道 flag（VOICE_IME_SILICONFLOW_ASR）仍是 env 覆盖开关。
        self._set_user_backend("qwen3-asr")
        os.environ["VOICE_IME_SILICONFLOW_ASR"] = "1"
        try:
            self.assertTrue(siliconflow_asr.selected())
        finally:
            del os.environ["VOICE_IME_SILICONFLOW_ASR"]


class MigratedDualSelectionTest(_CleanConfigEnv):
    """迁移态回归（install.sh 把 VOICE_IME_QWEN_ASR=1 迁成 enabled=true）。

    voice.py 渠道链先命中先赢且 qwen 排最前：enabled 残留会压过 asr.backend
    的选择——这就是 switch 脚本必须写全五个互斥 enabled 叶子的原因。
    """

    def test_migrated_qwen_flag_beats_siliconflow_backend(self) -> None:
        self._set_user_backend("siliconflow-asr")
        self.config_path.write_text(
            json.dumps(
                {
                    "asr": {
                        "backend": "siliconflow-asr",
                        "qwen3": {"enabled": True},  # install.sh 迁移残留
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        config.reload()
        # 危险现状（文档化）：enabled 残留让 qwen 仍被选中，先于 siliconflow 命中。
        self.assertTrue(qwen_asr_runtime.selected())
        self.assertTrue(siliconflow_asr.selected())

    def test_switch_style_exclusive_writes_fix_dual_selection(self) -> None:
        # switch 脚本的写法：backend + 五个 enabled 叶子显式写全（目标=1 其余=0）。
        self.config_path.write_text(
            json.dumps(
                {
                    "asr": {
                        "backend": "siliconflow-asr",
                        "qwen3": {"enabled": False},
                        "mimo": {"enabled": False},
                        "mimo_cloud": {"enabled": False},
                        "volc": {"enabled": False},
                        "siliconflow": {"enabled": True},
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        config.reload()
        selected = _selected_map()
        for name, value in selected.items():
            self.assertEqual(value, name == "siliconflow-asr")


if __name__ == "__main__":
    unittest.main()
