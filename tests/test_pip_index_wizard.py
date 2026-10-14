"""pip 包源向导选项：题目与默认值、选择接线（setup→init→setup-qwen-asr）与持久化。

全部沙箱化：无网络、无真实 HOME/systemd，资产/体检脚本一律 fake（沿用
test_config_wizard.py 与 test_install_output_clarity.py 的手法）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import termios
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PipWizardSandboxTest(unittest.TestCase):
    """向导 + 应用器层级：真实 config-wizard.sh / apply-wizard-config.py，临时 HOME。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="voice-ime-pip-wizard-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.config = self.home / "config.json"
        self.result = self.home / "result.json"
        self.env_file = self.home / ".config/environment.d/ibus-voice-ime.conf"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("VOICE_IME_", "XDG_"))}
        self.env.update(HOME=str(self.home), VOICE_IME_CONFIG=str(self.config),
                        VOICE_IME_WIZARD_RESULT=str(self.result))

    def wizard(self, *args, text=""):
        return subprocess.run(["bash", str(ROOT / "scripts/config-wizard.sh"), *args],
                              input=text, text=True, capture_output=True, env=self.env, timeout=10)

    def apply(self):
        return subprocess.run([sys.executable, str(ROOT / "scripts/apply-wizard-config.py"), str(self.result)],
                              text=True, capture_output=True, env=self.env, timeout=10)

    def seed_env_file(self, text):
        self.env_file.parent.mkdir(parents=True, exist_ok=True)
        self.env_file.write_text(text)
        return text

    def test_defaults_question_present_with_tsinghua_default(self):
        ran = self.wizard("--defaults")
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertIn("pip 包源（Python 依赖下载） [当前/默认：tsinghua]：采用默认", ran.stderr)
        self.assertEqual(json.loads(self.result.read_text())["pip_index"], "tsinghua")
        # 默认清华不提示代理（提示只在选中官方源时出现）。
        self.assertNotIn("--proxy http://127.0.0.1:7890", ran.stderr)

    def test_numbered_official_selection_prints_proxy_hint(self):
        # 非 TTY 编号路径：方案/下载/模型三个回车后，pip 包源输 2 选官方 PyPI。
        ran = self.wizard(text="\n\n\n2\n" + "\n" * 4)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        result = json.loads(self.result.read_text())
        self.assertEqual(result["voice_plan"], "local-qwen")
        self.assertEqual(result["pip_index"], "official")
        self.assertIn("官方 PyPI（pypi.org）", ran.stderr)
        self.assertIn("--proxy http://127.0.0.1:7890", ran.stderr)
        self.assertIn("export http_proxy/https_proxy", ran.stderr)

    def test_current_official_from_env_and_from_environmentd_file(self):
        # ① 进程环境带持久值（登录会话注入或本次运行覆盖）：--defaults 沿用官方源。
        self.env["VOICE_IME_PIP_INDEX_URL"] = "default"
        ran = self.wizard("--defaults")
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertIn("[当前/默认：official]", ran.stderr)
        self.assertEqual(json.loads(self.result.read_text())["pip_index"], "official")
        # ② 环境未设置但 environment.d 已持久（同一旧会话内重跑向导不丢选择）。
        self.env.pop("VOICE_IME_PIP_INDEX_URL")
        self.seed_env_file("VOICE_IME_PIP_INDEX_URL=default\nOTHER=keep\n")
        ran = self.wizard("--defaults")
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertEqual(json.loads(self.result.read_text())["pip_index"], "official")

    def test_custom_url_defaults_keeps_custom_state_visible(self):
        # P0 回归：用户显式 export 自定义包源 URL 时，--defaults（= --yes/回车）必须
        # 识别为 custom 态并在题面可见呈现，绝不折叠成清华镜像。
        self.env["VOICE_IME_PIP_INDEX_URL"] = "https://my.internal.mirror/simple"
        ran = self.wizard("--defaults")
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertIn("[当前/默认：custom]", ran.stderr)
        self.assertIn("自定义包源 URL（https://my.internal.mirror/simple）", ran.stderr)
        self.assertIn("保持当前自定义 URL", ran.stderr)
        self.assertEqual(json.loads(self.result.read_text())["pip_index"], "custom")

    def test_custom_url_piped_enter_keeps_custom_and_numbering_stable(self):
        # 管道交互（编号列表降级）：回车=保持 custom；两项菜单编号不变——
        # 1=清华镜像、2=官方 PyPI（与非 custom 态一致），3=保持自定义。
        self.env["VOICE_IME_PIP_INDEX_URL"] = "https://my.internal.mirror/simple"
        ran = self.wizard(text="\n\n\n\n" + "\n" * 4)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        result = json.loads(self.result.read_text())
        self.assertEqual(result["pip_index"], "custom")
        self.assertIn("3) 保持当前自定义 URL", ran.stderr)
        # 显式选 2 = 官方 PyPI（编号不变）
        ran = self.wizard(text="\n\n\n2\n" + "\n" * 4)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertEqual(json.loads(self.result.read_text())["pip_index"], "official")

    def test_custom_from_environmentd_only_is_custom_state(self):
        # 进程环境无变量、environment.d 持久行是自定义 URL：同样识别为 custom 态；
        # apply 对 custom 选择「不动既有持久化」（不写不清、不备份）。
        self.seed_env_file("VOICE_IME_PIP_INDEX_URL=https://persisted.example/simple\nKEEP=1\n")
        ran = self.wizard("--defaults")
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertEqual(json.loads(self.result.read_text())["pip_index"], "custom")
        self.assertIn("https://persisted.example/simple", ran.stderr)
        applied = self.apply()
        self.assertEqual(applied.returncode, 0, applied.stderr)
        report = json.loads(applied.stdout)
        self.assertEqual(report["pip_index"], "custom")
        self.assertFalse(report["pip_index_written"])
        self.assertFalse(report["pip_index_cleared"])
        self.assertEqual(self.env_file.read_text(), "VOICE_IME_PIP_INDEX_URL=https://persisted.example/simple\nKEEP=1\n")
        self.assertEqual(list(self.env_file.parent.glob("ibus-voice-ime.conf.bak.*")), [])

    def test_apply_custom_selection_keeps_official_persisted_line(self):
        # 本次运行 export 了自定义 URL（custom 态），environment.d 仍是官方哨兵行：
        # 回车保持 custom → apply 不动持久行（按次高级用法，登录会话语义不被改写）。
        before = self.seed_env_file("VOICE_IME_PIP_INDEX_URL=default\nKEEP=1\n")
        self.env["VOICE_IME_PIP_INDEX_URL"] = "https://one-shot.example/simple"
        self.assertEqual(self.wizard("--defaults").returncode, 0)
        self.env.pop("VOICE_IME_PIP_INDEX_URL")
        applied = self.apply()
        self.assertEqual(applied.returncode, 0, applied.stderr)
        report = json.loads(applied.stdout)
        self.assertEqual(report["pip_index"], "custom")
        self.assertFalse(report["pip_index_written"])
        self.assertFalse(report["pip_index_cleared"])
        self.assertEqual(self.env_file.read_text(), before)

    def test_apply_explicit_tsinghua_over_custom_clears_line_with_backup(self):
        # 交互显式选清华（管道输 1）：用户选择覆盖环境——清除自定义持久行并备份。
        before = self.seed_env_file("VOICE_IME_PIP_INDEX_URL=https://persisted.example/simple\nKEEP=1\n")
        self.env["VOICE_IME_PIP_INDEX_URL"] = "https://my.internal.mirror/simple"
        ran = self.wizard(text="\n\n\n1\n" + "\n" * 4)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertEqual(json.loads(self.result.read_text())["pip_index"], "tsinghua")
        applied = self.apply()
        self.assertEqual(applied.returncode, 0, applied.stderr)
        report = json.loads(applied.stdout)
        self.assertEqual(report["pip_index"], "tsinghua")
        self.assertTrue(report["pip_index_cleared"])
        self.assertEqual(self.env_file.read_text(), "KEEP=1\n")
        backups = list(self.env_file.parent.glob("ibus-voice-ime.conf.bak.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), before)

    def test_custom_url_env_prints_note_and_numbered_tsinghua_wins(self):
        self.env["VOICE_IME_PIP_INDEX_URL"] = "https://mirrors.example.test/simple"
        ran = self.wizard(text="\n\n\n1\n" + "\n" * 4)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertIn("自定义包源 URL", ran.stderr)
        self.assertEqual(json.loads(self.result.read_text())["pip_index"], "tsinghua")

    def test_non_qwen_plan_omits_field_and_apply_keeps_persisted_line(self):
        # 云端/仅键盘方案不提问、不写键；应用器对缺省键「不动既有持久化」。
        before = self.seed_env_file("VOICE_IME_PIP_INDEX_URL=default\nOTHER=keep\n")
        ran = self.wizard("--defaults", "--voice-plan", "keyboard-only")
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertNotIn("pip 包源", ran.stderr)
        self.assertNotIn("pip_index", json.loads(self.result.read_text()))
        applied = self.apply()
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(self.env_file.read_text(), before)

    def test_apply_official_replaces_old_line_with_backup(self):
        before = self.seed_env_file(
            "VOICE_IME_RIME_LIBRARY=/keep/so\nVOICE_IME_PIP_INDEX_URL=https://custom.example/simple\n")
        ran = self.wizard(text="\n\n\n2\n" + "\n" * 4)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        applied = self.apply()
        self.assertEqual(applied.returncode, 0, applied.stderr)
        report = json.loads(applied.stdout)
        self.assertEqual(report["pip_index"], "official")
        self.assertTrue(report["pip_index_written"])
        self.assertFalse(report["pip_index_cleared"])
        text = self.env_file.read_text()
        self.assertIn("VOICE_IME_PIP_INDEX_URL=default\n", text)
        self.assertIn("VOICE_IME_RIME_LIBRARY=/keep/so\n", text)
        self.assertNotIn("custom.example", text)
        backups = list(self.env_file.parent.glob("ibus-voice-ime.conf.bak.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), before)

    def test_apply_official_creates_env_file_when_absent(self):
        self.assertEqual(self.wizard(text="\n\n\n2\n" + "\n" * 4).returncode, 0)
        applied = self.apply()
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertTrue(json.loads(applied.stdout)["pip_index_written"])
        self.assertEqual(self.env_file.read_text(), "VOICE_IME_PIP_INDEX_URL=default\n")
        self.assertEqual(list(self.env_file.parent.glob("ibus-voice-ime.conf.bak.*")), [])

    def test_apply_tsinghua_clears_line_but_keeps_others(self):
        # 已持久官方源后改选默认（管道输 1 显式选清华）：清掉持久行，其余行保留并备份。
        before = self.seed_env_file("# comment\nVOICE_IME_PIP_INDEX_URL=default\nKEEP=1\n")
        ran = self.wizard(text="\n\n\n1\n" + "\n" * 4)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertEqual(json.loads(self.result.read_text())["pip_index"], "tsinghua")
        applied = self.apply()
        self.assertEqual(applied.returncode, 0, applied.stderr)
        report = json.loads(applied.stdout)
        self.assertEqual(report["pip_index"], "tsinghua")
        self.assertTrue(report["pip_index_cleared"])
        self.assertFalse(report["pip_index_written"])
        self.assertEqual(self.env_file.read_text(), "# comment\nKEEP=1\n")
        backups = list(self.env_file.parent.glob("ibus-voice-ime.conf.bak.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), before)

    def test_apply_idempotent_when_line_already_target(self):
        self.seed_env_file("VOICE_IME_PIP_INDEX_URL=default\n")
        self.env["VOICE_IME_PIP_INDEX_URL"] = "default"
        self.assertEqual(self.wizard("--defaults").returncode, 0)
        self.env.pop("VOICE_IME_PIP_INDEX_URL")
        applied = self.apply()
        self.assertEqual(applied.returncode, 0, applied.stderr)
        report = json.loads(applied.stdout)
        self.assertFalse(report["pip_index_written"])
        self.assertFalse(report["pip_index_cleared"])
        self.assertEqual(self.env_file.read_text(), "VOICE_IME_PIP_INDEX_URL=default\n")
        self.assertEqual(list(self.env_file.parent.glob("ibus-voice-ime.conf.bak.*")), [])

    def test_apply_rejects_invalid_pip_index_value(self):
        self.assertEqual(self.wizard("--defaults").returncode, 0)
        result = json.loads(self.result.read_text())
        result["pip_index"] = "bogus"
        self.result.write_text(json.dumps(result))
        applied = self.apply()
        self.assertNotEqual(applied.returncode, 0)
        self.assertIn("pip 包源不合法", applied.stderr)
        self.assertNotIn("Traceback", applied.stderr)
        self.assertFalse(self.config.exists())
        self.assertFalse(self.env_file.exists())


class SetupPipIndexWiringTest(unittest.TestCase):
    """真实 setup.sh/config-wizard/apply-wizard + init.sh，资产脚本全部 fake。

    验证环境注入链路：向导（--defaults 沿用当前值）→ setup.sh export →
    init.sh 子进程环境 → setup-qwen-asr.sh / install.sh 实际可见。
    """

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="voice-ime-pip-wiring-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.root / "scripts").mkdir()
        self.log = self.root / "events.jsonl"
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("VOICE_IME_", "XDG_", "STUB_", "PYTHONPATH"))}
        self.env.update(HOME=str(self.home), XDG_CONFIG_HOME=str(self.home / ".config"),
                        STUB_ROOT=str(self.root), PATH=str(self.bin) + ":" + self.env["PATH"])
        (self.root / "src").symlink_to(ROOT / "src")
        for name in ("setup.sh", "init.sh", "scripts/config-wizard.sh",
                     "scripts/apply-wizard-config.py", "scripts/tui-select.py"):
            shutil.copy2(ROOT / name, self.root / name)
        self.script("scripts/check-environment.sh", 'print("  FAIL: 0  WARN: 0")\n')
        self.script("scripts/doctor.sh", 'print("diagnostic doctor")\n')
        self.script("scripts/gpu-probe.sh", '''
import os
print('GPU_STATE=' + os.environ.get('STUB_GPU', 'ok'))
print('GPU_REASON=fixture GPU_ACTION=fixture GPU_BRIEF=fixture')
''')
        for name in ("scripts/setup-rime-ice.sh", "scripts/fetch-rnnoise-model.sh",
                     "scripts/setup-qwen-asr.sh", "install.sh"):
            self.script(name, '''
import json, os, pathlib, sys
name = pathlib.Path(__file__).name
root = pathlib.Path(os.environ['STUB_ROOT'])
with (root / 'events.jsonl').open('a') as stream:
    stream.write(json.dumps({'name': name, 'args': sys.argv[1:],
                            'pip': os.environ.get('VOICE_IME_PIP_INDEX_URL'),
                            'enable': os.environ.get('VOICE_IME_ENABLE_QWEN_ASR')}) + '\\n')
keys = {'setup-rime-ice.sh': 'STUB_RIME_RC', 'fetch-rnnoise-model.sh': 'STUB_RNNOISE_RC',
        'setup-qwen-asr.sh': 'STUB_QWEN_RC', 'install.sh': 'STUB_INSTALL_RC'}
sys.exit(int(os.environ.get(keys[name], '0')))
''')

    def script(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!" + sys.executable + "\n" + content, encoding="utf-8")
        path.chmod(0o755)

    def run_setup(self, *args, **extra):
        return subprocess.run(["bash", str(self.root / "setup.sh"), *args], cwd=str(self.root),
                              env=dict(self.env, **extra), stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60)

    def run_setup_tty(self, steps, **extra):
        """PTY 驱动 setup.sh 交互向导（tui-select 走 raw 终端菜单，按步喂键）。

        steps 为 (提示子串, 按键字节) 序列；最后一组按键后附加 y+CR 应答
        setup.sh 的「继续执行？」终端确认（提示走 stdout 管道，无法按提示等待）。
        """
        master, slave = pty.openpty()
        process = None
        try:
            try:
                termios.tcsetwinsize(slave, (24, 120))
            except AttributeError:  # Python 3.10 无 tcsetwinsize
                import fcntl, struct
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 120, 0, 0))
            process = subprocess.Popen(["bash", str(self.root / "setup.sh")], cwd=str(self.root),
                                       env=dict(self.env, **extra), stdin=slave,
                                       stdout=subprocess.PIPE, stderr=slave)
            decoration = b""
            for prompt, keys in steps:
                pending = b""
                deadline = time.monotonic() + 20
                while prompt.encode() not in pending:
                    remaining = deadline - time.monotonic()
                    self.assertGreater(remaining, 0, decoration.decode(errors="replace"))
                    ready, _, _ = select.select([master], [], [], remaining)
                    self.assertTrue(ready, "未出现预期提示：" + prompt)
                    chunk = os.read(master, 4096)
                    self.assertTrue(chunk, "setup 在输入完成前退出")
                    pending += chunk
                    decoration += chunk
                os.write(master, keys)
            os.write(master, b"y\n")   # 终端确认：\n 直通行终止（\r 在 raw 遗留队列里不换行）
            stdout, _ = process.communicate(timeout=45)
            while select.select([master], [], [], 0)[0]:
                decoration += os.read(master, 4096)
            return process.returncode, stdout.decode(errors="replace"), decoration.decode(errors="replace")
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()
            os.close(master)
            os.close(slave)

    def pip_events(self, name):
        return [event["pip"] for event in self.events() if event["name"] == name]

    def events(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_defaults_run_injects_nothing_and_does_not_ask(self):
        # --yes（非 TTY）→ 向导 --defaults：题目以「采用默认」出现，不注入任何变量。
        result = self.run_setup("--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("pip 包源（Python 依赖下载） [当前/默认：tsinghua]：采用默认", result.stderr)
        qwen = [event for event in self.events() if event["name"] == "setup-qwen-asr.sh"]
        self.assertEqual([event["args"] for event in qwen], [[], ["--verify-only"]])
        self.assertEqual([event["pip"] for event in qwen], [None, None])
        installs = [event for event in self.events() if event["name"] == "install.sh"]
        self.assertTrue(installs)
        self.assertTrue(all(event["pip"] is None for event in installs))
        # 默认清华不写任何持久化行（install 为 fake 不写 env 文件；apply 也不建）。
        self.assertFalse((self.home / ".config/environment.d/ibus-voice-ime.conf").exists())

    def test_persisted_default_flows_through_whole_chain_and_persists(self):
        # 环境已有持久值 default → --defaults 沿用官方源 → export 贯穿整条子进程链，
        # 且向导应用器（真实）把持久行写入临时 HOME 的 environment.d。
        result = self.run_setup("--yes", VOICE_IME_PIP_INDEX_URL="default")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[当前/默认：official]", result.stderr)
        self.assertIn("--proxy http://127.0.0.1:7890", result.stderr)
        qwen = [event for event in self.events() if event["name"] == "setup-qwen-asr.sh"]
        self.assertEqual([event["pip"] for event in qwen], ["default", "default"])
        installs = [event for event in self.events() if event["name"] == "install.sh"]
        self.assertTrue(all(event["pip"] == "default" for event in installs))
        envd = self.home / ".config/environment.d/ibus-voice-ime.conf"
        self.assertIn("VOICE_IME_PIP_INDEX_URL=default\n", envd.read_text())

    def test_dry_run_plan_shows_pip_source_only_when_overridden(self):
        plain = self.run_setup("--dry-run", "--yes")
        self.assertEqual(plain.returncode, 0, plain.stdout + plain.stderr)
        self.assertIn("实际选择：", plain.stdout)
        self.assertNotIn("pip 包源", plain.stdout)
        self.assertEqual(self.events(), [])
        overridden = self.run_setup("--dry-run", "--yes", VOICE_IME_PIP_INDEX_URL="default")
        self.assertEqual(overridden.returncode, 0, overridden.stdout + overridden.stderr)
        self.assertIn("pip 包源 官方 PyPI（VOICE_IME_PIP_INDEX_URL=default）", overridden.stdout)
        self.assertEqual(self.events(), [])

    def test_wizard_defaults_read_persisted_environmentd_without_env_var(self):
        # 同一旧登录会话（env 无变量但 environment.d 已持久）重跑 --defaults：
        # 向导从持久行取当前值，官方源继续生效且不误清持久行。
        envd = self.home / ".config/environment.d/ibus-voice-ime.conf"
        envd.parent.mkdir(parents=True, exist_ok=True)
        envd.write_text("VOICE_IME_PIP_INDEX_URL=default\n")
        result = self.run_setup("--dry-run", "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[当前/默认：official]", result.stderr)
        self.assertIn("pip 包源 官方 PyPI（VOICE_IME_PIP_INDEX_URL=default）", result.stdout)

    def test_redteam_yes_custom_url_passthrough_whole_chain(self):
        # P0 红队复现：VOICE_IME_PIP_INDEX_URL=自定义 URL + ./setup.sh --yes ——
        # 修复前向导把 custom 折叠成 tsinghua 并 unset 变量，安装链子进程实测
        # 收到 None 落回清华默认；修复后 custom 态原样透传整条子进程链。
        url = "https://my.internal.mirror/simple"
        result = self.run_setup("--yes", VOICE_IME_PIP_INDEX_URL=url)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # 向导题面呈现 custom 当前值（--yes = 回车保持）
        self.assertIn("[当前/默认：custom]", result.stderr)
        self.assertIn(f"自定义包源 URL（{url}）", result.stderr)
        self.assertIn("保持当前自定义 URL", result.stderr)
        self.assertEqual(self.pip_events("setup-qwen-asr.sh"), [url, url])
        self.assertTrue(self.pip_events("install.sh"))
        self.assertTrue(all(pip == url for pip in self.pip_events("install.sh")))
        # custom 属按次高级用法：apply 与 install 均不持久化（不写任何行）。
        self.assertFalse((self.home / ".config/environment.d/ibus-voice-ime.conf").exists())

    def test_defaults_with_environmentd_official_full_run_stays_official(self):
        # --defaults + 仅 environment.d 持久官方哨兵行（进程环境无变量）：
        # 仍 official——向导从持久行取当前值，setup export default 贯穿子进程链，
        # 持久行保持原样（已是目标状态不重复写盘）。
        envd = self.home / ".config/environment.d/ibus-voice-ime.conf"
        envd.parent.mkdir(parents=True, exist_ok=True)
        envd.write_text("VOICE_IME_PIP_INDEX_URL=default\nKEEP=1\n")
        result = self.run_setup("--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[当前/默认：official]", result.stderr)
        self.assertEqual(self.pip_events("setup-qwen-asr.sh"), ["default", "default"])
        self.assertTrue(all(pip == "default" for pip in self.pip_events("install.sh")))
        self.assertEqual(envd.read_text(), "VOICE_IME_PIP_INDEX_URL=default\nKEEP=1\n")

    def test_keyboard_only_absent_path_leaves_custom_env_untouched(self):
        # absent 路径不变：仅键盘方案不出现 pip 包源题（键缺省），setup 完全不动
        # 该变量——用户自行 export 的自定义 URL 原样透传给 install.sh 子进程。
        url = "https://my.internal.mirror/simple"
        result = self.run_setup("--keyboard-only", "--yes", VOICE_IME_PIP_INDEX_URL=url)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("pip 包源（Python 依赖下载）", result.stderr)
        self.assertTrue(self.pip_events("install.sh"))
        self.assertTrue(all(pip == url for pip in self.pip_events("install.sh")))
        # install 对自定义 URL 不代管：不写入任何持久化行。
        envd = self.home / ".config/environment.d/ibus-voice-ime.conf"
        if envd.exists():
            self.assertNotIn("VOICE_IME_PIP_INDEX_URL", envd.read_text())

    def test_tty_explicit_tsinghua_overrides_custom_env_unsets_variable(self):
        # 交互（PTY raw 菜单）显式选「清华镜像」：用户选择覆盖环境——setup 侧
        # unset VOICE_IME_PIP_INDEX_URL，安装链子进程不再见到自定义 URL。
        url = "https://my.internal.mirror/simple"
        steps = [("语音方案", b"\r"),
                 ("本地 Qwen 下载", b"\r"),
                 ("默认 Qwen 模型", b"\r"),
                 ("pip 包源（Python 依赖下载）", b"1"),      # 数字直选 1=清华镜像
                 ("语音热键（例如", b"3"),                    # 3=保持当前/默认
                 ("原文热键（例如", b"3"),
                 ("粘贴热键（例如", b"3"),
                 ("词库与降噪资产", b"\r")]
        rc, stdout, decoration = self.run_setup_tty(steps, VOICE_IME_PIP_INDEX_URL=url)
        self.assertEqual(rc, 0, stdout + decoration)
        self.assertIn("已选：清华镜像（默认）", decoration)
        self.assertEqual(self.pip_events("setup-qwen-asr.sh"), [None, None])
        self.assertTrue(self.pip_events("install.sh"))
        self.assertTrue(all(pip is None for pip in self.pip_events("install.sh")))

    def test_tty_enter_keeps_custom_url_passthrough(self):
        # 交互（PTY raw 菜单）回车取默认 = 保持 custom：自定义 URL 原样透传安装链。
        url = "https://my.internal.mirror/simple"
        steps = [("语音方案", b"\r"),
                 ("本地 Qwen 下载", b"\r"),
                 ("默认 Qwen 模型", b"\r"),
                 ("pip 包源（Python 依赖下载）", b"\r"),     # 回车=默认项（保持自定义）
                 ("语音热键（例如", b"3"),
                 ("原文热键（例如", b"3"),
                 ("粘贴热键（例如", b"3"),
                 ("词库与降噪资产", b"\r")]
        rc, stdout, decoration = self.run_setup_tty(steps, VOICE_IME_PIP_INDEX_URL=url)
        self.assertEqual(rc, 0, stdout + decoration)
        self.assertIn("已选：保持当前自定义 URL", decoration)
        self.assertEqual(self.pip_events("setup-qwen-asr.sh"), [url, url])
        self.assertTrue(all(pip == url for pip in self.pip_events("install.sh")))


class InstallPipIndexEnvdTest(unittest.TestCase):
    """真实 install.sh（轻量注册 fixture）：VOICE_IME_PIP_INDEX_URL=default 落 environment.d。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="voice-ime-pip-envd-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.root / "scripts").mkdir()
        self.env_file = self.home / ".config/environment.d/ibus-voice-ime.conf"
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("VOICE_IME_", "XDG_", "STUB_", "PYTHONPATH"))}
        self.env.update(HOME=str(self.home), XDG_CONFIG_HOME=str(self.home / ".config"),
                        PATH=str(self.bin) + ":" + self.env["PATH"])
        (self.root / "src").symlink_to(ROOT / "src")
        shutil.copy2(ROOT / "install.sh", self.root / "install.sh")
        self.script("scripts/check-environment.sh", "pass\n")
        self.script("scripts/env-sanitize.sh", "pass\n")
        # install 的 chmod 需在缺文件时也成功（fake root 没有 engine.py 等）。
        self.script("bin/chmod", "pass\n")
        self.script("bin/systemctl", "pass\n")
        self.script("bin/python3", '''
import os, sys
args = sys.argv[1:]
if args and args[0].endswith("/engine.py") and "--xml" in args:
    print("<component><name>voice-custom</name></component>"); sys.exit(0)
if args[:2] == ["-c", "import gi"]:
    sys.exit(0)
os.execv(sys.executable, [sys.executable, *args])
''')
        python = self.root / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.symlink_to(self.bin / "python3")

    def script(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!" + sys.executable + "\n" + content, encoding="utf-8")
        path.chmod(0o755)

    def run_install(self, **extra):
        return subprocess.run(["bash", str(self.root / "install.sh")], cwd=str(self.root),
                              env=dict(self.env, VOICE_IME_NO_RESTART="1", **extra),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)

    def pip_lines(self):
        if not self.env_file.exists():
            return []
        return [line for line in self.env_file.read_text().splitlines()
                if line.startswith("VOICE_IME_PIP_INDEX_URL=")]

    def test_env_default_writes_single_sentinel_line(self):
        result = self.run_install(VOICE_IME_PIP_INDEX_URL="default")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.pip_lines(), ["VOICE_IME_PIP_INDEX_URL=default"])
        self.assertIn("IBUS_COMPONENT_PATH=", self.env_file.read_text())

    def test_no_env_writes_no_line(self):
        # 默认（清华）不写任何持久化行。
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.env_file.exists())
        self.assertEqual(self.pip_lines(), [])

    def test_persisted_line_survives_rerun_without_env(self):
        # environment.d 已持久 + 本次会话未带变量：行按「用户既有非渠道行」保留。
        self.env_file.parent.mkdir(parents=True, exist_ok=True)
        self.env_file.write_text("VOICE_IME_PIP_INDEX_URL=default\nOTHER=keep\n")
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.pip_lines(), ["VOICE_IME_PIP_INDEX_URL=default"])
        self.assertIn("OTHER=keep", self.env_file.read_text())

    def test_env_default_replaces_stale_custom_line(self):
        # 本次运行显式选官方源：旧自定义 URL 行替换为哨兵行，不产生双行。
        self.env_file.parent.mkdir(parents=True, exist_ok=True)
        self.env_file.write_text("VOICE_IME_PIP_INDEX_URL=https://custom.example/simple\n")
        result = self.run_install(VOICE_IME_PIP_INDEX_URL="default")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.pip_lines(), ["VOICE_IME_PIP_INDEX_URL=default"])

    def test_custom_url_env_is_not_managed(self):
        # 自定义 URL 属按次高级用法：install 不代管、不持久化，已有行不动。
        self.env_file.parent.mkdir(parents=True, exist_ok=True)
        self.env_file.write_text("VOICE_IME_PIP_INDEX_URL=default\n")
        result = self.run_install(VOICE_IME_PIP_INDEX_URL="https://custom.example/simple")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.pip_lines(), ["VOICE_IME_PIP_INDEX_URL=default"])


if __name__ == "__main__":
    unittest.main()
