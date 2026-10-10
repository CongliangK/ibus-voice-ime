"""Bounded, staged Qwen environment checks; no GPU imports in the parent process.

Invoked through scripts/qwen-preflight.py. Checks never download models or change
user configuration. Runtime/smoke probes run in disposable child processes.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from ibus_voice_ime.asr.qwen_diagnostics import classify_error


def interpreter_info(python: str) -> dict:
    code = """import json,sys,sysconfig
print(json.dumps({'version':list(sys.version_info[:3]), 'base':getattr(sys,'_base_executable',sys.executable), 'include':sysconfig.get_path('include')}))"""
    result = subprocess.run([python, "-c", code], capture_output=True, text=True, timeout=15, check=True)
    return json.loads(result.stdout)


def select_python(explicit: str = "") -> str:
    # Do not silently inherit a rolling distribution's python3 or an activated
    # unrelated venv. Other versions are an explicit, verified user opt-in.
    candidates = [explicit] if explicit else [shutil.which("python3.12") or ""]
    for candidate in candidates:
        if not candidate:
            continue
        path = shutil.which(candidate) or candidate
        info = interpreter_info(path)
        major, minor, _ = info["version"]
        if major != 3 or not 10 <= minor <= 14:
            raise RuntimeError(f"不支持的 Qwen 解释器版本：{major}.{minor}（允许 3.10–3.14，自动选择仅使用 3.12）")
        if not explicit and minor != 12:
            continue
        return str(Path(path).absolute())
    raise RuntimeError(
        "未找到 Python 3.12；不会自动沿用系统 python3。请安装 python3.12 及其 venv/开发包，"
        "或安装 uv 后重试（安装器可用 uv 准备独立 3.12）；已有其他版本可显式设置 "
        "VOICE_IME_QWEN_ASR_SETUP_PYTHON=/path/to/python，并通过完整验收。"
    )


def validate_model(path: Path) -> None:
    """Check local config and ALL indexed shards, not just an index filename."""
    if not path.is_dir():
        # Distinguish "not downloaded yet" (config still holds a repo ID like
        # Qwen/Qwen3-ASR-1.7B) from a mistyped local path; both are actionable.
        hint = "（配置指向模型仓库 ID，尚未下载到本地）" if not str(path).startswith(("/", ".")) else ""
        raise RuntimeError(
            f"模型未下载或路径不存在{hint}：{path}\n"
            "请先运行 ./scripts/setup-qwen-asr.sh 下载模型，或修正模型路径配置。"
        )
    config = path / "config.json"
    if not config.is_file() or not isinstance(json.loads(config.read_text()), dict):
        raise RuntimeError(f"模型文件不完整：缺少/损坏 config.json：{path}")
    index = path / "model.safetensors.index.json"
    if index.is_file():
        data = json.loads(index.read_text())
        weights = data.get("weight_map") if isinstance(data, dict) else None
        if not isinstance(weights, dict) or not weights:
            raise RuntimeError(f"模型文件不完整：无效 weight_map：{index}")
        if not all(isinstance(value, str) for value in weights.values()):
            raise RuntimeError("模型文件不完整：无效 model shard 名称")
        shards = set(weights.values())
    else:
        shards = {"model.safetensors"}
    for shard in shards:
        if not isinstance(shard, str):
            raise RuntimeError("模型文件不完整：无效 model shard 名称")
        target = (path / shard).resolve()
        if not target.is_relative_to(path.resolve()) or not target.is_file() or target.stat().st_size == 0:
            raise RuntimeError(f"模型文件不完整：model shard 缺失/为空/路径越界：{shard}")


def check_static(python: str) -> dict:
    info = interpreter_info(python)
    major, minor, _ = info["version"]
    if major != 3 or not 10 <= minor <= 14:
        raise RuntimeError(f"Qwen Python 版本不受支持：{info['version']}")
    print(f"[OK] sidecar Python：{python} ({'.'.join(map(str, info['version']))})", flush=True)
    if minor != 12:
        print("[WARN] 非推荐 Python 3.12：必须通过运行时和模型推理验收，不能仅凭版本判定不兼容。", flush=True)
    header = Path(info["include"]) / "Python.h"
    if not header.is_file():
        raise RuntimeError(
            f"缺少 Python.h：{header}（解释器 {python}，Python {major}.{minor}）。"
            "推荐运行 ./scripts/setup-qwen-asr.sh，用仓库内带头文件的 Python 3.12 重建。"
            f"坚持系统解释器时：Fedora 用 dnf provides '*/Python.h' 查询（默认版本通常为 python3-devel，"
            f"非默认版本可能为 python3.{minor}-devel，不保证仓库有此包）；"
            f"Debian/Ubuntu 通常为 python3.{minor}-dev 与 build-essential。"
        )
    cc = os.environ.get("CC") or shutil.which("gcc") or shutil.which("clang")
    if not cc or not shutil.which(cc):
        raise RuntimeError("缺少 C 编译器：gcc/clang（或 CC 指定的编译器不存在）")
    # Check headers are usable, not just present. TemporaryDirectory also
    # catches permissions/space issues before downloading multiple GB of models.
    with tempfile.TemporaryDirectory(prefix="qwen-compile-check-") as directory:
        source = Path(directory) / "probe.c"
        source.write_text("#include <Python.h>\nint main(void) { return 0; }\n")
        result = subprocess.run(
            [cc, "-fsyntax-only", f"-I{header.parent}", str(source)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode:
            raise RuntimeError(f"Python.h 编译检查失败（{cc}）：\n{result.stderr}")
    print("[OK] C 编译器、Python 开发头文件和临时目录可用", flush=True)
    return info


RUNTIME_PROBE = '''
import importlib.metadata as metadata
import torch
import qwen_asr
import triton
for name in ('torch', 'triton', 'qwen-asr', 'transformers'):
    print(name, metadata.version(name), flush=True)
if not torch.cuda.is_available():
    raise RuntimeError('CUDA 不可用；检查 CUDA 版 PyTorch 和 NVIDIA 驱动')
from ibus_voice_ime import config
device = config.env_str('VOICE_IME_QWEN_ASR_DEVICE_MAP', 'cuda:0')
torch.cuda.set_device(torch.device(device))
x = torch.ones((16, 16), device=device)
assert float((x @ x).sum().item()) == 4096.0
torch.cuda.synchronize()
# Forces compilation/loading of Triton's CUDA driver helper (Python.h/libcuda).
triton.runtime.driver.active.get_current_device()
print('[OK] CUDA 实际运算和 Triton 驱动初始化通过', flush=True)
'''

SMOKE_PROBE = '''
import os, sys, tempfile, wave
from ibus_voice_ime.asr.qwen_asr_server import ModelManager, _companion_path
path = sys.argv[1]
manager = ModelManager(primary_path=path, secondary_path=_companion_path(path),
                       idle_timeout=0, check_interval=1)
manager.acquire_for_inference()
try:
    model = manager.active_model()
    if model is None:
        raise RuntimeError('模型未能加载：' + manager.load_error)
    with tempfile.TemporaryDirectory(prefix='qwen-smoke-') as directory:
        audio = os.path.join(directory, 'silence.wav')
        with wave.open(audio, 'wb') as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
            wav.writeframes(b'\\x00\\x00' * 16000)
        # Silence tests the execution path, not transcription accuracy.
        result = model.transcribe(audio=audio, language='Chinese')
        if not isinstance(result, (list, tuple)) or not result:
            raise RuntimeError('推理返回格式无效')
        print('[OK] 模型加载和短音频推理通过：' + manager.active_id, flush=True)
finally:
    manager.release_after_inference()
'''


def run_probe(python: str, source: str, arguments: list[str], timeout: float) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2]) + os.pathsep + env.get("PYTHONPATH", "")
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    # Private temporary cache forces an actual compiler/linker test even when
    # the regular user cache contains a previously built helper. Do not delete
    # or rewrite the user's cache. All CUDA allocations die with the child.
    with tempfile.TemporaryDirectory(prefix="qwen-triton-check-") as directory:
        env["TRITON_CACHE_DIR"] = directory
        try:
            result = subprocess.run(
                [python, "-u", "-c", source, *arguments], env=env,
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            output = exc.stdout or b""
            errors = exc.stderr or b""
            if isinstance(output, bytes):
                output = output.decode(errors="replace")
            if isinstance(errors, bytes):
                errors = errors.decode(errors="replace")
            raise RuntimeError(f"Qwen 环境验收超时（{timeout:g}s），检查子进程已终止。\n{output}\n{errors}") from exc
    if result.stdout:
        print(result.stdout, end="", flush=True)
    if result.returncode:
        raise RuntimeError(f"Qwen 验收失败（退出码 {result.returncode}）：\n{result.stderr}")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--select-python", action="store_true")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--explicit", default="")
    parser.add_argument("--stage", choices=("static", "runtime", "smoke"), default="static")
    parser.add_argument("--model")
    parser.add_argument("--model-only", action="store_true")
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args(argv)
    try:
        if args.select_python:
            print(select_python(args.explicit))
            return 0
        if "/" in args.python and not Path(args.python).exists():
            # An explicitly configured interpreter that vanished (venv moved or
            # deleted) would otherwise surface as a bare FileNotFoundError.
            raise RuntimeError(
                f"sidecar 解释器不存在：{args.python}\n"
                "配置指向的 venv 可能已被移动或删除；重跑 ./scripts/setup-qwen-asr.sh 重建。"
            )
        if args.model:
            validate_model(Path(args.model))
            print(f"[OK] 模型配置和全部权重分片存在：{args.model}", flush=True)
        if args.model_only:
            if not args.model:
                parser.error("--model-only 需要 --model")
            return 0
        if not math.isfinite(args.timeout) or args.timeout <= 0 or args.timeout > 1800:
            parser.error("--timeout 必须在 (0, 1800] 秒内")
        check_static(args.python)
        if args.stage in {"runtime", "smoke"}:
            run_probe(args.python, RUNTIME_PROBE, [], args.timeout)
        if args.stage == "smoke":
            if not args.model:
                parser.error("--stage smoke 需要 --model")
            run_probe(args.python, SMOKE_PROBE, [args.model], args.timeout)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        code, guidance = classify_error(str(exc))
        print(f"[FAIL:{code}] {exc}\n处理建议：{guidance}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
