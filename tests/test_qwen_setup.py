"""Run the real installer in isolated copies with fake GPU/network/interpreters.

No sudo, network, real HOME, systemd or GPU work is allowed in these tests.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class QwenSetupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='qwen-setup-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'scripts').mkdir()
        shutil.copy2(ROOT / 'scripts/setup-qwen-asr.sh', self.root / 'scripts/setup-qwen-asr.sh')
        shutil.copy2(ROOT / 'scripts/env-file-load.sh', self.root / 'scripts/env-file-load.sh')
        shutil.copy2(ROOT / 'requirements-qwen-asr.txt', self.root / 'requirements-qwen-asr.txt')
        (self.root / 'src').symlink_to(ROOT / 'src')
        self.log = self.root / 'events'
        self.target = self.root / 'target-python'
        self.home = self.root / 'home'
        self.home.mkdir()
        self.venv = self.root / '.venv-qwen-asr'
        self.env = {key: value for key, value in os.environ.items() if not key.startswith(('VOICE_IME_', 'PYTHONPATH'))}
        self.env.update(HOME=str(self.home), FAKE_ROOT=str(self.root),
                        VOICE_IME_QWEN_ASR_SETUP_PYTHON=str(self.target),
                        VOICE_IME_CONFIG=str(self.home / 'config.json'))
        self.make_python(self.target, 12)
        self.make_script(self.root / 'scripts/gpu-probe.sh',
                         "#!/bin/sh\nprintf 'GPU_STATE=%s\\n' \"${FAKE_GPU:-ok}\"\nprintf 'GPU_REASON=fake\\nGPU_ACTION=repair\\n'\n")
        self.make_script(self.root / 'scripts/switch-qwen-asr.sh',
                         "#!/bin/sh\nif [ -e /proc/$$/fd/9 ]; then echo lock-leaked >> \"$FAKE_ROOT/events\"; fi\necho activate >> \"$FAKE_ROOT/events\"\n")
        self.make_script(self.root / 'scripts/qwen-preflight.py', f'''#!{sys.executable}
import json, os, pathlib, sys
root = pathlib.Path(os.environ['FAKE_ROOT'])
args = sys.argv[1:]
if '--select-python' in args:
    print(root / 'target-python'); sys.exit(0)
stage = args[args.index('--stage') + 1] if '--stage' in args else 'model'
with (root / 'events').open('a') as stream: stream.write(stage + '\\n')
if stage == os.environ.get('FAKE_FAIL_STAGE'):
    print('fatal error: Python.h: No such file or directory', file=sys.stderr); sys.exit(1)
if '--model' in args:
    model = pathlib.Path(args[args.index('--model') + 1])
    if not (model / 'config.json').exists() or not (model / 'model.safetensors').exists(): sys.exit(1)
''')

    def make_script(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(0o755)

    def make_python(self, path, minor):
        base = str(self.target) if minor == 12 else str(self.root / 'old-python')
        self.make_script(path, f'''#!{sys.executable}
import json, os, pathlib, shutil, sys
root = pathlib.Path(os.environ['FAKE_ROOT'])
a = sys.argv[1:]
if '-c' in a:
    code = a[a.index('-c') + 1]
    if 'json.dumps' in code:
        print(json.dumps({{'version': [3, {minor}, 0], 'base': {base!r}, 'include': '/fake/include'}}))
    sys.exit(0)
if a[:2] == ['-m', 'venv']:
    target = pathlib.Path(a[2]); (target / 'bin').mkdir(parents=True)
    shutil.copy2(__file__, target / 'bin/python')
    (target / 'bin/pip').write_text('#!/bin/sh\\nexit 0\\n'); (target / 'bin/pip').chmod(0o755)
    cli = target / 'bin/modelscope'
    cli.write_text('#!{sys.executable}\\nimport json,pathlib,sys,os\\na=sys.argv\\np=pathlib.Path(a[a.index("--local_dir")+1]);p.mkdir(parents=True,exist_ok=True)\\n(p/"config.json").write_text("{{}}");(p/"model.safetensors").write_bytes(b"fixture")\\nwith (pathlib.Path(os.environ["FAKE_ROOT"])/"events").open("a") as f:f.write("download\\\\n")\\n')
    cli.chmod(0o755)
    with (root / 'events').open('a') as f: f.write('create-venv\\n')
    sys.exit(0)
if a[:2] == ['-m', 'pip']:
    rest = a[2:]
    if os.environ.get('FAKE_PIP_UPGRADE_FAIL') == '1' and '--upgrade' in rest and '-r' not in rest:
        sys.exit(1)
    with (root / 'events').open('a') as f: f.write('pip:' + ' '.join(a[2:]) + '\\n')
    sys.exit(0)
if a == ['-V']: print('Python 3.{minor}.0')
# stdin metadata baseline probe: succeed without importing real packages.
''')

    def seed_old_venv(self, minor=14):
        self.make_python(self.venv / 'bin/python', minor)
        self.make_script(self.venv / 'bin/pip', '#!/bin/sh\nexit 0\n')
        (self.venv / 'original-marker').write_text('preserve me')

    def run_setup(self, *arguments, **environment):
        env = dict(self.env, **environment)
        return subprocess.run(['bash', str(self.root / 'scripts/setup-qwen-asr.sh'), *arguments],
                              env=env, capture_output=True, text=True, timeout=20)

    def events(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_full_gate_runs_before_activation(self):
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        events = self.events()
        self.assertLess(events.index('runtime'), events.index('download'))
        self.assertLess(events.index('smoke'), events.index('activate'))
        self.assertNotIn('lock-leaked', events)
        self.assertTrue((self.venv / 'installed-requirements.txt').exists())

    def test_header_failure_preserves_old_environment(self):
        self.seed_old_venv()
        result = self.run_setup(FAKE_FAIL_STAGE='static')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.venv / 'original-marker').exists())
        self.assertNotIn('create-venv', self.events())
        self.assertNotIn('activate', self.events())

    def test_runtime_failure_rolls_back_rebuilt_environment(self):
        self.seed_old_venv()
        result = self.run_setup(FAKE_FAIL_STAGE='runtime')
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.venv / 'original-marker').exists())
        self.assertTrue(list(self.root.glob('.venv-qwen-asr.failed-*')))
        self.assertNotIn('download', self.events())
        self.assertNotIn('activate', self.events())

    def test_smoke_failure_never_enables_backend(self):
        result = self.run_setup(FAKE_FAIL_STAGE='smoke')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('activate', self.events())
        self.assertNotIn('配置完成', result.stdout)

    def test_pip_self_upgrade_failure_is_tolerated(self):
        # 网络抖动让 `pip install --upgrade pip` 失败时不应静默终止整个安装：
        # 旧 pip 仍可完成依赖安装，真正的失败由 -r 安装步骤报告。
        result = self.run_setup(FAKE_PIP_UPGRADE_FAIL='1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(any(e.startswith('pip:install -r') for e in self.events()))
        self.assertIn('继续使用现有 pip', result.stderr)

    def test_pip_mirror_default_tuna_overridable_and_disableable(self):
        # 默认走清华镜像加速 torch 轮子下载；pip 命令必须携带 --index-url。
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('pypi.tuna.tsinghua.edu.cn', result.stdout)
        self.assertTrue(all('--index-url' in e for e in self.events() if e.startswith('pip:install')))
        # 显式 override 换源（events 文件在多次 run_setup 间累积，需先清空）
        self.log.unlink(missing_ok=True)
        result = self.run_setup(VOICE_IME_PIP_INDEX_URL='https://mirrors.aliyun.com/pypi/simple')
        self.assertIn('mirrors.aliyun.com', result.stdout)
        # 'default' 禁用镜像：命令行不携带 --index-url
        self.log.unlink(missing_ok=True)
        result = self.run_setup(VOICE_IME_PIP_INDEX_URL='default')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(all('--index-url' not in e for e in self.events() if e.startswith('pip:install')))

    def test_switch_failure_keeps_verified_env_with_guidance(self):
        # 激活（写 config.json）失败时：已验收的环境必须保留，且给出单独重试指引，
        # 而不是回滚好环境或静默退出。
        self.make_script(self.root / 'scripts/switch-qwen-asr.sh', '#!/bin/sh\nexit 1\n')
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('启用后端失败', result.stdout + result.stderr)
        self.assertIn('switch-qwen-asr.sh', result.stdout + result.stderr)
        self.assertTrue((self.venv / 'installed-requirements.txt').exists())
        self.assertFalse(list(self.root.glob('.venv-qwen-asr.failed-*')))

    def test_journal_recovers_crash_orphaned_backup(self):
        # SIGKILL/断电后：原 venv 停在 .bak、半成品占据 venv 位、journal 指向备份。
        # 下次安装必须先恢复原环境再继续，而不是把半成品当作可复用环境。
        import shutil as shutil_module

        self.seed_old_venv()
        bak = self.root / '.venv-qwen-asr.bak-crash'
        shutil_module.move(str(self.venv), str(bak))
        self.venv.mkdir()
        (self.venv / 'half-built').write_text('x')
        (self.root / '.qwen-asr-setup.state').write_text(f'backup={bak}\n')
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        combined = result.stdout + result.stderr
        self.assertIn('已恢复上次中断前的原环境', combined)
        self.assertFalse((self.root / '.qwen-asr-setup.state').exists())
        self.assertTrue(list(self.root.glob('.venv-qwen-asr.failed-crash-*')))
        self.assertTrue(list(self.root.glob('.venv-qwen-asr.bak-*/original-marker')))

    def test_stale_journal_is_ignored_and_cleaned(self):
        # journal 指向的备份已被用户删除：忽略而不是阻塞，且清理状态文件。
        (self.root / '.qwen-asr-setup.state').write_text('backup=/nonexistent/bak\n')
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.root / '.qwen-asr-setup.state').exists())

    def test_no_gpu_is_failure_not_fake_success(self):
        result = self.run_setup(FAKE_GPU='none')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('create-venv', self.events())

    def test_force_without_gpu_only_prepares_never_enables(self):
        result = self.run_setup('--force', FAKE_GPU='none')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('runtime', self.events())
        self.assertNotIn('smoke', self.events())
        self.assertNotIn('activate', self.events())
        self.assertIn('未启用', result.stdout)

    def test_matching_environment_is_not_upgraded(self):
        self.seed_old_venv(minor=12)
        # Supply modelscope as if installed in the existing matched environment.
        self.make_script(self.venv / 'bin/modelscope', '#!/bin/sh\nexit 1\n')
        for size in ('0.6B', '1.7B'):
            model = self.root / 'vendor/models/qwen3-asr' / f'Qwen3-ASR-{size}'
            model.mkdir(parents=True)
            (model / 'config.json').write_text('{}')
            (model / 'model.safetensors').write_bytes(b'fixture')
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('create-venv', self.events())
        self.assertFalse(any(e.startswith('pip:install') for e in self.events()))
        self.assertIn('smoke', self.events())

    def test_verify_only_uses_persisted_paths_with_spaces_without_installing(self):
        model = self.root / 'custom models/Qwen3-ASR-0.6B'
        model.mkdir(parents=True)
        (model / 'config.json').write_text('{}')
        (model / 'model.safetensors').write_bytes(b'fixture')
        interpreter = self.root / 'custom python/bin/python'
        self.make_python(interpreter, 12)
        env_file = self.home / '.config/environment.d/ibus-voice-ime.conf'
        env_file.parent.mkdir(parents=True)
        env_file.write_text(f'VOICE_IME_QWEN_ASR_PYTHON={interpreter}\nVOICE_IME_QWEN_ASR_MODEL_PATH={model}\nROOT_DIR=/wrong/project\n')
        result = self.run_setup('--verify-only')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.events(), ['smoke'])
        self.assertFalse(self.venv.exists())
        self.assertNotIn('activate', self.events())

    def test_lock_rejects_concurrent_install(self):
        with (self.root / '.qwen-asr-setup.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('另一个 Qwen', result.stderr)
        self.assertNotIn('create-venv', self.events())

    def test_symlink_environment_is_never_moved(self):
        outside = self.root / 'other-project'
        outside.mkdir()
        self.venv.symlink_to(outside, target_is_directory=True)
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.venv.is_symlink())
        self.assertTrue(outside.exists())


if __name__ == '__main__':
    unittest.main()
