"""共享模型缓存与验收 receipt 的沙箱测试（fake fixture，参考 test_qwen_setup.py）。

不 sudo、不联网、不碰真实 HOME/GPU/缓存：ModelScope CLI、GPU 探测、解释器、
doctor 均为桩件；模型“下载”只写 fixture 字节。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class QwenCacheReceiptFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='qwen-cache-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'scripts').mkdir()
        for name in ('setup-qwen-asr.sh', 'env-file-load.sh', 'setup-qwen-python.py'):
            shutil.copy2(ROOT / 'scripts' / name, self.root / 'scripts' / name)
        shutil.copy2(ROOT / 'requirements-qwen-asr.txt', self.root / 'requirements-qwen-asr.txt')
        (self.root / 'src').symlink_to(ROOT / 'src')
        self.log = self.root / 'events'
        self.target = self.root / 'target-python'
        self.home = self.root / 'home'
        self.home.mkdir()
        self.venv = self.root / '.venv-qwen-asr'
        self.cache_root = self.root / 'xdg-cache' / 'ibus-voice-ime' / 'models'
        self.models = self.root / 'vendor/models/qwen3-asr'
        self.default_dir = self.models / 'Qwen3-ASR-1.7B'
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(('VOICE_IME_', 'PYTHONPATH'))}
        self.env.update(HOME=str(self.home), FAKE_ROOT=str(self.root),
                        XDG_CACHE_HOME=str(self.root / 'xdg-cache'),
                        VOICE_IME_QWEN_ASR_SETUP_PYTHON=str(self.target),
                        VOICE_IME_CONFIG=str(self.home / 'config.json'))
        (self.home / 'config.json').write_text(json.dumps({'asr': {'backend': 'qwen3-asr'}}))
        # check_asr_backend 尾部还会检查 RNNoise 降噪模型：预置以免与 Qwen 的
        # WARN 计数断言串扰（本文件只测 Qwen 缓存与 receipt）。
        rnnoise = self.root / 'vendor/models/rnnoise/bd.rnnn'
        rnnoise.parent.mkdir(parents=True, exist_ok=True)
        rnnoise.write_bytes(b'fixture-rnnoise')
        self.make_python(self.target, 12)
        self.make_script(self.root / 'scripts/gpu-probe.sh',
                         "#!/bin/sh\nprintf 'GPU_STATE=%s\\n' \"${FAKE_GPU:-ok}\"\nprintf 'GPU_BRIEF=fake-gpu\\n'\nprintf 'GPU_REASON=fake\\nGPU_ACTION=repair\\n'\n")
        self.make_script(self.root / 'scripts/switch-qwen-asr.sh',
                         "#!/bin/sh\necho activate >> \"$FAKE_ROOT/events\"\n")
        self.make_script(self.root / 'scripts/python-select.sh', '#!/bin/sh\ncommand -v python3\n')
        self.make_script(self.root / 'scripts/qwen-preflight.py', f'''#!{sys.executable}
import os, pathlib, sys
args = sys.argv[1:]
stage = args[args.index('--stage') + 1] if '--stage' in args else 'model'
with (pathlib.Path(os.environ['FAKE_ROOT']) / 'events').open('a') as stream:
    stream.write(stage + '\\n')
if stage == os.environ.get('FAKE_FAIL_STAGE'):
    print('fatal error', file=sys.stderr); sys.exit(1)
if '--model' in args:
    model = pathlib.Path(args[args.index('--model') + 1])
    if not (model / 'config.json').exists() or not (model / 'model.safetensors').exists():
        sys.exit(1)
''')

    # ------------------------------------------------------------ 桩件工具 --
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
# doctor 以本解释器执行脚本文件（qwen-preflight.py）：转发执行并对齐 sys.argv。
if a and os.path.isfile(a[0]) and a[0].endswith('.py'):
    sys.argv = a
    exec(compile(open(a[0], encoding='utf-8').read(), a[0], 'exec'))
    sys.exit(0)
if a[:1] == ['-']:
    code = sys.stdin.read()
    # 只转发 receipt 匹配器（import hashlib）；REUSE 元数据探针保持静默通过。
    if 'import hashlib' in code:
        sys.argv = a
        exec(compile(code, '<stdin>', 'exec'))
    sys.exit(0)
if '-c' in a:
    code = a[a.index('-c') + 1]
    if 'json.dumps' in code:
        print(json.dumps({{'version': [3, {minor}, 0], 'base': {base!r}, 'include': '/fake/include'}}))
    if 'qwen_asr_runtime' in code:
        if '_python' in code:
            print(os.environ.get('VOICE_IME_QWEN_ASR_PYTHON', ''))
        if 'model_id' in code:
            print(os.environ.get('VOICE_IME_QWEN_ASR_MODEL_PATH', ''))
    sys.exit(0)
if a[:2] == ['-m', 'venv']:
    target = pathlib.Path(a[2]); (target / 'bin').mkdir(parents=True)
    shutil.copy2(__file__, target / 'bin/python')
    (target / 'bin/pip').write_text('#!/bin/sh\\nexit 0\\n'); (target / 'bin/pip').chmod(0o755)
    cli = target / 'bin/modelscope'
    cli.write_text('#!{sys.executable}\\nimport os,pathlib,sys\\nif os.environ.get("FAKE_DOWNLOAD_FAIL"):sys.exit(1)\\na=sys.argv\\np=pathlib.Path(a[a.index("--local_dir")+1]);p.mkdir(parents=True,exist_ok=True)\\n(p/"config.json").write_text("{{}}");(p/"model.safetensors").write_bytes(b"fixture")\\nwith (pathlib.Path(os.environ["FAKE_ROOT"])/"events").open("a") as f:f.write("download\\\\n")\\n')
    cli.chmod(0o755)
    with (root / 'events').open('a') as f: f.write('create-venv\\n')
    sys.exit(0)
if a[:2] == ['-m', 'pip']:
    sys.exit(0)
if a == ['-V']: print('Python 3.{minor}.0')
''')

    # ------------------------------------------------------------ 驱动工具 --
    def run_setup(self, *arguments, **environment):
        env = dict(self.env, **environment)
        return subprocess.run(['bash', str(self.root / 'scripts/setup-qwen-asr.sh'), *arguments],
                              env=env, capture_output=True, text=True, timeout=30)

    def run_doctor_check(self, **environment):
        # 只 source doctor 的定义部分（与 test_qwen_setup.run_doctor_repair 同款切分）。
        definitions = (ROOT / 'scripts/doctor.sh').read_text().split(
            '# ------------------------------------------------------------------- 主流程 --')[0]
        self.make_script(self.root / 'scripts/doctor-definitions.sh', definitions)
        env = dict(self.env,
                   VOICE_IME_QWEN_ASR_PYTHON=str(self.venv / 'bin/python'),
                   VOICE_IME_QWEN_ASR_MODEL_PATH=str(self.default_dir))
        env.update(environment)
        return subprocess.run(
            ['bash', '-c', 'source "$1" ""; check_asr_backend; echo "warn-count=$WARNINGS"',
             '--', str(self.root / 'scripts/doctor-definitions.sh')],
            env=env, capture_output=True, text=True, timeout=40)

    def events(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def seed_real_model(self, base, name, weights=b'fixture'):
        model = base / f'Qwen3-ASR-{name}'
        model.mkdir(parents=True, exist_ok=True)
        (model / 'config.json').write_text('{}')
        (model / 'model.safetensors').write_bytes(weights)
        return model


class QwenModelCacheTest(QwenCacheReceiptFixture):
    """改动 1：下载落共享缓存，vendor 留符号链接；幂等、迁移、自定义路径。"""

    def test_fresh_download_lands_in_shared_cache_with_symlink(self):
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for size in ('0.6B', '1.7B'):
            vendor = self.models / f'Qwen3-ASR-{size}'
            cached = self.cache_root / f'Qwen3-ASR-{size}'
            self.assertTrue(vendor.is_symlink(), vendor)
            self.assertEqual(os.readlink(vendor), str(cached))
            self.assertTrue(cached.is_dir() and not cached.is_symlink())
            # 内容经 vendor 符号链接可达（引擎/验收/REUSE 读的仍是 vendor 路径）。
            self.assertEqual((vendor / 'model.safetensors').read_bytes(), b'fixture')
        # 临时目录全部定稿，缓存内无 .part-* 残留。
        self.assertFalse(list(self.cache_root.glob('.part-*')))
        self.assertIn('共享模型缓存', result.stdout)

    def test_ready_cache_second_run_skips_download(self):
        self.assertEqual(self.run_setup().returncode, 0)
        self.log.unlink()
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('download', self.events())
        self.assertIn('smoke', self.events())  # 运行时/推理验收仍照常执行
        vendor = self.models / 'Qwen3-ASR-0.6B'
        self.assertTrue(vendor.is_symlink())

    def test_legacy_real_directory_migrated_to_cache_with_content_intact(self):
        for size, weights in (('0.6B', b'legacy-0.6'), ('1.7B', b'legacy-1.7')):
            self.seed_real_model(self.models, size, weights)
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('download', self.events())  # 完整旧目录不触发下载
        for size, weights in (('0.6B', b'legacy-0.6'), ('1.7B', b'legacy-1.7')):
            vendor = self.models / f'Qwen3-ASR-{size}'
            cached = self.cache_root / f'Qwen3-ASR-{size}'
            self.assertTrue(vendor.is_symlink(), vendor)
            self.assertFalse(cached.is_symlink())
            self.assertEqual((cached / 'model.safetensors').read_bytes(), weights)
            self.assertEqual((vendor / 'model.safetensors').read_bytes(), weights)
        self.assertIn('已迁移到共享缓存', result.stdout)

    def test_custom_cache_path_via_env(self):
        custom = self.root / 'custom-model-cache'
        result = self.run_setup(VOICE_IME_MODEL_CACHE=str(custom))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        vendor = self.models / 'Qwen3-ASR-0.6B'
        cached = custom / 'Qwen3-ASR-0.6B'
        self.assertTrue(cached.is_dir())
        self.assertTrue(vendor.is_symlink())
        self.assertEqual(os.readlink(vendor), str(cached))
        # 默认 XDG 缓存位置不再被使用。
        self.assertFalse(self.cache_root.exists())

    def test_failed_download_leaves_no_partial_cache_entry(self):
        result = self.run_setup(FAKE_DOWNLOAD_FAIL='1')
        self.assertNotEqual(result.returncode, 0)
        # 临时目录被 EXIT trap 清理，不残留半成品分档。
        self.assertFalse(list(self.cache_root.glob('.part-*')))
        for size in ('0.6B', '1.7B'):
            self.assertFalse((self.models / f'Qwen3-ASR-{size}').exists())


class QwenVerifyReceiptTest(QwenCacheReceiptFixture):
    """改动 2：smoke/--verify-only 写 receipt；doctor 匹配降噪、漂移恢复 WARN。"""

    def test_successful_smoke_writes_complete_receipt(self):
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt_path = self.venv / 'verify-receipt.json'
        self.assertTrue(receipt_path.is_file())
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        self.assertEqual(receipt['stage'], 'smoke')
        self.assertEqual(receipt['python'], str(self.venv / 'bin/python'))
        digest = hashlib.sha256(
            (self.root / 'requirements-qwen-asr.txt').read_bytes()).hexdigest()
        self.assertEqual(receipt['reqs_sha256'], digest)
        self.assertEqual(receipt['model_path'], str(self.default_dir))
        recorded = receipt['model_files']
        self.assertIn('config.json', recorded)
        self.assertIn('model.safetensors', recorded)
        for name, entry in recorded.items():
            status = (self.default_dir / name).stat()
            self.assertEqual(entry['size'], status.st_size)
            self.assertEqual(entry['mtime'], int(status.st_mtime))
        self.assertEqual(receipt['gpu'], 'fake-gpu')
        self.assertEqual(receipt['dtype'], 'bfloat16')
        self.assertTrue(receipt['timestamp'])
        self.assertEqual(receipt['root_dir'], str(self.root))
        self.assertIn('验收 receipt 已写入', result.stdout)

    def test_doctor_info_when_receipt_matches_and_warn_after_requirements_drift(self):
        self.assertEqual(self.run_setup().returncode, 0)
        matched = self.run_doctor_check()
        combined = matched.stdout + matched.stderr
        self.assertEqual(matched.returncode, 0, combined)
        self.assertIn('[INFO]', matched.stdout)
        self.assertIn('配置未变', matched.stdout)
        self.assertIn('./scripts/setup-qwen-asr.sh --verify-only', matched.stdout)
        self.assertNotIn('不能证明推理可用', matched.stdout)
        self.assertIn('warn-count=0', matched.stdout)
        # requirements 漂移（sha256 变化）→ 恢复原 WARN，计数 +1。
        requirements = self.root / 'requirements-qwen-asr.txt'
        requirements.write_text(requirements.read_text() + '\n# drift for test\n')
        drifted = self.run_doctor_check()
        combined = drifted.stdout + drifted.stderr
        self.assertEqual(drifted.returncode, 0, combined)
        self.assertIn('不能证明推理可用', drifted.stdout)
        self.assertIn('warn-count=1', drifted.stdout)
        self.assertNotIn('[INFO]', drifted.stdout)

    def test_doctor_warns_when_model_path_changed(self):
        self.assertEqual(self.run_setup().returncode, 0)
        alternate = self.seed_real_model(self.root / 'custom models', '1.7B', b'other-weights')
        result = self.run_doctor_check(VOICE_IME_QWEN_ASR_MODEL_PATH=str(alternate))
        combined = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, combined)
        self.assertIn('不能证明推理可用', result.stdout)
        self.assertIn('warn-count=1', result.stdout)
        self.assertNotIn('[INFO]', result.stdout)

    def test_doctor_warns_when_receipt_missing(self):
        self.assertEqual(self.run_setup().returncode, 0)
        (self.venv / 'verify-receipt.json').unlink()
        result = self.run_doctor_check()
        combined = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, combined)
        self.assertIn('不能证明推理可用', result.stdout)
        self.assertIn('warn-count=1', result.stdout)

    def test_verify_only_refreshes_receipt(self):
        self.assertEqual(self.run_setup().returncode, 0)
        receipt_path = self.venv / 'verify-receipt.json'
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        receipt['timestamp'] = '1999-01-01T00:00:00+0800'
        receipt_path.write_text(json.dumps(receipt), encoding='utf-8')
        env_file = self.home / '.config/environment.d/ibus-voice-ime.conf'
        env_file.parent.mkdir(parents=True)
        env_file.write_text(
            f'VOICE_IME_QWEN_ASR_PYTHON={self.venv / "bin/python"}\n'
            f'VOICE_IME_QWEN_ASR_MODEL_PATH={self.default_dir}\n')
        self.log.unlink()
        result = self.run_setup('--verify-only')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.events(), ['smoke'])
        refreshed = json.loads(receipt_path.read_text(encoding='utf-8'))
        self.assertNotEqual(refreshed['timestamp'], '1999-01-01T00:00:00+0800')
        self.assertEqual(refreshed['stage'], 'smoke')
        self.assertIn('验收 receipt 已写入', result.stdout)


if __name__ == '__main__':
    unittest.main()
