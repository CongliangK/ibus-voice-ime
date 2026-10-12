"""远程卸载引导器离线测试：stub 卸载脚本记录调用参数；验证确认门与目录校验。"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "remote-uninstall.sh"

STUB_UNINSTALL = """#!/usr/bin/env bash
echo "args:$*" >> "$STUB_LOG"
echo "cwd:$(pwd)"
if [[ -n "${STUB_RM_CLONE:-}" ]]; then rm -rf -- "$STUB_RM_CLONE"; fi
exit "${STUB_EXIT:-0}"
"""


@unittest.skipUnless(shutil.which("bash"), "需要本地 bash，测试不联网")
class RemoteUninstallTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="remote-uninstall-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.clone = self.root / "clone"
        self.log = self.root / "events.log"
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("IBUS_VOICE_IME_", "STUB_"))
                    and key.lower() not in ("https_proxy", "http_proxy", "all_proxy")}
        self.env.update(HOME=str(self.home),
                        IBUS_VOICE_IME_DIR=str(self.clone),
                        IBUS_VOICE_IME_REPO=str(self.root / "nowhere.git"),
                        IBUS_VOICE_IME_REF="main",
                        STUB_LOG=str(self.log))

    def make_clone(self, *, marker: bool = True) -> None:
        self.clone.mkdir(parents=True)
        (self.clone / "uninstall-all.sh").write_text(STUB_UNINSTALL, encoding="utf-8")
        git_dir = self.clone / ".git"
        git_dir.mkdir()
        if marker:
            (git_dir / "ibus-voice-ime-bootstrap").write_text("deadbeef\n", encoding="utf-8")

    def run_script(self, *args: str) -> subprocess.CompletedProcess:
        # start_new_session：脱离控制终端，/dev/tty 打不开，与 SSH CI 场景一致。
        return subprocess.run(["bash", str(SCRIPT), *args], env=self.env,
                              capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, start_new_session=True)

    def test_help(self):
        result = self.run_script("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("远程卸载引导器", result.stdout)

    def test_unknown_option_rejected(self):
        result = self.run_script("--bogus")
        self.assertEqual(result.returncode, 2)

    def test_root_rejected(self):
        self.env.pop("HOME", None)
        result = self.run_script("--yes")
        self.assertEqual(result.returncode, 1)
        self.assertIn("HOME", result.stderr)

    def test_requires_confirmation_without_tty(self):
        self.make_clone()
        result = self.run_script()  # 无控制终端且未加 --yes
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("尚未做任何删除", result.stderr)
        self.assertFalse(self.log.exists())  # stub 未被调用

    def test_yes_runs_purge_all(self):
        self.make_clone()
        # stub 模拟成功路径：删除克隆目录，引导器应确认并退出 0。
        self.env["STUB_RM_CLONE"] = str(self.clone)
        result = self.run_script("--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        recorded = self.log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(recorded), 1)
        args = recorded[0].removeprefix("args:").split()
        self.assertIn("--purge", args)
        self.assertIn("--purge-models", args)
        self.assertIn("--purge-repo", args)
        self.assertNotIn("--keep-user-data", args)

    def test_keep_user_data_forwarded(self):
        self.make_clone()
        result = self.run_script("--yes", "--keep-user-data")
        self.assertEqual(result.returncode, 1)  # stub 未真删克隆 -> 警告转非零
        args = self.log.read_text(encoding="utf-8").splitlines()[0].removeprefix("args:").split()
        self.assertIn("--keep-user-data", args)
        self.assertNotIn("--purge", args)
        self.assertIn("--purge-models", args)

    def test_missing_clone_unreachable_repo_fails(self):
        # 无克隆、非 github 仓库布局：应报错而不是静默成功。
        result = self.run_script("--yes")
        self.assertEqual(result.returncode, 1)
        self.assertIn("找不到本地克隆", result.stderr)

    def test_clone_with_missing_script_warns_but_no_download(self):
        self.clone.mkdir(parents=True)
        (self.clone / "README.md").write_text("残缺克隆\n", encoding="utf-8")
        result = self.run_script("--yes")
        self.assertEqual(result.returncode, 1)
        self.assertIn("缺少 uninstall-all.sh", result.stdout + result.stderr)

    def test_nonzero_exit_propagates(self):
        self.make_clone()
        self.env["STUB_EXIT"] = "3"
        result = self.run_script("--yes")
        self.assertEqual(result.returncode, 3)
        self.assertIn("仍存在", result.stdout + result.stderr)

    def test_clone_leftover_warns_even_on_zero(self):
        self.make_clone()
        result = self.run_script("--yes")  # stub 退出 0 但目录仍在
        self.assertEqual(result.returncode, 1)
        self.assertIn("仍存在", result.stdout + result.stderr)

    def test_dir_with_trailing_slash_normalized(self):
        self.make_clone()
        self.env["IBUS_VOICE_IME_DIR"] = str(self.clone) + "/./"
        result = self.run_script("--yes")
        self.assertEqual(result.returncode, 1)  # 目录仍存在 -> 警告路径
        self.assertTrue(self.log.exists())

    def test_invalid_dir_value_rejected(self):
        self.env["IBUS_VOICE_IME_DIR"] = "has space"
        result = self.run_script("--yes")
        self.assertEqual(result.returncode, 1)
        self.assertIn("非法", result.stderr)


if __name__ == "__main__":
    unittest.main()
