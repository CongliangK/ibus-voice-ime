"""Hermetic local interpreter and uv source tests; no network/system changes."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('setup_qwen_python', ROOT / 'scripts/setup-qwen-python.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class PythonBootstrapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def managed(self):
        python = self.root / '.python/cpython/bin/python3.12'
        python.parent.mkdir(parents=True)
        python.write_text('fixture')
        python.chmod(0o755)
        return python

    def test_managed_install_never_uses_system_python_and_is_local(self):
        python = self.managed()
        calls = []
        def run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 0, str(python) + '\n', '')
        with mock.patch.object(setup.shutil, 'which', return_value='/fake/uv'), mock.patch.object(setup, 'validate_uv'), mock.patch.object(setup.subprocess, 'run', side_effect=run), mock.patch.object(setup, 'interpreter_info', return_value={'version': [3, 12, 13]}):
            self.assertEqual(setup.provision(self.root), str(python))
        for cmd, kwargs in calls:
            self.assertEqual(kwargs['env']['UV_PYTHON_INSTALL_DIR'], str(self.root / '.python'))
            self.assertEqual(kwargs['env']['UV_PYTHON_BIN_DIR'], str(self.root / '.tools/bin'))
            self.assertIn('--no-config', cmd)
            self.assertLessEqual(kwargs['timeout'], 600)
        self.assertIn('--managed-python', calls[1][0])
        self.assertIn('--no-python-downloads', calls[1][0])

    def test_explicit_bad_python_does_not_bootstrap_or_fallback(self):
        with mock.patch.object(setup, 'select_python', side_effect=FileNotFoundError), mock.patch.object(setup, 'bootstrap_uv') as bootstrap:
            with self.assertRaises(FileNotFoundError):
                setup.provision(self.root, '/bad/python')
            bootstrap.assert_not_called()

    def test_foreign_relative_missing_and_symlink_uv_results_never_execute(self):
        outside = self.root / 'external-python'
        outside.write_text('do not execute')
        outside.chmod(0o755)
        link = self.root / '.python/link'
        link.parent.mkdir()
        link.symlink_to(outside)
        for candidate in (str(outside), 'python3.12', str(link), str(self.root / '.python/missing')):
            with self.subTest(candidate=candidate), mock.patch.object(setup.shutil, 'which', return_value='/uv'), mock.patch.object(setup, 'validate_uv'), mock.patch.object(setup.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, candidate + '\n', '')), mock.patch.object(setup, 'interpreter_info') as probe:
                with self.assertRaisesRegex(RuntimeError, '非仓库内'):
                    setup.provision(self.root)
                probe.assert_not_called()

    def test_symlink_cache_directories_rejected_before_download_or_execution(self):
        with tempfile.TemporaryDirectory() as outside:
            for name in ('.tools', '.python', '.cache'):
                link = self.root / name
                link.symlink_to(outside, target_is_directory=True)
                with mock.patch.object(setup.subprocess, 'run') as run, mock.patch.object(setup.urllib.request, 'urlopen') as network:
                    with self.assertRaisesRegex(RuntimeError, '符号链接'):
                        setup.provision(self.root)
                    run.assert_not_called()
                    network.assert_not_called()
                link.unlink()

    def test_managed_314_rejected(self):
        python = self.managed()
        with mock.patch.object(setup.shutil, 'which', return_value='/uv'), mock.patch.object(setup, 'validate_uv'), mock.patch.object(setup.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, str(python), '')), mock.patch.object(setup, 'interpreter_info', return_value={'version': [3, 14, 0]}):
            with self.assertRaisesRegex(RuntimeError, '要求 Python 3.12'):
                setup.provision(self.root)

    def wheel_responses(self, digest_ok=True, binary=b'fake uv'):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr(f'uv-{setup.UV_VERSION}.data/scripts/uv', binary)
        wheel = buffer.getvalue()
        metadata = {'urls': [{'filename': f'uv-{setup.UV_VERSION}-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl', 'url': 'https://wheel.test/uv.whl', 'digests': {'sha256': hashlib.sha256(wheel).hexdigest() if digest_ok else '0' * 64}}]}
        return [io.BytesIO(json.dumps(metadata).encode()), io.BytesIO(wheel)]

    def test_absent_uv_bootstraps_hash_verified_local_binary_without_pip(self):
        with mock.patch.object(setup.platform, 'machine', return_value='x86_64'), mock.patch.object(setup.platform, 'system', return_value='Linux'), mock.patch.object(setup.urllib.request, 'urlopen', side_effect=self.wheel_responses()), mock.patch.object(setup, 'validate_uv') as validate:
            binary = Path(setup.bootstrap_uv(self.root))
        validate.assert_called_once()
        self.assertNotEqual(validate.call_args.args[0], str(binary))  # validate before atomic publish
        self.assertEqual(binary, self.root / '.tools/uv/uv')
        self.assertEqual(binary.read_bytes(), b'fake uv')
        self.assertTrue(os.access(binary, os.X_OK))
        self.assertFalse(list(binary.parent.glob('uv.tmp-*')))

    def test_bad_digest_never_installs_executable(self):
        with mock.patch.object(setup.platform, 'machine', return_value='x86_64'), mock.patch.object(setup.urllib.request, 'urlopen', side_effect=self.wheel_responses(False)):
            with self.assertRaisesRegex(RuntimeError, 'SHA256'):
                setup.bootstrap_uv(self.root)
        self.assertFalse((self.root / '.tools/uv/uv').exists())

    def test_zero_byte_executable_cached_uv_not_reused(self):
        binary = self.root / '.tools/uv/uv'
        binary.parent.mkdir(parents=True)
        binary.touch(); binary.chmod(0o755)
        with mock.patch.object(setup.urllib.request, 'urlopen') as network:
            with self.assertRaisesRegex(RuntimeError, '完整性'):
                setup.bootstrap_uv(self.root)
            network.assert_not_called()

    def test_new_bad_uv_never_published_and_temporary_cleaned(self):
        with mock.patch.object(setup.platform, 'machine', return_value='x86_64'), mock.patch.object(setup.urllib.request, 'urlopen', side_effect=self.wheel_responses(binary=b'')):
            with self.assertRaisesRegex(RuntimeError, '解压大小'):
                setup.bootstrap_uv(self.root)
        with mock.patch.object(setup.platform, 'machine', return_value='x86_64'), mock.patch.object(setup.urllib.request, 'urlopen', side_effect=self.wheel_responses()):
            with self.assertRaisesRegex(RuntimeError, '完整性'):
                setup.bootstrap_uv(self.root)
        self.assertFalse((self.root / '.tools/uv/uv').exists())
        self.assertFalse(list((self.root / '.tools/uv').glob('uv.tmp-*')))

    def test_uv_version_wrong_or_hung_rejected(self):
        for value in ('', 'not uv', 'uv 0.1.0'):
            with mock.patch.object(setup.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, value, '')):
                with self.assertRaisesRegex(RuntimeError, '版本'):
                    setup.validate_uv('/fake', pinned=True)
        with mock.patch.object(setup.subprocess, 'run', side_effect=subprocess.TimeoutExpired('uv', 15)):
            with self.assertRaisesRegex(RuntimeError, '完整性'):
                setup.validate_uv('/fake')

    def test_malformed_metadata_is_actionable(self):
        for metadata in (None, [], {'urls': None}, {'urls': [None, {}]}):
            with mock.patch.object(setup.platform, 'machine', return_value='x86_64'), mock.patch.object(setup.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(metadata).encode())):
                with self.assertRaisesRegex(RuntimeError, '元数据'):
                    setup.bootstrap_uv(self.root)

    def test_uv_find_error_preserves_stderr_and_stage(self):
        with mock.patch.object(setup.shutil, 'which', return_value='/uv'), mock.patch.object(setup, 'validate_uv'), mock.patch.object(setup.subprocess, 'run', side_effect=[subprocess.CompletedProcess([], 0), subprocess.CalledProcessError(2, 'find', stderr='broken managed cache')]):
            with self.assertRaisesRegex(RuntimeError, '查找仓库解释器.*broken managed cache'):
                setup.provision(self.root)

    def test_failed_download_has_no_system_fallback(self):
        with mock.patch.object(setup.shutil, 'which', return_value='/uv'), mock.patch.object(setup, 'validate_uv'), mock.patch.object(setup.subprocess, 'run', side_effect=subprocess.TimeoutExpired('uv', 600)), mock.patch.object(setup, 'interpreter_info') as selection:
            with self.assertRaisesRegex(RuntimeError, '安装 CPython 3.12超过总时限'):
                setup.provision(self.root)
            selection.assert_not_called()

    def test_download_has_real_wall_clock_deadline(self):
        old = signal.getsignal(signal.SIGALRM)
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, '总时限'):
            with setup.download_deadline(1):
                time.sleep(5)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(signal.getsignal(signal.SIGALRM), old)
