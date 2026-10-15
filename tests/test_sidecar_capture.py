"""Real CPU-only producers; no model, desktop, service install or network."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import tracemalloc
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ibus_voice_ime.asr import sidecar_capture as capture
from ibus_voice_ime.asr import qwen_asr_runtime as runtime


class CaptureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / 'qwen-asr-server.log'
        self.processes = []

    def tearDown(self):
        for proc in self.processes:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=3)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream:
                    stream.close()
        self.temp.cleanup()

    def start(self, code, keep=5, path=None):
        proc = subprocess.Popen([sys.executable, capture.__file__, str(path or self.path), str(keep),
                                 sys.executable, '-u', '-c', code], stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
        self.processes.append(proc)
        return proc

    def snapshot(self):
        lock = self.path.with_name(self.path.name + '.capture-lock')
        if not lock.exists():
            return b''
        with lock.open('ab') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            return self.path.read_bytes() if self.path.exists() else b''

    def until(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(.005)
        self.fail('condition did not reach its synchronization barrier')

    def test_live_writer_twenty_rotations_and_transcribe(self):
        metadata = self.root / 'stdout-stat'
        code = f"import os,stat\nopen({str(metadata)!r},'w').write(str(stat.S_ISFIFO(os.fstat(1).st_mode)))\n" + """import os,sys
for command in sys.stdin:
    n=int(command)
    for i in range(n*20,(n+1)*20):
        os.write(1 if i%2 else 2, (str(i).zfill(6)+' '+ 'x'*160+'\\n').encode())
"""
        proc = self.start(code)
        for round_ in range(21):
            proc.stdin.write(f'{round_}\n'.encode()); proc.stdin.flush()
            last = (round_ + 1) * 20 - 1
            marker = f'{last:06d} '.encode()
            data = self.until(lambda: (d if marker in d else None) if (d := self.snapshot()) else None)
            ids = [int(line.split()[0]) for line in data.splitlines()]
            self.assertEqual(ids, list(range(last-len(ids)+1, last+1)))
            self.assertLessEqual(len(ids), 10)
            self.assertIsNone(proc.poll())
            # The producer's stdout/stderr are pipes, never deleted log files.
            self.assertEqual(metadata.read_text(), 'True')
            if round_ == 10:
                response = mock.MagicMock()
                response.__enter__.return_value.read.return_value = b'{"text":" fine "}'
                with mock.patch.object(runtime, 'ensure_server', return_value='http://localhost'), mock.patch.object(
                    runtime.voice_terms, 'build_asr_context', return_value=''
                ), mock.patch.object(runtime.urllib.request, 'urlopen', return_value=response), mock.patch.dict(
                    os.environ, {'VOICE_IME_LOG_DIR': str(self.root)}
                ):
                    self.assertEqual(runtime.transcribe('/fixture.wav'), 'fine')
                self.assertEqual(self.snapshot(), data)
        proc.stdin.close(); proc.stdin = None
        self.assertEqual(proc.wait(timeout=3), 0)

    def test_transcribe_does_not_rotate_external_open_writer(self):
        original = b''.join(str(i).encode()+b'x'*160+b'\n' for i in range(100))
        self.path.write_bytes(original)
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"text":"fine"}'
        with self.path.open('ab', buffering=0) as writer, mock.patch.object(
            runtime, 'ensure_server', return_value='http://localhost'
        ), mock.patch.object(runtime, '_PROCESS', None), mock.patch.object(
            runtime.voice_terms, 'build_asr_context', return_value=''
        ), mock.patch.object(runtime.urllib.request, 'urlopen', return_value=response), mock.patch.dict(
            os.environ, {'VOICE_IME_LOG_DIR': str(self.root), 'VOICE_IME_LOG_KEEP_LINES': '5'}
        ):
            self.assertEqual(runtime.transcribe('/fixture.wav'), 'fine')
            writer.write(b'after transcription\n')
            self.assertEqual(os.fstat(writer.fileno()).st_nlink, 1)
        self.assertEqual(self.path.read_bytes(), original + b'after transcription\n')

    def test_runtime_early_death_includes_flushed_diagnostic(self):
        real_popen = subprocess.Popen
        processes = []
        def spawn(argv, **kwargs):
            # Keep the actual supervisor command and replace only the expensive
            # model-server executable with a CPU fixture.
            argv = argv[:4] + [sys.executable, '-c', 'print("startup diagnostic",flush=True);raise SystemExit(2)']
            proc = real_popen(argv, **kwargs)
            processes.append(proc)
            self.processes.append(proc)
            return proc
        env = {'VOICE_IME_LOG_DIR': str(self.root), 'VOICE_IME_QWEN_ASR_PYTHON': sys.executable,
               'VOICE_IME_QWEN_ASR_MODEL_PATH': str(self.root), 'VOICE_IME_QWEN_ASR_START_TIMEOUT': '2'}
        with mock.patch.dict(os.environ, env), mock.patch.object(runtime, '_PROCESS', None), mock.patch.object(
            runtime, '_PROCESS_KEY', None
        ), mock.patch.object(runtime, '_server_matches', return_value=False), mock.patch.object(
            runtime, 'is_ready', return_value=False
        ), mock.patch.object(runtime.subprocess, 'Popen', side_effect=spawn):
            runtime._clear_failure()
            try:
                with self.assertRaisesRegex(RuntimeError, 'startup diagnostic'):
                    runtime.ensure_server()
                self.assertIsNotNone(processes[0].returncode)
            finally:
                runtime._clear_failure()

    def test_keep_zero_fragmented_utf8_and_final_partial(self):
        data = ('你好\n' * 3000 + '尾巴').encode()
        code = f'import os\ndata={data!r}\nfor i in range(0,len(data),7): os.write(1,data[i:i+7])'
        proc = self.start(code, keep=0)
        self.assertEqual(proc.wait(timeout=5), 0)
        self.assertEqual(self.path.read_bytes(), data)

    def test_partial_tail_bytes_are_preserved(self):
        sink = capture.Sink(self.path, 3)
        try:
            expected = b''.join(str(i).encode()+b'x'*160+b'\n' for i in range(20))+b'\xe4\xbd'
            sink.append(expected)
            self.assertEqual(self.path.read_bytes(), b''.join(expected.splitlines(keepends=True)[-3:]))
            sink.append(b'\xa0')
            self.assertTrue(self.path.read_bytes().endswith('你'.encode()))
        finally:
            sink.close()

    def test_concurrent_capture_owners_keep_zero(self):
        processes = [self.start(f'import os\nfor i in range(2000): os.write(1, ("{j}:"+str(i)+"\\n").encode())', keep=0) for j in range(3)]
        for proc in processes:
            self.assertEqual(proc.wait(timeout=5), 0)
        lines = self.path.read_text().splitlines()
        self.assertEqual(len(lines), 6000)
        for j in range(3):
            self.assertEqual([int(x.split(':')[1]) for x in lines if x.startswith(f'{j}:')], list(range(2000)))

    def test_concurrent_capture_owners_retained_suffix(self):
        processes = [self.start(f'import os\nfor i in range(5000): os.write(1, ("{j}:"+str(i)+" "+"x"*160+"\\n").encode())', keep=50) for j in range(3)]
        for proc in processes:
            self.assertEqual(proc.wait(timeout=8), 0)
        lines = self.path.read_text().splitlines()
        self.assertLessEqual(len(lines), 100)
        for j in range(3):
            ids = [int(x.split()[0].split(':')[1]) for x in lines if x.startswith(f'{j}:')]
            if ids:
                self.assertEqual(ids, list(range(ids[0], 5000)))

    def test_20mib_partial_line_bounded_working_memory(self):
        sink = capture.Sink(self.path, 5)
        tracemalloc.start()
        try:
            block = b'x' * capture.CHUNK
            for _ in range(320):
                sink.append(block)
            _, peak = tracemalloc.get_traced_memory()
            self.assertLess(peak, 1024 * 1024)
            self.assertEqual(self.path.stat().st_size, 20 * 1024 * 1024)
            print(f'20 MiB unterminated stream: peak Python allocation={peak} bytes')
        finally:
            tracemalloc.stop(); sink.close()

    def test_20mib_subprocess_backpressure_and_eof(self):
        code = 'import os\nblock=b"x"*255+b"\\n"\nfor _ in range(81920): os.write(1,block)\nos.write(2,b"final-marker\\n")'
        start = time.monotonic()
        proc = self.start(code, keep=100)
        self.assertEqual(proc.wait(timeout=20), 0)
        duration = time.monotonic() - start
        data = self.path.read_bytes()
        self.assertTrue(data.endswith(b'final-marker\n'))
        self.assertLessEqual(len(data.splitlines()), 200)
        print(f'20 MiB subprocess stream: elapsed={duration:.3f}s, retained={len(data)} bytes, EOF reaped')

    def test_spawn_failure_diagnostic_is_flushed(self):
        proc = subprocess.Popen([sys.executable, capture.__file__, str(self.path), '5', '/does-not-exist'], stderr=subprocess.PIPE)
        self.processes.append(proc)
        self.assertEqual(proc.wait(timeout=3), 1)
        self.assertIn(b'capture failed', self.path.read_bytes())

    def test_sink_failure_terminates_only_owned_child(self):
        pidfile = self.root / 'pid'
        go = self.root / 'go'
        code = f'import os,time\nopen({str(pidfile)!r},"w").write(str(os.getpid()))\nprint("ready",flush=True)\nwhile not os.path.exists({str(go)!r}): time.sleep(.01)\nwhile True: os.write(1,b"x"*65536)'
        external = subprocess.Popen(['sleep', '30'])
        self.processes.append(external)
        proc = self.start(code)
        self.until(lambda: b'ready' in self.snapshot())
        self.path.rename(self.root / 'previous.log')
        self.path.mkdir()
        go.touch()
        self.assertEqual(proc.wait(timeout=3), 1)
        self.assertIn(b'capture failed', proc.stderr.read())
        self.assertIsNone(external.poll())
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)

    def test_termination_escalates_only_owned_child(self):
        pidfile = self.root / 'pid'
        code = f'import os,time,signal\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\nopen({str(pidfile)!r},"w").write(str(os.getpid()))\nprint("ready",flush=True)\nwhile True: time.sleep(.01)'
        proc = self.start(code)
        self.until(lambda: b'ready' in self.snapshot())
        start = time.monotonic()
        proc.terminate()
        proc.wait(timeout=2)
        self.assertLess(time.monotonic()-start, 1.5)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)

    def test_runtime_shutdown_during_lock_contention_reaps_child(self):
        pidfile = self.root / 'pid'
        code = f'import os,time\nopen({str(pidfile)!r},"w").write(str(os.getpid()))\nprint("ready",flush=True)\nwhile True: os.write(1,b"next\\n"*2000)'
        proc = self.start(code)
        self.until(lambda: pidfile.exists() and self.path.exists() and self.path.stat().st_size > 0)
        with self.path.with_name(self.path.name + '.capture-lock').open('ab') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            runtime._terminate_process(proc)
        self.assertIsNotNone(proc.poll())
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)

    def test_slow_retention_cannot_orphan_sigterm_ignoring_child(self):
        pidfile = self.root / 'child'
        barrier = self.root / 'retaining'
        code = f'import os,time,signal\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\nopen({str(pidfile)!r},"w").write(str(os.getpid()))\nprint("ready",flush=True)\nwhile True: time.sleep(.01)'
        wrapper = f"""import sys,time
from pathlib import Path
sys.path.insert(0,{str(Path(capture.__file__).resolve().parents[2])!r})
from ibus_voice_ime.asr import sidecar_capture as c
original=c.Sink._retain
def delayed(self):
    if Path({str(pidfile)!r}).exists():
        Path({str(barrier)!r}).touch()
        time.sleep(4)
    return original(self)
c.Sink._retain=delayed
sys.exit(c.run(Path({str(self.path)!r}),5,[sys.executable,'-u','-c',{code!r}]))
"""
        proc = subprocess.Popen([sys.executable, '-c', wrapper], stderr=subprocess.PIPE, start_new_session=True)
        self.processes.append(proc)
        self.until(barrier.exists)
        runtime._terminate_process(proc)
        self.assertIsNotNone(proc.poll())
        try:
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pidfile.read_text()), 0)
        finally:
            try:
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_termination_and_descendant_pipe_are_bounded(self):
        pidfile = self.root / 'descendant'
        code = f'import subprocess\np=subprocess.Popen(["sleep","30"])\nopen({str(pidfile)!r},"w").write(str(p.pid))\nprint("final diagnostic",flush=True)'
        proc = self.start(code)
        try:
            self.assertEqual(proc.wait(timeout=3), 0)
            self.assertIn(b'final diagnostic', self.path.read_bytes())
        finally:
            if pidfile.exists():
                os.kill(int(pidfile.read_text()), signal.SIGTERM)

    def test_engine_abrupt_exit_leaves_capture_alive(self):
        go = self.root / 'go'
        pidfile = self.root / 'supervisor'
        code = f'import os,time\nprint("before",flush=True)\nwhile not os.path.exists({str(go)!r}): time.sleep(.01)\nprint("after",flush=True)'
        args = [sys.executable, capture.__file__, str(self.path), '5', sys.executable, '-u', '-c', code]
        parent = subprocess.run([sys.executable, '-c', f'import subprocess,os\np=subprocess.Popen({args!r},start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\nopen({str(pidfile)!r},"w").write(str(p.pid))\nos._exit(0)'], timeout=3)
        self.assertEqual(parent.returncode, 0)
        try:
            self.until(lambda: b'before' in self.snapshot())
            go.touch()
            self.until(lambda: b'after' in self.snapshot())
        finally:
            try:
                os.kill(int(pidfile.read_text()), signal.SIGTERM)
            except ProcessLookupError:
                pass


if __name__ == '__main__':
    unittest.main()
