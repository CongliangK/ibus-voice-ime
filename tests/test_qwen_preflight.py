"""Hermetic installation/diagnostic gates; never import torch or use the GPU."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ibus_voice_ime.asr import qwen_preflight as preflight
from ibus_voice_ime.asr.qwen_diagnostics import classify_error


class InterpreterSelectionTest(unittest.TestCase):
    def test_default_never_inherits_python314(self):
        with mock.patch.object(preflight.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "未找到 Python 3.12"):
                preflight.select_python()

    def test_default_chooses_312(self):
        with mock.patch.object(preflight.shutil, "which", return_value="/usr/bin/python3.12"), mock.patch.object(
            preflight, "interpreter_info", return_value={"version": [3, 12, 10]}
        ):
            self.assertEqual(preflight.select_python(), "/usr/bin/python3.12")

    def test_explicit_314_is_allowed_but_requires_later_gates(self):
        with mock.patch.object(preflight.shutil, "which", return_value=None), mock.patch.object(
            preflight, "interpreter_info", return_value={"version": [3, 14, 5]}
        ):
            self.assertEqual(preflight.select_python("/custom/python"), "/custom/python")

    def test_explicit_invalid_python_never_silently_falls_back(self):
        with mock.patch.object(preflight, "interpreter_info", side_effect=FileNotFoundError("missing")):
            with self.assertRaises(FileNotFoundError):
                preflight.select_python("/missing/python")

    def test_unsupported_version_rejected(self):
        with mock.patch.object(preflight, "interpreter_info", return_value={"version": [3, 9, 0]}):
            with self.assertRaisesRegex(RuntimeError, "不支持"):
                preflight.select_python("/custom/python")


class ModelFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        (self.path / "config.json").write_text('{}')

    def test_single_nonempty_weight_passes(self):
        (self.path / "model.safetensors").write_bytes(b'fixture')
        preflight.validate_model(self.path)

    def test_empty_weight_rejected(self):
        (self.path / "model.safetensors").touch()
        with self.assertRaisesRegex(RuntimeError, "为空"):
            preflight.validate_model(self.path)

    def test_index_alone_is_not_readiness(self):
        (self.path / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {"a": "shard.safetensors"}}))
        with self.assertRaisesRegex(RuntimeError, "分片|shard"):
            preflight.validate_model(self.path)

    def test_all_shards_required(self):
        (self.path / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {"a": "1.safetensors", "b": "2.safetensors"}}))
        (self.path / "1.safetensors").write_bytes(b'x')
        with self.assertRaises(RuntimeError):
            preflight.validate_model(self.path)
        (self.path / "2.safetensors").write_bytes(b'x')
        preflight.validate_model(self.path)

    def test_path_traversal_rejected(self):
        (self.path / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {"a": "../outside"}}))
        with self.assertRaisesRegex(RuntimeError, "路径越界"):
            preflight.validate_model(self.path)

    def test_missing_model_message_guides_download(self):
        with self.assertRaisesRegex(RuntimeError, "未下载"):
            preflight.validate_model(self.path / "nope")

    def test_repo_id_gets_download_hint(self):
        # 配置仍指向 HuggingFace/ModelScope 仓库 ID（本地从未下载）时，报错必须
        # 点明"这是 ID 未下载"，而不是把 ID 误称为本地目录。
        with self.assertRaisesRegex(RuntimeError, "仓库 ID"):
            preflight.validate_model(Path("Qwen/Qwen3-ASR-1.7B"))

    def test_corrupt_or_nonobject_config_rejected(self):
        for text in ('not json', '[]'):
            (self.path / "config.json").write_text(text)
            with self.assertRaises((ValueError, RuntimeError)):
                preflight.validate_model(self.path)


class StaticGateTest(unittest.TestCase):
    def test_missing_headers_fail_before_compiler(self):
        with mock.patch.object(preflight, 'interpreter_info', return_value={
            'version': [3, 14, 5], 'include': '/nonexistent-qwen-header-directory'
        }), mock.patch.object(preflight.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'Python.h'):
                preflight.check_static('/fake/python')
            run.assert_not_called()

    def test_compiler_failure_preserves_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'Python.h').touch()
            with mock.patch.object(preflight, 'interpreter_info', return_value={
                'version': [3, 12, 1], 'include': directory
            }), mock.patch.object(preflight.shutil, 'which', return_value='/fake/gcc'), mock.patch.object(
                preflight.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'fatal error: bad header')
            ):
                with self.assertRaisesRegex(RuntimeError, 'fatal error: bad header'):
                    preflight.check_static('/fake/python')


class BoundedProbeTest(unittest.TestCase):
    def test_child_failure_contains_real_stderr(self):
        with self.assertRaisesRegex(RuntimeError, 'actual compiler error'):
            preflight.run_probe(sys.executable, "import sys; print('actual compiler error', file=sys.stderr); sys.exit(1)", [], 5)

    def test_timeout_is_bounded_and_reported(self):
        with self.assertRaisesRegex(RuntimeError, '子进程已终止'):
            preflight.run_probe(sys.executable, 'import time; time.sleep(60)', [], 0.1)

    def test_disposable_cache_and_offline_environment(self):
        original = os.environ.get('TRITON_CACHE_DIR')
        captured = {}
        def child(*args, **kwargs):
            captured.update(kwargs['env'])
            self.assertTrue(Path(captured['TRITON_CACHE_DIR']).is_dir())
            return subprocess.CompletedProcess([], 0, '', '')
        with mock.patch.object(preflight.subprocess, 'run', side_effect=child):
            preflight.run_probe('/fake/python', '', [], 5)
        self.assertFalse(Path(captured['TRITON_CACHE_DIR']).exists())
        self.assertEqual(captured['HF_HUB_OFFLINE'], '1')
        self.assertEqual(os.environ.get('TRITON_CACHE_DIR'), original)


class MissingInterpreterTest(unittest.TestCase):
    def test_explicit_missing_interpreter_clear_message(self):
        # 配置里的解释器路径消失（venv 被移动/删除）时报"解释器不存在"并给重建指引，
        # 而不是裸 FileNotFoundError 被分类成 unknown。
        root = Path(__file__).resolve().parents[1]
        proc = subprocess.run(
            [sys.executable, str(root / "scripts" / "qwen-preflight.py"),
             "--python", "/nonexistent/qwen-python", "--stage", "static"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("解释器不存在", proc.stderr)
        self.assertIn("setup-qwen-asr.sh", proc.stderr)


class ErrorClassificationTest(unittest.TestCase):
    def test_specific_errors_take_precedence_over_gcc(self):
        for message, expected in (
            ('gcc: fatal error: Python.h: No such file or directory', 'python_headers'),
            ('gcc: cannot find -lcuda', 'cuda_driver'),
            ('gcc: No space left on device', 'disk_space'),
            ('gcc: Permission denied', 'permissions'),
            ('triton driver.c gcc returned non-zero exit status 1', 'triton_compile'),
            ('importlib.metadata.PackageNotFoundError: No package metadata was found for torch', 'dependencies'),
        ):
            with self.subTest(message=message):
                self.assertEqual(classify_error(json.dumps({'error': message}))[0], expected)


class HeaderProbeTest(unittest.TestCase):
    def test_probe_reports_version_matched_fix_when_header_missing(self):
        from ibus_voice_ime.asr import qwen_diagnostics as diagnostics
        with mock.patch.object(diagnostics.sysconfig, 'get_path', return_value='/nonexistent/include/python3.14'), \
             mock.patch.object(diagnostics.sys, 'version_info', types.SimpleNamespace(major=3, minor=14)):
            hint = diagnostics.missing_python_header_hint()
        self.assertIn('缺少 Python.h', hint)
        self.assertIn('python3.14-devel', hint)
        self.assertIn('python3.14-dev', hint)
        self.assertIn('/nonexistent/include/python3.14', hint)

    def test_probe_silent_when_header_present(self):
        from ibus_voice_ime.asr import qwen_diagnostics as diagnostics
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'Python.h').write_text('')
            with mock.patch.object(diagnostics.sysconfig, 'get_path', return_value=directory):
                self.assertEqual(diagnostics.missing_python_header_hint(), '')


if __name__ == '__main__':
    unittest.main()
