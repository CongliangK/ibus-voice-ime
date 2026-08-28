# -*- coding: utf-8 -*-
"""rime_ice 资产完整性判定（半残态检测）的单元测试。

场景来源（2026-08 实际故障）：新 clone 只带 git 跟踪的 rime_ice.schema.yaml，
cn_dicts 词库与编译产物在 .gitignore 排除清单里。旧判定只查「编译产物是否存在」，
用户目录残留的旧 rime_ice.table.bin 会让引擎误判可用；实际 librime 维护性重部署
因词库源缺失而失败，会话出零候选（只剩记忆层出词）。新判定要求三要素齐备。
"""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ibus_voice_ime.rime.rime_backend import rime_ice_assets_present


class RimeIceAssetsPresentTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        base = Path(self._tmp.name)
        self.shared = base / "rime-data"
        self.staging = base / "build"
        self.user = base / "rime-user"
        for d in (self.shared, self.staging, self.user):
            d.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _seed(
        self,
        *,
        schema: bool = True,
        dicts: bool = True,
        staging_table: bool = True,
        user_table: bool = False,
    ) -> None:
        if schema:
            (self.shared / "rime_ice.schema.yaml").write_text("# schema\n", encoding="utf-8")
        if dicts:
            cn = self.shared / "cn_dicts"
            cn.mkdir(exist_ok=True)
            (cn / "base.dict.yaml").write_text("# dict\n", encoding="utf-8")
        if staging_table:
            (self.staging / "rime_ice.table.bin").write_bytes(b"bin")
        if user_table:
            (self.user / "build").mkdir(exist_ok=True)
            (self.user / "build" / "rime_ice.table.bin").write_bytes(b"bin")

    def test_all_present(self) -> None:
        self._seed()
        self.assertTrue(rime_ice_assets_present(self.shared, self.staging, self.user))

    def test_half_state_dicts_missing(self) -> None:
        """本次故障的确切形态：schema 在、编译产物在、词库源缺失。"""
        self._seed(dicts=False)
        self.assertFalse(rime_ice_assets_present(self.shared, self.staging, self.user))

    def test_half_state_dicts_missing_with_stale_user_table(self) -> None:
        """用户目录残留旧编译产物也救不了词库源缺失（旧判定在这里误判为可用）。"""
        self._seed(dicts=False, staging_table=False, user_table=True)
        self.assertFalse(rime_ice_assets_present(self.shared, self.staging, self.user))

    def test_user_build_only_counts(self) -> None:
        self._seed(staging_table=False, user_table=True)
        self.assertTrue(rime_ice_assets_present(self.shared, self.staging, self.user))

    def test_no_build_products(self) -> None:
        self._seed(staging_table=False)
        self.assertFalse(rime_ice_assets_present(self.shared, self.staging, self.user))

    def test_no_schema(self) -> None:
        self._seed(schema=False)
        self.assertFalse(rime_ice_assets_present(self.shared, self.staging, self.user))

    def test_staging_none_falls_back_to_user_root(self) -> None:
        """staging_dir=None 时按 user_data_dir/rime_ice.table.bin 判定（与实现一致）。"""
        self._seed(staging_table=False)
        (self.user / "rime_ice.table.bin").write_bytes(b"bin")
        self.assertTrue(rime_ice_assets_present(self.shared, None, self.user))


if __name__ == "__main__":
    unittest.main()
