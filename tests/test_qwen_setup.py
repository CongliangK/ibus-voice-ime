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
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]


class QwenSetupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='qwen-setup-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'scripts').mkdir()
        shutil.copy2(ROOT / 'scripts/setup-qwen-asr.sh', self.root / 'scripts/setup-qwen-asr.sh')
        shutil.copy2(ROOT / 'scripts/net-probe.sh', self.root / 'scripts/net-probe.sh')
        shutil.copy2(ROOT / 'scripts/env-file-load.sh', self.root / 'scripts/env-file-load.sh')
        shutil.copy2(ROOT / 'scripts/setup-qwen-python.py', self.root / 'scripts/setup-qwen-python.py')
        shutil.copy2(ROOT / 'requirements-qwen-asr.txt', self.root / 'requirements-qwen-asr.txt')
        (self.root / 'src').symlink_to(ROOT / 'src')
        self.log = self.root / 'events'
        self.target = self.root / 'target-python'
        self.home = self.root / 'home'
        self.home.mkdir()
        self.venv = self.root / '.venv-qwen-asr'
        # 代理/PIP 环境完全受控：剥掉宿主 shell 可能带有的全部代理变量与
        # PIP_PROXY（安装器行为由这些环境驱动，用例须能精确注入场景 A/B）。
        self.env = {key: value for key, value in os.environ.items()
                    if not key.lower().endswith('_proxy') and key != 'PIP_PROXY'
                    and not key.startswith(('VOICE_IME_', 'PYTHONPATH', 'XDG_'))}
        self.env.update(HOME=str(self.home), FAKE_ROOT=str(self.root),
                        VOICE_IME_QWEN_ASR_SETUP_PYTHON=str(self.target),
                        VOICE_IME_CONFIG=str(self.home / 'config.json'))
        self.make_python(self.target, 12)
        self.make_script(self.root / 'scripts/gpu-probe.sh',
                         "#!/bin/sh\nprintf 'GPU_STATE=%s\\n' \"${FAKE_GPU:-ok}\"\nprintf 'GPU_BRIEF=fake-gpu\\n'\nprintf 'GPU_REASON=fake\\nGPU_ACTION=repair\\n'\n")
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
if a[:1] == ['-'] and len(a) == 3 and a[2] == '-':
    # REUSE/增量补装判据探针（stdin 程序）：按注入退出码模拟三态
    # （0=全满足 / 3=仅缺包 / 其他=冲突），避免依赖宿主解释器真实元数据。
    sys.exit(int(os.environ.get('FAKE_REUSE_RC', '0')))
if a[:1] == ['-'] and len(a) == 4:
    # P2-2 urllib 降级探测（stdin 程序 + URL/包/版本 4 个位置参数）：测试禁止
    # 外网，缺省按注入自报分类（visible=目标版本可见）；FAKE_URL_PROBE_REAL=1
    # 时真实执行 stdin 的 urllib 探测（用例只指向 127.0.0.1 回环索引）。
    if os.environ.get('FAKE_URL_PROBE_REAL') == '1':
        sys.argv = ['-'] + a[1:]   # 对齐 `python - url pkg ver` 的 argv 语义
        exec(compile(sys.stdin.read(), '<url-probe>', 'exec'), {{'sys': sys, '__name__': '__main__'}})
        sys.exit(0)
    print('KIND=' + os.environ.get('FAKE_URL_PROBE_KIND', 'visible'))
    print('CODE=' + os.environ.get('FAKE_URL_PROBE_CODE', '200'))
    print('NOTE=' + os.environ.get('FAKE_URL_PROBE_NOTE', 'fixture fallback probe'))
    sys.exit(0)
if '-c' in a:
    code = a[a.index('-c') + 1]
    if 'json.dumps' in code:
        print(json.dumps({{'version': [3, {minor}, 0], 'base': {base!r}, 'include': '/fake/include'}}))
        sys.exit(0)
    if 'import socks' in code:
        # PySocks 存在性检查：可用注入模拟缺 PySocks；--no-index 装入后写标记模拟自举成功。
        marker = root / 'pysocks-installed'
        if os.environ.get('FAKE_VENV_NO_PYSOCKS') == '1' and not marker.exists():
            sys.exit(1)
        sys.exit(0)
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
    if rest[:1] != ['--isolated'] and '--version' in rest:
        sys.exit(0)  # venv 结构完好性探测（python -m pip --version）
    assert rest[0] == '--isolated', rest
    rest = rest[1:]
    url = rest[rest.index('--index-url') + 1] if '--index-url' in rest else ''
    rc = 0
    versions = ''
    if 'index' in rest and 'versions' in rest:
        # 索引可见性探测：按目标 URL 注入版本列表/失败（pypi.org 单独注入，
        # 供“官方源可见性”回退条件测试）。
        key = 'FAKE_PIP_INDEX_VERSIONS_PYPI' if 'pypi.org' in url else 'FAKE_PIP_INDEX_VERSIONS'
        versions = os.environ.get(key, '')
        if 'aliyun.com' in url:
            versions = '0.0.1'  # Legacy cases skip the new candidate; covered in network suite.
        if os.environ.get('FAKE_PIP_INDEX_RC') == '1' and 'pypi.org' not in url:
            print(os.environ.get('FAKE_PIP_INDEX_STDERR', 'ERROR: No matching distribution found for qwen-asr'), file=sys.stderr)
            rc = 1
    elif '-r' in rest:
        fail_url = os.environ.get('FAKE_PIP_FAIL_URL', '')
        if fail_url and fail_url in url:
            print(os.environ.get('FAKE_PIP_FAIL_STDERR', 'ERROR: Could not find a version that satisfies the requirement qwen-asr==0.0.6 (from versions: 0.0.1, 0.0.5)'), file=sys.stderr)
            rc = 1
    elif '--upgrade' in rest:
        if os.environ.get('FAKE_PIP_UPGRADE_FAIL') == '1':
            rc = 1
    with (root / 'events').open('a') as f: f.write('pip:' + ' '.join(rest) + '\\n')
    if '--no-index' in rest:
        (root / 'pysocks-installed').write_text('1')  # 模拟 PySocks wheel 装入成功
    if rc == 0 and versions and 'index' in rest:
        print('qwen-asr (' + versions.split(', ')[-1] + ')')
        print('Available versions: ' + versions)
    sys.exit(rc)
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

    def run_doctor_repair(self, **environment):
        # Source only the actual doctor definitions; no host desktop/asset checks.
        definitions = (ROOT / 'scripts/doctor.sh').read_text().split('# ------------------------------------------------------------------- 主流程 --')[0]
        self.make_script(self.root / 'scripts/doctor-definitions.sh', definitions)
        self.make_script(self.root / 'scripts/ibus-restart.sh', '#!/bin/sh\necho restart >> "$FAKE_ROOT/events"\nexit "${FAKE_RESTART_CODE:-0}"\n')
        env = dict(self.env, VOICE_IME_QWEN_ASR_PYTHON=str(self.venv / 'bin/python'))
        env.update(environment)
        return subprocess.run(['bash', '-c', 'source "$1" fix; FAILED_ITEMS=(qwen-python-headers qwen-setup); do_fix; echo "repair-failures=$FIX_FAILURES"', '--', str(self.root / 'scripts/doctor-definitions.sh')], env=env, capture_output=True, text=True, timeout=25)

    def test_doctor_header_repair_calls_new_setup_only_once(self):
        self.seed_old_venv()
        result = self.run_doctor_repair(DOCTOR_PROXY='http://proxy.invalid:1234')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.events().count('create-venv'), 1)
        self.assertEqual(self.events().count('smoke'), 1)
        self.assertNotIn('activate', self.events())  # repair leaves configuration intact
        self.assertIn('仓库内自动准备', result.stdout)
        self.assertTrue(list(self.root.glob('.venv-qwen-asr.bak-*/original-marker')))
        self.assertIn('repair-failures=0', result.stdout)

    def test_doctor_setup_failure_remembered_and_not_repeated(self):
        result = self.run_doctor_repair(FAKE_FAIL_STAGE='static')
        self.assertEqual(self.events().count('static'), 1)
        self.assertIn('repair-failures=1', result.stdout)
        self.assertNotIn('restart', self.events())

    def test_doctor_repair_without_gpu_skips_local_reinstall(self):
        # 模式感知修复：无可用 GPU 时本地 Qwen 装不出可用链路——不盲目重装
        # （安装器自身也会拒绝），只复述换云端/仅键盘出路，不计修复失败。
        result = self.run_doctor_repair(FAKE_GPU='none')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('跳过本地 Qwen 重装', result.stdout)
        self.assertIn('switch-siliconflow-asr.sh', result.stdout)
        self.assertNotIn('create-venv', self.events())
        self.assertNotIn('static', self.events())
        self.assertIn('repair-failures=0', result.stdout)

    def test_doctor_external_python_override_not_claimed_repaired(self):
        result = self.run_doctor_repair(VOICE_IME_QWEN_ASR_PYTHON='/external/python')
        self.assertIn('不能声称当前 sidecar 已修复', result.stdout)
        self.assertNotIn('restart', self.events())

    def test_full_gate_runs_before_activation(self):
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        events = self.events()
        self.assertLess(events.index('runtime'), events.index('download'))
        self.assertLess(events.index('smoke'), events.index('activate'))
        self.assertNotIn('lock-leaked', events)
        self.assertTrue((self.venv / 'installed-requirements.txt').exists())

    def test_download_budget_selected_sizes_peak_and_cache_hint(self):
        cache = self.home / '.cache/pip'
        cache.mkdir(parents=True)
        # 非空缓存只需检查目录，不递归统计体积。
        (cache / 'fixture').write_bytes(b'cached')
        result = self.run_setup(VOICE_IME_QWEN_ASR_SIZES='0.6B', VOICE_IME_QWEN_ASR_MODEL='0.6b')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('== 下载预算', result.stdout)
        self.assertIn('模型 0.6B ≈1.8GB', result.stdout)
        self.assertNotIn('模型 1.7B ≈4.4GB', result.stdout)
        self.assertIn('依赖 venv 典型 ~6GB', result.stdout)
        self.assertIn('同机重装依赖通常直接命中缓存', result.stdout)
        self.assertIn('预计磁盘峰值', result.stdout)
        self.assertIn('19.8GB', result.stdout)
        self.assertIn('中断可续装', result.stdout)
        self.assertLess(result.stdout.index('== 下载预算'), result.stdout.index('Qwen3-ASR venv Python'))

    def test_download_budget_both_sizes_and_no_cache(self):
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for line in ('模型 0.6B ≈1.8GB', '模型 1.7B ≈4.4GB', '24.2GB', 'pip 缓存未检测到'):
            self.assertIn(line, result.stdout)

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
        # 'default' 明确官方源；即使调用者已 export PIP_INDEX_URL 也不继承它。
        self.log.unlink(missing_ok=True)
        result = self.run_setup(VOICE_IME_PIP_INDEX_URL='default', PIP_INDEX_URL='https://bad.test/simple')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('https://pypi.org/simple', result.stdout)
        self.assertTrue(all('--index-url https://pypi.org/simple' in e for e in self.events() if e.startswith('pip:install')))

    def test_switch_failure_keeps_verified_env_with_guidance(self):
        # 激活（写 config.json）失败时：已验收的环境必须保留，且给出单独重试指引，
        # 而不是回滚好环境或静默退出。
        self.make_script(self.root / 'scripts/switch-qwen-asr.sh', '#!/bin/sh\nexit 1\n')
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('桌面激活失败', result.stdout + result.stderr)
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
        (self.root / '.qwen-asr-setup.state').write_text(f'backup={self.root}/.venv-qwen-asr.bak-missing\n')
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.root / '.qwen-asr-setup.state').exists())

    def test_journal_cannot_move_foreign_directory(self):
        outside = self.root / 'important-data'
        outside.mkdir()
        (outside / 'keep').write_text('user data')
        state = self.root / '.qwen-asr-setup.state'
        state.write_text(f'backup={outside}\n')
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((outside / 'keep').exists())
        self.assertTrue(state.exists())
        self.assertFalse(self.venv.exists())

    def test_journal_symlink_backup_and_destination_rejected(self):
        outside = self.root / 'external'
        outside.mkdir()
        (outside / 'keep').write_text('preserve')
        backup = self.root / '.venv-qwen-asr.bak-link'
        backup.symlink_to(outside, target_is_directory=True)
        state = self.root / '.qwen-asr-setup.state'
        state.write_text(f'backup={backup}\n')
        self.assertNotEqual(self.run_setup().returncode, 0)
        self.assertTrue((outside / 'keep').exists())
        backup.unlink()
        backup.mkdir()
        self.venv.symlink_to(self.root / 'missing', target_is_directory=True)
        self.assertNotEqual(self.run_setup().returncode, 0)
        self.assertTrue(self.venv.is_symlink())
        self.assertTrue(backup.exists())

    def test_committed_environment_clears_journal_before_activation(self):
        self.seed_old_venv()
        self.make_script(self.root / 'scripts/switch-qwen-asr.sh', '#!/bin/sh\nif [ -e "$FAKE_ROOT/.qwen-asr-setup.state" ]; then echo stale-journal >> "$FAKE_ROOT/events"; fi\nexit 1\n')
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('stale-journal', self.events())
        self.assertFalse((self.venv / 'original-marker').exists())
        self.assertTrue(list(self.root.glob('.venv-qwen-asr.bak-*/original-marker')))

    @staticmethod
    def _reuse_versions(torch='2.6.0'):
        # 模拟版本从 requirements 动态推导，防止手抄清单与 pin 漂移
        # （2026-10-11：手抄 modelscope=1.22.0 已不满足 >=1.40 而误报失败）。
        from packaging.requirements import Requirement
        versions = {}
        for raw in (ROOT / 'requirements-qwen-asr.txt').read_text().splitlines():
            line = raw.split('#', 1)[0].strip()
            if not line:
                continue
            req = Requirement(line)
            chosen = '1.0'
            for spec in req.specifier:
                if spec.operator == '==':
                    chosen = spec.version
                    break
                if spec.operator in ('>=', '~='):
                    chosen = spec.version
            versions[req.name] = chosen
        versions['torch'] = torch
        return versions

    def test_torch_baseline_checks_project_range_not_just_pip_check(self):
        from unittest import mock
        import importlib.metadata
        # Execute the installer baseline's actual Python, with synthetic metadata.
        script = (ROOT / 'scripts/setup-qwen-asr.sh').read_text()
        code = script.split('import importlib.metadata as m\n', 1)[1].split('\nPY', 1)[0]
        code = 'import importlib.metadata as m\n' + code
        for torch, expected in (('2.5.1', 1), ('2.6.0', 0), ('2.12.0', 0), ('2.13.0', 1), ('invalid', 1)):
            versions = self._reuse_versions(torch)
            with self.subTest(torch=torch), mock.patch.object(importlib.metadata, 'version', side_effect=versions.__getitem__):
                # 嵌入代码从 sys.argv[1] 读 requirements 路径；unittest discover 会
                # 把 'discover' 注入 sys.argv[1]，必须显式指回仓库 requirements 文件。
                with mock.patch('sys.argv', ['reuse-check', str(ROOT / 'requirements-qwen-asr.txt')]):
                    with self.assertRaises(SystemExit) as error:
                        exec(code, {})
                self.assertEqual(error.exception.code, expected)

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

    def test_verify_only_not_blocked_by_invalid_sizes(self):
        # 只读验收读取实际持久化的 sidecar 配置，SIZES 是下载集合变量，
        # 不应阻断 --verify-only（红队 2026-10-11 发现）。
        model = self.root / 'custom models/Qwen3-ASR-0.6B'
        model.mkdir(parents=True)
        (model / 'config.json').write_text('{}')
        (model / 'model.safetensors').write_bytes(b'fixture')
        interpreter = self.root / 'custom python/bin/python'
        self.make_python(interpreter, 12)
        env_file = self.home / '.config/environment.d/ibus-voice-ime.conf'
        env_file.parent.mkdir(parents=True)
        env_file.write_text(f'VOICE_IME_QWEN_ASR_PYTHON={interpreter}\nVOICE_IME_QWEN_ASR_MODEL_PATH={model}\n')
        result = self.run_setup('--verify-only', VOICE_IME_QWEN_ASR_SIZES='bogus')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.events(), ['smoke'])

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

    # ------------------------------------------------------------------
    # 2026-10-14 行为变更配套：venv 增量补装梯度 / 受控回退 / 代理归一与 SOCKS 自举
    # ------------------------------------------------------------------
    def seed_models(self):
        for size in ('0.6B', '1.7B'):
            model = self.root / 'vendor/models/qwen3-asr' / f'Qwen3-ASR-{size}'
            model.mkdir(parents=True)
            (model / 'config.json').write_text('{}')
            (model / 'model.safetensors').write_bytes(b'fixture')

    def test_reuse_baseline_missing_packages_requests_increment(self):
        # 判据三态：仅 PackageNotFoundError（已装包全满足约束）→ 退出码 3 = 增量补装，
        # 不再触发整体重建。
        from unittest import mock
        import importlib.metadata
        script = (ROOT / 'scripts/setup-qwen-asr.sh').read_text()
        code = script.split('import importlib.metadata as m\n', 1)[1].split('\nPY', 1)[0]
        code = 'import importlib.metadata as m\n' + code
        with mock.patch.object(importlib.metadata, 'version',
                               side_effect=importlib.metadata.PackageNotFoundError):
            with mock.patch('sys.argv', ['reuse-check', str(ROOT / 'requirements-qwen-asr.txt')]):
                with self.assertRaises(SystemExit) as error:
                    exec(code, {})
        self.assertEqual(error.exception.code, 3)

    def test_missing_packages_only_incremental_install_no_rebuild(self):
        # 上次安装失败留下的 venv 只缺 qwen-asr：直接原 venv 补装，不整体重建
        # （避免一次缺包就重装 ~6GB 依赖），venv 内既有文件保持原样。
        self.seed_old_venv(minor=12)
        self.make_script(self.venv / 'bin/modelscope', '#!/bin/sh\nexit 1\n')
        self.seed_models()
        result = self.run_setup(FAKE_REUSE_RC='3')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('增量补装', result.stdout)
        self.assertTrue(any(e.startswith('pip:install -r') for e in self.events()))
        self.assertNotIn('create-venv', self.events())
        self.assertTrue((self.venv / 'original-marker').exists())
        self.assertFalse(list(self.root.glob('.venv-qwen-asr.bak-*')))
        self.assertFalse(list(self.root.glob('.venv-qwen-asr.failed-*')))

    def test_incremental_failure_preserves_venv_for_rerun(self):
        # 增量补装失败（如网络未恢复）：保留现状退出，不移动/不重建 venv；
        # 网络恢复后重跑同一命令即可续装。
        self.seed_old_venv(minor=12)
        self.make_script(self.venv / 'bin/modelscope', '#!/bin/sh\nexit 1\n')
        self.seed_models()
        result = self.run_setup(FAKE_REUSE_RC='3', FAKE_PIP_FAIL_URL='tuna',
                                VOICE_IME_PIP_INDEX_FALLBACK='off',
                                FAKE_PIP_FAIL_STDERR='ERROR: Could not fetch URL http://127.0.0.1:1/simple/qwen-asr/: connection error')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.venv / 'original-marker').exists())
        self.assertFalse(list(self.root.glob('.venv-qwen-asr.bak-*')))
        self.assertFalse(list(self.root.glob('.venv-qwen-asr.failed-*')))
        combined = result.stdout + result.stderr
        self.assertIn('依赖安装失败', combined)
        # 连接类失败应被 net-probe 归类并打印（stage=deps）
        self.assertIn('分类：网络连接类', combined)

    def test_mirror_lag_falls_back_to_official_once(self):
        # 回退三条件齐备（默认索引 + 版本不可用签名 + 官方源实证可见）→ 受控回退
        # 官方源重试一次；不放松版本约束、只重试一次。
        result = self.run_setup(
            FAKE_PIP_FAIL_URL='tuna',
            FAKE_PIP_FAIL_STDERR='ERROR: Could not find a version that satisfies the requirement qwen-asr==0.0.6 (from versions: 0.0.1, 0.0.5)',
            FAKE_PIP_INDEX_VERSIONS='0.0.1, 0.0.5',
            FAKE_PIP_INDEX_VERSIONS_PYPI='0.0.1, 0.0.6')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        installs = [e for e in self.events() if e.startswith('pip:install -r')]
        self.assertEqual(len(installs), 2, installs)
        self.assertIn('pypi.tuna.tsinghua.edu.cn', installs[0])
        self.assertIn('pypi.org/simple', installs[1])
        combined = result.stdout + result.stderr
        self.assertIn('受控回退', combined)
        self.assertIn('重试一次', combined)
        self.assertIn('换源重试成功', combined)

    def test_explicit_index_never_auto_switches(self):
        # 用户显式设置 VOICE_IME_PIP_INDEX_URL：即使满足其余回退条件也绝不静默
        # 切换，只打印建议命令，且失败只装一次。
        result = self.run_setup(
            VOICE_IME_PIP_INDEX_URL='https://mirrors.example.test/pypi/simple',
            FAKE_PIP_FAIL_URL='mirrors.example.test',
            FAKE_PIP_FAIL_STDERR='ERROR: Could not find a version that satisfies the requirement qwen-asr==0.0.6 (from versions: 0.0.1, 0.0.5)',
            FAKE_PIP_INDEX_VERSIONS='0.0.1, 0.0.5',
            FAKE_PIP_INDEX_VERSIONS_PYPI='0.0.6')
        self.assertNotEqual(result.returncode, 0)
        installs = [e for e in self.events() if e.startswith('pip:install -r')]
        self.assertEqual(len(installs), 1, installs)
        combined = result.stdout + result.stderr
        self.assertIn('绝不静默切换', combined)
        self.assertIn('VOICE_IME_PIP_INDEX_URL=default', combined)

    def test_fallback_disabled_by_env(self):
        result = self.run_setup(
            VOICE_IME_PIP_INDEX_FALLBACK='off',
            FAKE_PIP_FAIL_URL='tuna',
            FAKE_PIP_FAIL_STDERR='ERROR: Could not find a version that satisfies the requirement qwen-asr==0.0.6 (from versions: 0.0.1, 0.0.5)',
            FAKE_PIP_INDEX_VERSIONS='0.0.1, 0.0.5',
            FAKE_PIP_INDEX_VERSIONS_PYPI='0.0.6')
        self.assertNotEqual(result.returncode, 0)
        installs = [e for e in self.events() if e.startswith('pip:install -r')]
        self.assertEqual(len(installs), 1, installs)
        combined = result.stdout + result.stderr
        self.assertIn('VOICE_IME_PIP_INDEX_FALLBACK', combined)
        self.assertIn('禁用', combined)

    def test_socks_all_proxy_missing_pysocks_advice_without_deadloop(self):
        # 故障 B 复现：仅残留 ALL_PROXY=socks5://（http/https 四变量为空）。
        # 预检必须看见它、给出诊断与自举尝试；自举失败（代理端口死）给最小修复
        # 建议，不阻断（pip 退出码为准）、不死循环、不静默。
        result = self.run_setup(ALL_PROXY='socks5://127.0.0.1:1', FAKE_VENV_NO_PYSOCKS='1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        combined = result.stdout + result.stderr
        self.assertIn('缺 PySocks', combined)
        self.assertIn('受控自举', combined)
        self.assertIn('改用 HTTP 代理', combined)
        self.assertIn('unset ALL_PROXY', combined)
        self.assertIn('all_proxy', combined)  # 8 变量枚举里如实展示来源
        # 自举失败后不反复重试
        self.assertEqual(len([e for e in self.events() if '--no-index' in e]), 0)

    def test_socks_bootstrap_disabled_by_env(self):
        result = self.run_setup(ALL_PROXY='socks5://127.0.0.1:1', FAKE_VENV_NO_PYSOCKS='1',
                                VOICE_IME_PIP_SOCKS_BOOTSTRAP='0')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        combined = result.stdout + result.stderr
        self.assertIn('VOICE_IME_PIP_SOCKS_BOOTSTRAP=0 关闭', combined)
        self.assertIn('改用 HTTP 代理', combined)
        self.assertEqual(len([e for e in self.events() if '--no-index' in e]), 0)

    def test_proxy_report_redacts_credentials_and_names_source(self):
        result = self.run_setup(https_proxy='http://user:secret@127.0.0.1:3128')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        combined = result.stdout + result.stderr
        self.assertIn('***@127.0.0.1:3128', combined)
        self.assertNotIn('secret', combined)
        self.assertIn('来源 $https_proxy', combined)
        # TCP 预检（扩展到有效代理）对无监听端口给出警告但不阻断
        self.assertIn('无法连接该代理', combined)

    def test_archive_rotation_keeps_only_newest_and_prints_paths(self):
        # 留档轮转：failed / failed-crash / bak 三类各只保留最新 1 份，清理时打印路径。
        for ts in ('20260101-000000-1', '20260102-000000-2'):
            for kind in ('.venv-qwen-asr.failed-', '.venv-qwen-asr.failed-crash-', '.venv-qwen-asr.bak-'):
                (self.root / (kind + ts)).mkdir()
        result = self.run_setup()   # 成功路径也会在启动时轮转（journal 消费之后）
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for pattern in ('.venv-qwen-asr.failed-[0-9]*', '.venv-qwen-asr.failed-crash-*', '.venv-qwen-asr.bak-*'):
            remain = sorted(p.name for p in self.root.glob(pattern))
            self.assertEqual(len(remain), 1, (pattern, remain))
            self.assertIn('20260102-000000-2', remain[0])
        # 清理动作可见（打印被删路径）
        self.assertEqual(result.stdout.count('清理旧留档'), 3)
        self.assertIn('20260101-000000-1', result.stdout)


class NetProbeTest(unittest.TestCase):
    """net-probe.sh 分类诊断（本地回环 fake 索引 / mock 代理，零外网流量）。

    判定依据 = 根因调查报告 §6 签名表：`from versions: none` 与“包不存在/代理
    内容劫持”逐字同形，analyze 模式必须主动探测（直连 vs 经代理）区分并标注
    确定性；all_proxy 有值时绝不断言 pip 直连。
    """

    @staticmethod
    def start_server(handler):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server

    @staticmethod
    def make_index_handler(page_200):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/simple/qwen-asr/' and page_200 is not None:
                    body = page_200.encode()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_error(404, 'not found')

            def log_message(self, *args):
                pass
        return Handler

    @staticmethod
    def make_mock_proxy(status, body=b''):
        # 内容级劫持形态：对代理收到的任何请求都回固定响应（403/垃圾 200/空 200）。
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(status)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            do_HEAD = do_GET

            def log_message(self, *args):
                pass
        return Handler

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='net-probe-test-')
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name) / 'home'
        home.mkdir()
        self.home = home

    def probe_env(self, **overrides):
        # 代理/PIP 环境完全受控：先剥掉宿主可能带有的全部代理变量，再按用例注入。
        env = {key: value for key, value in os.environ.items()
               if not key.lower().endswith('_proxy') and key != 'PIP_PROXY'
               and not key.startswith(('VOICE_IME_', 'XDG_', 'PYTHONPATH'))}
        env['HOME'] = str(self.home)
        env.update(overrides)
        return env

    def run_probe(self, *args, **env_overrides):
        return subprocess.run(['bash', str(ROOT / 'scripts/net-probe.sh'), *args],
                              env=self.probe_env(**env_overrides), capture_output=True,
                              text=True, timeout=90)

    def write_stderr(self, text):
        path = Path(self.tmp.name) / 'stderr.txt'
        path.write_text(text)
        return str(path)

    def test_socks_missing_classification_with_all_proxy(self):
        # 故障 B：只 unset 了 http/https 四变量的 shell 里仍残留 ALL_PROXY=socks5://。
        err = self.write_stderr(
            'ERROR: Could not install packages due to an OSError: Missing dependencies for SOCKS support.\n'
            'WARNING: There was an error checking the latest version of pip.\n')
        result = self.run_probe('--analyze', err, '--index-url', 'https://pypi.tuna.tsinghua.edu.cn/simple',
                                '--stage', 'deps', ALL_PROXY='socks5://127.0.0.1:7897')
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn('▶ 分类：SOCKS 支持缺失', out)
        self.assertIn('实证', out)
        self.assertIn('ALL_PROXY', out)
        self.assertIn('unset ALL_PROXY', out)
        self.assertIn('只 unset http/https 四个不够', out)

    def test_all_proxy_reported_as_effective_proxy_never_claimed_direct(self):
        # 8 变量报告：all_proxy 有值时如实报告有效代理，不得输出“pip 直连”假阴性。
        err = self.write_stderr('ERROR: Could not install packages due to an OSError: Missing dependencies for SOCKS support.\n')
        result = self.run_probe('--analyze', err, '--index-url', 'https://pypi.tuna.tsinghua.edu.cn/simple',
                                all_proxy='socks5://127.0.0.1:7890')
        out = result.stdout
        self.assertIn('来源 $all_proxy', out)
        self.assertIn('协议 socks5', out)
        self.assertNotIn('pip 将直连', out)

    def test_mirror_lag_signature_classified_without_network_probe(self):
        # stderr 列出旧版本 = 真·镜像滞后专属签名，直接实证归类，无需探测。
        err = self.write_stderr(
            'ERROR: Could not find a version that satisfies the requirement qwen-asr==0.0.6 '
            '(from versions: 0.0.1, 0.0.2, 0.0.5)\n'
            'ERROR: No matching distribution found for qwen-asr==0.0.6\n')
        result = self.run_probe('--analyze', err, '--index-url', 'https://pypi.tuna.tsinghua.edu.cn/simple',
                                '--stage', 'deps')
        out = result.stdout
        self.assertIn('▶ 分类：包或版本不可用', out)
        self.assertIn('镜像滞后', out)
        self.assertIn('实证', out)

    def test_versions_none_local_404_probed_as_package_missing(self):
        # versions:none + 探测 404 → 包不存在（实证）；不再断言“索引可达/版本缺失”。
        server = self.start_server(self.make_index_handler(None))
        self.addCleanup(server.shutdown)
        err = self.write_stderr(
            'ERROR: Could not find a version that satisfies the requirement qwen-asr==0.0.6 (from versions: none)\n'
            'ERROR: No matching distribution found for qwen-asr==0.0.6\n')
        result = self.run_probe('--analyze', err,
                                '--index-url', f'http://127.0.0.1:{server.server_port}/simple',
                                '--package', 'qwen-asr', '--version', '0.0.6', '--stage', 'deps')
        out = result.stdout
        self.assertIn('▶ 分类：包或版本不可用', out)
        self.assertIn('包不存在', out)
        self.assertIn('实证', out)

    def test_versions_none_mock_403_reports_path_difference(self):
        # 直连可见、代理路径 403 只能证明路径响应不同，不能证明劫持。
        index = self.start_server(self.make_index_handler(
            '<a href="../../packages/qwen_asr-0.0.6-py3-none-any.whl#sha256=abc">qwen_asr-0.0.6-py3-none-any.whl</a>'))
        self.addCleanup(index.shutdown)
        proxy = self.start_server(self.make_mock_proxy(403, b'Forbidden'))
        self.addCleanup(proxy.shutdown)
        err = self.write_stderr(
            'ERROR: Could not find a version that satisfies the requirement qwen-asr==0.0.6 (from versions: none)\n'
            'ERROR: No matching distribution found for qwen-asr==0.0.6\n')
        result = self.run_probe('--analyze', err,
                                '--index-url', f'http://127.0.0.1:{index.server_port}/simple',
                                '--package', 'qwen-asr', '--version', '0.0.6', '--stage', 'deps',
                                http_proxy=f'http://127.0.0.1:{proxy.server_port}')
        out = result.stdout
        self.assertIn('▶ 分类：索引访问异常', out)
        self.assertIn('不能据此证明内容劫持', out)
        self.assertIn('实证', out)
        # 验证命令必须同时给直连与经代理两条对比命令
        self.assertIn("--noproxy '*'", out)
        self.assertIn('-x "$http_proxy"', out)
        self.assertIn('其他包源可能可用', out)

    def test_credentials_redacted_in_env_report(self):
        err = self.write_stderr('WARNING: Retrying (1/2) ... ProxyError(\'Cannot connect to proxy.\')\n')
        result = self.run_probe('--analyze', err, '--index-url', 'http://127.0.0.1:9/simple',
                                https_proxy='http://user:secret@127.0.0.1:3128')
        out = result.stdout
        self.assertIn('***@127.0.0.1:3128', out)
        self.assertNotIn('secret', out)

    def test_standalone_resolves_pinned_version_from_requirements(self):
        # 独立模式缺省从 requirements-qwen-asr.txt 解析 qwen-asr 钉定版本（不硬编码）。
        index = self.start_server(self.make_index_handler(
            '<a href="../../packages/qwen_asr-0.0.5-py3-none-any.whl#sha256=abc">qwen_asr-0.0.5-py3-none-any.whl</a>'))
        self.addCleanup(index.shutdown)
        result = self.run_probe(VOICE_IME_PIP_INDEX_URL=f'http://127.0.0.1:{index.server_port}/simple')
        out = result.stdout
        self.assertIn('0.0.6', out)          # 目标版本来自 requirements 解析（非硬编码）
        self.assertIn('镜像同步滞后', out)
        self.assertIn('--noproxy', out)

    def test_six_elements_present_in_analysis(self):
        err = self.write_stderr('ERROR: ResolutionImpossible: for help visit https://pip.pypa.io/errors\n')
        result = self.run_probe('--analyze', err, '--index-url', 'https://pypi.tuna.tsinghua.edu.cn/simple',
                                '--stage', 'selfcheck')
        out = result.stdout
        for element in ('[1] 失败阶段', 'pip 自升级（selfcheck）', '[2] 实际索引',
                        '[3] 代理环境', '[4] pip 关键错误', '▶ 分类：', '[6]'):
            self.assertIn(element, out)
        self.assertIn('▶ 分类：依赖解析类', out)


if __name__ == '__main__':
    unittest.main()
