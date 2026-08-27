"""Bootstrap tests for the validation harness."""

from __future__ import annotations

import unittest
from pathlib import Path


class ValidationSmokeTest(unittest.TestCase):
    def test_validation_docs_exist(self) -> None:
        self.assertTrue(Path("docs/validation.md").is_file())

    def test_mise_tasks_exist(self) -> None:
        text = Path("mise.toml").read_text(encoding="utf-8")
        self.assertIn("[tasks.dev]", text)
        self.assertIn("[tasks.affected]", text)
        self.assertIn("[tasks.release]", text)


if __name__ == "__main__":
    unittest.main()
