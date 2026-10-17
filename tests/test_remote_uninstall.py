"""远程卸载引导器离线测试：stub 卸载脚本记录调用参数；验证确认门、版本探测、
克隆更新优先与兜底删除。"""
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

# 旧版卸载脚本 stub：--help 输出不含 --purge-models（版本探测识别为旧版）。
STUB_OLD = """#!/usr/bin/env bash
if [[ "${1:-}" == --help ]]; then  # 版本探测调用：打印头注释（同真实脚本），不记日志
  awk 'NR>1{if(/^#/){sub(/^# ?/,"");print;next}exit}' "$0"
  exit 0
fi
echo "args:$*" >> "$STUB_LOG"
echo "cwd:$(pwd)"
exit "${STUB_EXIT:-0}"
"""

# 新版卸载脚本 stub：--help（头注释）声明支持 --purge-models / --purge-repo。
STUB_NEW = "#!/usr/bin/env bash\n# supports: --purge-models --purge-repo\n" + STUB_OLD[STUB_OLD.index("\n") + 1:]


@unittest.skipUnless(shutil.which("bash"), "需要本地 bash，测试不联网")
class RemoteUninstallTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="remote-uninstall-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.cache = self.home / ".cache"
        self.cache.mkdir(parents=True)
        self.clone = self.root / "clone"
        self.log = self.root / "events.log"
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("IBUS_VOICE_IME_", "STUB_"))
                    and key != "VOICE_IME_MODEL_CACHE"
                    and key.lower() not in ("https_proxy", "http_proxy", "all_proxy")}
        self.env.update(HOME=str(self.home),
                        XDG_CACHE_HOME=str(self.cache),
                        IBUS_VOICE_IME_DIR=str(self.clone),
                        IBUS_VOICE_IME_REPO=str(self.root / "nowhere.git"),
                        IBUS_VOICE_IME_REF="main",
                        STUB_LOG=str(self.log))

    def make_clone(self, *, marker: bool = True, stub: str = STUB_OLD) -> None:
        self.clone.mkdir(parents=True)
        (self.clone / "uninstall-all.sh").write_text(stub, encoding="utf-8")
        (self.clone / "setup.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (self.clone / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
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
        self.assertTrue(self.clone.exists())  # 确认前零删除

    def test_old_script_still_gets_full_purge(self):
        # 旧版卸载脚本：只收到兼容参数，模型缓存与克隆由引导器兜底删除。
        self.make_clone(stub=STUB_OLD)
        (self.cache / "ibus-voice-ime" / "models").mkdir(parents=True)
        (self.cache / "ibus-voice-ime" / "models" / "w.bin").write_bytes(b"w")
        result = self.run_script("--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        args = self.log.read_text(encoding="utf-8").splitlines()[0].removeprefix("args:").split()
        self.assertEqual(args, ["--purge"])
        self.assertFalse(self.clone.exists())       # 引导器兜底删除克隆
        self.assertFalse((self.cache / "ibus-voice-ime").exists())  # 兜底删模型缓存

    def test_new_script_receives_purge_models(self):
        self.make_clone(stub=STUB_NEW)
        result = self.run_script("--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        args = self.log.read_text(encoding="utf-8").splitlines()[0].removeprefix("args:").split()
        self.assertIn("--purge", args)
        self.assertIn("--purge-models", args)
        self.assertNotIn("--purge-repo", args)      # 克隆由引导器统一兜底删除
        self.assertFalse(self.clone.exists())

    def test_keep_user_data_forwarded(self):
        self.make_clone(stub=STUB_NEW)
        result = self.run_script("--yes", "--keep-user-data")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        args = self.log.read_text(encoding="utf-8").splitlines()[0].removeprefix("args:").split()
        self.assertIn("--keep-user-data", args)
        self.assertNotIn("--purge", args)
        self.assertIn("--purge-models", args)
        self.assertFalse(self.clone.exists())

    def test_envd_custom_model_cache_captured_for_fallback(self):
        # environment.d 中的 VOICE_IME_MODEL_CACHE 在卸载脚本删除该文件前
        # 就被引导器抓取为兜底候选（回归：运行后才读会扑空）。
        self.make_clone(stub=STUB_OLD)
        envd = self.home / ".config" / "environment.d"
        envd.mkdir(parents=True)
        custom = self.home / "custom-models"
        custom.mkdir()
        (custom / "w.bin").write_bytes(b"w")
        (envd / "ibus-voice-ime.conf").write_text(
            "VOICE_IME_MODEL_CACHE=" + str(custom) + "\n", encoding="utf-8")
        result = self.run_script("--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(custom.exists())
        self.assertFalse((self.cache / "ibus-voice-ime").exists())

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

    def test_nonzero_exit_keeps_clone_for_debugging(self):
        self.make_clone()
        self.env["STUB_EXIT"] = "3"
        result = self.run_script("--yes")
        self.assertEqual(result.returncode, 3)
        self.assertTrue(self.clone.exists())        # 失败保留克隆供排查
        self.assertIn("保留克隆目录供排查", result.stdout + result.stderr)

    def test_dev_clone_without_marker_refused(self):
        # 无引导器标记（开发克隆）：拒绝删除，保持非零提醒。
        self.make_clone(marker=False)
        result = self.run_script("--yes")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(self.clone.exists())
        self.assertIn("拒绝删除克隆目录", result.stdout + result.stderr)

    def test_model_cache_outside_home_not_touched(self):
        self.make_clone(stub=STUB_OLD)
        outside = self.root / "outside-models"
        outside.mkdir()
        (outside / "w.bin").write_bytes(b"w")
        self.env["VOICE_IME_MODEL_CACHE"] = str(outside)
        result = self.run_script("--yes")
        self.assertEqual(result.returncode, 1)  # HOME 外拒绝删除 -> 警告非零
        self.assertTrue((outside / "w.bin").exists())
        self.assertIn("HOME 外", result.stdout + result.stderr)

    def test_xdg_cache_outside_home_not_touched(self):
        self.make_clone(stub=STUB_OLD)
        outside = self.root / "outside-cache"
        models = outside / "ibus-voice-ime" / "models"
        models.mkdir(parents=True)
        sentinel = models / "w.bin"
        sentinel.write_bytes(b"w")
        self.env["XDG_CACHE_HOME"] = str(outside)
        result = self.run_script("--yes")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(sentinel.read_bytes(), b"w")
        self.assertTrue(self.clone.exists())
        self.assertIn("HOME 外", result.stdout + result.stderr)

    def test_dir_with_trailing_slash_normalized(self):
        self.make_clone(marker=False)  # 拒删路径下的警告分支
        self.env["IBUS_VOICE_IME_DIR"] = str(self.clone) + "/./"
        result = self.run_script("--yes")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(self.log.exists())

    def test_invalid_dir_value_rejected(self):
        self.env["IBUS_VOICE_IME_DIR"] = "has space"
        result = self.run_script("--yes")
        self.assertEqual(result.returncode, 1)
        self.assertIn("非法", result.stderr)


@unittest.skipUnless(shutil.which("git") and shutil.which("bash"), "需要本地 git 与 bash，测试不联网")
class RemoteUninstallUpdateTest(unittest.TestCase):
    """确认后的第一步：引导器管理的克隆先尝试更新到远端最新版。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="remote-uninstall-update-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.cache = self.home / ".cache"
        self.cache.mkdir(parents=True)
        self.remote = self.root / "remote.git"
        self.clone = self.root / "clone"
        self.log = self.root / "events.log"
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("IBUS_VOICE_IME_", "STUB_", "GIT_"))
                    and key != "VOICE_IME_MODEL_CACHE"}
        self.env.update(HOME=str(self.home), XDG_CACHE_HOME=str(self.cache),
                        GIT_CONFIG_NOSYSTEM="1",
                        GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
                        IBUS_VOICE_IME_DIR=str(self.clone),
                        IBUS_VOICE_IME_REPO=str(self.remote),
                        IBUS_VOICE_IME_REF="main",
                        STUB_LOG=str(self.log))
        self.git_env = {**self.env, "GIT_AUTHOR_NAME": "离线测试",
                        "GIT_AUTHOR_EMAIL": "t@example.invalid",
                        "GIT_COMMITTER_NAME": "离线测试",
                        "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.remote)],
                       check=True, capture_output=True, env=self.git_env)
        self.seed = self.root / "seed"
        self.git(None, "clone", str(self.remote), str(self.seed))
        for name in ("setup.sh", "install.sh"):
            (self.seed / name).write_text("#!/bin/sh\n", encoding="utf-8")
        # v1 是旧版卸载脚本（不支持 --purge-models）；远端随后升级到新版。
        (self.seed / "uninstall-all.sh").write_text(STUB_OLD, encoding="utf-8")
        (self.seed / "version.txt").write_text("v1\n", encoding="utf-8")
        self.git(self.seed, "add", "-A")
        self.git(self.seed, "commit", "-m", "v1")
        self.git(self.seed, "push", "origin", "main")
        self.git(None, "clone", str(self.remote), str(self.clone))
        marker = self.clone / ".git" / "ibus-voice-ime-bootstrap"
        marker.write_text("old\n", encoding="utf-8")

    def git(self, cwd, *args):
        argv = ["git"]
        if cwd is not None:
            argv += ["-C", str(cwd)]
        subprocess.run(argv + list(args), check=True, capture_output=True,
                       env=self.git_env)

    def run_script(self, *args):
        return subprocess.run(["bash", str(SCRIPT), *args], env=self.env,
                              capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, start_new_session=True)

    def test_clone_updated_to_remote_latest_before_uninstall(self):
        # 远端把旧版卸载脚本升级到新版；引导器应先更新克隆、再按新版脚本
        # 探测参数（回归：探测发生在更新之前会漏传 --purge-models）。
        (self.seed / "uninstall-all.sh").write_text(STUB_NEW, encoding="utf-8")
        (self.seed / "version.txt").write_text("v2\n", encoding="utf-8")
        self.git(self.seed, "commit", "-am", "v2")
        self.git(self.seed, "push", "origin", "main")
        result = self.run_script("--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("已更新克隆到远端最新版", result.stdout)
        args = self.log.read_text(encoding="utf-8").splitlines()[0].removeprefix("args:").split()
        self.assertIn("--purge-models", args)      # 探测在更新之后：新版参数已透传
        self.assertFalse(self.clone.exists())      # 更新后照常兜底删除

    def test_update_failure_falls_back_to_local(self):
        # 克隆的 origin 不可达（指向空目录）：回退本地版本继续卸载。
        broken = self.root / "broken.git"
        broken.mkdir()
        subprocess.run(["git", "-C", str(self.clone), "remote", "set-url", "origin",
                        str(broken)], check=True, capture_output=True, env=self.git_env)
        result = self.run_script("--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("更新失败", result.stdout)
        self.assertTrue(self.log.exists())


@unittest.skipUnless(shutil.which("git") and shutil.which("bash"), "需要本地 git 与 bash，测试不联网")
class RemoteUninstallEnvironmentIsolationTest(unittest.TestCase):
    def test_fixtures_ignore_inherited_cache_paths(self):
        # 在子进程中污染环境，重跑两个 fixture 的兜底路径及显式越界拒绝。
        # 所有污染路径仍归测试所有；真实用户的缓存从不成为删除候选。
        with tempfile.TemporaryDirectory(prefix="remote-uninstall-environment-") as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            temp = root / "tmp"
            temp.mkdir()
            external_cache = root / "external-cache"
            external_models = root / "external-models"
            sentinels = [external_cache / "ibus-voice-ime" / "models" / "w.bin",
                         external_models / "w.bin"]
            for sentinel in sentinels:
                sentinel.parent.mkdir(parents=True)
                sentinel.write_bytes(b"keep")
            for pollution in ("xdg", "model", "both"):
                with self.subTest(pollution=pollution):
                    env = dict(os.environ, HOME=str(home), TMPDIR=str(temp),
                               XDG_CACHE_HOME=str(home / ".cache"))
                    env.pop("VOICE_IME_MODEL_CACHE", None)
                    if pollution in ("xdg", "both"):
                        env["XDG_CACHE_HOME"] = str(external_cache)
                    if pollution in ("model", "both"):
                        env["VOICE_IME_MODEL_CACHE"] = str(external_models)
                    result = subprocess.run(
                        [sys.executable, str(Path(__file__).resolve()),
                         "RemoteUninstallTest.test_old_script_still_gets_full_purge",
                         "RemoteUninstallUpdateTest.test_update_failure_falls_back_to_local",
                         "RemoteUninstallTest.test_model_cache_outside_home_not_touched",
                         "RemoteUninstallTest.test_xdg_cache_outside_home_not_touched"],
                        env=env, capture_output=True, text=True, timeout=30,
                        stdin=subprocess.DEVNULL, start_new_session=True)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    for sentinel in sentinels:
                        self.assertEqual(sentinel.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
