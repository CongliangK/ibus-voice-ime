"""Dependency-free Qwen failure classification shared by installer and runtime."""
from __future__ import annotations

import json


def error_detail(raw: str) -> str:
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict) and isinstance(payload.get("error"), str):
            return payload["error"]
    except (ValueError, TypeError):
        pass
    return raw


def classify_error(raw: str) -> tuple[str, str]:
    detail = error_detail(raw)
    low = detail.lower()
    if "python.h" in low and any(x in low for x in ("not found", "no such file", "缺少")):
        return "python_headers", (
            "缺少 sidecar 解释器对应的 Python 开发头文件。Fedora 安装对应版本的 python3-devel/"
            "python3.12-devel；Debian/Ubuntu 安装 python3.x-dev 和 build-essential。"
            "不要只给系统默认 Python 装开发包；以错误中的解释器版本为准。"
        )
    if "out of memory" in low or "显存不足" in detail:
        return "oom", "显存不足：关闭占用显存的程序，或 ./scripts/switch-qwen-asr.sh 0.6b 换小模型。"
    if "not compiled with cuda" in low:
        return "cpu_torch", "sidecar 装的是 CPU 版 PyTorch：安装匹配驱动的 CUDA 轮子后运行 ./scripts/setup-qwen-asr.sh --verify-only。"
    if "bfloat16" in low:
        return "dtype", "显卡不支持 bfloat16：设 VOICE_IME_QWEN_ASR_DTYPE=float16 后重启输入法。"
    if any(x in low for x in ("safetensor", "deserializ", "模型文件不完整", "model shard")):
        return "model_files", "模型文件可能不完整：运行 ./scripts/setup-qwen-asr.sh 校验并补齐模型；不要先删除全部模型。"
    if any(x in low for x in ("no space left", "disk quota exceeded")):
        return "disk_space", "磁盘或配额不足：检查模型目录、临时目录和 Triton 缓存所在磁盘的剩余空间。"
    if "permission denied" in low:
        return "permissions", "权限不足：检查模型、TMPDIR 和 Triton 缓存目录是否归当前用户所有且可写；不要用 sudo 启动输入法。"
    if any(x in low for x in ("libcuda", "cannot find -lcuda", "driver version is insufficient", "cuda driver")):
        return "cuda_driver", "CUDA 驱动库不可用或版本不匹配：检查 nvidia-smi、驱动库及链接路径；不要链接 CUDA stub 库冒充真实驱动。"
    if any(x in low for x in ("failed to find c compiler", "缺少 c 编译器")):
        return "compiler", "缺少 C 编译器：Fedora 安装 gcc；Debian/Ubuntu 安装 build-essential，然后重新验证。"
    if any(x in low for x in ("triton", "gcc", "clang", "returned non-zero exit status")):
        return "triton_compile", (
            "Triton 编译/初始化失败：查看完整日志中 gcc 命令前的 fatal error 或链接错误，"
            "检查匹配版本的 Python.h、编译器、CUDA 驱动库。运行 ./scripts/setup-qwen-asr.sh --verify-only；"
            "如为版本兼容问题，改用 Python 3.12 重建独立环境（安装器会备份旧环境）。"
        )
    if any(x in low for x in ("no module named", "importerror", "no package metadata", "packagenotfound")):
        return "dependencies", "sidecar 依赖缺失或不兼容：运行 ./scripts/setup-qwen-asr.sh 修复独立环境。"
    if "cuda" in low:
        return "cuda_runtime", "CUDA 运行失败：运行 ./scripts/doctor.sh 查看实际 sidecar 解释器及 GPU 检查结果。"
    return "unknown", "运行 ./scripts/doctor.sh，并提交完整 qwen-asr-server.log；服务存活不代表模型或推理已验证。"
