"""Offline regression tests for the pip install network/proxy/mirror contract
fixed in commit 8c31555 (scripts/setup-qwen-asr.sh + scripts/net-probe.sh).

These complement tests/test_qwen_setup.py (which already covers venv REUSE /
incremental install / explicit-index no-switch / SOCKS-advice basics) with the
remaining acceptance scenarios: live-HTTP-proxy passthrough, dead proxy port,
socks-with-PySocks, full SOCKS PySocks bootstrap over a loopback socks5 relay,
residual ALL_PROXY reporting, pip.conf isolation, mirror/official reachability
gating of the controlled fallback, and idempotent resume after interruption.

Isolation is identical to tests/test_qwen_setup.py: temporary repo copy, fake
GPU/python/pip, loopback-only (127.0.0.1) fake index / mock proxy / socks5
relay. No external network, no real HOME/systemd/IBus/GPU.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]


def pinned_qwen_asr_version() -> str:
    """The qwen-asr pin drives pip stderr fixtures; never hardcode it."""
    for raw in (ROOT / 'requirements-qwen-asr.txt').read_text().splitlines():
        match = re.match(r'\s*qwen[-_]asr\s*==\s*([0-9A-Za-z.+_-]+)', raw)
        if match:
            return match.group(1)
    raise AssertionError('qwen-asr pin not found in requirements-qwen-asr.txt')


V = pinned_qwen_asr_version()
TUNA = 'https://pypi.tuna.tsinghua.edu.cn/simple'
PYPI = 'https://pypi.org/simple'


class QwenPipNetworkTest(unittest.TestCase):
    """Run the real installer (repo copy) with fake GPU/interpreter/pip and
    loopback-only index/proxy/socks servers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='qwen-pip-net-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'scripts').mkdir()
        for name in ('setup-qwen-asr.sh', 'net-probe.sh', 'env-file-load.sh',
                     'setup-qwen-python.py'):
            shutil.copy2(ROOT / 'scripts' / name, self.root / 'scripts' / name)
        shutil.copy2(ROOT / 'requirements-qwen-asr.txt',
                     self.root / 'requirements-qwen-asr.txt')
        (self.root / 'src').symlink_to(ROOT / 'src')
        self.log = self.root / 'events'
        self.target = self.root / 'target-python'
        self.home = self.root / 'home'
        self.home.mkdir()
        self.venv = self.root / '.venv-qwen-asr'
        # Proxy/PIP environment fully controlled: strip every proxy-ish and
        # PIP_* variable the host shell might carry, then inject per scenario.
        self.env = {key: value for key, value in os.environ.items()
                    if not key.lower().endswith('_proxy')
                    and not key.startswith(('PIP_', 'VOICE_IME_', 'PYTHONPATH', 'XDG_'))}
        self.env.update(HOME=str(self.home), FAKE_ROOT=str(self.root),
                        VOICE_IME_QWEN_ASR_SETUP_PYTHON=str(self.target),
                        VOICE_IME_CONFIG=str(self.home / 'config.json'))
        self.make_python(self.target, 12)
        self.make_script(self.root / 'scripts/gpu-probe.sh',
                         "#!/bin/sh\nprintf 'GPU_STATE=%s\\n' \"${FAKE_GPU:-ok}\"\nprintf 'GPU_BRIEF=fake-gpu\\n'\nprintf 'GPU_REASON=fake\\nGPU_ACTION=repair\\n'\n")
        self.make_script(self.root / 'scripts/switch-qwen-asr.sh',
                         "#!/bin/sh\nif [ -e /proc/$$/fd/9 ]; then echo lock-leaked >> \"$FAKE_ROOT/events\"; fi\necho activate >> \"$FAKE_ROOT/events\"\n")
        self.make_script(self.root / 'scripts/qwen-preflight.py', f'''#!{sys.executable}
import os, pathlib, sys
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

    # ------------------------------------------------------------------ fakes
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
    cli.write_text('#!{sys.executable}\\nimport pathlib,sys,os\\na=sys.argv\\np=pathlib.Path(a[a.index("--local_dir")+1]);p.mkdir(parents=True,exist_ok=True)\\n(p/"config.json").write_text("{{}}");(p/"model.safetensors").write_bytes(b"fixture")\\nwith (pathlib.Path(os.environ["FAKE_ROOT"])/"events").open("a") as f:f.write("download\\\\n")\\n')
    cli.chmod(0o755)
    with (root / 'events').open('a') as f: f.write('create-venv\\n')
    sys.exit(0)
if a[:2] == ['-m', 'pip']:
    rest = a[2:]
    if rest[:1] != ['--isolated'] and '--version' in rest:
        sys.exit(0)  # venv structure probe (python -m pip --version)
    assert rest[0] == '--isolated', rest
    rest = rest[1:]
    url = rest[rest.index('--index-url') + 1] if '--index-url' in rest else ''
    pypi = 'pypi.org' in url
    rc = 0
    versions = ''
    if 'index' in rest and 'versions' in rest:
        # Index visibility probe; per-URL injection so the official-index probe
        # (fallback condition 3) can be failed independently of the mirror.
        versions = os.environ.get('FAKE_PIP_INDEX_VERSIONS_PYPI' if pypi else 'FAKE_PIP_INDEX_VERSIONS', '')
        if os.environ.get('FAKE_PIP_INDEX_RC_PYPI' if pypi else 'FAKE_PIP_INDEX_RC') == '1':
            key = 'FAKE_PIP_INDEX_STDERR_PYPI' if pypi else 'FAKE_PIP_INDEX_STDERR'
            fallback_msg = 'ERROR: Could not fetch URL ' + url + ': connection error'
            print(os.environ.get(key, fallback_msg), file=sys.stderr)
            rc = 1
    elif '-r' in rest:
        fail_url = os.environ.get('FAKE_PIP_FAIL_URL', '')
        if fail_url and fail_url in url:
            default = ('ERROR: Could not find a version that satisfies the requirement '
                       'qwen-asr=={V} (from versions: 0.0.1, 0.0.5)')
            print(os.environ.get('FAKE_PIP_FAIL_STDERR', default), file=sys.stderr)
            rc = 1
    elif '--upgrade' in rest:
        if os.environ.get('FAKE_PIP_UPGRADE_FAIL') == '1':
            rc = 1
    with (root / 'events').open('a') as f:
        f.write('pip:' + ' '.join(rest) + '\\n')
        # Record the environment the pip subprocess actually inherited — the
        # assertion basis for proxy passthrough and PIP_CONFIG_FILE isolation.
        bits = ','.join('%s=%s' % (k, os.environ.get(k, '')) for k in
                        ('http_proxy', 'https_proxy', 'HTTPS_PROXY', 'all_proxy',
                         'ALL_PROXY', 'PIP_CONFIG_FILE', 'PIP_INDEX_URL'))
        f.write('pipenv:' + bits + '\\n')
    if '--no-index' in rest:
        (root / 'pysocks-installed').write_text('1')  # PySocks wheel "installed"
    if rc == 0 and versions and 'index' in rest:
        print('qwen-asr (' + versions.split(', ')[-1] + ')')
        print('Available versions: ' + versions)
    sys.exit(rc)
if a == ['-V']: print('Python 3.{minor}.0')
''')

    def seed_old_venv(self, minor=14):
        self.make_python(self.venv / 'bin/python', minor)
        self.make_script(self.venv / 'bin/pip', '#!/bin/sh\nexit 0\n')
        (self.venv / 'original-marker').write_text('preserve me')

    def seed_models(self):
        for size in ('0.6B', '1.7B'):
            model = self.root / 'vendor/models/qwen3-asr' / f'Qwen3-ASR-{size}'
            model.mkdir(parents=True)
            (model / 'config.json').write_text('{}')
            (model / 'model.safetensors').write_bytes(b'fixture')

    def run_setup(self, *arguments, **environment):
        env = dict(self.env, **environment)
        return subprocess.run(['bash', str(self.root / 'scripts/setup-qwen-asr.sh'), *arguments],
                              env=env, capture_output=True, text=True, timeout=60)

    def events(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def install_events(self):
        return [e for e in self.events() if e.startswith('pip:install -r')]

    def pipenv_events(self):
        return [e for e in self.events() if e.startswith('pipenv:')]

    # ---------------------------------------------------------- loopback fake net
    @staticmethod
    def start_http(handler):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    @staticmethod
    def http_200_handler(hits):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                body = b'ok'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_HEAD = do_GET

            def log_message(self, *args):
                pass
        return Handler

    @staticmethod
    def start_tcp_listener():
        # Bare TCP listener: enough for proxy_tcp_probe (connect-only) checks.
        state = {'server': None}

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                pass

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        server = Server(('127.0.0.1', 0), Handler)
        state['server'] = server
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    @staticmethod
    def start_socks5_relay(connects):
        # Minimal SOCKS5 server: no-auth greeting + CONNECT, then blind relay
        # to the (loopback) target. curl uses it natively for the PySocks
        # bootstrap path, exactly like the real thing would.

        class Handler(socketserver.BaseRequestHandler):
            def _recv_exact(self, count):
                buf = b''
                while len(buf) < count:
                    chunk = self.request.recv(count - len(buf))
                    if not chunk:
                        raise EOFError('socks peer closed early')
                    buf += chunk
                return buf

            def handle(self):
                sock = self.request
                try:
                    header = self._recv_exact(2)
                    self._recv_exact(header[1])            # offered methods
                    sock.sendall(b'\x05\x00')              # no auth required
                    ver, cmd, _rsv, atyp = self._recv_exact(4)
                    if cmd != 1:
                        raise ValueError('only CONNECT supported')
                    if atyp == 1:
                        host = socket.inet_ntoa(self._recv_exact(4))
                    elif atyp == 3:
                        length = self._recv_exact(1)[0]
                        host = self._recv_exact(length).decode()
                    else:
                        host = socket.inet_ntop(socket.AF_INET6, self._recv_exact(16))
                    port = int.from_bytes(self._recv_exact(2), 'big')
                    remote = socket.create_connection((host, port), timeout=15)
                except Exception:
                    try:
                        sock.sendall(b'\x05\x01\x00\x01' + b'\x00' * 6)
                    except Exception:
                        pass
                    return
                connects.append((host, port))
                sock.sendall(b'\x05\x00\x00\x01' + b'\x00' * 6)

                def pump(source, sink):
                    try:
                        while True:
                            data = source.recv(65536)
                            if not data:
                                break
                            sink.sendall(data)
                    except Exception:
                        pass
                    try:
                        sink.shutdown(socket.SHUT_WR)
                    except Exception:
                        pass

                relay = threading.Thread(target=pump, args=(sock, remote), daemon=True)
                relay.start()
                pump(remote, sock)
                relay.join(timeout=10)

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        server = Server(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    # ================================================================ scenario 1
    def test_s1_mirror_has_pinned_version_no_fallback(self):
        # 清华镜像（fake 索引 = fake venv pip 探测）正常且指定版本存在：
        # 预检报告“在索引可见”，安装一次完成，绝不触发受控回退。
        result = self.run_setup(FAKE_PIP_INDEX_VERSIONS=f'0.0.1, {V}')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        combined = result.stdout + result.stderr
        self.assertIn(f'目标版本 {V} 在索引可见', combined)
        self.assertIn(TUNA, result.stdout)
        installs = self.install_events()
        self.assertEqual(len(installs), 1, installs)
        self.assertIn(TUNA, installs[0])
        for banned in ('受控回退', '官方源重试成功', '未回退官方源'):
            self.assertNotIn(banned, combined)

    # ================================================================ scenario 2
    def test_s2_precheck_reports_missing_version_on_reachable_mirror(self):
        # 镜像可达（探测拿到版本列表）但缺指定版本：预检结论分类为“索引页有效
        # 但缺该版本”，仅警告不阻断，安装继续由 pip 退出码决定。
        result = self.run_setup(FAKE_PIP_INDEX_VERSIONS='0.0.1, 0.0.5')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        combined = result.stdout + result.stderr
        self.assertIn(f'索引页有效但缺 {V}', combined)
        self.assertIn('确定性：实证', combined)
        self.assertIn('仅是预检诊断，不阻断安装', combined)
        self.assertEqual(len(self.install_events()), 1)
        self.assertNotIn('受控回退', combined)

    # ================================================================ scenario 3
    def test_s3_versions_none_with_probe_confirmed_gap_falls_back_once(self):
        # 受控回退条件②的另一半：失败签名是 versions:none（与劫持同形），主动
        # 探测确认“页面有效缺版本”后才回退——第二次安装调用使用官方 index-url，
        # 决策醒目打印，且只重试一次。
        result = self.run_setup(
            FAKE_PIP_FAIL_URL='tuna',
            FAKE_PIP_FAIL_STDERR=(
                f'ERROR: Could not find a version that satisfies the requirement '
                f'qwen-asr=={V} (from versions: none)\n'
                f'ERROR: No matching distribution found for qwen-asr=={V}'),
            FAKE_PIP_INDEX_VERSIONS='0.0.1, 0.0.5',       # 镜像探测：缺目标版本
            FAKE_PIP_INDEX_VERSIONS_PYPI=f'0.0.1, {V}')   # 官方探测：版本可见
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        installs = self.install_events()
        self.assertEqual(len(installs), 2, installs)
        self.assertIn(TUNA, installs[0])
        self.assertIn(PYPI, installs[1])
        combined = result.stdout + result.stderr
        self.assertIn('受控回退', combined)
        self.assertIn('决策原因', combined)
        self.assertIn('探测确认索引页有效但缺该版本', combined)
        self.assertIn('重试一次', combined)
        self.assertIn('官方源重试成功', combined)

    # ================================================================ scenario 4
    def test_s4a_mirror_and_official_unreachable_network_class_no_fallback(self):
        # 官方源与镜像都不可达（无代理，直连失败）：不回退、诊断“网络连接类”、
        # 非零退出。versions:none 与网络失败同形时必须靠探测消歧为网络问题，
        # 而不是误判镜像缺包去换源。
        conn = ('ERROR: Could not fetch URL https://pypi.tuna.tsinghua.edu.cn/simple/qwen-asr/: '
                'connection error\n'
                'WARNING: Retrying (1/2 after 5 seconds) ... NewConnectionError')
        result = self.run_setup(
            FAKE_PIP_FAIL_URL='tuna',
            FAKE_PIP_FAIL_STDERR=conn + f'\nERROR: No matching distribution found for qwen-asr=={V} (from versions: none)',
            FAKE_PIP_INDEX_RC='1', FAKE_PIP_INDEX_STDERR=conn)
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertEqual(len(self.install_events()), 1)
        self.assertIn('未回退官方源', combined)
        self.assertIn('换源无效', combined)
        self.assertIn('▶ 分类：网络连接类', combined)
        self.assertNotIn('受控回退：', combined)

    def test_s4b_mirror_lag_signature_but_official_unreachable_never_falls_back(self):
        # 回退条件③独立把关：失败签名是“列出旧版本”（真镜像滞后），但官方源
        # 探测不可见（连接失败）→ 回退无意义，绝不回退、非零退出。
        conn = ('ERROR: Could not fetch URL https://pypi.org/simple/qwen-asr/: connection error\n'
                'WARNING: Retrying (1/2 after 5 seconds) ... NewConnectionError')
        result = self.run_setup(
            FAKE_PIP_FAIL_URL='tuna',
            FAKE_PIP_FAIL_STDERR=(
                f'ERROR: Could not find a version that satisfies the requirement '
                f'qwen-asr=={V} (from versions: 0.0.1, 0.0.5)'),
            FAKE_PIP_INDEX_VERSIONS='0.0.1, 0.0.5',
            FAKE_PIP_INDEX_RC_PYPI='1', FAKE_PIP_INDEX_STDERR_PYPI=conn)
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertEqual(len(self.install_events()), 1)
        self.assertIn('官方源探测未确认', combined)
        self.assertIn('回退无意义', combined)
        self.assertNotIn('受控回退：', combined)

    # ================================================================ scenario 5
    def test_s5_http_proxy_alive_env_passed_to_pip_without_misdiagnosis(self):
        # HTTP 代理正常（回环 mock，端口在听）：安装子进程如实继承代理变量、
        # 无“无法连接代理”警告、无 SOCKS/代理类误诊、不触发回退。
        proxy_hits: list[str] = []
        proxy = self.start_http(self.http_200_handler(proxy_hits))
        self.addCleanup(proxy.shutdown)
        proxy_url = f'http://127.0.0.1:{proxy.server_port}'
        result = self.run_setup(http_proxy=proxy_url, https_proxy=proxy_url,
                                HTTP_PROXY=proxy_url, HTTPS_PROXY=proxy_url)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        combined = result.stdout + result.stderr
        self.assertIn(f'来源 $https_proxy', combined)
        envs = self.pipenv_events()
        self.assertTrue(envs)
        for line in envs:
            self.assertIn(f'https_proxy={proxy_url}', line)
            self.assertIn(f'PIP_CONFIG_FILE=/dev/null', line)
        installs = self.install_events()
        self.assertEqual(len(installs), 1, installs)
        self.assertIn(TUNA, installs[0])
        for banned in ('无法连接该代理', '缺 PySocks', 'SOCKS 支持缺失',
                       '▶ 分类：代理类', '受控回退', '未回退官方源'):
            self.assertNotIn(banned, combined)

    # ================================================================ scenario 6
    def test_s6_dead_http_proxy_port_precheck_warning_and_proxy_class(self):
        # HTTP 代理地址无服务监听（127.0.0.1:1 死端口）：预检 TCP 探测警告 +
        # pip 失败后分类“代理类”，不回退（签名非版本不可用）、非零退出。
        result = self.run_setup(
            https_proxy='http://127.0.0.1:1',
            FAKE_PIP_FAIL_URL='tuna',
            FAKE_PIP_FAIL_STDERR=(
                'ERROR: Could not fetch URL https://pypi.tuna.tsinghua.edu.cn/simple/qwen-asr/: '
                'connection error\n'
                "WARNING: Retrying (1/2 after 5 seconds) ... "
                "ProxyError('Cannot connect to proxy.')"))
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn('无法连接该代理', combined)
        self.assertIn('不是包不存在，而是所有请求都经过这个不可用的代理', combined)
        self.assertIn('▶ 分类：代理类', combined)
        self.assertEqual(len(self.install_events()), 1)
        self.assertIn('未回退官方源', combined)
        self.assertNotIn('受控回退：', combined)

    # ================================================================ scenario 7
    def test_s7_socks5_proxy_with_pysocks_installs_without_bootstrap(self):
        # SOCKS5 代理配置存在且 venv 依赖完整（fake venv python 可 import
        # socks）：如实报告 socks 有效代理，安装正常进行，不触发自举。
        listener = self.start_tcp_listener()
        self.addCleanup(listener.shutdown)
        socks_url = f'socks5://127.0.0.1:{listener.server_address[1]}'
        result = self.run_setup(ALL_PROXY=socks_url)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        combined = result.stdout + result.stderr
        self.assertIn(f'有效代理：{socks_url}', combined)
        self.assertIn('协议 socks5', combined)
        self.assertIn('来源 $ALL_PROXY', combined)
        self.assertIn('venv 已有 PySocks', combined)
        self.assertNotIn('受控自举', combined)
        self.assertFalse([e for e in self.events() if '--no-index' in e])
        self.assertEqual(len(self.install_events()), 1)
        for banned in ('无法连接该代理', '缺 PySocks', '受控回退'):
            self.assertNotIn(banned, combined)

    # ================================================================ scenario 8
    # 历史 BUG（P1，已修复）：ensure_pip_socks_support 的 mktemp 曾缺 `-d`
    # （建普通文件非目录，curl -o 恒 curl(23) 失败，自举在任何环境走不通）。
    # 本测试断言修复后的完整自举契约：SOCKS5 代理 + venv 缺 PySocks → 诊断 +
    # curl 经 socks 从当前索引取 PySocks wheel，--no-index 装进 venv，随后正常安装。
    def test_s8_socks5_missing_pysocks_bootstrap_completes_via_loopback_socks(self):
        # SOCKS5 代理 + venv 缺 PySocks：诊断 + 受控自举端到端成功——curl 走
        # （回环 socks5 relay 扮演的）socks 代理从当前索引取 PySocks wheel，
        # --no-index 装进 venv，随后正常安装。
        index_hits: list[str] = []
        socks_connects: list[tuple[str, int]] = []

        class IndexHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                index_hits.append(self.path)
                if self.path == '/simple/pysocks/':
                    body = (b'<a href="/packages/PySocks-1.2.4-py3-none-any.whl'
                            b'#sha256=deadbeef">PySocks-1.2.4-py3-none-any.whl</a><br/>')
                    self.send_response(200)
                elif self.path.startswith('/packages/'):
                    body = b'PK-fake-pysocks-wheel'
                    self.send_response(200)
                else:
                    self.send_error(404, 'not found')
                    return
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        index = ThreadingHTTPServer(('127.0.0.1', 0), IndexHandler)
        threading.Thread(target=index.serve_forever, daemon=True).start()
        self.addCleanup(index.shutdown)
        socks = self.start_socks5_relay(socks_connects)
        self.addCleanup(socks.shutdown)
        index_url = f'http://127.0.0.1:{index.server_port}/simple'
        socks_url = f'socks5://127.0.0.1:{socks.server_address[1]}'

        result = self.run_setup(VOICE_IME_PIP_INDEX_URL=index_url, ALL_PROXY=socks_url,
                                FAKE_VENV_NO_PYSOCKS='1',
                                FAKE_PIP_INDEX_VERSIONS=f'0.0.1, {V}')
        combined = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, combined)
        # 诊断先行：检测到 socks 且缺 PySocks
        self.assertIn('检测到 socks 代理', combined)
        self.assertIn('缺 PySocks', combined)
        # 自举路径完整走通：simple 页 + wheel 都经 socks relay 拉取
        self.assertIn('受控自举', combined)
        self.assertTrue(socks_connects, 'curl 未经 socks 代理发起请求')
        self.assertTrue(any('/simple/pysocks/' in hit for hit in index_hits), index_hits)
        self.assertTrue(any(hit.startswith('/packages/') for hit in index_hits), index_hits)
        self.assertIn('✓ PySocks 已装入项目 venv', combined)
        self.assertTrue([e for e in self.events() if '--no-index' in e])
        installs = self.install_events()
        self.assertEqual(len(installs), 1, installs)
        self.assertIn(index_url, installs[0])

    # ================================================================ scenario 9
    def test_s9_residual_uppercase_all_proxy_reported_as_socks(self):
        # 故障 B 回归锚点：用户已 unset http_proxy/https_proxy 四变量、仅残留
        # ALL_PROXY=socks5://… ——有效代理被如实报告为 socks（来源 $ALL_PROXY），
        # 绝无“pip 直连”假阴性；缺 PySocks 时诊断 + 自举建议指向 unset ALL_PROXY。
        result = self.run_setup(ALL_PROXY='socks5://127.0.0.1:1',
                                FAKE_VENV_NO_PYSOCKS='1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        combined = result.stdout + result.stderr
        self.assertIn('ALL_PROXY=socks5://127.0.0.1:1', combined)
        self.assertIn('协议 socks5', combined)
        self.assertIn('来源 $ALL_PROXY', combined)
        self.assertNotIn('均未设置', combined)
        self.assertNotIn('将直连', combined)
        self.assertIn('缺 PySocks', combined)
        self.assertIn('受控自举', combined)
        self.assertIn('unset ALL_PROXY', combined)

    # =============================================================== scenario 10
    def test_s10_pip_conf_fully_isolated_from_pip_subprocesses(self):
        # 代理/索引投毒全部来自 pip.conf（甚至用户 shell 已 export
        # PIP_CONFIG_FILE 指向它）：安装路径必须以 PIP_CONFIG_FILE=/dev/null
        # 隔离——pip 子进程看到的配置文件是 /dev/null、索引仍是 --index-url
        # 参数、代理环境里没有 pip.conf 的代理。
        pip_conf = self.home / '.config/pip/pip.conf'
        pip_conf.parent.mkdir(parents=True)
        pip_conf.write_text('[global]\nproxy = http://127.0.0.1:9\n'
                            'index-url = http://127.0.0.9/simple\n')
        result = self.run_setup(PIP_CONFIG_FILE=str(pip_conf))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        combined = result.stdout + result.stderr
        installs = self.install_events()
        self.assertEqual(len(installs), 1, installs)
        self.assertIn(TUNA, installs[0])
        self.assertNotIn('127.0.0.9', combined)
        envs = self.pipenv_events()
        self.assertTrue(envs)
        for line in envs:
            self.assertIn('PIP_CONFIG_FILE=/dev/null', line)
            self.assertNotIn(str(pip_conf), line)
            self.assertNotIn('http_proxy=http://127.0.0.1:9', line)
            self.assertNotIn('https_proxy=http://127.0.0.1:9', line)
        # pip.conf 的代理不进入代理环境报告（8 变量口径），也不引发死代理警告
        self.assertIn('8 个代理相关变量均未设置', combined)
        self.assertNotIn('无法连接该代理', combined)

    # =============================================================== scenario 11
    def test_s11_pip_index_rc0_empty_output_falls_back_to_urllib(self):
        # P2-2 回归：fake pip `index versions` rc==0 且空输出 → 必须按承诺降级
        # urllib 拉 simple 页分类（此前停留在「无法归类（推断）」）。回环 fake
        # 索引页含钉定版本；FAKE_URL_PROBE_REAL=1 让 fake venv python 真实执行
        # 安装器传入的 urllib 探测程序（仅指向 127.0.0.1，零外网）。
        page = (f'<a href="../../packages/qwen_asr-{V}-py3-none-any.whl#sha256=abc">'
                f'qwen_asr-{V}-py3-none-any.whl</a>')

        class IndexHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/simple/qwen-asr/':
                    body = page.encode()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_error(404, 'not found')

            def log_message(self, *args):
                pass
        index = ThreadingHTTPServer(('127.0.0.1', 0), IndexHandler)
        threading.Thread(target=index.serve_forever, daemon=True).start()
        self.addCleanup(index.shutdown)
        result = self.run_setup(VOICE_IME_PIP_INDEX_URL=f'http://127.0.0.1:{index.server_port}/simple',
                                FAKE_URL_PROBE_REAL='1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        combined = result.stdout + result.stderr
        self.assertIn(f'目标版本 {V} 在索引可见', combined)
        self.assertIn('已降级 urllib', combined)
        self.assertIn('确定性：实证', combined)
        self.assertNotIn('无法归类', combined)
        installs = self.install_events()
        self.assertEqual(len(installs), 1, installs)

    # =============================================================== scenario 12
    def test_s12_pip_index_rc0_empty_urllib_garbage_page_classified(self):
        # P2-2 的降级分类覆盖：urllib 拉到的 simple 页是垃圾页（200 非 simple
        # 内容）→ 降级后结论为「索引页返回非 simple 内容」（实证），不再无法归类。
        class GarbageHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'<html>captive portal login</html>'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass
        index = ThreadingHTTPServer(('127.0.0.1', 0), GarbageHandler)
        threading.Thread(target=index.serve_forever, daemon=True).start()
        self.addCleanup(index.shutdown)
        result = self.run_setup(VOICE_IME_PIP_INDEX_URL=f'http://127.0.0.1:{index.server_port}/simple',
                                FAKE_URL_PROBE_REAL='1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        combined = result.stdout + result.stderr
        self.assertIn('索引页返回非 simple 内容', combined)
        self.assertIn('已降级 urllib', combined)
        self.assertIn('确定性：实证', combined)
        self.assertIn('仅是预检诊断，不阻断安装', combined)

    # =============================================================== scenario 13
    def test_s13_interrupted_install_resumes_idempotently_without_rebuild(self):
        # 中途取消/失败（fake pip 首次安装被中断）后再次安装：幂等续装成功，
        # venv 不被反复重建（无 create-venv、无 .bak-*/.failed-* 留档）。
        self.seed_old_venv(minor=12)
        self.make_script(self.venv / 'bin/modelscope', '#!/bin/sh\nexit 1\n')
        self.seed_models()
        first = self.run_setup(
            FAKE_REUSE_RC='3', FAKE_PIP_FAIL_URL='tuna',
            FAKE_PIP_FAIL_STDERR='ERROR: Operation cancelled by user (KeyboardInterrupt)\n'
                                 'Killed')
        self.assertNotEqual(first.returncode, 0)
        combined_first = first.stdout + first.stderr
        self.assertIn('▶ 分类：中断类', combined_first)
        self.assertIn('增量补装', combined_first)
        self.assertNotIn('create-venv', self.events())
        self.assertTrue((self.venv / 'original-marker').exists())
        self.assertFalse(list(self.root.glob('.venv-qwen-asr.bak-*')))
        self.assertFalse(list(self.root.glob('.venv-qwen-asr.failed-*')))
        # 第二次运行（网络/中断恢复）：同 venv 增量续装成功
        self.log.unlink(missing_ok=True)
        second = self.run_setup(FAKE_REUSE_RC='3')
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn('增量补装', second.stdout)
        self.assertNotIn('create-venv', self.events())
        installs = self.install_events()
        self.assertEqual(len(installs), 1, installs)
        self.assertTrue((self.venv / 'original-marker').exists())
        self.assertFalse(list(self.root.glob('.venv-qwen-asr.bak-*')))
        self.assertFalse(list(self.root.glob('.venv-qwen-asr.failed-*')))


class NetProbeNetworkTest(unittest.TestCase):
    """net-probe.sh --analyze classification with loopback fake index only."""

    @staticmethod
    def start_index(page):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/simple/qwen-asr/':
                    body = page.encode()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_error(404, 'not found')

            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    @staticmethod
    def start_mock_proxy(status):
        # 回环 HTTP 代理：对任何请求回固定状态码（4xx 劫持 / 5xx 网关错误）。
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'x'
                self.send_response(status)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_HEAD = do_GET

            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='net-probe-net-test-')
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'home'
        self.home.mkdir()

    def probe_env(self, **overrides):
        env = {key: value for key, value in os.environ.items()
               if not key.lower().endswith('_proxy')
               and not key.startswith(('PIP_', 'VOICE_IME_', 'XDG_', 'PYTHONPATH'))}
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

    def test_s2_analyze_reachable_mirror_missing_version_classified(self):
        # 镜像可达（有效 simple 页）但缺指定版本：versions:none 主动探测确认
        # 后分类“包或版本不可用”（页面有效但缺目标版本，实证），建议换官方源。
        index = self.start_index(
            '<a href="../../packages/qwen_asr-0.0.5-py3-none-any.whl#sha256=abc">'
            'qwen_asr-0.0.5-py3-none-any.whl</a>')
        self.addCleanup(index.shutdown)
        err = self.write_stderr(
            f'ERROR: Could not find a version that satisfies the requirement '
            f'qwen-asr=={V} (from versions: none)\n'
            f'ERROR: No matching distribution found for qwen-asr=={V}')
        result = self.run_probe('--analyze', err,
                                '--index-url', f'http://127.0.0.1:{index.server_port}/simple',
                                '--package', 'qwen-asr', '--version', V, '--stage', 'deps')
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn('▶ 分类：包或版本不可用', out)
        self.assertIn('页面有效但缺目标版本', out)
        self.assertIn('实证', out)
        self.assertIn('VOICE_IME_PIP_INDEX_URL=default', out)

    def test_s11_dead_proxy_connection_failure_never_claimed_content_hijack(self):
        # P2-1 回归：经代理探测在「连接层」失败（all_proxy=socks5://死端口 →
        # HTTP 000/network-error）而直连可见目标版本——必须归因为「代理不可达/
        # 连接失败」（仍属代理类），不得断言「内容劫持」（内容级吞掉）；双复现
        # 命令（直连 + 经代理）结构与措辞风格保持。
        index = self.start_index(
            f'<a href="../../packages/qwen_asr-{V}-py3-none-any.whl#sha256=abc">'
            f'qwen_asr-{V}-py3-none-any.whl</a>')
        self.addCleanup(index.shutdown)
        err = self.write_stderr(
            f'ERROR: Could not find a version that satisfies the requirement '
            f'qwen-asr=={V} (from versions: none)\n'
            f'ERROR: No matching distribution found for qwen-asr=={V}')
        result = self.run_probe('--analyze', err,
                                '--index-url', f'http://127.0.0.1:{index.server_port}/simple',
                                '--package', 'qwen-asr', '--version', V, '--stage', 'deps',
                                all_proxy='socks5://127.0.0.1:1')
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn('▶ 分类：代理类 —— 代理不可达/连接失败', out)
        self.assertIn('连接层失败', out)
        self.assertIn('不是内容级', out)
        self.assertIn('换镜像无法解决', out)
        self.assertNotIn('内容劫持', out)
        # 直连 vs 经代理两条对比复现命令缺一不可（来源变量指向 all_proxy）。
        self.assertIn("--noproxy '*'", out)
        self.assertIn('-x "$all_proxy"', out)

    def test_s12_content_level_403_via_proxy_still_hijack(self):
        # P2-1 反向锚点：内容层异常（经代理 403、直连可见目标版本）仍判
        # 「内容劫持（实证）」——分层归因不得把真劫持弱化成连接失败。
        index = self.start_index(
            f'<a href="../../packages/qwen_asr-{V}-py3-none-any.whl#sha256=abc">'
            f'qwen_asr-{V}-py3-none-any.whl</a>')
        self.addCleanup(index.shutdown)

        class Proxy403(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'Forbidden'
                self.send_response(403)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass
        proxy = ThreadingHTTPServer(('127.0.0.1', 0), Proxy403)
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.shutdown)
        err = self.write_stderr(
            f'ERROR: Could not find a version that satisfies the requirement '
            f'qwen-asr=={V} (from versions: none)\n'
            f'ERROR: No matching distribution found for qwen-asr=={V}')
        result = self.run_probe('--analyze', err,
                                '--index-url', f'http://127.0.0.1:{index.server_port}/simple',
                                '--package', 'qwen-asr', '--version', V, '--stage', 'deps',
                                http_proxy=f'http://127.0.0.1:{proxy.server_port}')
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn('▶ 分类：代理类 —— 内容劫持（实证）', out)
        self.assertIn('内容级', out)
        self.assertNotIn('代理不可达/连接失败', out)

    def test_s13_via_proxy_5xx_gateway_error_not_content_hijack(self):
        # RT2 P2-A 回归：经代理 5xx（502）且直连可见目标版本 → 「代理网关错误/
        # 上游不可达（实证）」——代理侧故障，不是内容级吞掉；不得出现
        # 「内容劫持」字样，修复建议指向代理服务。
        index = self.start_index(
            f'<a href="../../packages/qwen_asr-{V}-py3-none-any.whl#sha256=abc">'
            f'qwen_asr-{V}-py3-none-any.whl</a>')
        self.addCleanup(index.shutdown)
        proxy = self.start_mock_proxy(502)
        self.addCleanup(proxy.shutdown)
        err = self.write_stderr(
            f'ERROR: Could not find a version that satisfies the requirement '
            f'qwen-asr=={V} (from versions: none)\n'
            f'ERROR: No matching distribution found for qwen-asr=={V}')
        result = self.run_probe('--analyze', err,
                                '--index-url', f'http://127.0.0.1:{index.server_port}/simple',
                                '--package', 'qwen-asr', '--version', V, '--stage', 'deps',
                                http_proxy=f'http://127.0.0.1:{proxy.server_port}')
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn('▶ 分类：代理类 —— 代理网关错误/上游不可达（实证）', out)
        self.assertIn('HTTP 502', out)
        self.assertIn('代理侧故障', out)
        self.assertIn('不是“内容级”吞掉', out)
        self.assertNotIn('内容劫持', out)
        # 修复建议指向代理服务，且换镜像无效的结论保持。
        self.assertIn('检查代理服务及其上游', out)
        self.assertIn('换镜像无法解决', out)
        self.assertIn("--noproxy '*'", out)
        self.assertIn('-x "$http_proxy"', out)

    def test_s13b_standalone_via_proxy_5xx_gateway_error(self):
        # P2-A standalone 同口径：502 经代理、直连可见 → 代理网关错误结论。
        index = self.start_index(
            f'<a href="../../packages/qwen_asr-{V}-py3-none-any.whl#sha256=abc">'
            f'qwen_asr-{V}-py3-none-any.whl</a>')
        self.addCleanup(index.shutdown)
        proxy = self.start_mock_proxy(503)
        self.addCleanup(proxy.shutdown)
        result = self.run_probe('--package', 'qwen-asr', '--version', V,
                                VOICE_IME_PIP_INDEX_URL=f'http://127.0.0.1:{index.server_port}/simple',
                                http_proxy=f'http://127.0.0.1:{proxy.server_port}')
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn('代理网关错误/上游不可达', out)
        self.assertIn('HTTP 503', out)
        self.assertIn('不是内容级吞掉', out)
        self.assertNotIn('内容劫持', out)

    def test_s14_garbage_direct_with_dead_proxy_reports_proxy_facts(self):
        # RT2 P2-B 回归（场景一：有代理变量）：直连垃圾页 + 经代理探测连接失败
        # （all_proxy=socks5://死端口）→ 文案如实并列两侧事实（配置的代理协议与
        # 来源变量、经代理探测结果），不再自相矛盾地断言「当前无代理变量」。
        index = self.start_index('<html>captive portal login</html>')
        self.addCleanup(index.shutdown)
        err = self.write_stderr(
            f'ERROR: Could not find a version that satisfies the requirement '
            f'qwen-asr=={V} (from versions: none)\n'
            f'ERROR: No matching distribution found for qwen-asr=={V}')
        result = self.run_probe('--analyze', err,
                                '--index-url', f'http://127.0.0.1:{index.server_port}/simple',
                                '--package', 'qwen-asr', '--version', V, '--stage', 'deps',
                                all_proxy='socks5://127.0.0.1:1')
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn('▶ 分类：代理类 —— 内容异常', out)
        self.assertIn('配置的代理（协议 socks5，来源 $all_proxy）', out)
        self.assertIn('经代理探测得 HTTP 000 / network-error', out)
        self.assertIn('可能是透明劫持，也可能是代理故障', out)
        self.assertIn('直连/经代理两条命令', out)
        self.assertNotIn('当前无代理变量', out)

    def test_s15_garbage_direct_without_proxy_keeps_original_wording(self):
        # RT2 P2-B 场景二（纯无代理）：直连垃圾页、8 个代理变量均未设置 →
        # 维持原文案（当前无代理变量——疑似透明劫持/网关拦截/captive portal）。
        index = self.start_index('<html>captive portal login</html>')
        self.addCleanup(index.shutdown)
        err = self.write_stderr(
            f'ERROR: Could not find a version that satisfies the requirement '
            f'qwen-asr=={V} (from versions: none)\n'
            f'ERROR: No matching distribution found for qwen-asr=={V}')
        result = self.run_probe('--analyze', err,
                                '--index-url', f'http://127.0.0.1:{index.server_port}/simple',
                                '--package', 'qwen-asr', '--version', V, '--stage', 'deps')
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn('▶ 分类：代理类 —— 内容异常', out)
        self.assertIn('当前无代理变量——疑似透明劫持/网关拦截/captive portal', out)
        self.assertIn('换网络或加可信代理后重跑', out)

    def test_s10_netprobe_documents_pip_conf_isolation(self):
        # 代理来源是 pip.conf/PIP_PROXY 而非环境变量：net-probe 必须说明这些
        # 来源在 setup-qwen-asr.sh 安装路径已被 --isolated + PIP_CONFIG_FILE=
        # /dev/null 隔离（PIP_PROXY 对安装器 pip 无效），不得当作生效代理误报。
        pip_conf = self.home / '.config/pip/pip.conf'
        pip_conf.parent.mkdir(parents=True)
        pip_conf.write_text('[global]\nproxy = http://127.0.0.1:9\n')
        err = self.write_stderr(
            "WARNING: Retrying (1/2 after 5 seconds) ... "
            "ProxyError('Cannot connect to proxy.')")
        result = self.run_probe('--analyze', err, '--index-url', 'http://127.0.0.1:9/simple',
                                '--stage', 'deps', PIP_PROXY='http://127.0.0.1:9999')
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn('PIP_PROXY=http://127.0.0.1:9999', out)
        self.assertIn('此变量对其无效', out)
        self.assertIn('PIP_PROXY 与各级 pip.conf', out)
        self.assertIn('--isolated + PIP_CONFIG_FILE=/dev/null 隔离', out)
        self.assertIn('▶ 分类：代理类', out)


if __name__ == '__main__':
    unittest.main()
