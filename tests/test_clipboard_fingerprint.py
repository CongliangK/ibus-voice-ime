"""Unit tests for the paste checkpoint content fingerprint (stdlib only)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ibus_voice_ime.clipboard_paste import content_fingerprint  # noqa: E402


class ContentFingerprintTest(unittest.TestCase):
    def test_empty(self) -> None:
        fp = content_fingerprint("")
        self.assertIn("len=0", fp)
        self.assertIn("lines=0", fp)

    def test_single_line(self) -> None:
        fp = content_fingerprint("hello")
        self.assertIn("sha=", fp)
        self.assertIn("lines=1", fp)
        self.assertIn("len=5", fp)

    def test_multi_line_count(self) -> None:
        self.assertIn("lines=3", content_fingerprint("a\nb\nc"))
        self.assertIn("lines=2", content_fingerprint("行一\n行二"))

    def test_stable_and_distinct(self) -> None:
        self.assertEqual(content_fingerprint("同一文本"), content_fingerprint("同一文本"))
        self.assertNotEqual(content_fingerprint("a"), content_fingerprint("b"))

    def test_matches_log_format(self) -> None:
        # 诊断脚本靠 "OK sha=... lines=... len=..." 精确比对引擎回传。
        fp = content_fingerprint("x\ny")
        self.assertRegex(fp, r"^sha=[0-9a-f]{8} lines=2 len=3$")


if __name__ == "__main__":
    unittest.main()
