# -*- coding: utf-8 -*-
"""Sandbox-level behavioural tests for the switch scripts in the unified JSON
config era.

switch 脚本不再把渠道行写进 environment.d：渠道与后端设置写
~/.config/ibus-voice-ime/config.json（`cfg set asr.backend …`），environment.d
的渠道行只删不写回，systemctl 用户管理器同步 unset。这里在 /tmp 沙箱里跑
脚本副本（stub 掉 systemctl/pkill/ibus，HOME 指向沙箱），断言真实落盘行为：

1. switch-siliconflow 后 config.json 的 asr.backend==siliconflow-asr、模型
   为 SenseVoiceSmall，密钥本体（sk-dummy）不出现在任何文件；
   environment.d 预置的渠道行被清除、路径行（VOICE_IME_RIME_LIBRARY）保留；
2. 接着 switch-qwen-asr 轮换回本地：asr.backend==qwen3-asr，config.json 仍
   无密钥，environment.d 仍无渠道行；
3. 五个 switch 脚本文本都含 `cfg set asr.backend` 与 systemctl unset 清理段。

绝不触碰真实 HOME / systemd 用户会话。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT / "src"))

SWITCH_SCRIPTS = (
    "switch-mimo-asr.sh",
    "switch-mimo-cloud-asr.sh",
    "switch-qwen-asr.sh",
    "switch-siliconflow-asr.sh",
    "switch-volc-bigmodel-asr.sh",
)

_STUB_TOOLS = ("systemctl", "pkill", "ibus", "ibus-daemon")


def _make_stub(bin_dir: Path, name: str) -> None:
    # 静默成功的 stub：阻断对真实 systemd/ibus 的任何影响。
    (bin_dir / name).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (bin_dir / name).chmod(0o755)


class _SwitchSandbox(unittest.TestCase):
    """Build /tmp/cfg-sandbox-* once per test class; scripts run on copies."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.sb = Path(tempfile.mkdtemp(prefix="cfg-sandbox-"))
        # 脚本副本（ROOT_DIR 将指向沙箱）
        (cls.sb / "scripts").mkdir()
        for name in ("switch-siliconflow-asr.sh", "switch-qwen-asr.sh", "ibus-restart.sh"):
            target = cls.sb / "scripts" / name
            shutil.copy2(ROOT / "scripts" / name, target)
            target.chmod(0o755)
        # stub 掉会碰真实系统的工具
        bin_dir = cls.sb / "bin"
        bin_dir.mkdir()
        for tool in _STUB_TOOLS:
            _make_stub(bin_dir, tool)
        # src/config 以符号链接复用仓库本体（config.py 经 resolve() 找到
        # 仓库内 config/defaults.json）
        (cls.sb / "src").symlink_to(ROOT / "src")
        (cls.sb / "config").symlink_to(ROOT / "config")
        # switch-qwen-asr.sh 需要的本地模型目录与 venv 占位
        for size in ("Qwen3-ASR-0.6B", "Qwen3-ASR-1.7B"):
            model_dir = cls.sb / "vendor" / "models" / "qwen3-asr" / size
            model_dir.mkdir(parents=True)
            (model_dir / "model.safetensors").touch()
        venv_py = cls.sb / ".venv-qwen-asr" / "bin" / "python"
        venv_py.parent.mkdir(parents=True)
        venv_py.write_text("#!/bin/sh\n", encoding="utf-8")
        venv_py.chmod(0o755)
        # 沙箱 HOME 与 environment.d
        cls.home = cls.sb / "home"
        (cls.home / ".config" / "environment.d").mkdir(parents=True)
        cls.env_file = cls.home / ".config" / "environment.d" / "ibus-voice-ime.conf"
        cls.config_json = cls.home / ".config" / "ibus-voice-ime" / "config.json"

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.sb, ignore_errors=True)

    def _run_script(self, script: str, *args: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("VOICE_IME_", "MIMO", "VOLC", "SILICONFLOW"))
        }
        env.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.sb / 'bin'}:{env.get('PATH', '/usr/bin:/bin')}",
                "VOICE_IME_CONFIG": str(self.config_json),
                # 避免 ibus-restart.sh 走 GNOME 的 ibus-daemon --replace 分支。
                "XDG_CURRENT_DESKTOP": "",
            }
        )
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", str(self.sb / "scripts" / script), *args],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def _seed_env_file(self) -> None:
        # 模拟老用户残留：渠道行 + 路径行混在 environment.d。
        self.env_file.write_text(
            "\n".join(
                [
                    "# ibus-voice-ime persisted environment",
                    "VOICE_IME_ASR_BACKEND=qwen3-asr",
                    "VOICE_IME_QWEN_ASR=1",
                    "VOICE_IME_SILICONFLOW_MODEL=Qwen/Qwen3-ASR-1.7B",
                    "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True",
                    "VOICE_IME_RIME_LIBRARY=/opt/rime/lib/librime.so.1",
                    "VOICE_IME_RIME_SHARED_DATA_DIR=/opt/rime/share/rime-data",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def _assert_no_channel_lines(self, where: str) -> None:
        text = self.env_file.read_text(encoding="utf-8") if self.env_file.exists() else ""
        for line in text.splitlines():
            if line.startswith("#") or "=" not in line:
                continue
            key = line.split("=", 1)[0]
            self.assertFalse(
                key.startswith(
                    (
                        "VOICE_IME_ASR_BACKEND",
                        "VOICE_IME_QWEN_ASR",
                        "VOICE_IME_MIMO",
                        "VOICE_IME_VOLC",
                        "VOICE_IME_SILICONFLOW",
                        "PYTORCH_CUDA_ALLOC_CONF",
                    )
                ),
                f"environment.d 仍残留渠道行 {key}（{where}）",
            )


class SiliconflowSwitchJsonTest(_SwitchSandbox):
    def test_10_switch_writes_json_and_cleans_env_file(self) -> None:
        self._seed_env_file()
        result = self._run_script(
            "switch-siliconflow-asr.sh",
            extra_env={"VOICE_IME_SILICONFLOW_API_KEY": "sk-dummy-000000000000"},
        )
        self.assertEqual(result.returncode, 0, f"脚本失败：\n{result.stdout}\n{result.stderr}")

        data = json.loads(self.config_json.read_text(encoding="utf-8"))
        self.assertEqual(data["asr"]["backend"], "siliconflow-asr")
        self.assertEqual(data["asr"]["siliconflow"]["model"], "FunAudioLLM/SenseVoiceSmall")
        # 密钥本体绝不落盘（config.json 全文、environment.d 全文）。
        self.assertNotIn("sk-dummy", self.config_json.read_text(encoding="utf-8"))
        self.assertNotIn("sk-dummy", self.env_file.read_text(encoding="utf-8"))
        # 密钥名进 config（BWS 运行时按名注入）。
        self.assertEqual(
            data["asr"]["siliconflow"].get("api_key_secret"),
            "SILICONFLOW_API_KEY",
        )
        # environment.d：渠道行被清除，路径行保留。
        self._assert_no_channel_lines("switch-siliconflow 后")
        remaining = self.env_file.read_text(encoding="utf-8")
        self.assertIn("VOICE_IME_RIME_LIBRARY=/opt/rime/lib/librime.so.1", remaining)
        self.assertIn("VOICE_IME_RIME_SHARED_DATA_DIR=/opt/rime/share/rime-data", remaining)


class QwenRotationJsonTest(_SwitchSandbox):
    def test_installer_activation_retains_verified_model_device_and_dtype(self) -> None:
        model = self.sb / 'custom models' / 'Qwen3-ASR-0.6B'
        model.mkdir(parents=True, exist_ok=True)
        result = self._run_script('switch-qwen-asr.sh', '0.6b', extra_env={
            'VOICE_IME_QWEN_SWITCH_MODEL_PATH': str(model),
            'VOICE_IME_QWEN_SWITCH_DEVICE_MAP': 'cuda:1',
            'VOICE_IME_QWEN_SWITCH_DTYPE': 'float16',
        })
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(self.config_json.read_text())['asr']['qwen3']
        self.assertEqual(data['model_path'], str(model))
        self.assertEqual(data['device_map'], 'cuda:1')
        self.assertEqual(data['dtype'], 'float16')

    def test_10_siliconflow_then_qwen_rotates_backend(self) -> None:
        self._seed_env_file()
        first = self._run_script(
            "switch-siliconflow-asr.sh",
            extra_env={"VOICE_IME_SILICONFLOW_API_KEY": "sk-dummy-000000000000"},
        )
        self.assertEqual(first.returncode, 0, first.stderr)

        second = self._run_script("switch-qwen-asr.sh", "1.7b")
        self.assertEqual(second.returncode, 0, f"脚本失败：\n{second.stdout}\n{second.stderr}")

        data = json.loads(self.config_json.read_text(encoding="utf-8"))
        self.assertEqual(data["asr"]["backend"], "qwen3-asr")
        self.assertEqual(
            data["asr"]["qwen3"]["model_path"],
            str(self.sb / "vendor" / "models" / "qwen3-asr" / "Qwen3-ASR-1.7B"),
        )
        # 手动 Key 早已离开进程环境；轮换后 config.json 与 environment.d 都无密钥。
        self.assertNotIn("sk-dummy", self.config_json.read_text(encoding="utf-8"))
        self._assert_no_channel_lines("switch-qwen 轮换后")
        # 路径行在第二次清理中同样保留。
        remaining = self.env_file.read_text(encoding="utf-8")
        self.assertIn("VOICE_IME_RIME_LIBRARY=/opt/rime/lib/librime.so.1", remaining)


class MigratedStateSwitchTest(_SwitchSandbox):
    """迁移态回归：qwen enabled=true 残留下 switch siliconflow 必须纠偏。

    install.sh 会把老 VOICE_IME_QWEN_ASR=1 迁成 asr.qwen3.enabled=true；
    voice.py 渠道链 qwen 排最前、先命中先赢——若 switch 只写 asr.backend，
    迁移态用户切云渠道后实际仍走 qwen（无 GPU 机器语音瘫痪）。
    """

    def test_switch_clears_migrated_qwen_flag(self) -> None:
        # 预置迁移态：environment.d 渠道行 + config.json 里的 enabled 残留。
        self._seed_env_file()
        self.config_json.parent.mkdir(parents=True, exist_ok=True)
        self.config_json.write_text(
            json.dumps({"asr": {"qwen3": {"enabled": True}}}, ensure_ascii=False),
            encoding="utf-8",
        )
        result = self._run_script(
            "switch-siliconflow-asr.sh",
            extra_env={"VOICE_IME_SILICONFLOW_API_KEY": "sk-dummy-000000000000"},
        )
        self.assertEqual(result.returncode, 0, f"脚本失败：\n{result.stdout}\n{result.stderr}")

        data = json.loads(self.config_json.read_text(encoding="utf-8"))
        self.assertEqual(data["asr"]["backend"], "siliconflow-asr")
        # 五个 enabled 叶子显式写全：目标渠道=1，其余=0（互斥语义）。
        self.assertEqual(data["asr"]["qwen3"].get("enabled"), False)
        self.assertEqual(data["asr"]["mimo"].get("enabled"), False)
        self.assertEqual(data["asr"]["mimo_cloud"].get("enabled"), False)
        self.assertEqual(data["asr"]["volc"].get("enabled"), False)
        self.assertEqual(data["asr"]["siliconflow"].get("enabled"), True)

        # 端到端：按引擎视角（env 清空 + 指向沙箱 config）验证渠道命中。
        scrubbed = {
            key: os.environ.pop(key)
            for key in list(os.environ)
            if key.startswith(("VOICE_IME_", "MIMO", "VOLC", "SILICONFLOW"))
        }
        self.addCleanup(lambda: os.environ.update(scrubbed))
        os.environ["VOICE_IME_CONFIG"] = str(self.config_json)
        from ibus_voice_ime import config  # noqa: E402
        from ibus_voice_ime.asr import qwen_asr_runtime, siliconflow_asr  # noqa: E402

        config.reload()
        self.addCleanup(config.reload)
        self.assertTrue(siliconflow_asr.selected())
        self.assertFalse(qwen_asr_runtime.selected())


class QwenActivationFailureTest(_SwitchSandbox):
    def setUp(self):
        for tool in _STUB_TOOLS:
            _make_stub(self.sb / 'bin', tool)
        self.config_json.parent.mkdir(parents=True, exist_ok=True)
        self.config_json.write_text('{"custom": "preserve", "asr": {"backend": "siliconflow-asr"}}')
        self._seed_env_file()

    def fail_ibus(self, operation):
        stub = self.sb / 'bin/ibus'
        stub.write_text(f'#!/bin/sh\nif [ "$1" = "{operation}" ]; then echo fake-{operation}-failure >&2; exit 7; fi\nexit 0\n')
        stub.chmod(0o755)

    def assert_failed_activation(self, result, stage):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('桌面激活失败', result.stderr)
        self.assertIn(stage, result.stderr)
        self.assertNotIn('已切换到', result.stdout)
        data = json.loads(self.config_json.read_text())
        self.assertEqual(data['asr']['backend'], 'qwen3-asr')
        self.assertEqual(data['custom'], 'preserve')

    def test_restart_failure_not_success(self):
        self.fail_ibus('restart')
        self.assert_failed_activation(self._run_script('switch-qwen-asr.sh', '0.6b'), '重启命令失败')

    def test_engine_selection_failure_not_success(self):
        self.fail_ibus('engine')
        self.assert_failed_activation(self._run_script('switch-qwen-asr.sh', '0.6b'), '选择 voice-custom')

    def test_missing_ibus_never_uses_real_desktop(self):
        # Isolated PATH: no fallback to a host ibus/daemon, even if installed.
        bin_dir = self.sb / 'bin'
        for name in ('bash', 'python3', 'dirname', 'mkdir', 'mktemp', 'grep', 'mv', 'chmod', 'sleep'):
            target = bin_dir / name
            if not target.exists():
                target.symlink_to(shutil.which(name))
        (bin_dir / 'ibus').unlink()
        (bin_dir / 'ibus-daemon').unlink()
        self.assert_failed_activation(self._run_script('switch-qwen-asr.sh', '0.6b', extra_env={'PATH': str(bin_dir)}), '重启命令失败')

    def test_corrupt_json_does_not_modify_config_or_legacy_env(self):
        for content in ('{bad json', 'null', '[]'):
            self.config_json.write_text(content)
            before = self.env_file.read_text()
            result = self._run_script('switch-qwen-asr.sh', '0.6b')
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.config_json.read_text(), content)
            self.assertEqual(self.env_file.read_text(), before)
            self.assertNotIn('已切换到', result.stdout)

    def test_config_write_failure_not_partial_switch(self):
        blocked = self.sb / 'blocked-config'
        blocked.write_text('not a directory')
        original = self.config_json.read_text()
        result = self._run_script('switch-qwen-asr.sh', '0.6b', extra_env={'VOICE_IME_CONFIG': str(blocked / 'config.json')})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config_json.read_text(), original)
        self.assertNotIn('已切换到', result.stdout)


class SwitchScriptTextPolicyTest(unittest.TestCase):
    def test_all_switch_scripts_use_cfg_set_and_systemctl_unset(self) -> None:
        for name in SWITCH_SCRIPTS:
            with self.subTest(script=name):
                script = (ROOT / "scripts" / name).read_text(encoding="utf-8")
                self.assertIn("config.set('asr.backend'" if name == 'switch-qwen-asr.sh' else "cfg set asr.backend", script)
                self.assertIn("systemctl --user unset-environment", script)
                # environment.d 只删不写回：不允许再向 ENV_FILE 追加任何行。
                self.assertNotIn('>> "$ENV_FILE"', script)


if __name__ == "__main__":
    unittest.main()
