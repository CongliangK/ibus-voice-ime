"""Unit tests for log_trim retention behavior (stdlib only, hermetic)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ibus_voice_ime import log_trim  # noqa: E402


class TrimTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "engine.log"
        self._saved = ("VOICE_IME_LOG_KEEP_LINES", os.environ.get("VOICE_IME_LOG_KEEP_LINES"))
        os.environ.pop("VOICE_IME_LOG_KEEP_LINES", None)

    def tearDown(self):
        key, value = self._saved
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
        self._tmp.cleanup()

    def _write_lines(self, count: int, line_len: int = 160) -> None:
        # 每行明显超过 _MIN_BYTES_FACTOR，确保 size 门槛不拦截测试意图。
        self.path.write_text(
            "".join(f"line-{i:06d} " + "x" * line_len + "\n" for i in range(count)),
            encoding="utf-8",
        )

    def test_small_file_untouched(self):
        self._write_lines(50)
        self.assertFalse(log_trim.trim_to_last_lines(self.path, keep=1000))
        self.assertEqual(len(self.path.read_text().splitlines()), 50)

    def test_large_file_trimmed_to_keep(self):
        self._write_lines(3000)
        self.assertTrue(log_trim.trim_to_last_lines(self.path, keep=1000))
        lines = self.path.read_text().splitlines()
        self.assertEqual(len(lines), 1000)
        # 保留的是尾部（最近的日志）：0..2999 行中最后 1000 行 = 2000..2999。
        self.assertTrue(lines[0].startswith("line-002000"))
        self.assertTrue(lines[-1].startswith("line-002999"))

    def test_below_double_keep_not_rewritten(self):
        # 1000 < 行数 <= 2×keep：已裁剪过一轮的量级，不反复重写。
        self._write_lines(1500)
        self.assertFalse(log_trim.trim_to_last_lines(self.path, keep=1000))
        self.assertEqual(len(self.path.read_text().splitlines()), 1500)

    def test_keep_zero_disables(self):
        self._write_lines(5000)
        self.assertFalse(log_trim.trim_to_last_lines(self.path, keep=0))
        self.assertEqual(len(self.path.read_text().splitlines()), 5000)

    def test_missing_file_is_noop(self):
        self.assertFalse(log_trim.trim_to_last_lines(self.path / "nope.log", keep=1000))

    def test_env_override(self):
        self._write_lines(600)
        with tempfile.TemporaryDirectory() as tmp2:
            # env 值非数字回落默认
            os.environ["VOICE_IME_LOG_KEEP_LINES"] = "abc"
            self.assertEqual(log_trim.keep_lines_from_env(), 1000)
            os.environ["VOICE_IME_LOG_KEEP_LINES"] = "200"
            self.assertEqual(log_trim.keep_lines_from_env(), 200)
            # env=200 时 600 行文件应被裁到 200
            self.assertTrue(log_trim.trim_to_last_lines(self.path))
            self.assertEqual(len(self.path.read_text().splitlines()), 200)

    def test_partial_last_line_preserved(self):
        content = "".join(f"line-{i:06d} " + "x" * 160 + "\n" for i in range(2500))
        content += "in-flight-partial-line-without-newline" + "y" * 400
        self.path.write_text(content, encoding="utf-8")
        self.assertTrue(log_trim.trim_to_last_lines(self.path, keep=1000))
        text = self.path.read_text()
        # 末尾无换行的进行中行占最后一个行位：999 条完整行 + 1 条 partial = 1000。
        self.assertTrue(text.endswith("yyyy"))
        self.assertEqual(len(text.splitlines()), 1000)


if __name__ == "__main__":
    unittest.main()
