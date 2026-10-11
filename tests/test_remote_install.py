"""远程引导器离线集成测试：本地 Git 远端与仅记录调用的安装入口。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

try:
    import pty
except ImportError:
    pty = None

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "remote-install.sh"
STUB_SETUP = """#!/usr/bin/env bash
exec python3 -c '
import json, os, pathlib, sys
record = {"args": sys.argv[1:], "isatty": os.isatty(0),
          "cwd": os.getcwd(), "version": pathlib.Path("version.txt").read_text().strip()}
if os.environ.get("STUB_READ_STDIN"):
    record["stdin"] = sys.stdin.read()
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(record) + "\\n")
sys.exit(int(os.environ.get("STUB_EXIT", "0")))
' "$@"
"""


@unittest.skipUnless(shutil.which("git") and shutil.which("bash"), "需要本地 git 与 bash，测试不联网")
class RemoteInstallTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="remote-install-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.remote = self.root / "remote"
        self.remote.mkdir()
        self.install = self.root / "clone"
        self.log = self.root / "events.jsonl"
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("IBUS_VOICE_IME_", "GIT_", "STUB_"))}
        self.env.update(HOME=str(self.home), GIT_CONFIG_NOSYSTEM="1",
                        GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
                        GIT_AUTHOR_NAME="离线测试", GIT_AUTHOR_EMAIL="test@example.invalid",
                        GIT_COMMITTER_NAME="离线测试", GIT_COMMITTER_EMAIL="test@example.invalid",
                        IBUS_VOICE_IME_DIR=str(self.install),
                        IBUS_VOICE_IME_REPO=self.remote.as_uri(),
                        IBUS_VOICE_IME_REF="main", STUB_LOG=str(self.log))
        self.git(self.remote, "init", "-b", "main")
        (self.remote / "setup.sh").write_text(STUB_SETUP, encoding="utf-8")
        (self.remote / "version.txt").write_text("第一版\n", encoding="utf-8")
        self.commit_remote()

    def git(self, directory, *args):
        result = subprocess.run(["git", "-C", str(directory), *args], env=self.env,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def commit_remote(self):
        self.git(self.remote, "add", "--all")
        self.git(self.remote, "commit", "-m", "离线测试版本")

    def run_installer(self, *args, pipe=False, **environment):
        argv = ["bash", "-s", "--", *args] if pipe else ["bash", str(SCRIPT), *args]
        # 独立会话没有控制终端，不能仅凭 /dev/tty 节点存在判定交互。
        return subprocess.run(argv, env=dict(self.env, **environment), cwd=self.root,
                              input=SCRIPT.read_text(encoding="utf-8") if pipe else "",
                              capture_output=True, text=True, timeout=20,
                              start_new_session=True)

    def events(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_new_clone_and_argument_passthrough(self):
        self.assert_success(self.run_installer("--defaults"))
        event, = self.events()
        self.assertEqual(event["args"], ["--defaults"])
        self.assertFalse(event["isatty"])
        self.assertEqual(event["cwd"], str(self.install))
        self.assertTrue((self.install / ".git").is_dir())
        self.assertEqual(self.git(self.install, "rev-parse", "--is-shallow-repository"), "true")

    def test_real_script_pipe_preserves_arguments(self):
        arguments = ("--defaults", "--proxy", "包含 空格与'引号的参数", "--yes")
        self.assert_success(self.run_installer(*arguments, pipe=True))
        self.assertEqual(self.events()[0]["args"], list(arguments))
        self.assertFalse(self.events()[0]["isatty"])

    def test_default_directory_and_local_path_repository(self):
        self.env.pop("IBUS_VOICE_IME_DIR")
        self.assert_success(self.run_installer(IBUS_VOICE_IME_REPO=str(self.remote)))
        # 默认目录必须避开运行时数据根 ~/.local/share/ibus-voice-ime：
        # 安装后 rime-user 等用户数据会让克隆变脏，下次更新被拒（红队发现）。
        target = self.home / ".local/share/ibus-voice-ime-repo"
        self.assertTrue((target / ".git").is_dir())
        self.assertEqual(self.events()[0]["cwd"], str(target))

    def test_runtime_data_root_is_rejected_even_if_explicit(self):
        # 显式把克隆目录指到运行时数据根也必须拒绝，防止首次安装后变脏。
        result = self.run_installer(IBUS_VOICE_IME_DIR=str(self.home / ".local/share/ibus-voice-ime"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("运行时数据根", result.stderr)

    def test_trailing_slash_cannot_bypass_symlink_check(self):
        # /tmp/link/ 与 /tmp/link/. 的 -L 判定均为假；两种尾缀都须先
        # 归一再检查（红队发现）。
        victim = self.root / "victim"
        victim.mkdir()
        (victim / "keep.txt").write_text("保留")
        link = self.root / "link"
        link.symlink_to(victim)
        for variant in (str(link) + "/", str(link) + "/."):
            with self.subTest(variant=variant):
                result = self.run_installer(IBUS_VOICE_IME_DIR=variant)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("符号链接", result.stderr)
                self.assertEqual((victim / "keep.txt").read_text(), "保留")

    def test_data_root_guard_cannot_be_bypassed_by_dot_segments(self):
        # /./ 与 /../ 中缀不得绕开运行时数据根比对（红队发现）。
        for variant in (".local/share/ibus-voice-ime/../ibus-voice-ime",
                        ".local/share/./ibus-voice-ime"):
            with self.subTest(variant=variant):
                result = self.run_installer(IBUS_VOICE_IME_DIR=str(self.home / variant))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("运行时数据根", result.stderr)

    def test_setup_exit_code_is_preserved(self):
        result = self.run_installer("--defaults", STUB_EXIT="37")
        self.assertEqual(result.returncode, 37, result.stdout + result.stderr)
        self.assertEqual(self.events()[0]["args"], ["--defaults"])

    def test_no_controlling_terminal_preserves_stdin_without_extra_flags(self):
        result = subprocess.run(["bash", str(SCRIPT), "--defaults"],
                                env=dict(self.env, STUB_READ_STDIN="1"), cwd=self.root,
                                input="保留的输入\n", capture_output=True, text=True,
                                timeout=20, start_new_session=True)
        self.assert_success(result)
        event, = self.events()
        self.assertFalse(event["isatty"])
        self.assertEqual(event["stdin"], "保留的输入\n")
        self.assertEqual(event["args"], ["--defaults"])

    @unittest.skipIf(pty is None or not Path("/dev/tty").exists(), "当前环境无伪终端或 /dev/tty，跳过终端重定向验证")
    def test_pipe_stdin_reconnects_to_controlling_terminal(self):
        try:
            master, slave = pty.openpty()
        except OSError as exc:
            self.skipTest(f"当前环境不能创建伪终端：{exc}")
        # 包装进程建立控制终端，再将脚本通过管道交给 bash，模拟 curl | bash。
        wrapper = """
import fcntl, subprocess, sys, termios
try:
    fcntl.ioctl(int(sys.argv[1]), termios.TIOCSCTTY, 0)
except OSError as exc:
    print('当前环境不能设置控制终端：' + str(exc), file=sys.stderr)
    sys.exit(77)
result = subprocess.run(['bash', '-s', '--', '--defaults'],
                        input=sys.stdin.read(), capture_output=True, text=True, timeout=15)
print(result.stdout, end='')
print(result.stderr, end='', file=sys.stderr)
sys.exit(result.returncode)
"""
        try:
            result = subprocess.run([sys.executable, "-c", wrapper, str(slave)],
                                    env=self.env, cwd=self.root, pass_fds=(slave,),
                                    start_new_session=True, input=SCRIPT.read_text(encoding="utf-8"),
                                    capture_output=True, text=True, timeout=20)
        finally:
            os.close(slave)
            os.close(master)
        if result.returncode == 77:
            self.skipTest(result.stderr.strip())
        self.assert_success(result)
        event, = self.events()
        self.assertTrue(event["isatty"])
        self.assertEqual(event["args"], ["--defaults"])

    def test_clean_clone_fetches_new_commit_and_reruns_setup(self):
        self.assert_success(self.run_installer("--defaults"))
        (self.remote / "version.txt").write_text("第二版\n", encoding="utf-8")
        self.commit_remote()
        self.assert_success(self.run_installer("--keyboard-only"))
        self.assertEqual([event["version"] for event in self.events()], ["第一版", "第二版"])
        self.assertEqual(self.events()[1]["args"], ["--keyboard-only"])
        self.assertEqual(self.git(self.install, "rev-parse", "HEAD"),
                         self.git(self.remote, "rev-parse", "HEAD"))
        self.assertEqual(self.git(self.install, "status", "--porcelain"), "")

    def test_clean_shallow_clone_can_switch_branch(self):
        self.assert_success(self.run_installer())
        self.git(self.remote, "checkout", "-b", "release/test")
        (self.remote / "version.txt").write_text("其他分支\n", encoding="utf-8")
        self.commit_remote()
        self.assert_success(self.run_installer(IBUS_VOICE_IME_REF="release/test"))
        self.assertEqual(self.events()[1]["version"], "其他分支")

    def test_ignored_file_collision_refuses_instead_of_overwriting(self):
        # 本地被忽略的文件与新版本跟踪文件同名时，reset --hard 会静默
        # 覆盖用户数据；必须拒绝并点名文件（红队 2026-10-11 发现）。
        (self.remote / ".gitignore").write_text("secret.local\n", encoding="utf-8")
        self.commit_remote()
        self.assert_success(self.run_installer())
        # 第二版移除忽略条目并把 secret.local 纳入跟踪；克隆端仍是旧的
        # .gitignore，本地 secret.local 处于被忽略状态——正是碰撞场景。
        (self.remote / ".gitignore").write_text("", encoding="utf-8")
        (self.remote / "secret.local").write_text("REMOTE\n", encoding="utf-8")
        self.commit_remote()
        (self.install / "secret.local").write_text("USER SECRET\n", encoding="utf-8")
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("secret.local", result.stderr)
        self.assertEqual((self.install / "secret.local").read_text(encoding="utf-8"), "USER SECRET\n")

    def test_local_commits_are_not_deleted_by_update(self):
        # 用户在克隆里做过本地提交时，浅克隆无法区分落后/分叉，
        # 引导器必须拒绝自动重置而不是删掉本地提交（红队 2026-10-11 发现）。
        self.assert_success(self.run_installer())
        (self.install / "local-note.txt").write_text("本地提交\n", encoding="utf-8")
        self.git(self.install, "add", "--all")
        self.git(self.install, "commit", "-m", "用户本地提交")
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("本地提交", result.stderr)
        self.assertEqual((self.install / "local-note.txt").read_text(encoding="utf-8"), "本地提交\n")

    def test_tag_named_origin_main_cannot_divert_update(self):
        # 名为 origin/main 的标签不得让更新停在旧版本：reset 必须用
        # 全限定 refs/remotes/... 引用（红队 2026-10-11 发现）。
        self.assert_success(self.run_installer())
        old_head = self.git(self.remote, "rev-parse", "HEAD")
        (self.remote / "version.txt").write_text("新版\n", encoding="utf-8")
        self.commit_remote()
        # 在远端打一个会与简写歧义的标签（fetch 会把它带成 refs/tags/...）。
        self.git(self.remote, "tag", "origin/main", old_head)
        self.assert_success(self.run_installer())
        self.assertEqual((self.install / "version.txt").read_text(encoding="utf-8"), "新版\n")
        self.assertEqual(self.git(self.install, "rev-parse", "HEAD"),
                         self.git(self.remote, "rev-parse", "main"))

    def assert_dirty_clone_preserved(self, staged=False, untracked=False):
        self.assert_success(self.run_installer())
        changed = self.install / ("user-notes.txt" if untracked else "version.txt")
        changed.write_text("用户修改，不可覆盖\n", encoding="utf-8")
        if staged:
            self.git(self.install, "add", str(changed))
        old_head = self.git(self.install, "rev-parse", "HEAD")
        old_status = self.git(self.install, "status", "--porcelain")
        (self.remote / "version.txt").write_text("远端新版本\n", encoding="utf-8")
        self.commit_remote()
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("未提交变更", result.stderr)
        self.assertEqual(changed.read_text(encoding="utf-8"), "用户修改，不可覆盖\n")
        self.assertEqual(self.git(self.install, "rev-parse", "HEAD"), old_head)
        self.assertEqual(self.git(self.install, "status", "--porcelain"), old_status)
        self.assertEqual(len(self.events()), 1)

    def test_uncommitted_changes_are_preserved(self):
        self.assert_dirty_clone_preserved()

    def test_staged_changes_are_preserved(self):
        self.assert_dirty_clone_preserved(staged=True)

    def test_untracked_files_are_preserved(self):
        self.assert_dirty_clone_preserved(untracked=True)

    def test_existing_non_git_directory_is_not_overwritten(self):
        self.install.mkdir()
        note = self.install / "user-notes.txt"
        note.write_text("个人数据", encoding="utf-8")
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不会覆盖", result.stderr)
        self.assertEqual(note.read_text(encoding="utf-8"), "个人数据")
        self.assertFalse((self.install / ".git").exists())
        self.assertEqual(self.events(), [])

    def test_wrong_origin_is_not_overwritten(self):
        self.assert_success(self.run_installer())
        result = self.run_installer(IBUS_VOICE_IME_REPO=str(self.remote))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不匹配", result.stderr)
        self.assertEqual(len(self.events()), 1)

    def test_invalid_environment_variables_have_chinese_errors(self):
        for name in ("IBUS_VOICE_IME_DIR", "IBUS_VOICE_IME_REPO", "IBUS_VOICE_IME_REF"):
            for value in ("", "包含 空格", "包含\t制表符", "包含\n换行", "-选项"):
                with self.subTest(name=name, value=value):
                    result = self.run_installer(**{name: value})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("错误：", result.stderr)
                    self.assertIn(name + " 非法", result.stderr)
                    self.assertFalse(self.install.exists())
        result = self.run_installer(IBUS_VOICE_IME_REF="../非法分支")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("IBUS_VOICE_IME_REF 非法", result.stderr)

    def test_clone_failure_has_network_and_mirror_guidance(self):
        missing = (self.root / "missing-remote").as_uri()
        result = self.run_installer(IBUS_VOICE_IME_REPO=missing)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("克隆失败", result.stderr)
        self.assertIn("https_proxy", result.stderr)
        self.assertIn("IBUS_VOICE_IME_REPO", result.stderr)
        self.assertIn("镜像", result.stderr)
        self.assertEqual(self.events(), [])

    def test_fetch_failure_does_not_reset_existing_clone(self):
        self.assert_success(self.run_installer())
        head = self.git(self.install, "rev-parse", "HEAD")
        result = self.run_installer(IBUS_VOICE_IME_REF="missing-branch")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("获取远端分支失败", result.stderr)
        self.assertIn("https_proxy", result.stderr)
        self.assertEqual(self.git(self.install, "rev-parse", "HEAD"), head)
        self.assertEqual(len(self.events()), 1)


if __name__ == "__main__":
    unittest.main()
