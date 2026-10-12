"""安装输出隔离集成测试：真实四脚本，下载、GPU 与桌面调用全部 fake。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ScriptFixture(unittest.TestCase):
    prefix = 'ibus-voice-ime-output-'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix=self.prefix)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        (self.root / 'scripts').mkdir()
        self.log = self.root / 'events.jsonl'
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(('VOICE_IME_', 'XDG_', 'STUB_', 'PYTHONPATH'))}
        self.env.update(HOME=str(self.home), XDG_CONFIG_HOME=str(self.home / '.config'),
                        STUB_ROOT=str(self.root), PATH=str(self.bin) + ':' + self.env['PATH'])
        (self.root / 'src').symlink_to(ROOT / 'src')
        for name in ('init.sh', 'setup.sh', 'scripts/doctor.sh', 'scripts/check-environment.sh'):
            shutil.copy2(ROOT / name, self.root / name)

    def script(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'#!{sys.executable}\n' + content, encoding='utf-8')
        path.chmod(0o755)

    def file(self, relative, text=''):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def run_script(self, name, *args, **environment):
        return subprocess.run(['bash', str(self.root / name), *args], cwd=self.root,
                              env=dict(self.env, **environment), stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=25)

    def events(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]


class InitOutputClarityTest(ScriptFixture):
    # 确认输出的精确命令支持带空格的克隆路径。
    prefix = 'install clarity '

    def setUp(self):
        super().setUp()
        self.script('scripts/check-environment.sh', 'print("  FAIL: 0  WARN: 0")\n')
        self.script('scripts/doctor.sh', 'print("diagnostic doctor")\n')
        self.script('scripts/gpu-probe.sh', '''
import os, shlex
print('GPU_STATE=' + shlex.quote(os.environ.get('STUB_GPU', 'ok')))
print('GPU_REASON=fixture GPU_ACTION=fixture GPU_BRIEF=fixture')
''')
        for name in ('scripts/setup-rime-ice.sh', 'scripts/fetch-rnnoise-model.sh',
                     'scripts/setup-qwen-asr.sh', 'install.sh'):
            self.script(name, '''
import json, os, pathlib, sys
name = pathlib.Path(__file__).name
root = pathlib.Path(os.environ['STUB_ROOT'])
with (root / 'events.jsonl').open('a') as stream:
    stream.write(json.dumps({'name': name, 'args': sys.argv[1:],
                            'sizes': os.environ.get('VOICE_IME_QWEN_ASR_SIZES'),
                            'model': os.environ.get('VOICE_IME_QWEN_ASR_MODEL'),
                            'enable': os.environ.get('VOICE_IME_ENABLE_QWEN_ASR')}) +'\\n')
keys = {'setup-rime-ice.sh': 'STUB_RIME_RC', 'fetch-rnnoise-model.sh': 'STUB_RNNOISE_RC',
        'setup-qwen-asr.sh': 'STUB_QWEN_RC', 'install.sh': 'STUB_INSTALL_RC'}
sys.exit(int(os.environ.get(keys[name], '0')))
''')

    @staticmethod
    def records(output):
        return [line.split('\t') for line in output.splitlines()
                if line.startswith('VOICE_IME_INIT_RESULT\t')]

    def test_degraded_only_exits_zero_with_impact_and_exact_retry_commands(self):
        proxy = 'http://proxy.invalid:7890/a $b`c"d\\e'
        result = self.run_script('init.sh', '--no-voice', '--skip-install', '--no-zhwiki',
                                 '--no-moegirl', '--proxy', proxy,
                                 STUB_RIME_RC='1', STUB_RNNOISE_RC='2')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('⚠ 降级项', result.stdout)
        self.assertIn('luna_pinyin_simp', result.stdout)
        self.assertIn('notch', result.stdout)
        self.assertIn('degraded: 2  blocked: 0', result.stdout)
        self.assertIn('EXIT: 0', result.stdout)
        records = self.records(result.stdout)
        self.assertEqual([record[1] for record in records], ['degraded', 'degraded'])
        for record in records:
            self.assertTrue(record[4].startswith("'" + str(self.root)))
            # 用真实 shell 执行一次输出命令，验证转义而非仅用 shlex 检查表面语法。
            rerun = subprocess.run(['bash', '-c', record[4]], cwd=self.root,
                                   env=self.env, capture_output=True, text=True, timeout=10)
            self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
        self.assertEqual(self.events()[-2]['args'], ['--proxy', proxy, '--no-zhwiki', '--no-moegirl'])
        self.assertEqual(self.events()[-1]['args'], ['--proxy', proxy])

    def test_local_voice_blocked_exits_one_and_keeps_model_selection_in_retry(self):
        result = self.run_script('init.sh', '--no-rime-ice', '--no-denoise', '--skip-install',
                                 '--proxy', 'http://proxy.invalid:7890', STUB_QWEN_RC='9',
                                 VOICE_IME_QWEN_ASR_SIZES='0.6B', VOICE_IME_QWEN_ASR_MODEL='0.6b')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('阻断项', result.stdout)
        self.assertIn('degraded: 0  blocked: 1', result.stdout)
        record, = self.records(result.stdout)
        self.assertEqual(record[1], 'blocked')
        self.assertIn('Qwen3-ASR', record[2])
        self.assertIn("'" + str(self.root / 'scripts/setup-qwen-asr.sh') + "'", record[4])
        self.assertIn("VOICE_IME_QWEN_ASR_SIZES='0.6B'", record[4])
        self.assertIn("VOICE_IME_QWEN_ASR_MODEL='0.6b'", record[4])
        self.assertIn('--proxy', record[4])

    def test_strict_assets_makes_requested_degradation_fail(self):
        result = self.run_script('init.sh', '--no-voice', '--skip-install', '--strict-assets',
                                 STUB_RIME_RC='1', STUB_RNNOISE_RC='1')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('⚠ 降级项', result.stdout)
        self.assertIn('--strict-assets', result.stdout)
        self.assertIn('degraded: 2  blocked: 0', result.stdout)
        self.assertIn('EXIT: 1', result.stdout)

    def test_gpu_none_and_partial_are_skips_even_in_strict_mode(self):
        for state in ('none', 'partial'):
            with self.subTest(state=state):
                result = self.run_script('init.sh', '--no-rime-ice', '--no-denoise',
                                         '--skip-install', '--strict-assets', STUB_GPU=state,
                                         STUB_QWEN_RC='1')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('不算失败', result.stdout)
                self.assertIn('degraded: 0  blocked: 0', result.stdout)
                self.assertEqual(self.records(result.stdout), [])
        self.assertEqual(self.events(), [])

    def test_final_registration_failure_always_emits_blocked_summary(self):
        result = self.run_script('init.sh', '--no-voice', '--no-rime-ice', '--no-denoise',
                                 STUB_INSTALL_RC='7')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        record, = self.records(result.stdout)
        self.assertEqual(record[1:3], ['blocked', 'IBus 注册激活（install.sh）'])
        self.assertEqual(shlex.split(record[4]), [str(self.root / 'install.sh')])

    def test_ready_assets_do_not_download_or_become_strict_failures(self):
        self.file('vendor/rime/share/rime-data/cn_dicts/base.dict.yaml')
        self.file('vendor/rime/share/rime-data/zhwiki.dict.yaml')
        self.file('vendor/rime/share/rime-data/moegirl.dict.yaml')
        self.file('vendor/rime/build/rime_ice.table.bin')
        self.file('vendor/models/rnnoise/bd.rnnn')
        result = self.run_script('init.sh', '--no-voice', '--skip-install', '--strict-assets',
                                 STUB_RIME_RC='1', STUB_RNNOISE_RC='1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('ready: 2  degraded: 0  blocked: 0', result.stdout)
        self.assertEqual(self.events(), [])

    def test_rime_soft_failure_rc3_is_degraded_and_strict_fails(self):
        # 核心成功、请求的扩展词典缺失：helper 专用退出码 3 必须记降级，
        # --strict-assets 下非零（红队发现 strict 模式原盲区）。
        result = self.run_script('init.sh', '--no-voice', '--no-denoise', '--skip-install',
                                 STUB_RIME_RC='3')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('[degraded] rime-ice 扩展词典', result.stderr)
        self.assertIn('ready: 0  degraded: 1  blocked: 0', result.stdout)
        result = self.run_script('init.sh', '--no-voice', '--no-denoise', '--skip-install',
                                 '--strict-assets', STUB_RIME_RC='3')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_missing_requested_extension_invalidates_cached_ready(self):
        # 核心缓存存在但请求的 zhwiki 缺失：不得记 ready，需重跑 helper 补齐。
        self.file('vendor/rime/share/rime-data/cn_dicts/base.dict.yaml')
        self.file('vendor/rime/share/rime-data/moegirl.dict.yaml')
        self.file('vendor/rime/build/rime_ice.table.bin')
        result = self.run_script('init.sh', '--no-voice', '--no-denoise', '--skip-install',
                                 STUB_RIME_RC='0')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('setup-rime-ice.sh', [event['name'] for event in self.events()])

    def test_newline_proxy_cannot_forge_or_truncate_tsv_records(self):
        # proxy 含换行不得截断续装命令或伪造 VOICE_IME_INIT_RESULT 条目
        # （红队实测合法换行路径同样可触发，统一由 sanitize 防护）。
        result = self.run_script('setup.sh', '--yes', '--no-rime-ice', '--no-denoise',
                                 '--proxy', 'http://proxy.invalid:7890\nVOICE_IME_INIT_RESULT\tblocked\tINJECTED\tFORGED\ttrue',
                                 STUB_QWEN_RC='1')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        tail = result.stdout.split('diagnostic doctor')[-1]
        # 安全属性：不得出现伪造的独立失败条目（换行已压平为 proxy 值内的
        # 普通空格，注入文本只能作为无害数据留在引号内）。
        self.assertNotIn('安装失败：INJECTED', tail)
        self.assertNotIn('FORGED IMPACT', tail)
        for line in tail.splitlines():
            if '续装命令' in line:
                self.assertNotIn('\t', line)
                self.assertTrue(line.count("'") % 2 == 0, line)

    def test_setup_repeats_blocked_commands_after_diagnostic_output(self):
        result = self.run_script('setup.sh', '--yes', '--no-rime-ice', '--no-denoise',
                                 '--proxy', 'http://proxy.invalid:7890', STUB_QWEN_RC='1')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        tail = result.stdout.split('diagnostic doctor')[-1]
        self.assertIn('安装失败：Qwen3-ASR 本地后端（安装/验收）', tail)
        self.assertIn("'" + str(self.root / 'scripts/setup-qwen-asr.sh') + "'", tail)
        self.assertIn("'http://proxy.invalid:7890'", tail)
        self.assertIn('子步骤幂等，续装不重跑向导/不重复下载已就绪资产', tail)
        self.assertNotIn('重跑 ./setup.sh', tail)
        self.assertNotIn('--verify-only', [arg for event in self.events() for arg in event['args']])

    def test_setup_degraded_success_continues_to_verify_and_doctor(self):
        result = self.run_script('setup.sh', '--yes', '--no-zhwiki',
                                 STUB_RIME_RC='1', STUB_RNNOISE_RC='1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('⚠ 降级项', result.stdout)
        self.assertIn('diagnostic doctor', result.stdout)
        qwen = [event for event in self.events() if event['name'] == 'setup-qwen-asr.sh']
        self.assertEqual([event['args'] for event in qwen], [[], ['--verify-only']])

    def test_gpu_none_init_skip_blocks_setup_verify(self):
        # 模式感知门控：init 语音步骤因无 GPU 跳过（voice=gpu_none）后，
        # setup 不得再盲目执行 --verify-only；体检照常运行。
        for state in ('none', 'partial'):
            with self.subTest(state=state):
                result = self.run_script('setup.sh', '--yes', '--no-rime-ice',
                                         '--no-denoise', STUB_GPU=state)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(f'VOICE_IME_INIT_STATE\tvoice=gpu_{state}', result.stdout)
                self.assertIn('语音验收跳过', result.stdout)
                self.assertIn(f'voice=gpu_{state}', result.stdout)
                self.assertIn('diagnostic doctor', result.stdout)
                qwen = [event for event in self.events() if event['name'] == 'setup-qwen-asr.sh']
                self.assertEqual(qwen, [], result.stdout)

    def test_prepare_only_event_sequence_without_activation_or_verify(self):
        # 两段式第一阶段：等价资产准备（init --skip-install + Qwen ENABLE_NOW=0），
        # 无 install.sh 注册、无云端 switch、无 --verify-only、无体检。
        result = self.run_script('setup.sh', '--yes', '--no-rime-ice', '--no-denoise',
                                 '--prepare-only')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([event['name'] for event in self.events()], ['setup-qwen-asr.sh'])
        qwen, = [event for event in self.events() if event['name'] == 'setup-qwen-asr.sh']
        self.assertEqual(qwen['args'], [])
        self.assertEqual(qwen['enable'], '0')
        self.assertNotIn('diagnostic doctor', result.stdout)
        self.assertIn('准备完成。第二阶段需在有 GNOME 会话时执行：', result.stdout)
        self.assertIn('1) ./install.sh', result.stdout)

    def test_prepare_only_gpu_none_still_prepares_keyboard_assets(self):
        result = self.run_script('setup.sh', '--yes', '--no-rime-ice', '--no-denoise',
                                 '--prepare-only', STUB_GPU='none')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.events(), [])
        self.assertIn('准备完成', result.stdout)
        self.assertIn('VOICE_IME_INIT_STATE\tvoice=gpu_none', result.stdout)

    def test_prepare_only_rejects_incompatible_modes(self):
        for flags in (['--prepare-only', '--verify-only'], ['--prepare-only', '--reconfigure'],
                      ['--prepare-only', '--cloud', 'siliconflow']):
            with self.subTest(flags=flags):
                result = self.run_script('setup.sh', '--yes', *flags)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn('错误', result.stderr)
                self.assertEqual(self.events(), [])

    def test_setup_strict_failure_repeats_degraded_retry_after_doctor(self):
        result = self.run_script('setup.sh', '--yes', '--keyboard-only', '--strict-assets',
                                 '--no-zhwiki', '--proxy', 'http://proxy.invalid:7890',
                                 STUB_RIME_RC='1', STUB_RNNOISE_RC='1')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        tail = result.stdout.split('diagnostic doctor')[-1]
        self.assertIn('⚠ 降级项（严格资产模式时视为失败）：rime-ice', tail)
        self.assertIn("setup-rime-ice.sh' '--proxy' 'http://proxy.invalid:7890' '--no-zhwiki'", tail)
        self.assertIn('fetch-rnnoise-model.sh', tail)


class RestartConvergenceTest(ScriptFixture):
    """真实 setup/init/install/switch 编排；IBus 会话与所有桌面调用仅事件 stub。"""

    def setUp(self):
        super().setUp()
        for name in ('install.sh', 'scripts/switch-qwen-asr.sh', 'scripts/activate-voice-engine.sh'):
            shutil.copy2(ROOT / name, self.root / name)
        for name in ('scripts/check-environment.sh', 'scripts/doctor.sh', 'scripts/env-sanitize.sh'):
            self.script(name, 'pass\n')
        # install 的 chmod 不得沿 src 测试链接改变工作区文件的权限。
        self.script('bin/chmod', 'pass\n')
        self.script('bin/pkill', 'pass\n')
        self.script('bin/systemctl', 'pass\n')
        self.script('bin/python3', '''
import os, sys
args = sys.argv[1:]
if args and args[0].endswith('/engine.py') and '--xml' in args:
    print('<component><name>voice-custom</name></component>'); sys.exit(0)
if args[:2] == ['-c', 'import gi']:
    sys.exit(0)
os.execv(sys.executable, [sys.executable, *args])
''')
        python = self.root / '.venv/bin/python'
        python.parent.mkdir(parents=True)
        python.symlink_to(self.bin / 'python3')
        self.script('scripts/gpu-probe.sh', '''
import os
print('GPU_STATE=' + os.environ.get('STUB_GPU', 'ok'))
print('GPU_REASON=fixture GPU_ACTION=fixture')
''')
        self.script('scripts/ibus-restart.sh', '''
import subprocess, sys
sys.exit(subprocess.call(['ibus', 'restart']))
''')
        self.script('bin/ibus', '''
import json, os, pathlib, sys
root = pathlib.Path(os.environ['STUB_ROOT'])
args = sys.argv[1:]
with (root / 'events.jsonl').open('a') as stream:
    stream.write(json.dumps({'name': 'ibus', 'args': args}) + '\\n')
if args == ['restart']:
    if (root / 'home/.local/share/ibus/component/voice-custom.xml').exists():
        (root / 'component-loaded').touch()
if args == ['engine', 'voice-custom']:
    sys.exit(0 if (root / 'component-loaded').exists() else 1)
''')
        self.script('bin/gsettings', '''
import json, os, pathlib, sys
root = pathlib.Path(os.environ['STUB_ROOT'])
with (root / 'events.jsonl').open('a') as stream:
    stream.write(json.dumps({'name': 'gsettings', 'args': sys.argv[1:]}) + '\\n')
if sys.argv[1] == 'get':
    print("[('ibus', 'voice-custom')]" if (root / 'gnome-source').exists() else '[]')
if sys.argv[1] == 'set':
    (root / 'gnome-source').touch()
''')
        for name in ('scripts/install-gnome-voice-hotkey.sh', 'scripts/install-gnome-clipboard-paste-hotkey.sh'):
            self.script(name, '''
import json, os, pathlib
root = pathlib.Path(os.environ['STUB_ROOT'])
with (root / 'events.jsonl').open('a') as stream:
    stream.write(json.dumps({'name': pathlib.Path(__file__).name, 'args': []}) + '\\n')
''')
        self.script('scripts/setup-qwen-asr.sh', '''
import json, os, pathlib, subprocess, sys
root = pathlib.Path(os.environ['STUB_ROOT'])
with (root / 'events.jsonl').open('a') as stream:
    stream.write(json.dumps({'name': 'qwen', 'args': sys.argv[1:]}) + '\\n')
if '--verify-only' in sys.argv or os.environ.get('STUB_QWEN_RC'):
    sys.exit(int(os.environ.get('STUB_QWEN_RC', '0')))
if os.environ.get('VOICE_IME_ENABLE_QWEN_ASR') == '0':
    sys.exit(0)
model = root / 'vendor/models/qwen3-asr/Qwen3-ASR-1.7B'
model.mkdir(parents=True, exist_ok=True)
# 新机激活前必须已把组件加载进当前会话，不能只检查 XML 存在。
if not (root / 'component-loaded').exists():
    print('fixture: component not loaded', file=sys.stderr); sys.exit(1)
sys.exit(subprocess.call(['bash', str(root / 'scripts/switch-qwen-asr.sh'), '1.7b']))
''')

    def restart_events(self):
        return [event for event in self.events() if event['name'] == 'ibus' and event['args'] == ['restart']]

    def test_fresh_keyboard_setup_restarts_once(self):
        result = self.run_script('setup.sh', '--yes', '--keyboard-only', '--no-rime-ice',
                                 '--no-denoise', '--skip-doctor')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.restart_events()), 1)
        self.assertLessEqual(len(self.restart_events()), 2)
        self.assertIn('== 开始使用 ==', result.stdout)

    def test_fresh_local_voice_setup_restarts_twice_and_loads_component_before_activation(self):
        result = self.run_script('setup.sh', '--yes', '--no-rime-ice', '--no-denoise', '--skip-doctor')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.restart_events()), 2, self.events())
        self.assertLessEqual(len(self.restart_events()), 2)
        self.assertTrue((self.root / 'component-loaded').exists())
        self.assertIn('本地语音路径已预注册，不重复重启', result.stdout)
        self.assertIn('仅注册/写配置', result.stdout)

    def test_gpu_skipped_setup_only_final_registration_restarts(self):
        for state in ('none', 'partial'):
            with self.subTest(state=state):
                self.log.unlink(missing_ok=True)
                result = self.run_script('setup.sh', '--yes', '--no-rime-ice', '--no-denoise',
                                         '--skip-doctor', STUB_GPU=state)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len(self.restart_events()), 1)
                self.assertFalse(any(event['name'] == 'qwen' for event in self.events()))

    def test_no_restart_still_writes_component_environment_and_config_without_desktop_activation(self):
        result = self.run_script('install.sh', VOICE_IME_NO_RESTART='1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for path in ('home/.local/share/ibus/component/voice-custom.xml',
                     'home/.config/environment.d/ibus-voice-ime.conf',
                     'home/.config/ibus-voice-ime/config.json'):
            self.assertTrue((self.root / path).is_file(), path)
        self.assertEqual(self.restart_events(), [])
        self.assertFalse(any(event['name'] == 'gsettings' or event['name'].startswith('install-gnome')
                             for event in self.events()))

    def test_voice_preparation_failure_keeps_pre_registered_session_without_extra_restart(self):
        result = self.run_script('setup.sh', '--yes', '--no-rime-ice', '--no-denoise',
                                 '--skip-doctor', STUB_QWEN_RC='1')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(len(self.restart_events()), 1)
        self.assertTrue((self.root / 'component-loaded').exists())
        self.assertNotIn('== 开始使用 ==', result.stdout)

    def test_switch_activation_failure_after_restart_never_causes_third_restart(self):
        self.script('scripts/activate-voice-engine.sh', 'import sys\nsys.exit(1)\n')
        result = self.run_script('setup.sh', '--yes', '--no-rime-ice', '--no-denoise', '--skip-doctor')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(len(self.restart_events()), 2)
        self.assertIn('桌面激活失败', result.stdout + result.stderr)
        self.assertIn('阻断项', result.stdout)
        self.assertNotIn('== 开始使用 ==', result.stdout)


class DiagnosticOutputClarityTest(ScriptFixture):
    def setUp(self):
        super().setUp()
        # 拦截 ctypes/gi 探测，其余真实配置模块仍由测试解释器执行。
        self.script('bin/python3', '''
import os, sys
args = sys.argv[1:]
code = None
if args and args[0] == '-':
    code = sys.stdin.read()
elif len(args) >= 2 and args[0] == '-c':
    code = args[1]
if code and ('import gi' in code or 'from gi.repository' in code):
    sys.exit(int(os.environ.get('STUB_GI_RC', '0')))
if code and 'ctypes.CDLL' in code:
    print('OK'); sys.exit(0)
if args and args[0] == '-':
    os.execv(sys.executable, [sys.executable, '-c', code, *args[1:]])
os.execv(sys.executable, [sys.executable, *args])
''')
        for name in ('.venv/bin/python', '.venv-qwen-asr/bin/python'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(self.bin / 'python3')
        self.script('scripts/gpu-probe.sh', "print('GPU_STATE=ok GPU_BRIEF=fixture')\n")
        self.script('scripts/qwen-preflight.py', 'print("[OK] fixture preflight")\n')
        self.script('bin/pgrep', 'import sys\nsys.exit(1)\n')
        self.script('bin/git', 'import sys\nsys.exit(1)\n')
        self.script('bin/arecord', 'print("card 0: fixture [fixture]")\n')
        for name in ('ibus', 'pw-record', 'ffmpeg', 'sox'):
            self.script('bin/' + name, 'pass\n')
        self.script('bin/gsettings', '''
import sys
print("[('ibus', 'voice-custom')]" if sys.argv[-1] == 'sources' else
      "['/fixture/voice/', '/fixture/paste/']")
''')
        self.script('bin/dconf', '''
import os
print("'" + os.environ['STUB_ROOT'] + "/scripts/voice-toggle.sh'")
''')
        self.script('scripts/voice-toggle.sh', 'pass\n')
        self.script('run-engine.sh', 'pass\n')
        self.file('home/.local/share/ibus/component/voice-custom.xml',
                  f'<component><exec>{self.root}/run-engine.sh</exec></component>')
        self.file('home/.config/environment.d/ibus-voice-ime.conf',
                  f'VOICE_IME_RIME_LIBRARY={self.root}/vendor/rime/lib/librime.so.1\n')
        self.file('vendor/rime/lib/librime.so.1')
        self.file('vendor/rime/lib/rime-plugins/librime-lua.so')
        self.file('vendor/rime/share/rime-data/rime_ice.schema.yaml')
        self.file('vendor/rime/share/rime-data/cn_dicts/base.dict.yaml')
        self.model = self.root / 'vendor/models/qwen3-asr/Qwen3-ASR-1.7B'
        self.model.mkdir(parents=True)
        self.env.update(VOICE_IME_QWEN_ASR_PYTHON=str(self.root / '.venv-qwen-asr/bin/python'),
                        VOICE_IME_QWEN_ASR_MODEL_PATH=str(self.model))

    def summary(self, result):
        summary = result.stdout.rsplit('== 汇总 ==', 1)[1]
        details = [line for line in summary.splitlines() if line.startswith('  [WARN] ')]
        count = int(re.search(r'FAIL: \d+  WARN: (\d+)', summary).group(1))
        self.assertEqual(len(details), count, result.stdout + result.stderr)
        for line in details:
            self.assertRegex(line, r'^  \[WARN\] .+ —— .+；.+$')
        return summary, details

    def test_environment_summary_preserves_format_exit_semantics_and_details(self):
        result = self.run_script('scripts/check-environment.sh')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summary, details = self.summary(result)
        model_line, = [line for line in details if 'Qwen 模型目录存在' in line]
        self.assertIn('分层验收设计', model_line)
        self.assertIn('不跑模型推理', model_line)
        self.assertIn('最近验收过', model_line)
        self.assertIn('setup-qwen-asr.sh --verify-only', model_line)
        self.assertIn('否则运行 ./scripts/setup-qwen-asr.sh 完整', model_line)
        self.assertIn('  FAIL: 0  WARN:', summary)
        self.assertIn('EXIT: 0', summary)
        for line in details:
            self.assertEqual(result.stdout.count(line), 2)

    def test_doctor_final_summary_includes_each_warn_and_environment_total(self):
        precheck = self.run_script('scripts/check-environment.sh')
        _, env_details = self.summary(precheck)
        doctor = self.run_script('scripts/doctor.sh')
        self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        summary, details = self.summary(doctor)
        imported = [line for line in details if '[环境预检]' in line]
        self.assertEqual(len(imported), len(env_details))
        self.assertEqual(imported, [line.replace('[WARN] ', '[WARN] [环境预检] ', 1)
                                    for line in env_details])
        # 本地 doctor 自有警告：懒加载引擎、未编译词库、RNNoise 缺失
        # （"模型目录不能证明推理"与环境预检同标题，汇总去重后只在预检条目出现一次）。
        self.assertEqual(len(details), len(env_details) + 3)
        self.assertIn('引擎进程未运行 —— 懒加载设计', summary)
        self.assertIn('Super+Space', summary)
        self.assertIn('词库源在但编译产物缺失 —— 首次按键', summary)
        self.assertIn('1-3 分钟', summary)
        self.assertIn('setup-rime-ice.sh', summary)
        self.assertIn('FAIL: 0', summary)
        self.assertIn('EXIT: 0', summary)

    def test_nonexistent_explicit_config_path_is_fail_not_lazy_loading_warn(self):
        config = self.file('home/.config/ibus-voice-ime/config.json', json.dumps({
            'rime': {'library': str(self.root / 'missing/librime.so.1')}}))
        result = self.run_script('scripts/doctor.sh', VOICE_IME_CONFIG=str(config))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        summary, _ = self.summary(result)
        self.assertIn('config 的 rime.library 指向不存在的路径', result.stdout)
        self.assertNotIn('引擎进程未运行 —— 懒加载设计', summary)
        self.assertIn('EXIT: 1', summary)

    def test_environment_fail_is_still_fail_when_embedded_in_doctor(self):
        environment = self.run_script('scripts/check-environment.sh', STUB_GI_RC='1')
        self.assertEqual(environment.returncode, 1, environment.stdout + environment.stderr)
        self.summary(environment)
        doctor = self.run_script('scripts/doctor.sh', STUB_GI_RC='1')
        self.assertEqual(doctor.returncode, 1, doctor.stdout + doctor.stderr)
        summary, _ = self.summary(doctor)
        # 汇总段逐条透传预检 FAIL 的原因与修复方案（962a20f 起不再使用泛化句）。
        self.assertIn('[环境预检] PyGObject 未安装', summary)
        self.assertIn('python3-gobject', summary)
        self.assertNotIn('FAIL: 0', summary)
        self.assertIn('FAIL: 1', summary)
        self.assertIn('EXIT: 1', summary)

    def test_no_gpu_qwen_backend_environment_outputs_skip_not_warn(self):
        # 模式感知预检：无 GPU 时本地 Qwen venv/模型检查失去意义——输出 [SKIP]
        # （不计数、不误导用户下载），GPU 状态不再进入 WARN 汇总。
        self.script('scripts/gpu-probe.sh', "print(\"GPU_STATE=none GPU_REASON='fixture 无卡'\")\n")
        result = self.run_script('scripts/check-environment.sh')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('[SKIP] 本地 Qwen3-ASR 检查', result.stdout)
        self.assertIn('switch-siliconflow-asr.sh', result.stdout)
        self.assertNotRegex(result.stdout, r'\[WARN\][^\n]*NVIDIA')
        self.assertIn('FAIL: 0', result.stdout)
        summary, details = self.summary(result)
        for line in details:
            self.assertNotIn('NVIDIA', line)

    def test_gpu_partial_doctor_includes_driver_reason_and_action(self):
        # partial（有卡驱动不可用）：FAIL 附原因/修复动作，侧重点与 none 不同。
        self.script('scripts/gpu-probe.sh',
                    "print(\"GPU_STATE=partial GPU_REASON='内核模块未编好' GPU_ACTION='akmods --force 重建后重启'\")\n")
        result = self.run_script('scripts/doctor.sh')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('内核模块未编好', result.stdout)
        self.assertIn('akmods', result.stdout)

    def test_doctor_qwen_backend_without_gpu_fails_with_clear_guidance(self):
        # backend=qwen3-asr 且无可用 GPU：体检如实 FAIL，但给出换云端/仅键盘
        # 明确指引；CUDA 运行时验收跳过（不把环境不匹配误报成运行时失败）。
        self.script('scripts/gpu-probe.sh', "print(\"GPU_STATE=none GPU_REASON='fixture 无卡'\")\n")
        result = self.run_script('scripts/doctor.sh')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('本机无可用 NVIDIA GPU', result.stdout)
        self.assertIn('./scripts/switch-siliconflow-asr.sh', result.stdout)
        self.assertIn('[SKIP] CUDA 运行时验收', result.stdout)
        summary, _ = self.summary(result)
        self.assertRegex(summary, r'FAIL: [1-9]')
        self.assertIn('EXIT: 1', summary)


if __name__ == '__main__':
    unittest.main()


class PrepareOnlyFailurePathTest(unittest.TestCase):
    """--prepare-only 的失败路径与第二阶段指引（红队两项 P2 的回归）。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="prepare-fail-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "scripts").mkdir()
        self.home = self.root / "home"; self.home.mkdir()
        self.log = self.root / "events.jsonl"
        shutil.copy2(ROOT / "setup.sh", self.root / "setup.sh")
        # setup.sh 调用根目录 init.sh：stub 它（记录调用、按 STUB_INIT_RC 退出、
        # 输出受控 TSV 标记），doctor 也 stub 以断言"未运行"。
        self.script("init.sh", """#!/usr/bin/env bash
printf '%s\n' "init args=[$*] rc=${STUB_INIT_RC:-0}" >> "$STUB_LOG"
printf 'VOICE_IME_INIT_STATE\tvoice=%s\n' "${STUB_INIT_VOICE:-ok}"
exit "${STUB_INIT_RC:-0}"
""")
        self.script("scripts/doctor.sh", "#!/bin/sh\necho DOCTOR-RAN\nexit 0\n")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("VOICE_IME_", "STUB_"))}
        self.env.update(HOME=str(self.home), STUB_LOG=str(self.log),
                        TMPDIR=str(self.root / "tmp"), PATH=os.environ["PATH"])

    def script(self, rel, text):
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        target.chmod(0o755)

    def run_setup(self, *args, **extra):
        return subprocess.run(["bash", str(self.root / "setup.sh"), *args],
                              env=dict(self.env, **extra), capture_output=True,
                              text=True, timeout=60, cwd=str(self.root))

    def test_prepare_only_failure_skips_doctor(self):
        result = self.run_setup("--yes", "--prepare-only", "--no-rime-ice",
                                "--no-denoise", STUB_INIT_RC="9")
        self.assertEqual(result.returncode, 9, result.stdout + result.stderr)
        self.assertNotIn("DOCTOR-RAN", result.stdout)
        self.assertIn("已按当前模式跳过体检", result.stdout + result.stderr)

    def test_prepare_only_success_guides_install_first_then_verify(self):
        result = self.run_setup("--yes", "--prepare-only", "--no-voice",
                                "--no-rime-ice", "--no-denoise")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("1) ./install.sh", result.stdout)
        self.assertIn("必须", result.stdout)
        self.assertIn("2) ./setup.sh --verify-only", result.stdout)
        self.assertNotIn("DOCTOR-RAN", result.stdout)
