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
                            'model': os.environ.get('VOICE_IME_QWEN_ASR_MODEL')}) + '\\n')
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

    def test_setup_strict_failure_repeats_degraded_retry_after_doctor(self):
        result = self.run_script('setup.sh', '--yes', '--keyboard-only', '--strict-assets',
                                 '--no-zhwiki', '--proxy', 'http://proxy.invalid:7890',
                                 STUB_RIME_RC='1', STUB_RNNOISE_RC='1')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        tail = result.stdout.split('diagnostic doctor')[-1]
        self.assertIn('⚠ 降级项（严格资产模式时视为失败）：rime-ice', tail)
        self.assertIn("setup-rime-ice.sh' '--proxy' 'http://proxy.invalid:7890' '--no-zhwiki'", tail)
        self.assertIn('fetch-rnnoise-model.sh', tail)


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
        self.assertIn('环境依赖存在 FAIL 项', doctor.stdout)
        self.assertIn('FAIL: 1', summary)
        self.assertIn('EXIT: 1', summary)


if __name__ == '__main__':
    unittest.main()
