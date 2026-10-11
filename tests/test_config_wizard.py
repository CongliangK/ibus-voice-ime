"""安装向导与稀疏配置应用的沙箱行为测试（不触碰桌面/真实 HOME）。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import termios
import time
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ConfigWizardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="voice-ime-wizard-test-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.config = self.home / "config.json"
        self.result = self.home / "result.json"
        self.llm = self.home / ".config/ibus-voice-ime/llm.json"
        self.env_file = self.home / ".config/environment.d/ibus-voice-ime.conf"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("VOICE_IME_", "XDG_"))}
        self.env.update(HOME=str(self.home), VOICE_IME_CONFIG=str(self.config), VOICE_IME_WIZARD_RESULT=str(self.result))

    def wizard(self, *args, text=""):
        return subprocess.run(["bash", str(ROOT / "scripts/config-wizard.sh"), *args], input=text,
                              text=True, capture_output=True, env=self.env, timeout=10)

    def apply(self):
        return subprocess.run([sys.executable, str(ROOT / "scripts/apply-wizard-config.py"), str(self.result)],
                              text=True, capture_output=True, env=self.env, timeout=10)

    def tty_wizard(self, steps, *args):
        master, slave = pty.openpty()
        process = None
        try:
            saved = termios.tcgetattr(slave)
            if hasattr(termios, "tcsetwinsize"):
                termios.tcsetwinsize(slave, (24, 120))
            process = subprocess.Popen(["bash", str(ROOT / "scripts/config-wizard.sh"), *args],
                                       stdin=slave, stdout=subprocess.PIPE, stderr=slave, env=self.env)
            decoration = b""
            for prompt, keys in steps:
                pending = b""
                deadline = time.monotonic() + 5
                while prompt.encode() not in pending:
                    remaining = deadline - time.monotonic()
                    self.assertGreater(remaining, 0, decoration.decode(errors="replace"))
                    ready, _, _ = select.select([master], [], [], remaining)
                    self.assertTrue(ready, "未出现预期提示：" + prompt)
                    chunk = os.read(master, 4096)
                    self.assertTrue(chunk, "向导在输入完成前退出")
                    pending += chunk
                    decoration += chunk
                os.write(master, keys)
            stdout, _ = process.communicate(timeout=5)
            while select.select([master], [], [], 0)[0]:
                decoration += os.read(master, 4096)
            self.assertEqual(termios.tcgetattr(slave), saved, "向导终端属性未恢复")
            return process.returncode, stdout.decode(), decoration.decode()
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()
            os.close(master)
            os.close(slave)

    def test_tty_wizard_captures_all_three_hotkeys_and_confirms(self):
        steps = []
        for keys in (b"\x1b\x16", b"\x01", b"V"):
            steps.extend([("3) 保持当前/默认", b"2"),
                          ("Ctrl+Shift 也可能丢失 Shift，请手动输入。", keys),
                          ("3) 手动输入", b"\r")])
        rc, stdout, stderr = self.tty_wizard(steps, "--reconfigure", "--voice-plan", "keyboard-only")
        self.assertEqual(rc, 0, stderr)
        self.assertEqual(stdout, str(self.result) + "\n")
        self.assertEqual(json.loads(self.result.read_text())["hotkeys"],
                         dict(voice="Ctrl+Alt+V", raw="Ctrl+A", paste="Shift+V"))
        self.assertEqual(stderr.count("已选："), 3)
        self.assertFalse(self.config.exists())

    def test_tty_wizard_manual_and_keep_current_paths(self):
        steps = [("3) 保持当前/默认", b"1"), ("[当前/默认：Ctrl+Alt+V]：", b"Ctrl+Garbage\n"),
                 ("输入不合法", b"Super+V\n"),
                 ("3) 保持当前/默认", b"3"), ("3) 保持当前/默认", b"\r")]
        rc, stdout, stderr = self.tty_wizard(steps, "--reconfigure", "--voice-plan", "keyboard-only")
        self.assertEqual(rc, 0, stderr)
        self.assertEqual(stdout, str(self.result) + "\n")
        self.assertEqual(json.loads(self.result.read_text())["hotkeys"],
                         dict(voice="Super+V", raw="Ctrl+Alt+B", paste="Ctrl+Alt+P"))
        self.assertEqual(stderr.count("已选："), 3)

    def test_tty_wizard_capture_cancel_does_not_write_result(self):
        steps = [("3) 保持当前/默认", b"2"),
                 ("Ctrl+Shift 也可能丢失 Shift，请手动输入。", b"\x1b")]
        rc, stdout, stderr = self.tty_wizard(steps, "--reconfigure", "--voice-plan", "keyboard-only")
        self.assertEqual(rc, 130, stderr)
        self.assertEqual(stdout, "")
        self.assertFalse(self.result.exists())
        self.assertFalse(self.config.exists())

    def test_defaults_and_piped_enters_produce_same_answers(self):
        defaults = self.wizard("--defaults")
        self.assertEqual(defaults.returncode, 0, defaults.stderr)
        first = json.loads(self.result.read_text())
        piped = self.wizard(text="\n" * 11)
        self.assertEqual(piped.returncode, 0, piped.stderr)
        self.assertEqual(json.loads(self.result.read_text()), first)
        self.assertEqual(piped.stdout.strip(), str(self.result))
        self.assertEqual(first["voice_plan"], "local-qwen")
        self.assertEqual(first["qwen_sizes"], ["1.7B"])
        self.assertEqual(first["qwen_model"], "1.7b")
        self.assertEqual(first["hotkeys"], {"voice": "Ctrl+Alt+V", "raw": "Ctrl+Alt+B", "paste": "Ctrl+Alt+P"})
        self.assertIsNone(first["postprocess"])

    def test_piped_zero_at_assets_clears_all_assets(self):
        # 管道交互里资产多选输 0 = 一个都不装（红队：此前空集不可达）。
        ran = self.wizard(text="\n" * 6 + "0\n")
        self.assertEqual(ran.returncode, 0, ran.stderr)
        result = json.loads(self.result.read_text())
        self.assertEqual(result["assets"],
                         {"rime_ice": False, "zhwiki": False, "moegirl": False, "denoise": False})

    def test_defaults_and_piped_enters_produce_same_answers_tail(self):
        defaults = self.wizard("--defaults")
        self.assertEqual(defaults.returncode, 0, defaults.stderr)
        first = json.loads(self.result.read_text())
        self.assertNotIn("启用 LLM 后处理？", defaults.stderr)
        self.assertIn("未配置云端 LLM（llm.json 不存在）", defaults.stderr)
        self.assertIn("scripts/setup-llm-cloud.sh", defaults.stderr)
        self.assertTrue(all(first["assets"].values()))
        self.assertFalse(self.config.exists())
        self.assertEqual(self.result.stat().st_mode & 0o777, 0o600)

    def test_existing_values_merge_unknown_keys_and_backup(self):
        before = {"custom": {"keep": [1, 2]}, "asr": {"backend": "mimo-cloud", "qwen3": {"model": "0.6b", "max_batch": 4}},
                  "recording": {"raw_hotkeys": "Super+B", "arecord_device": "default"}, "llm": {"postprocess": True, "timeout": 9}}
        self.config.write_text(json.dumps(before))
        self.write_llm(enabled=True)
        ran = self.wizard("--defaults", "--reconfigure")
        self.assertEqual(ran.returncode, 0, ran.stderr)
        result = json.loads(self.result.read_text())
        self.assertEqual(result["voice_plan"], "mimo-cloud")
        self.assertEqual(result["qwen_model"], "0.6b")
        self.assertEqual(result["hotkeys"]["raw"], "Super+B")
        self.assertTrue(result["postprocess"])
        self.assertNotIn("安装 rime-ice", ran.stderr)
        result["hotkeys"]["voice"] = "Ctrl+Shift+V"
        result["postprocess"] = False
        self.result.write_text(json.dumps(result))
        applied = self.apply()
        self.assertEqual(applied.returncode, 0, applied.stderr)
        report = json.loads(applied.stdout)
        self.assertTrue(report["hotkeys_changed"])
        self.assertEqual(json.loads(Path(report["backup_path"]).read_text()), before)
        after = json.loads(self.config.read_text())
        self.assertEqual(after["custom"], before["custom"])
        self.assertEqual(after["recording"]["arecord_device"], "default")
        self.assertEqual(after["asr"]["qwen3"]["max_batch"], 4)
        self.assertEqual(after["llm"]["timeout"], 9)
        self.assertTrue(after["llm"]["postprocess"])  # 旧无效键保留，但向导不再写它。
        self.assertFalse(json.loads(self.llm.read_text())["enabled"])
        self.assertTrue(report["llm_changed"])
        self.assertEqual(after["recording"]["hotkeys"], "Ctrl+Shift+V")
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(report["backup_path"]).stat().st_mode & 0o777, 0o600)

    def test_invalid_hotkey_reprompts_and_gtk_syntax_normalizes(self):
        # 方案/下载/默认模型三个回车，坏热键之后改为 GTK 写法，再接受其余值。
        ran = self.wizard(text="\n\n\nCtrl+Garbage\n<Control><Shift>v\n" + "\n" * 7)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertIn("输入不合法", ran.stderr)
        self.assertEqual(json.loads(self.result.read_text())["hotkeys"]["voice"], "Ctrl+Shift+V")

    def test_engine_legal_bare_letter_lists_are_accepted(self):
        # 引擎支持空白分隔的裸字母列表（v b = Ctrl+Alt+V + Ctrl+Alt+B），
        # 既有合法配置作为默认值时不得被 --defaults 拒绝（红队 2026-10-11 发现）。
        self.config.write_text(json.dumps({"recording": {"hotkeys": "v b"}}))
        ran = self.wizard("--defaults", "--reconfigure")
        self.assertEqual(ran.returncode, 0, ran.stderr)
        result = json.loads(self.result.read_text())
        self.assertEqual(result["hotkeys"]["voice"], "Ctrl+Alt+V, Ctrl+Alt+B")

    def test_partial_garbage_hotkey_is_rejected_per_token(self):
        # 引擎会静默丢弃解析失败的 token；向导必须逐 token 严格，
        # 不得在丢掉 garbage 后侥幸放行 v（红队 2026-10-11 发现）。
        ran = self.wizard(text="\n\n\ngarbage v\nCtrl+Alt+V\n" + "\n" * 7)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertIn("输入不合法", ran.stderr)
        self.assertEqual(json.loads(self.result.read_text())["hotkeys"]["voice"], "Ctrl+Alt+V")

    def test_explicit_flags_skip_the_corresponding_questions(self):
        ran = self.wizard("--defaults", "--voice-plan", "keyboard-only", "--no-zhwiki", "--no-denoise")
        self.assertEqual(ran.returncode, 0, ran.stderr)
        result = json.loads(self.result.read_text())
        self.assertEqual(result["voice_plan"], "keyboard-only")
        self.assertFalse(result["assets"]["zhwiki"])
        self.assertFalse(result["assets"]["denoise"])
        self.assertNotIn("本地 Qwen 下载", ran.stderr)
        self.assertNotIn("安装 zhwiki", ran.stderr)
        self.assertNotIn("安装 RNNoise", ran.stderr)

    def test_invalid_existing_json_is_never_overwritten(self):
        self.assertEqual(self.wizard("--defaults").returncode, 0)
        for value in ("{broken", "[]", "null"):
            with self.subTest(value=value):
                self.config.write_text(value)
                self.assertNotEqual(self.apply().returncode, 0)
                self.assertEqual(self.config.read_text(), value)
                self.assertNotEqual(self.wizard("--defaults").returncode, 0)
                self.assertEqual(list(self.home.glob("config.json.bak.*")), [])

    def test_untrusted_result_cannot_add_secret_fields(self):
        ran = self.wizard("--defaults", "--voice-plan", "siliconflow")
        self.assertEqual(ran.returncode, 0, ran.stderr)
        result = json.loads(self.result.read_text())
        result["api_key"] = "sk-DO-NOT-PERSIST-123456789"
        result["config"] = {"asr": {"siliconflow": {"api_key": "sk-DO-NOT-PERSIST-123456789"}}}
        self.result.write_text(json.dumps(result))
        applied = self.apply()
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertNotIn("DO-NOT-PERSIST", self.config.read_text())
        config = json.loads(self.config.read_text())
        self.assertEqual(config["asr"]["backend"], "siliconflow-asr")
        self.assertTrue(config["asr"]["siliconflow"]["enabled"])
        self.assertFalse(config["asr"]["qwen3"]["enabled"])

    def test_apply_repairs_invalid_existing_hotkey(self):
        self.assertEqual(self.wizard("--defaults").returncode, 0)
        self.config.write_text('{"recording": {"hotkeys": "Ctrl+Garbage"}}')
        applied = self.apply()
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertTrue(json.loads(applied.stdout)["hotkeys_changed"])

    def test_result_path_cannot_overwrite_user_config(self):
        original = '{"llm": {"postprocess": true}}'
        self.config.write_text(original)
        self.env["VOICE_IME_WIZARD_RESULT"] = str(self.config)
        ran = self.wizard("--defaults")
        self.assertNotEqual(ran.returncode, 0)
        self.assertEqual(self.config.read_text(), original)

    def write_llm(self, **updates):
        data = dict(enabled=False, base_url="https://llm.service.cn/v1", api_key="secret-test-key",
                    model="polish-model", extra_body={"thinking": {"type": "disabled"}}, custom=[1, 2])
        data.update(updates)
        self.llm.parent.mkdir(parents=True, exist_ok=True)
        self.llm.write_text(json.dumps(data))
        return data

    def test_llm_current_enabled_drives_question_and_both_toggle_directions(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                self.config.write_text(json.dumps({"llm": {"postprocess": not enabled}}))
                before = self.write_llm(enabled=enabled)
                ran = self.wizard("--defaults", "--reconfigure")
                self.assertEqual(ran.returncode, 0, ran.stderr)
                self.assertIn(f"启用 LLM 后处理？y/n [当前/默认：{'y' if enabled else 'n'}]", ran.stderr)
                result = json.loads(self.result.read_text())
                self.assertEqual(result["postprocess"], enabled)
                # 同值不写 llm.json，也不备份。
                applied = self.apply()
                self.assertEqual(applied.returncode, 0, applied.stderr)
                self.assertFalse(json.loads(applied.stdout)["llm_changed"])
                backups_before = set(self.llm.parent.glob("llm.json.bak.*"))
                result["postprocess"] = not enabled
                self.result.write_text(json.dumps(result))
                applied = self.apply()
                self.assertEqual(applied.returncode, 0, applied.stderr)
                self.assertTrue(json.loads(applied.stdout)["llm_changed"])
                self.assertEqual(json.loads(self.llm.read_text()), dict(before, enabled=not enabled))
                backups = set(self.llm.parent.glob("llm.json.bak.*")) - backups_before
                self.assertEqual(len(backups), 1)
                backup = backups.pop()
                self.assertEqual(json.loads(backup.read_text()), before)
                self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
                self.assertEqual(self.llm.stat().st_mode & 0o777, 0o600)
                # 确认运行时即使遗留环境固定为 0，也遵守云端 enabled。
                runtime = subprocess.run([sys.executable, "-c",
                    "import sys; sys.path.insert(0, sys.argv[1]); from ibus_voice_ime.text.llm_postprocess import enabled; print(enabled())",
                    str(ROOT / "src")], env=dict(self.env, VOICE_IME_LLM_POSTPROCESS="0"),
                    text=True, capture_output=True, timeout=10)
                self.assertEqual(runtime.returncode, 0, runtime.stderr)
                self.assertEqual(runtime.stdout.strip(), str(not enabled))

    def test_absent_llm_is_skipped_and_never_created(self):
        self.assertEqual(self.wizard("--defaults").returncode, 0)
        applied = self.apply()
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertFalse(json.loads(applied.stdout)["llm_changed"])
        self.assertFalse(self.llm.exists())
        self.assertNotIn("llm", json.loads(self.config.read_text()))
        result = json.loads(self.result.read_text())
        result["postprocess"] = True
        self.result.write_text(json.dumps(result))
        applied = self.apply()
        self.assertNotEqual(applied.returncode, 0)
        self.assertIn("llm.json 不存在", applied.stderr)
        self.assertFalse(self.llm.exists())

    def test_corrupt_llm_refused_without_overwrite_or_backup(self):
        self.write_llm()
        self.assertEqual(self.wizard("--defaults").returncode, 0)
        result = json.loads(self.result.read_text())
        result["postprocess"] = True
        self.result.write_text(json.dumps(result))
        for text in ('{broken', '[]', 'null', '{"enabled": "false"}'):
            with self.subTest(text=text):
                self.llm.write_text(text)
                applied = self.apply()
                self.assertNotEqual(applied.returncode, 0)
                self.assertIn("llm.json", applied.stderr)
                self.assertNotIn("Traceback", applied.stderr)
                self.assertEqual(self.llm.read_text(), text)
                self.assertFalse(self.config.exists())
                self.assertEqual(list(self.llm.parent.glob("llm.json.bak.*")), [])
                self.assertNotEqual(self.wizard("--defaults").returncode, 0)

    def test_llm_path_uses_runtime_override_and_xdg_rules(self):
        for key, value, target in (
            ("VOICE_IME_LLM_CONFIG", str(self.home / "custom-llm.json"), self.home / "custom-llm.json"),
            ("XDG_CONFIG_HOME", str(self.home / "xdg"), self.home / "xdg/ibus-voice-ime/llm.json"),
        ):
            with self.subTest(key=key):
                self.env[key] = value
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('{"enabled": true, "custom": "keep"}')
                ran = self.wizard("--defaults")
                self.assertEqual(ran.returncode, 0, ran.stderr)
                result = json.loads(self.result.read_text())
                self.assertTrue(result["postprocess"])
                result["postprocess"] = False
                self.result.write_text(json.dumps(result))
                applied = self.apply()
                self.assertEqual(applied.returncode, 0, applied.stderr)
                self.assertEqual(json.loads(target.read_text()), {"enabled": False, "custom": "keep"})
                self.env.pop(key)

    def test_changed_hotkeys_clear_only_three_environment_lines_and_backup(self):
        before = ("# keep comment\nVOICE_IME_HOTKEYS=Super+V\n"
                  "VOICE_IME_RAW_HOTKEYS=Super+B\nVOICE_IME_CLIPBOARD_HOTKEYS=Super+P\n"
                  "VOICE_IME_HOTKEYS=Ctrl+V\nVOICE_IME_RIME_LIBRARY=/keep/path\nOTHER=keep\n")
        self.env_file.parent.mkdir(parents=True)
        self.env_file.write_text(before)
        self.assertEqual(self.wizard("--defaults").returncode, 0)
        result = json.loads(self.result.read_text())
        result["hotkeys"]["voice"] = "Ctrl+Shift+V"
        self.result.write_text(json.dumps(result))
        applied = self.apply()
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(json.loads(applied.stdout)["env_hotkeys_cleared"], 4)
        self.assertEqual(self.env_file.read_text(), "# keep comment\nVOICE_IME_RIME_LIBRARY=/keep/path\nOTHER=keep\n")
        backups = list(self.env_file.parent.glob("ibus-voice-ime.conf.bak.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), before)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)

    def test_unchanged_hotkeys_do_not_clean_or_backup_environment_file(self):
        self.env_file.parent.mkdir(parents=True)
        before = "VOICE_IME_HOTKEYS=Super+V\n"
        self.env_file.write_text(before)
        self.assertEqual(self.wizard("--defaults").returncode, 0)
        applied = self.apply()
        self.assertEqual(applied.returncode, 0, applied.stderr)
        report = json.loads(applied.stdout)
        self.assertFalse(report["hotkeys_changed"])
        self.assertEqual(report["env_hotkeys_cleared"], 0)
        self.assertEqual(self.env_file.read_text(), before)
        self.assertEqual(list(self.env_file.parent.glob("ibus-voice-ime.conf.bak.*")), [])

    def test_changed_hotkeys_with_missing_environment_file_skips_cleanup(self):
        self.assertEqual(self.wizard("--defaults").returncode, 0)
        result = json.loads(self.result.read_text())
        result["hotkeys"]["voice"] = "Ctrl+Shift+V"
        self.result.write_text(json.dumps(result))
        applied = self.apply()
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertTrue(json.loads(applied.stdout)["hotkeys_changed"])
        self.assertEqual(json.loads(applied.stdout)["env_hotkeys_cleared"], 0)
        self.assertFalse(self.env_file.exists())

    def test_environment_cleanup_failure_is_fail_closed(self):
        self.assertEqual(self.wizard("--defaults").returncode, 0)
        result = json.loads(self.result.read_text())
        result["hotkeys"]["voice"] = "Ctrl+Shift+V"
        self.result.write_text(json.dumps(result))
        # 确定性的 I/O 失败（包括以 root 跑测试时），不依赖 chmod 权限策略。
        self.env_file.mkdir(parents=True)
        applied = self.apply()
        self.assertNotEqual(applied.returncode, 0)
        self.assertIn("警告：environment.d 热键覆盖清理失败", applied.stderr)
        self.assertEqual(applied.stdout, "")
        self.assertFalse(self.config.exists())

    def test_standalone_wizard_cleans_implicit_tmpdir_result(self):
        self.env.pop("VOICE_IME_WIZARD_RESULT")
        self.env["TMPDIR"] = str(self.home)
        ran = self.wizard("--defaults")
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertTrue(Path(ran.stdout.strip()).name.startswith("ibus-voice-ime-wizard-"))
        self.assertEqual(list(self.home.glob("ibus-voice-ime-wizard-*.json")), [])

    def test_empty_input_cancels_without_writing_result(self):
        ran = self.wizard()
        self.assertNotEqual(ran.returncode, 0)
        self.assertFalse(self.result.exists())
        self.assertFalse(self.config.exists())

    def test_numbered_plan_selects_nondefault_and_assets_share_one_question(self):
        ran = self.wizard(text="2\n\n\n\n1,4\n")
        self.assertEqual(ran.returncode, 0, ran.stderr)
        result = json.loads(self.result.read_text())
        self.assertEqual(result["voice_plan"], "siliconflow")
        self.assertEqual(result["assets"], dict(rime_ice=True, zhwiki=False, moegirl=False, denoise=True))
        self.assertNotIn("本地 Qwen 下载", ran.stderr)
        self.assertEqual(ran.stdout, str(self.result) + "\n")
        self.assertEqual(ran.stderr.count("已选："), 5)

    def test_numbered_plan_invalid_number_reprompts(self):
        ran = self.wizard(text="9\n2\n" + "\n" * 4)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertIn("输入不合法", ran.stderr)
        self.assertEqual(json.loads(self.result.read_text())["voice_plan"], "siliconflow")

    def test_numbered_download_both_model_and_llm_toggle(self):
        self.write_llm(enabled=True)
        ran = self.wizard(text="\n3\n2\n\n\n\n2\n\n")
        self.assertEqual(ran.returncode, 0, ran.stderr)
        result = json.loads(self.result.read_text())
        self.assertEqual(result["qwen_sizes"], ["0.6B", "1.7B"])
        self.assertEqual(result["qwen_model"], "0.6b")
        self.assertFalse(result["postprocess"])
        self.assertTrue(all(result["assets"].values()))

    def test_numbered_assets_cannot_reenable_explicit_overrides(self):
        ran = self.wizard("--voice-plan", "keyboard-only", "--no-zhwiki", "--no-denoise",
                          text="\n\n\n1,2\n")
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertEqual(json.loads(self.result.read_text())["assets"],
                         dict(rime_ice=True, zhwiki=False, moegirl=True, denoise=False))
        self.assertNotIn("安装 zhwiki", ran.stderr)
        self.assertNotIn("安装 RNNoise", ran.stderr)

    def test_numbered_model_outside_download_set_reprompts(self):
        ran = self.wizard(text="\n2\n1\n2\n" + "\n" * 4)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertIn("默认模型必须在本次下载集合中", ran.stderr)
        result = json.loads(self.result.read_text())
        self.assertEqual(result["qwen_sizes"], ["0.6B"])
        self.assertEqual(result["qwen_model"], "0.6b")


class TuiSelectTest(unittest.TestCase):
    def selector(self, *args, text=""):
        return subprocess.run([sys.executable, str(ROOT / "scripts/tui-select.py"), *args],
                              input=text, text=True, capture_output=True, timeout=5)

    def test_empty_line_uses_zero_based_default(self):
        ran = self.selector("--title", "测试菜单", "--default", "1", "--", "第一项", "第二项", text="\n")
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertEqual(ran.stdout, "1\n")
        self.assertIn("2) 第二项 [默认]", ran.stderr)
        self.assertIn("测试菜单", ran.stderr)
        self.assertNotIn("\x1b", ran.stderr)

    def test_number_is_one_based_and_confirms(self):
        ran = self.selector("--", "第一项", "第二项", text="2\n")
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertEqual(ran.stdout, "1\n")

    def test_multi_checked_accepts_commas_spaces_duplicates_and_empty_set(self):
        for checked, expected in (("0, 2 2", "0 2\n"), ("", "\n")):
            with self.subTest(checked=checked):
                ran = self.selector("--multi", "--checked", checked, "--", "甲", "乙", "丙", text="\n")
                self.assertEqual(ran.returncode, 0, ran.stderr)
                self.assertEqual(ran.stdout, expected)

    def test_multi_numeric_reply_replaces_checked_set(self):
        ran = self.selector("--multi", "--checked", "0", "--", "甲", "乙", "丙", text="3,2 3\n")
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertEqual(ran.stdout, "1 2\n")

    def test_multi_reply_zero_clears_nonempty_checked(self):
        # 默认勾选非空时，空集在降级模式原本不可达（红队 2026-10-11）：
        # 0 是显式"清空全部"，与 TTY 空格逐项取消的能力对齐。
        ran = self.selector("--multi", "--checked", "0,1,2", "--", "甲", "乙", "丙", text="0\n")
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertEqual(ran.stdout, "\n")
        self.assertIn("0=清空全部", ran.stderr)

    def test_invalid_numbers_reprompt_until_valid(self):
        ran = self.selector("--", "甲", "乙", text="0\n3\nno\n1,2\n2\n")
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertEqual(ran.stdout, "1\n")
        self.assertEqual(ran.stderr.count("输入不合法"), 4)

    def test_invalid_multi_numbers_reprompt(self):
        ran = self.selector("--multi", "--", "甲", "乙", text="1,9\n2\n")
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertEqual(ran.stdout, "1\n")
        self.assertIn("输入不合法", ran.stderr)

    def test_eof_is_failure_even_after_invalid_input(self):
        for text in ("", "99\n", "\x1b\n", "q\n"):
            with self.subTest(text=text):
                ran = self.selector("--", "甲", "乙", text=text)
                self.assertEqual(ran.returncode, 1, ran.stderr)
                self.assertEqual(ran.stdout, "")
                self.assertIn("输入已结束", ran.stderr)
                if text:
                    self.assertIn("输入不合法", ran.stderr)

    def test_invalid_parameters_fail_without_traceback(self):
        for args in (("--default", "9"), ("--default", "坏"), ("--checked", "0"),
                     ("--multi", "--checked", "2"), ("--未知",)):
            with self.subTest(args=args):
                ran = self.selector(*args, "--", "甲", "乙", text="\n")
                self.assertEqual(ran.returncode, 2, ran.stderr)
                self.assertEqual(ran.stdout, "")
                self.assertIn("错误", ran.stderr)
                self.assertNotIn("Traceback", ran.stderr)

    def test_chinese_help_only_uses_stderr(self):
        for flag in ("-h", "--help"):
            ran = self.selector(flag)
            self.assertEqual(ran.returncode, 0, ran.stderr)
            self.assertEqual(ran.stdout, "")
            self.assertIn("用法", ran.stderr)
            self.assertIn("回车", ran.stderr)

    def tty_selection(self, keys, *args, send_signal=False, pipe_output=False, check_disposition=False):
        master, slave = pty.openpty()
        process = None
        try:
            saved = termios.tcgetattr(slave)
            if hasattr(termios, "tcsetwinsize"):
                termios.tcsetwinsize(slave, (24, 30))
            options = [] if "--capture" in args else ["--", "第一项很长" * 10, "第二项"]
            command = [sys.executable, str(ROOT / "scripts/tui-select.py"), *args, *options]
            if check_disposition:
                wrapper = """import runpy, signal, sys
namespace = runpy.run_path(sys.argv.pop(1))
def previous(signum, frame):
    pass
signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
for sig in signals:
    signal.signal(sig, previous)
rc = namespace['main']()
assert all(signal.getsignal(sig) is previous for sig in signals), '调用方信号处理未恢复'
sys.exit(rc)
"""
                command = [sys.executable, "-c", wrapper, *command[1:]]
            process = subprocess.Popen(command, stdin=slave,
                                       stdout=subprocess.PIPE if pipe_output else slave, stderr=subprocess.PIPE)
            # 等 raw 模式就绪再输入，避免在规范模式中被回显/缓冲。
            decoration = b""
            deadline = time.monotonic() + 5
            while b"\x1b[?25l" not in decoration:
                remaining = deadline - time.monotonic()
                self.assertGreater(remaining, 0, "终端菜单未及时就绪")
                ready, _, _ = select.select([process.stderr], [], [], remaining)
                self.assertTrue(ready, "终端菜单未及时就绪")
                chunk = os.read(process.stderr.fileno(), 4096)
                self.assertTrue(chunk, "选择器在菜单就绪前退出")
                decoration += chunk
            if send_signal:
                process.send_signal(signal.SIGINT if send_signal is True else send_signal)
            else:
                os.write(master, keys)
            captured, tail = process.communicate(timeout=5)
            decoration += tail
            self.assertEqual(termios.tcgetattr(slave), saved, "终端属性未恢复")
            output = captured if pipe_output else b""
            while select.select([master], [], [], 0)[0]:
                output += os.read(master, 4096)
            self.assertIn(b"\x1b[?25h", decoration)
            return process.returncode, output.decode(), decoration.decode()
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()
            os.close(master)
            os.close(slave)

    def test_capture_non_tty_requires_manual_input(self):
        ran = self.selector("--capture", "--title", "语音热键", text="v\n")
        self.assertEqual(ran.returncode, 2, ran.stderr)
        self.assertEqual(ran.stdout, "")
        self.assertIn("需要终端", ran.stderr)
        self.assertIn("手动输入", ran.stderr)

    def test_real_tty_capture_byte_mapping_matches_normalizer(self):
        for keys, spec in ((b"\x1b\x16", "Ctrl+Alt+V"), (b"\x01", "Ctrl+A"),
                           (b"V", "Shift+V"), (b"v", "v"), (b"\x1bx", "Alt+X"),
                           (b"\x1bV", "Alt+Shift+V"), (b"\x03", "Ctrl+C")):
            with self.subTest(keys=keys):
                normalized = subprocess.run(
                    [sys.executable, str(ROOT / "scripts/apply-wizard-config.py"),
                     "--validate-hotkey", spec], text=True, capture_output=True, timeout=5)
                self.assertEqual(normalized.returncode, 0, normalized.stderr)
                rc, stdout, stderr = self.tty_selection(keys + b"\r", "--capture", "--title", "语音热键")
                self.assertEqual(rc, 0, stderr)
                self.assertEqual(stdout.strip(), normalized.stdout.strip())
                self.assertIn("使用 " + normalized.stdout.strip(), stderr)
                self.assertIn("（实验性）按住修饰键后再按一个字母完成录入", stderr)
                self.assertIn("Super 键、纯修饰键组合（如 Ctrl+Super）终端捕获不到", stderr)

    def test_real_tty_capture_stdout_pipe_and_caller_signal_dispositions(self):
        for keys, sig, expected in ((b"\x1b\x16\r", False, 0), (b"\x1b", False, 130),
                                    (b"", signal.SIGTERM, 130), (b"", signal.SIGHUP, 130),
                                    (b"", signal.SIGINT, 130)):
            with self.subTest(keys=keys, sig=sig):
                rc, stdout, stderr = self.tty_selection(
                    keys, "--capture", send_signal=sig, pipe_output=True, check_disposition=True)
                self.assertEqual(rc, expected, stderr)
                self.assertEqual(stdout, "Ctrl+Alt+V\n" if expected == 0 else "")

    def test_real_tty_capture_ignores_navigation_function_and_invalid_keys(self):
        # 序列末尾的 A 等字母不可被当成主键；也覆盖 Linux 控制台 F1 与
        # SS3 F1（\x1bOP）。\x1bOB 现在按消歧规则解析为 Alt+Shift+O 合法
        # 组合，不再属于"被忽略的功能键"，故从无效序列中移除。
        invalid = b"\x1b[A\x1bOP\x1b[15~\x1b[[A\t\r\n 1"
        rc, stdout, stderr = self.tty_selection(invalid + b"\x01\r", "--capture")
        self.assertEqual(rc, 0, stderr)
        self.assertEqual(stdout.strip(), "Ctrl+A")
        # \t、\r、\n、空格、1 五个无效主键各提示一次；转义序列整体静默忽略。
        self.assertEqual(stderr.count("主键只支持字母"), 5)

    def test_real_tty_capture_alt_shift_o_with_pushback(self):
        # ESC O（无后续字节）是完整的 Alt+Shift+O 组合，不是 SS3 前缀；
        # 紧随其后的确认键经推回缓冲不会被吞（红队 P2-2 修复）。
        rc, stdout, stderr = self.tty_selection(b"\x1bO\r", "--capture")
        self.assertEqual(rc, 0, stderr)
        self.assertEqual(stdout.strip(), "Alt+Shift+O")
        # SS3 功能键（F1=\x1bOP）整体忽略，不产生捕获。
        rc, stdout, stderr = self.tty_selection(b"\x1bOP\x01\r", "--capture")
        self.assertEqual(rc, 0, stderr)
        self.assertEqual(stdout.strip(), "Ctrl+A")

    def test_real_tty_capture_rerecord_and_manual_validation(self):
        rc, stdout, stderr = self.tty_selection(b"v2\x01\r", "--capture")
        self.assertEqual(rc, 0, stderr)
        self.assertEqual(stdout.strip(), "Ctrl+A")
        rc, stdout, stderr = self.tty_selection(b"v3Ctrl+Garbage\r<Control><Shift>v\r", "--capture")
        self.assertEqual(rc, 0, stderr)
        self.assertEqual(stdout.strip(), "Ctrl+Shift+V")
        self.assertIn("输入不合法", stderr)
        rc, stdout, stderr = self.tty_selection(b"v3Super+V\r", "--capture", "--expect-format", "格式示例")
        self.assertEqual(rc, 0, stderr)
        self.assertEqual(stdout.strip(), "Super+V")
        self.assertIn("格式提示：格式示例", stderr)

    def test_real_tty_capture_cancel_and_signals_restore_terminal(self):
        for keys in (b"\x1b", b"v\x1b", b"v3\x1b"):
            with self.subTest(keys=keys):
                rc, stdout, stderr = self.tty_selection(keys, "--capture")
                self.assertEqual(rc, 130, stderr)
                self.assertEqual(stdout, "")
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(sig=sig):
                rc, stdout, stderr = self.tty_selection(b"", "--capture", send_signal=sig)
                self.assertEqual(rc, 130, stderr)
                self.assertEqual(stdout, "")

    def test_real_tty_down_enter_and_terminal_restore(self):
        rc, stdout, stderr = self.tty_selection(b"\x1b[B\r")
        self.assertEqual(rc, 0, stderr)
        self.assertEqual(stdout.strip(), "1")
        self.assertIn("\x1b[7m", stderr)
        self.assertNotIn("很长" * 10, stderr)  # 窄窗口确实截断长选项。

    def test_real_tty_digit_and_home_end_movement(self):
        for keys, expected in ((b"2", "1"), (b"j\x1b[H\r", "0"),
                               (b"\x1b[F\r", "1"), (b"jk\r", "0")):
            with self.subTest(keys=keys):
                rc, stdout, stderr = self.tty_selection(keys)
                self.assertEqual(rc, 0, stderr)
                self.assertEqual(stdout.strip(), expected)

    def test_real_tty_multi_space_and_numeric_toggle(self):
        rc, stdout, stderr = self.tty_selection(b" \x1b[B \r", "--multi", "--checked", "0")
        self.assertEqual(rc, 0, stderr)
        self.assertEqual(stdout.strip(), "1")
        rc, stdout, stderr = self.tty_selection(b"12\r", "--multi")
        self.assertEqual(rc, 0, stderr)
        self.assertEqual(stdout.strip(), "0 1")

    def test_real_tty_cancel_and_sigint_restore_terminal(self):
        for keys in (b"\x1b", b"q", b"\x03"):
            with self.subTest(keys=keys):
                rc, stdout, stderr = self.tty_selection(keys)
                self.assertEqual(rc, 130, stderr)
                self.assertEqual(stdout, "")
        rc, stdout, stderr = self.tty_selection(b"", send_signal=True)
        self.assertEqual(rc, 130, stderr)
        self.assertEqual(stdout, "")

    def test_real_tty_sigterm_restores_terminal(self):
        # SIGTERM 默认动作会在 finally 恢复 termios 之前杀死进程，
        # 遗留 raw 模式 + 隐藏光标（红队 2026-10-11 PTY 实测）。
        master, slave = pty.openpty()
        process = None
        try:
            saved = termios.tcgetattr(slave)
            if hasattr(termios, "tcsetwinsize"):
                termios.tcsetwinsize(slave, (24, 30))
            process = subprocess.Popen([sys.executable, str(ROOT / "scripts/tui-select.py"),
                                        "--title", "t", "--", "第一项", "第二项"],
                                       stdin=slave, stdout=slave, stderr=subprocess.PIPE)
            decoration = b""
            deadline = time.monotonic() + 5
            while b"\x1b[?25l" not in decoration:
                remaining = deadline - time.monotonic()
                self.assertGreater(remaining, 0, "终端菜单未及时就绪")
                ready, _, _ = select.select([process.stderr], [], [], remaining)
                self.assertTrue(ready, "终端菜单未及时就绪")
                chunk = os.read(process.stderr.fileno(), 4096)
                self.assertTrue(chunk, "选择器在菜单就绪前退出")
                decoration += chunk
            process.send_signal(signal.SIGTERM)
            _, tail = process.communicate(timeout=5)
            decoration += tail
            self.assertEqual(process.returncode, 130, decoration.decode(errors="replace"))
            self.assertEqual(termios.tcgetattr(slave), saved, "SIGTERM 后终端属性未恢复")
            self.assertIn(b"\x1b[?25h", decoration, "SIGTERM 后未恢复光标显示")
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()
            os.close(master)
            os.close(slave)


if __name__ == "__main__":
    unittest.main()
