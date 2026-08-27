# -*- coding: utf-8 -*-
"""Log retention: keep only the most recent N lines of a log file.

日志洪水治理：所有 append 型日志（engine.log / error.log / sidecar 日志）在
写入侧调用 ``trim_to_last_lines``，超过阈值时裁剪到最近 N 行（默认 1000，
``VOICE_IME_LOG_KEEP_LINES`` 可调，0 = 关闭）。裁剪只保留信息密度最高的尾部
（最近的错误与生命周期事件），对非开发者用户友好，也方便调试时直接全文查看。

实现注意：先做廉价的 size 检查（os.stat），只有文件超过字节门槛才数行、才重写；
正常情况下（文件小于门槛）每次调用只是一次 stat。
"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_KEEP_LINES = 1000
# 字节门槛：低于它必然不足 keep 行（按每行 ~128B 保守估算），直接跳过。
_MIN_BYTES_FACTOR = 128


def keep_lines_from_env() -> int:
    raw = os.environ.get("VOICE_IME_LOG_KEEP_LINES", "").strip()
    if not raw:
        return DEFAULT_KEEP_LINES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_KEEP_LINES
    return max(0, value)


def trim_to_last_lines(path: str | Path, keep: int | None = None, *, min_bytes: int | None = None) -> bool:
    """把 ``path`` 裁剪到最近 ``keep`` 行；实际发生裁剪返回 True。

    ``keep`` 缺省读 ``VOICE_IME_LOG_KEEP_LINES``（默认 1000，0 = 禁用）。
    只有当行数明显超过 keep（> 2×keep，即已经积累了整整一轮的新日志）时才
    重写文件，避免每次调用都做无谓的读改写。文件不存在/不可读时静默返回
    False——日志裁剪绝不能影响主流程。
    """
    if keep is None:
        keep = keep_lines_from_env()
    if keep <= 0:
        return False
    file_path = Path(path)
    try:
        if not file_path.is_file():
            return False
        threshold_bytes = min_bytes if min_bytes is not None else keep * _MIN_BYTES_FACTOR
        if file_path.stat().st_size <= threshold_bytes:
            return False
        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        # 尾部不完整行（无换行结尾的最后一行）保留，不丢弃正在写入的事件。
        if len(lines) <= keep * 2:
            return False
        tail = lines[-keep:]
        tmp = file_path.with_name(file_path.name + ".trim-tmp")
        tmp.write_text("".join(tail), encoding="utf-8")
        os.replace(tmp, file_path)
        return True
    except Exception:
        return False


__all__ = ["keep_lines_from_env", "trim_to_last_lines"]
