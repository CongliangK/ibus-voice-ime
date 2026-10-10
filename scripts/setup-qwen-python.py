#!/usr/bin/env python3
"""Prepare repository-local CPython; stdout contains only its selected path."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from ibus_voice_ime.asr.qwen_preflight import interpreter_info, select_python

UV_VERSION = "0.11.20"
MAX_WHEEL = 64 * 1024 * 1024
MAX_BINARY = 128 * 1024 * 1024


def local_directory(root: Path, path: Path) -> None:
    """Check against the repository, NOT against a possibly escaped cache root.

    No symlink directories (even dangling ones) in our write paths. This is a
    single-writer installer, not a defense against concurrent hostile renames.
    """
    root = root.absolute()
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError(f"仓库根目录异常：{root}")
    relative = path.absolute().relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise RuntimeError(f"拒绝写入符号链接/非目录缓存路径：{current}")
    if not path.resolve().is_relative_to(root.resolve()):
        raise RuntimeError(f"缓存目录越出仓库：{path}")


def validate_uv(binary: str, *, pinned: bool = False) -> None:
    try:
        result = subprocess.run([binary, "--version"], capture_output=True,
                                text=True, check=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise RuntimeError(f"uv 完整性检查失败：{binary}；{detail}") from exc
    match = re.fullmatch(r"uv (\d+\.\d+\.\d+)(?:[^\n]*)\n?", result.stdout.strip())
    if not match or (pinned and match[1] != UV_VERSION):
        raise RuntimeError(f"uv 版本检查失败：{binary}；{result.stdout.strip()!r}")


@contextmanager
def download_deadline(seconds: int):
    # urlopen's socket timeout is NOT a wall-clock bound (slow trickle attacks).
    # This standalone Linux helper runs downloads on the main thread.
    def expired(*_):
        raise TimeoutError(f"uv 自举下载超过总时限 {seconds}s")
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def fetch(url: str, limit: int) -> bytes:
    if not isinstance(url, str) or not url.startswith("https://"):
        raise RuntimeError("uv 自举源必须为 HTTPS URL。")
    with urllib.request.urlopen(url, timeout=30) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError("uv 自举下载大小超限。")
    return data


def bootstrap_uv(root: Path) -> str:
    target = root / ".tools" / "uv" / "uv"
    local_directory(root, target.parent)
    if target.is_symlink():
        raise RuntimeError(f"拒绝使用符号链接 uv：{target}")
    if target.exists():
        validate_uv(str(target), pinned=True)
        return str(target)
    arch = platform.machine()
    if platform.system() != "Linux" or arch not in {"x86_64", "aarch64"}:
        raise RuntimeError("自动准备 uv 仅支持 Linux x86_64/aarch64；请先安装 uv 或显式指定 Python 3.12。")
    index = os.environ.get("VOICE_IME_UV_METADATA_URL", f"https://pypi.org/pypi/uv/{UV_VERSION}/json")
    print(f"准备仓库内 uv {UV_VERSION}（uv 自举元数据源：{index}）", file=sys.stderr)
    with download_deadline(180):
        metadata = json.loads(fetch(index, 2 * 1024 * 1024))
        if not isinstance(metadata, dict) or not isinstance(metadata.get("urls"), list):
            raise RuntimeError("uv 发布元数据格式无效：需要 urls 数组。")
        matches = [item for item in metadata["urls"] if isinstance(item, dict)
                   and isinstance(item.get("filename"), str)
                   and f"uv-{UV_VERSION}-py3-none-manylinux_2_17_{arch}." in item["filename"]]
        if len(matches) != 1:
            raise RuntimeError("uv 发布元数据未包含适配本机的唯一 wheel。")
        item = matches[0]
        digests = item.get("digests")
        digest = digests.get("sha256") if isinstance(digests, dict) else None
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise RuntimeError("uv wheel SHA256 元数据无效。")
        wheel = fetch(item.get("url"), MAX_WHEEL)
    if hashlib.sha256(wheel).hexdigest() != digest:
        raise RuntimeError("uv wheel SHA256 校验失败，拒绝执行。")
    with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
        binaries = [entry for entry in archive.infolist() if entry.filename.endswith(".data/scripts/uv")]
        if len(binaries) != 1 or not 0 < binaries[0].file_size <= MAX_BINARY:
            raise RuntimeError("uv wheel 可执行文件缺失/重复/解压大小超限。")
        with archive.open(binaries[0]) as stream:
            binary = stream.read(MAX_BINARY + 1)
        if len(binary) > MAX_BINARY:
            raise RuntimeError("uv 解压大小超限。")
    local_directory(root, target.parent)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix="uv.tmp-", dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(binary)
        temporary.chmod(0o755)
        validate_uv(str(temporary), pinned=True)
        temporary.replace(target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return str(target)


def run_uv(cmd: list[str], env: dict, stage: str, timeout: int, *, capture: bool = False):
    try:
        return subprocess.run(cmd, env=env, stdout=subprocess.PIPE if capture else sys.stderr,
                              stderr=subprocess.PIPE if capture else None,
                              text=True, check=True, timeout=timeout)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"uv {stage}失败（退出码 {exc.returncode}）：{exc.stderr or '见上方 uv 输出'}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"uv {stage}超过总时限 {timeout}s：{exc.stderr or ''}") from exc


def provision(root: Path, explicit: str = "") -> str:
    if explicit:
        return select_python(explicit)
    for directory in (".python", ".tools", ".tools/bin", ".cache", ".cache/uv"):
        local_directory(root, root / directory)
    uv = shutil.which("uv")
    if uv:
        validate_uv(uv)
    else:
        uv = bootstrap_uv(root)
    env = os.environ.copy()
    env.update(UV_PYTHON_INSTALL_DIR=str(root / ".python"),
               UV_PYTHON_BIN_DIR=str(root / ".tools" / "bin"),
               UV_CACHE_DIR=str(root / ".cache" / "uv"))
    print("Python 下载源：" + env.get("UV_PYTHON_INSTALL_MIRROR", "uv 官方 python-build-standalone（GitHub）") +
          "；安装目录：" + env["UV_PYTHON_INSTALL_DIR"], file=sys.stderr)
    run_uv([uv, "python", "install", "3.12", "--no-config"], env, "安装 CPython 3.12", 600)
    result = run_uv([uv, "python", "find", "3.12", "--managed-python", "--no-python-downloads", "--no-config"],
                    env, "查找仓库解释器", 30, capture=True)
    candidate = Path(result.stdout.strip())
    # Reject external/relative/nonexecutable paths BEFORE executing any probe.
    if (not candidate.is_absolute() or not candidate.resolve().is_relative_to((root / ".python").absolute())
            or not candidate.is_file() or not os.access(candidate, os.X_OK)):
        raise RuntimeError(f"uv 返回非仓库内/不可执行解释器，拒绝使用：{candidate}")
    local_directory(root, root / ".python")
    info = interpreter_info(str(candidate))
    if info["version"][:2] != [3, 12]:
        raise RuntimeError(f"自动准备要求 Python 3.12，uv 返回：{info['version']}")
    return str(candidate)


def main() -> int:
    try:
        print(provision(ROOT, os.environ.get("VOICE_IME_QWEN_ASR_SETUP_PYTHON", "")))
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
        print(f"Python 3.12 本地准备失败：{exc}\n"
              "三类源互不替代：uv 自举用 PyPI 元数据/wheel；CPython 用 GitHub python-build-standalone；pip 包默认清华。\n"
              "网络受限可用 ./scripts/setup-qwen-asr.sh --proxy http://127.0.0.1:7890；"
              "或设置 UV_PYTHON_INSTALL_MIRROR 为有效的 python-build-standalone 镜像。\n"
              "损坏的项目 uv 请检查 .tools/uv/uv 后移走重试；已有系统 3.12 可显式设置 "
              "VOICE_IME_QWEN_ASR_SETUP_PYTHON=/usr/bin/python3.12。"
              "不会修改系统 Python；准备失败不会替换现有 venv。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
