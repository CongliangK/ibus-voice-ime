# -*- coding: utf-8 -*-
"""Path-only self-healing; all script tests use copied repos and fake HOME/tools."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "scripts/env-sanitize.sh"
PATH_KEYS = (
    "VOICE_IME_QWEN_ASR_PYTHON",
    "VOICE_IME_QWEN_ASR_MODEL_PATH",
    "VOICE_IME_QWEN_ASR_MODELS_DIR",  # compatibility key, not currently mapped
    "VOICE_IME_MIMO_ASR_PYTHON",
    "VOICE_IME_MIMO_ASR_MODELS_DIR",
    "VOICE_IME_MIMO_ASR_MODEL_PATH",
    "VOICE_IME_MIMO_ASR_TOKENIZER_PATH",
)


class EnvSanitizeSandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="env-sanitize-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo 中文 with spaces"
        self.root.mkdir()
        self.home = Path(self.tmp.name) / "home 中文 with spaces"
        self.home.mkdir()
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("VOICE_IME_", "XDG_", "MIMO", "VOLC", "SILICONFLOW", "PI_BWS_", "BWS_"))
        }
        self.env.update(HOME=str(self.home), XDG_CONFIG_HOME=str(self.home / ".config"))
        self.env_file = self.home / ".config/environment.d/ibus-voice-ime.conf"
        self.snapshot = self.home / "received.json"
        self.env["SANITIZE_SNAPSHOT"] = str(self.snapshot)

    def run_bash(self, *args, extra_env=None):
        env = dict(self.env)
        env.update(extra_env or {})
        return subprocess.run(
            ["bash", *map(str, args)], env=env, cwd=self.root,
            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
        )

    def script(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)
        return path

    def copy(self, relative):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
        return target

    def make_entrypoints(self):
        for relative in ("run-engine.sh", "install.sh", "scripts/switch-qwen-asr.sh",
                         "scripts/env-sanitize.sh", "scripts/env-file-load.sh"):
            self.copy(relative)
        self.script("src/ibus_voice_ime/__init__.py", "")
        (self.root / "src/ibus_voice_ime/config.py").symlink_to(ROOT / "src/ibus_voice_ime/config.py")
        self.script("src/ibus_voice_ime/engine.py", """import json, os, sys
if '--xml' in sys.argv:
    print('<component/>')
else:
    with open(os.environ['SANITIZE_SNAPSHOT'], 'w', encoding='utf-8') as f:
        json.dump(dict(os.environ), f)
""")
        python = shlex.quote(sys.executable)
        # install's gi check is unrelated to environment sanitization.
        self.script(".venv/bin/python", f"#!/bin/sh\nif [ \"$1\" = -c ] && [ \"$2\" = 'import gi' ]; then exit 0; fi\nexec {python} \"$@\"\n")
        self.script("scripts/ibus-restart.sh", f"""#!/bin/sh
exec {python} -c 'import json,os; open(os.environ["SANITIZE_SNAPSHOT"], "w").write(json.dumps(dict(os.environ)))'
""")
        for name in ("activate-voice-engine.sh", "install-gnome-voice-hotkey.sh",
                     "install-gnome-clipboard-paste-hotkey.sh"):
            self.script("scripts/" + name, "#!/bin/sh\nexit 0\n")
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        # Use a closed PATH: never fall through to the real desktop or manager.
        # wc/tail/cut 必须闭合：引擎日志已存在时 trim_log 走 wc 分支，
        # 同一沙箱第二次启动会因缺 wc 返回 127（红队 2026-10-11 发现）。
        for name in ("bash", "dirname", "mkdir", "chmod", "mktemp", "grep", "mv", "rm", "cat", "env", "python3",
                     "wc", "tail", "cut"):
            (bin_dir / name).symlink_to(shutil.which(name))
        for name in ("systemctl", "pkill", "ibus", "ibus-daemon", "gsettings"):
            self.script("bin/" + name, "#!/bin/sh\nexit 0\n")
        self.env["PATH"] = str(bin_dir)
        (self.root / "vendor/models/qwen3-asr/Qwen3-ASR-0.6B").mkdir(parents=True)

    def received(self):
        return json.loads(self.snapshot.read_text(encoding="utf-8"))


class EnvSanitizeHelperTest(EnvSanitizeSandbox):
    def helper(self, values, *keys):
        return self.run_bash(HELPER, *(keys or tuple(values)), extra_env=values)

    def test_env_sanitize_missing_paths_emit_unset(self):
        # 注意 '~missing'（未知用户简写）不在其中：~user 形式查不到 passwd
        # 时保守保留，绝不误 unset 可能有效的救援值（红队 P2-1 语义）。
        for value in (str(self.root / "deleted/python"), "relative/deleted/python", "~/deleted/python"):
            with self.subTest(value=value):
                result = self.helper({PATH_KEYS[0]: value})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "unset " + PATH_KEYS[0] + "\n")
                self.assertIn("会话环境 " + PATH_KEYS[0] + " 指向不存在的路径，已忽略：" + value, result.stderr)
                self.assertIn("旧安装残留？", result.stderr)

    def test_env_sanitize_existing_empty_nonpath_and_unset_are_noop(self):
        existing = self.root / "有效 python"
        existing.touch()
        home_file = self.home / "python 中文"
        home_file.touch()
        for value in (str(existing), str(self.root), "python3", "1.7b", "", "~", "~/python 中文"):
            with self.subTest(value=value):
                result = self.helper({PATH_KEYS[0]: value})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "")
        result = self.helper({}, PATH_KEYS[0])
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_env_sanitize_spaces_chinese_and_shell_metacharacters_are_safe(self):
        marker = self.root / "must not execute"
        value = str(self.root / f"旧 安装/$(touch '{marker}')`;echo danger;python")
        key = PATH_KEYS[0]
        result = self.run_bash(
            "-c", 'eval "$("$1" "$2")"; [[ ! -v "$2" ]]',
            "test", HELPER, key, extra_env={key: value},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(value, result.stderr)
        self.assertFalse(marker.exists())

    def test_env_sanitize_invalid_key_cannot_inject_eval(self):
        result = self.helper({}, "VOICE_IME_X; echo unsafe", "9BAD")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_env_sanitize_only_requested_keys_are_checked(self):
        result = self.helper({PATH_KEYS[0]: str(self.root / "deleted"),
                              "VOICE_IME_VOLC_BIGMODEL_BASE_URL": "https://example.invalid/asr"}, PATH_KEYS[0])
        self.assertEqual(result.stdout, "unset " + PATH_KEYS[0] + "\n")
        self.assertNotIn("VOLC", result.stderr)

    def test_env_sanitize_from_file_reports_stale_and_keeps_existing(self):
        # 使用者实测场景：environment.d 里留着旧克隆的解释器/模型路径。
        survivor = self.root / "rescue/python"
        survivor.parent.mkdir(parents=True, exist_ok=True)
        survivor.touch()
        env_file = self.root / "env-file.conf"
        env_file.write_text(
            f"{PATH_KEYS[0]}={self.root}/deleted-old-ime/.venv-qwen-asr/bin/python\n"
            f"{PATH_KEYS[1]}={survivor}\n"
            "VOICE_IME_RIME_LIBRARY=/opt/rime/lib.so\n",
            encoding="utf-8")
        result = subprocess.run(["bash", str(HELPER), "--from-file", str(env_file), *PATH_KEYS],
                                env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"unset {PATH_KEYS[0]}\n")
        self.assertIn("环境文件", result.stderr)
        self.assertNotIn(PATH_KEYS[1], result.stdout)

    def test_env_sanitize_from_file_uses_file_not_process_env_and_last_wins(self):
        # 判定只看文件（进程环境不得干扰），同一键取最后一次出现（systemd 覆盖语义）。
        survivor = self.root / "rescue/dir"
        survivor.mkdir(parents=True, exist_ok=True)
        env_file = self.root / "env-file.conf"
        env_file.write_text(f"{PATH_KEYS[0]}={survivor}\n{PATH_KEYS[0]}={self.root}/stale\n",
                            encoding="utf-8")
        result = subprocess.run(["bash", str(HELPER), "--from-file", str(env_file), PATH_KEYS[0]],
                                env={**self.env, PATH_KEYS[0]: str(survivor)},
                                capture_output=True, text=True, timeout=10)
        self.assertEqual((result.returncode, result.stdout), (0, f"unset {PATH_KEYS[0]}\n"))

    def test_env_sanitize_from_file_never_executes_values(self):
        marker = self.root / "must not execute"
        env_file = self.root / "env-file.conf"
        env_file.write_text(
            f"{PATH_KEYS[0]}={self.root}/旧 安装/$(touch '{marker}')`;echo danger;python\n",
            encoding="utf-8")
        result = subprocess.run(["bash", str(HELPER), "--from-file", str(env_file), PATH_KEYS[0]],
                                env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"unset {PATH_KEYS[0]}\n")
        self.assertFalse(marker.exists())


class EnvSanitizeEntrypointTest(EnvSanitizeSandbox):
    def setUp(self):
        super().setUp()
        self.make_entrypoints()

    def stale_values(self):
        return {key: str(self.root / "deleted-old-ime" / key / "python") for key in PATH_KEYS}

    def test_run_engine_env_sanitize_stale_paths_are_absent(self):
        values = self.stale_values()
        values[PATH_KEYS[0]] = "/tmp/deleted-old-ime/.venv-qwen-asr/bin/python"
        result = self.run_bash(self.root / "run-engine.sh", extra_env=values)
        self.assertEqual(result.returncode, 0, result.stderr)
        received = self.received()
        # run-engine redirects stderr to engine.log before self-healing.
        log = (self.home / ".local/share/ibus-voice-ime/engine.log").read_text(encoding="utf-8")
        for key in PATH_KEYS:
            self.assertNotIn(key, received)
            self.assertEqual(log.count("会话环境 " + key + " 指向不存在的路径"), 1)

    def test_run_engine_env_sanitize_valid_custom_python_survives(self):
        python = self.script("custom 中文/python", "#!/bin/sh\nexit 0\n")
        result = self.run_bash(self.root / "run-engine.sh", extra_env={PATH_KEYS[0]: str(python)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.received()[PATH_KEYS[0]], str(python))
        log = (self.home / ".local/share/ibus-voice-ime/engine.log").read_text(encoding="utf-8")
        self.assertNotIn("会话环境 " + PATH_KEYS[0], log)

    def test_run_engine_env_sanitize_checks_loaded_file_and_new_value_wins(self):
        python = self.script("new installation/python", "#!/bin/sh\nexit 0\n")
        self.env_file.parent.mkdir(parents=True)
        self.env_file.write_text(f"{PATH_KEYS[0]}={python}\n{PATH_KEYS[1]}={self.root}/deleted-model\n", encoding="utf-8")
        result = self.run_bash(self.root / "run-engine.sh", extra_env=self.stale_values())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.received()[PATH_KEYS[0]], str(python))
        self.assertNotIn(PATH_KEYS[1], self.received())

    def test_switch_env_sanitize_daemon_does_not_inherit_stale_paths(self):
        self.env_file.parent.mkdir(parents=True)
        self.env_file.write_text("VOICE_IME_QWEN_ASR=1\nVOICE_IME_RIME_LIBRARY=/opt/rime/lib.so\n", encoding="utf-8")
        result = self.run_bash(self.root / "scripts/switch-qwen-asr.sh", "0.6b", extra_env=self.stale_values())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for key in PATH_KEYS:
            self.assertNotIn(key, self.received())
            self.assertEqual(result.stderr.count("会话环境 " + key + " 指向不存在的路径"), 1)
        self.assertEqual(self.env_file.read_text(encoding="utf-8"), "VOICE_IME_RIME_LIBRARY=/opt/rime/lib.so\n")

    def test_switch_env_sanitize_valid_custom_paths_survive(self):
        python = self.script("custom 中文/python", "#!/bin/sh\nexit 0\n")
        values = {key: str(python if key.endswith("PYTHON") else self.root) for key in PATH_KEYS}
        result = self.run_bash(self.root / "scripts/switch-qwen-asr.sh", "0.6b", extra_env=values)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for key, value in values.items():
            self.assertEqual(self.received()[key], value)
        self.assertNotIn("会话环境", result.stderr)

    def test_install_env_sanitize_before_write_and_restart(self):
        result = self.run_bash(self.root / "install.sh", extra_env=self.stale_values())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for key in PATH_KEYS:
            self.assertNotIn(key, self.received())
            self.assertNotIn(key, self.env_file.read_text(encoding="utf-8"))
            self.assertIn("会话环境 " + key + " 指向不存在的路径", result.stderr)

    def test_install_env_sanitize_preserves_existing_custom_python(self):
        python = self.script("custom 中文/python", "#!/bin/sh\nexit 0\n")
        result = self.run_bash(self.root / "install.sh", extra_env={PATH_KEYS[0]: str(python)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.received()[PATH_KEYS[0]], str(python))

    def test_install_drops_stale_environment_d_path_lines(self):
        # 使用者实测死循环：旧克隆路径写在 environment.d 里，删目录/重装都修不好，
        # 因为 install 会把「路径型」渠道行原样写回，而它在会话环境里压过一切。
        python = self.script("custom 中文/python", "#!/bin/sh\nexit 0\n")
        self.env_file.parent.mkdir(parents=True)
        self.env_file.write_text(
            "VOICE_IME_QWEN_ASR=1\n"
            f"VOICE_IME_QWEN_ASR_PYTHON={self.root}/deleted-old-ime/.venv-qwen-asr/bin/python\n"
            f"VOICE_IME_QWEN_ASR_MODEL_PATH={self.root}/deleted-old-ime/vendor/models/qwen3-asr/Qwen3-ASR-1.7B\n"
            f"VOICE_IME_MIMO_ASR_PYTHON={python}\n"
            "VOICE_IME_RIME_LIBRARY=/opt/rime/lib.so\n",
            encoding="utf-8")
        result = self.run_bash(self.root / "install.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        content = self.env_file.read_text(encoding="utf-8")
        self.assertNotIn("VOICE_IME_QWEN_ASR_PYTHON", content)
        self.assertNotIn("VOICE_IME_QWEN_ASR_MODEL_PATH", content)
        # 仍有效的显式自定义路径必须保留（不得误清用户救援值）。
        self.assertIn(f"VOICE_IME_MIMO_ASR_PYTHON={python}", content)
        self.assertIn("指向已消失路径的路径型渠道行", result.stdout)

    def test_helper_consumer_semantics_spaces_and_tilde_user_survive(self):
        # 与真实消费者（strip + expanduser）对齐：前后空格的有效路径、
        # 有效 ~user 路径不得误 unset；未知 ~user 保守保留（红队 P2-1）。
        import getpass
        rescue = self.script("rescue/python", "#!/bin/sh\nexit 0\n")
        user = getpass.getuser()
        # strip 语义只在 QWEN 两键：padded/tilde 用例必须用真实键名。
        strip_key = PATH_KEYS[0]
        cases = {strip_key: f"  {rescue} ",
                 # ~user 家目录必然存在（.cache 在 CI runner 等环境可能没有）。
                 "VOICE_IME_QWEN_ASR_MODEL_PATH": f"~{user}",
                 "V_MISSING": "/tmp/definitely-deleted-old/x"}
        out = self.helper(**cases)
        self.assertIn("unset V_MISSING", out)
        self.assertNotIn(f"unset {strip_key}", out)
        self.assertNotIn("unset VOICE_IME_QWEN_ASR_MODEL_PATH", out)

    def test_helper_unknown_tilde_user_and_newline_warning_are_safe(self):
        # 未知 ~user 与消费者同语义（expanduser 得字面路径、不存在 → unset）；
        # 警告值单行化，换行不得伪造日志行。
        out = self.helper(V_UNK="~no-such-user-2050/x", V_LF="/tmp/missing\n⚠ 伪造行")
        self.assertIn("unset V_UNK", out)
        self.assertIn("unset V_LF", out)
        self.assertIn("⏎", self.helper_warnings)

    def helper(self, **env):
        import subprocess as sp
        proc = sp.run(["bash", str(ROOT / "scripts/env-sanitize.sh"), *env],
                      env={**self.env, **env}, capture_output=True, text=True, timeout=10)
        self.helper_warnings = proc.stderr
        return proc.stdout


if __name__ == "__main__":
    unittest.main()
