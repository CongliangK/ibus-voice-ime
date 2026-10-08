# -*- coding: utf-8 -*-
"""Regression tests for scripts/env-file-load.sh (space-safe KEY=VALUE parsing).

The old ``set -a; source <file>`` path in run-engine.sh exploded (exit 127)
whenever HOME or the repo path contained a space: the second word of the value
was executed as a command.  systemd environment.d does no shell expansion, so
line-based parsing restores the intended semantics.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOADER = ROOT / "scripts" / "env-file-load.sh"


def _eval_in_bash(env_file: Path) -> dict[str, str]:
    """Run the loader's output through eval in a child bash and dump exports."""
    script = (
        f'eval "$(\'{LOADER}\' \'{env_file}\')" 2>/dev/null; '
        'python3 -c "import os,json;print(json.dumps({k:v for k,v in os.environ.items() if k.startswith(\'TESTENV_\')}))"'
    )
    out = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    ).stdout.strip()
    import json

    return json.loads(out) if out else {}


class EnvFileLoadTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="env-file-load-")
        self.env_file = Path(self._tmp.name) / "ibus-voice-ime.conf"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, content: str) -> None:
        self.env_file.write_text(content, encoding="utf-8")

    def test_value_with_spaces_survives(self) -> None:
        # The killer case: path with spaces must round-trip as ONE value.
        self._write("TESTENV_LIB=/tmp/my dir/ime/vendor/rime/lib/librime.so.1\n")
        values = _eval_in_bash(self.env_file)
        self.assertEqual(values.get("TESTENV_LIB"), "/tmp/my dir/ime/vendor/rime/lib/librime.so.1")

    def test_comments_and_blank_lines_skipped(self) -> None:
        self._write(
            "# a comment\n"
            "\n"
            "TESTENV_A=1\n"
            "   # indented comment\n"
            "TESTENV_B=two words\n"
        )
        values = _eval_in_bash(self.env_file)
        self.assertEqual(values.get("TESTENV_A"), "1")
        self.assertEqual(values.get("TESTENV_B"), "two words")

    def test_invalid_keys_and_non_assignments_skipped(self) -> None:
        # Injection guard: command-looking lines and invalid identifiers must
        # not reach eval.
        self._write(
            "rm -rf /tmp/x\n"
            "TESTENV; rm -rf /=1\n"
            "9BAD=1\n"
            "TESTENV_OK=safe\n"
            "TESTENV_EMPTY=\n"
        )
        values = _eval_in_bash(self.env_file)
        self.assertEqual(values.get("TESTENV_OK"), "safe")
        self.assertEqual(values.get("TESTENV_EMPTY"), "")
        self.assertNotIn("9BAD", values)

    def test_value_with_special_shell_chars_survives(self) -> None:
        self._write("TESTENV_X=a$b`c`'d'\"e\";f\n")
        values = _eval_in_bash(self.env_file)
        self.assertEqual(values.get("TESTENV_X"), "a$b`c`'d'\"e\";f")

    def test_missing_file_is_silent_noop(self) -> None:
        values = _eval_in_bash(Path(self._tmp.name) / "does-not-exist.conf")
        self.assertEqual(values, {})

    def test_last_assignment_wins_like_environment_d(self) -> None:
        self._write("TESTENV_A=first\nTESTENV_A=second\n")
        values = _eval_in_bash(self.env_file)
        self.assertEqual(values.get("TESTENV_A"), "second")


if __name__ == "__main__":
    unittest.main()
