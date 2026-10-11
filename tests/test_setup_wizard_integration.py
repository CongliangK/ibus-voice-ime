"""安装入口隔离集成测试：真实编排/配置合并，资产与桌面操作全部 stub。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import pty
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def result_payload(**updates):
    result = {
        'voice_plan': 'local-qwen', 'qwen_sizes': ['1.7B'], 'qwen_model': '1.7b',
        'hotkeys': {'voice': 'Ctrl+Alt+V', 'raw': 'Ctrl+Alt+B', 'paste': 'Ctrl+Alt+P'},
        'postprocess': None,
        'assets': dict.fromkeys(('rime_ice', 'zhwiki', 'moegirl', 'denoise'), True),
        'reconfigure': False,
    }
    result.update(updates)
    return result


class SetupWizardIntegrationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='setup-wizard-integration-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'scripts').mkdir()
        self.home = self.root / 'home'
        self.home.mkdir()
        self.config = self.home / 'config.json'
        self.llm = self.home / '.config/ibus-voice-ime/llm.json'
        self.tmpdir = self.root / 'tmp'
        self.tmpdir.mkdir()
        self.log = self.root / 'events.jsonl'
        (self.root / 'src').symlink_to(ROOT / 'src')
        shutil.copy2(ROOT / 'setup.sh', self.root / 'setup.sh')
        shutil.copy2(ROOT / 'scripts/apply-wizard-config.py',
                     self.root / 'scripts/apply-wizard-config-real.py')
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(('VOICE_IME_', 'PYTHONPATH', 'XDG_', 'STUB_'))}
        self.env.update(HOME=str(self.home), VOICE_IME_CONFIG=str(self.config), TMPDIR=str(self.tmpdir),
                        STUB_ROOT=str(self.root), STUB_RESULT=json.dumps(result_payload()))
        self.script('scripts/config-wizard.sh', '''
import json, os, pathlib, sys
root = pathlib.Path(os.environ['STUB_ROOT'])
with (root / 'events.jsonl').open('a') as stream:
    stream.write(json.dumps({'name': 'wizard', 'args': sys.argv[1:]}) + '\\n')
r = json.loads(os.environ['STUB_RESULT'])
p = pathlib.Path(os.environ.get('VOICE_IME_WIZARD_RESULT', str(root / 'selection.json')))
p.write_text(json.dumps(r)); p.chmod(0o600)
print('向导提示（测试 stdout 最后一行契约）')
print(p)
''')
        self.script('scripts/apply-wizard-config.py', '''
import json, os, pathlib, sys
root = pathlib.Path(os.environ['STUB_ROOT'])
with (root / 'events.jsonl').open('a') as stream:
    stream.write(json.dumps({'name': 'apply', 'args': sys.argv[1:]}) + '\\n')
os.execv(sys.executable, [sys.executable, str(root / 'scripts/apply-wizard-config-real.py'), *sys.argv[1:]])
''')
        for name in ('init.sh', 'install.sh', 'scripts/doctor.sh', 'scripts/setup-qwen-asr.sh',
                     'scripts/switch-siliconflow-asr.sh', 'scripts/switch-mimo-cloud-asr.sh',
                     'scripts/switch-volc-bigmodel-asr.sh'):
            self.script(name, '''
import json, os, pathlib, sys
root = pathlib.Path(os.environ['STUB_ROOT'])
name = pathlib.Path(__file__).name
with (root / 'events.jsonl').open('a') as stream:
    stream.write(json.dumps({'name': name, 'args': sys.argv[1:],
        'sizes': os.environ.get('VOICE_IME_QWEN_ASR_SIZES'),
        'model': os.environ.get('VOICE_IME_QWEN_ASR_MODEL'),
        'postprocess': os.environ.get('VOICE_IME_SWITCH_LLM_POSTPROCESS'),
        'has_key': bool(os.environ.get('VOICE_IME_SILICONFLOW_API_KEY'))}) + '\\n')
if name == 'init.sh': sys.exit(int(os.environ.get('STUB_INIT_RC', '0')))
''')

    def script(self, relative, content):
        path = self.root / relative
        path.write_text(f'#!{sys.executable}\n' + content)
        path.chmod(0o755)

    def run_setup(self, *args, payload=None, **environment):
        env = dict(self.env, **environment)
        if payload is not None:
            env['STUB_RESULT'] = json.dumps(payload)
        return subprocess.run(['bash', str(self.root / 'setup.sh'), *args], env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)

    def events(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def names(self):
        return [event['name'] for event in self.events()]

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_yes_noninteractive_preserves_old_local_plan(self):
        result = self.run_setup('--yes')
        self.assert_success(result)
        self.assertEqual(self.names(), ['wizard', 'apply', 'init.sh', 'setup-qwen-asr.sh', 'doctor.sh'])
        wizard, _, init, verify, _ = self.events()
        self.assertIn('--defaults', wizard['args'])
        self.assertEqual(init['args'], [])
        self.assertEqual((init['sizes'], init['model']), ('0.6B 1.7B', '1.7b'))
        self.assertEqual(verify['args'], ['--verify-only'])
        self.assertIn('语音方案 local-qwen', result.stdout)
        self.assertIn('下载分档 0.6B 1.7B', result.stdout)
        self.assertIn('热键 未改动', result.stdout)

    def test_non_tty_without_yes_automatically_defaults(self):
        result = self.run_setup('--skip-verify', '--skip-doctor')
        self.assert_success(result)
        self.assertIn('--defaults', self.events()[0]['args'])
        self.assertEqual(self.names(), ['wizard', 'apply', 'init.sh'])

    def test_explicit_defaults_uses_actual_wizard_single_size(self):
        result = self.run_setup('--defaults', '--yes', '--skip-verify', '--skip-doctor')
        self.assert_success(result)
        self.assertEqual(self.events()[-1]['sizes'], '1.7B')

    def test_noninteractive_flag_equals_defaults_plus_yes(self):
        # --non-interactive / -n = 向导全默认（单档 1.7B）+ 跳过确认，
        # 全程零提问；与 --defaults --yes 显式组合完全等价。
        for flag in ('--non-interactive', '-n'):
            with self.subTest(flag=flag):
                self.log.write_text('')  # 夹具复用，清空上一子用例的事件流
                result = self.run_setup(flag, '--skip-verify', '--skip-doctor')
                self.assert_success(result)
                self.assertEqual(self.names(), ['wizard', 'apply', 'init.sh'])
                self.assertIn('--defaults', self.events()[0]['args'])
                self.assertEqual(self.events()[-1]['sizes'], '1.7B')
                self.assertNotIn('继续执行', result.stdout)

    def test_real_wizard_and_apply_interface_defaults(self):
        # 真实向导协议冒烟：只替换资产/桌面子脚本，不运行任何真实安装。
        shutil.copy2(ROOT / 'scripts/config-wizard.sh', self.root / 'scripts/config-wizard.sh')
        result = self.run_setup('--defaults', '--yes', '--skip-verify', '--skip-doctor',
                                VOICE_IME_WIZARD_RESULT=str(self.root / 'real-selection.json'))
        self.assert_success(result)
        selection = json.loads((self.root / 'real-selection.json').read_text())
        self.assertEqual(selection['voice_plan'], 'local-qwen')
        self.assertEqual(selection['qwen_sizes'], ['1.7B'])
        self.assertEqual(self.events()[-1]['name'], 'init.sh')
        self.assertEqual(self.events()[-1]['sizes'], '1.7B')
        self.assertEqual(json.loads(self.config.read_text())['recording']['hotkeys'], 'Ctrl+Alt+V')

    def test_auto_defaults_preserves_explicit_model_environment(self):
        result = self.run_setup('--yes', '--skip-verify', '--skip-doctor',
                                VOICE_IME_QWEN_ASR_SIZES='0.6B', VOICE_IME_QWEN_ASR_MODEL='0.6')
        self.assert_success(result)
        self.assertEqual(self.events()[-1]['sizes'], '0.6B')
        self.assertEqual(self.events()[-1]['model'], '0.6b')

    def test_result_drives_sizes_assets_and_postprocess(self):
        self.llm.parent.mkdir(parents=True)
        self.llm.write_text('{"enabled": false, "custom": "keep"}')
        payload = result_payload(qwen_sizes=['0.6B'], qwen_model='0.6b', postprocess=True,
                                 assets=dict.fromkeys(('rime_ice', 'zhwiki', 'moegirl', 'denoise'), False))
        result = self.run_setup('--defaults', '--yes', '--skip-doctor', payload=payload)
        self.assert_success(result)
        init = self.events()[2]
        self.assertEqual(init['sizes'], '0.6B')
        self.assertEqual(init['model'], '0.6b')
        self.assertIsNone(init['postprocess'])  # 不再伪造遗留 env 开关。
        self.assertEqual(json.loads(self.llm.read_text()), {'enabled': True, 'custom': 'keep'})
        self.assertIn('云端 LLM 后处理 有改动', result.stdout)
        self.assertIn('即刻生效，无需重注册', result.stdout)
        self.assertEqual(init['args'], ['--no-rime-ice', '--no-zhwiki', '--no-moegirl', '--no-denoise'])
        self.assertEqual(json.loads(self.config.read_text())['asr']['qwen3']['model'], '0.6b')

    def test_keyboard_result_skips_local_verify(self):
        result = self.run_setup('--defaults', '--yes', payload=result_payload(voice_plan='keyboard-only'))
        self.assert_success(result)
        self.assertEqual(self.names(), ['wizard', 'apply', 'init.sh', 'doctor.sh'])
        self.assertEqual(self.events()[2]['args'], ['--no-voice'])

    def test_cloud_explicit_flag_skips_wizard(self):
        result = self.run_setup('--yes', '--cloud', 'siliconflow', '--skip-doctor',
                                VOICE_IME_SILICONFLOW_API_KEY='test-secret')
        self.assert_success(result)
        self.assertEqual(self.names(), ['init.sh', 'switch-siliconflow-asr.sh'])
        self.assertEqual(self.events()[0]['args'], ['--no-voice'])
        self.assertTrue(self.events()[1]['has_key'])
        self.assertFalse(self.config.exists())
        self.assertNotIn('test-secret', result.stdout + result.stderr + self.log.read_text())

    def test_all_existing_selection_flags_skip_wizard(self):
        for flags in (['--keyboard-only'], ['--no-voice'], ['--no-rime-ice'], ['--no-zhwiki'],
                      ['--no-moegirl'], ['--no-denoise'], ['--cloud', 'mimo']):
            with self.subTest(flags=flags):
                self.log.unlink(missing_ok=True)
                result = self.run_setup('--yes', '--skip-verify', '--skip-doctor', *flags)
                self.assert_success(result)
                self.assertNotIn('wizard', self.names())
                self.assertNotIn('apply', self.names())

    def test_wizard_cloud_plans_switch_after_init(self):
        for plan, switch, args in (('siliconflow', 'switch-siliconflow-asr.sh', []),
                                   ('mimo-cloud', 'switch-mimo-cloud-asr.sh', ['cn']),
                                   ('volc', 'switch-volc-bigmodel-asr.sh', [])):
            with self.subTest(plan=plan):
                self.log.unlink(missing_ok=True)
                result = self.run_setup('--defaults', '--yes', '--skip-doctor',
                                        payload=result_payload(voice_plan=plan),
                                        VOICE_IME_SILICONFLOW_API_KEY='test-secret')
                self.assert_success(result)
                self.assertEqual(self.names(), ['wizard', 'apply', 'init.sh', switch])
                self.assertEqual(self.events()[2]['args'], ['--no-voice'])
                self.assertEqual(self.events()[-1]['args'], args)

    def test_siliconflow_non_tty_missing_key_keeps_guidance(self):
        result = self.run_setup('--yes', '--cloud', 'siliconflow', '--skip-doctor')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.names(), ['init.sh'])
        self.assertIn('未设置 VOICE_IME_SILICONFLOW_API_KEY', result.stderr)

    def test_siliconflow_tty_key_only_reaches_switch(self):
        master, slave = pty.openpty()
        try:
            process = subprocess.Popen(['bash', str(self.root / 'setup.sh'), '--yes', '--cloud',
                                        'siliconflow', '--skip-doctor'], env=self.env,
                                       stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            # 等待关闭 echo 的读取提示，避免测试输入在 tty 中被回显。
            prompt = ''
            while not prompt.endswith('不保存）：'):
                character = process.stdout.read(1)
                if not character:
                    break
                prompt += character
            os.write(master, b'tty-secret\n')
            stdout, _ = process.communicate(timeout=20)
            stderr = ''
            self.assertEqual(process.returncode, 0, prompt + stdout + stderr)
            self.assertEqual(self.names(), ['init.sh', 'switch-siliconflow-asr.sh'])
            self.assertFalse(self.events()[0]['has_key'])
            self.assertTrue(self.events()[1]['has_key'])
            self.assertNotIn('tty-secret', prompt + stdout + stderr + self.log.read_text())
        finally:
            os.close(master)
            os.close(slave)

    def test_noninteractive_siliconflow_tty_never_blocks_on_key_prompt(self):
        # --non-interactive 在有 tty 且缺 key 时也不得挂起在 read：
        # 直接报错并给出环境变量指引（红队 2026-10-11 发现）。
        master, slave = pty.openpty()
        try:
            self.env.pop('VOICE_IME_SILICONFLOW_API_KEY', None)
            process = subprocess.Popen(['bash', str(self.root / 'setup.sh'), '--non-interactive',
                                        '--cloud', 'siliconflow', '--skip-doctor'], env=self.env,
                                       stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            stdout, _ = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 1, stdout)
            self.assertNotIn('请输入硅基流动', stdout)
            self.assertIn('VOICE_IME_SILICONFLOW_API_KEY', stdout)
            self.assertEqual(self.names(), ['init.sh'])  # 未走到 switch
        finally:
            os.close(master)
            os.close(slave)

    def test_reconfigure_no_hotkey_or_voice_change_only_applies(self):
        self.config.write_text(json.dumps({'custom': {'keep': 'value'}}))
        result = self.run_setup('--reconfigure', '--yes')
        self.assert_success(result)
        self.assertEqual(self.names(), ['wizard', 'apply'])
        self.assertIn('--reconfigure', self.events()[0]['args'])
        self.assertIn('--defaults', self.events()[0]['args'])
        self.assertEqual(json.loads(self.config.read_text())['custom'], {'keep': 'value'})
        backups = list(self.home.glob('config.json.bak.*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text()), {'custom': {'keep': 'value'}})

    def test_reconfigure_hotkey_change_calls_install_only_after_apply(self):
        self.config.write_text('{}')
        payload = result_payload(hotkeys={'voice': 'Ctrl+Shift+V', 'raw': 'Ctrl+Alt+B', 'paste': 'Ctrl+Alt+P'})
        result = self.run_setup('--reconfigure', '--yes', payload=payload)
        self.assert_success(result)
        self.assertEqual(self.names(), ['wizard', 'apply', 'install.sh'])
        self.assertIn('热键 有改动', result.stdout)

    def test_reconfigure_voice_change_reregisters_without_switch_or_download(self):
        self.config.write_text(json.dumps({'asr': {'backend': 'siliconflow-asr'}}))
        result = self.run_setup('--reconfigure', '--yes')
        self.assert_success(result)
        self.assertEqual(self.names(), ['wizard', 'apply', 'install.sh'])

    def test_reconfigure_missing_installation_errors_before_wizard(self):
        result = self.run_setup('--reconfigure', '--yes')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.names(), [])
        self.assertIn('请先运行 ./setup.sh 正常安装', result.stderr)

    def test_registered_engine_without_config_can_reconfigure(self):
        component = self.home / '.local/share/ibus/component/voice-custom.xml'
        component.parent.mkdir(parents=True)
        component.write_text('<component/>')
        self.assert_success(self.run_setup('--reconfigure', '--yes'))
        self.assertEqual(self.names(), ['wizard', 'apply'])

    def test_dry_run_uses_result_without_applying_or_installing(self):
        self.config.write_text('{}')
        result = self.run_setup('--reconfigure', '--dry-run',
                                payload=result_payload(qwen_sizes=['0.6B'], qwen_model='0.6b'))
        self.assert_success(result)
        self.assertEqual(self.names(), ['wizard'])
        self.assertEqual(self.config.read_text(), '{}')
        self.assertEqual(list(self.home.glob('config.json.bak.*')), [])
        self.assertIn('默认模型 0.6b', result.stdout)
        self.assertIn('不下载', result.stdout)

    def test_llm_only_reconfigure_is_immediate_without_install(self):
        self.config.write_text('{}')
        self.llm.parent.mkdir(parents=True)
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                self.log.unlink(missing_ok=True)
                self.llm.write_text(json.dumps({'enabled': enabled, 'unknown': {'keep': [1, 2]}}))
                result = self.run_setup('--reconfigure', '--yes',
                                        payload=result_payload(postprocess=not enabled))
                self.assert_success(result)
                self.assertEqual(self.names(), ['wizard', 'apply'])
                self.assertIn('无需重注册', result.stdout)
                self.assertIn('云端 LLM 后处理 有改动', result.stdout)
                self.assertEqual(json.loads(self.llm.read_text()),
                                 {'enabled': not enabled, 'unknown': {'keep': [1, 2]}})
                self.assertNotIn('llm', json.loads(self.config.read_text()))
                self.assertIsNone(self.events()[-1].get('postprocess'))

    def test_absent_llm_preview_reports_not_configured(self):
        result = self.run_setup('--defaults', '--dry-run')
        self.assert_success(result)
        self.assertIn('未配置云端 LLM（llm.json 不存在）', result.stdout)
        self.assertFalse(self.llm.exists())

    def test_preview_invalid_existing_hotkeys_counts_as_change_and_apply_repairs(self):
        for value in ('Ctrl+Garbage', 42, ['bad']):
            with self.subTest(value=value):
                self.log.unlink(missing_ok=True)
                self.config.write_text(json.dumps({'recording': {'hotkeys': value}}))
                preview = self.run_setup('--reconfigure', '--dry-run')
                self.assert_success(preview)
                self.assertIn('热键 有改动', preview.stdout)
                self.assertNotIn('Traceback', preview.stderr)
                self.assertEqual(self.names(), ['wizard'])
                self.assertEqual(json.loads(self.config.read_text())['recording']['hotkeys'], value)
                self.log.unlink()
                applied = self.run_setup('--reconfigure', '--yes')
                self.assert_success(applied)
                self.assertEqual(self.names(), ['wizard', 'apply', 'install.sh'])
                self.assertEqual(json.loads(self.config.read_text())['recording']['hotkeys'], 'Ctrl+Alt+V')

    def test_real_wizard_result_cleanup_dry_run_and_apply(self):
        shutil.copy2(ROOT / 'scripts/config-wizard.sh', self.root / 'scripts/config-wizard.sh')
        for flags in (('--defaults', '--dry-run'), ('--defaults', '--yes', '--skip-verify', '--skip-doctor')):
            with self.subTest(flags=flags):
                result = self.run_setup(*flags)
                self.assert_success(result)
                self.assertEqual(list(self.tmpdir.glob('ibus-voice-ime-wizard-*.json')), [])

    def test_user_explicit_result_is_retained_even_in_tmpdir_with_matching_prefix(self):
        path = self.tmpdir / 'ibus-voice-ime-wizard-user.json'
        for flags in (('--defaults', '--dry-run'), ('--defaults', '--yes', '--skip-verify', '--skip-doctor')):
            with self.subTest(flags=flags):
                result = self.run_setup(*flags, VOICE_IME_WIZARD_RESULT=str(path))
                self.assert_success(result)
                self.assertTrue(path.is_file())
                self.assertEqual(json.loads(path.read_text()), result_payload())

    def test_malformed_result_has_chinese_field_error_rc2_without_traceback(self):
        invalid = []
        for field in ('voice_plan', 'qwen_sizes', 'qwen_model', 'hotkeys', 'postprocess', 'assets', 'reconfigure'):
            payload = result_payload()
            del payload[field]
            invalid.append((field, payload))
        for field, value in (('hotkeys', []), ('postprocess', 1), ('assets', 'bad'), ('voice_plan', []),
                             ('qwen_sizes', [None]), ('qwen_model', False)):
            invalid.append((field, result_payload(**{field: value})))
        invalid.extend((('hotkeys.raw', result_payload(hotkeys={'voice': 'Ctrl+V', 'paste': 'Ctrl+P'})),
                        ('assets.zhwiki', result_payload(assets={'rime_ice': True, 'zhwiki': 1, 'moegirl': True, 'denoise': True}))))
        for field, payload in invalid:
            with self.subTest(field=field, payload=payload):
                self.log.unlink(missing_ok=True)
                result = self.run_setup('--defaults', '--dry-run', payload=payload)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn('错误：向导结果 JSON 非法', result.stderr)
                self.assertIn(field, result.stderr)
                self.assertNotIn('Traceback', result.stderr)
                self.assertEqual(self.names(), ['wizard'])
                self.assertFalse(self.config.exists())
                self.assertEqual(list(self.tmpdir.glob('ibus-voice-ime-wizard-*.json')), [])

    def test_hotkey_environment_cleanup_failure_stops_before_install(self):
        self.config.write_text('{}')
        env_file = self.home / '.config/environment.d/ibus-voice-ime.conf'
        env_file.mkdir(parents=True)  # 可复现的清理 I/O 错误。
        result = self.run_setup('--reconfigure', '--yes', payload=result_payload(
            hotkeys={'voice': 'Ctrl+Shift+V', 'raw': 'Ctrl+Alt+B', 'paste': 'Ctrl+Alt+P'}))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('警告：environment.d 热键覆盖清理失败', result.stderr)
        self.assertEqual(self.names(), ['wizard', 'apply'])
        self.assertEqual(self.config.read_text(), '{}')
        self.assertEqual(list(self.tmpdir.glob('ibus-voice-ime-wizard-*.json')), [])

    def test_verify_only_never_runs_wizard(self):
        result = self.run_setup('--verify-only', '--yes', '--proxy', 'http://example.invalid:7890')
        self.assert_success(result)
        self.assertEqual(self.names(), ['setup-qwen-asr.sh', 'doctor.sh'])
        self.assertEqual(self.events()[0]['args'], ['--verify-only', '--proxy', 'http://example.invalid:7890'])

    def test_init_failure_diagnoses_without_switch_or_verify(self):
        result = self.run_setup('--yes', '--cloud', 'mimo', STUB_INIT_RC='7')
        self.assertEqual(result.returncode, 7)
        self.assertEqual(self.names(), ['init.sh', 'doctor.sh'])
        self.assertIn('安装失败：init.sh 未返回步骤汇总', result.stdout)


class QwenSizeSelectionTest(unittest.TestCase):
    def setUp(self):
        self.fixture = self.new_fixture()

    def new_fixture(self):
        # 复用仓库既有隔离安装夹具，不继承其测试类，避免重复收集既有测试。
        from tests.test_qwen_setup import QwenSetupTest
        fixture = QwenSetupTest(methodName='test_full_gate_runs_before_activation')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_unset_sizes_preserves_both_downloads(self):
        result = self.fixture.run_setup()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.fixture.events().count('download'), 2)

    def test_legal_subsets_only_download_selected_sizes(self):
        for size, model in (('0.6B', '0.6b'), ('1.7B', '1.7b')):
            with self.subTest(size=size):
                # 每个子用例使用新夹具，不沿用上次已下载的分档。
                fixture = self.new_fixture()
                result = fixture.run_setup(VOICE_IME_QWEN_ASR_SIZES=size,
                                           VOICE_IME_QWEN_ASR_MODEL=model)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                models = fixture.root / 'vendor/models/qwen3-asr'
                self.assertEqual([p.name for p in models.iterdir()], [f'Qwen3-ASR-{size}'])
                self.assertEqual(fixture.events().count('download'), 1)

    def test_existing_excluded_size_not_checked_or_changed(self):
        excluded = self.fixture.root / 'vendor/models/qwen3-asr/Qwen3-ASR-0.6B'
        excluded.mkdir(parents=True)
        marker = excluded / 'config.json'
        marker.write_text('故意损坏的模型配置')
        result = self.fixture.run_setup(VOICE_IME_QWEN_ASR_SIZES='1.7B')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(marker.read_text(), '故意损坏的模型配置')
        self.assertEqual(self.fixture.events().count('model'), 2)  # 所选档下载前后检查
        self.assertEqual(self.fixture.events().count('download'), 1)

    def test_explicit_both_sizes_with_whitespace(self):
        result = self.fixture.run_setup(VOICE_IME_QWEN_ASR_SIZES='  0.6B   1.7B  ')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.fixture.events().count('download'), 2)

    def test_invalid_sizes_fail_before_any_install_work(self):
        for sizes in ('', ' ', '0.6b', '2B', '1.7B bogus', '1.7B\nbogus'):
            with self.subTest(sizes=sizes):
                result = self.fixture.run_setup(VOICE_IME_QWEN_ASR_SIZES=sizes)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn('无效的 VOICE_IME_QWEN_ASR_SIZES', result.stderr)
                self.assertEqual(self.fixture.events(), [])
                self.assertFalse((self.fixture.root / '.qwen-asr-setup.lock').exists())


if __name__ == '__main__':
    unittest.main()
