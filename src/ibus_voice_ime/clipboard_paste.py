# -*- coding: utf-8 -*-
"""Clipboard helpers for IME-level paste.

The goal is to read the desktop clipboard and let ``engine.py`` submit the
content through IBus ``commit_text``.  This avoids browser/application paste
handlers because the target app receives normal IME committed text rather than a
``Ctrl+V``/paste event.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess

_DEFAULT_HOTKEYS = ("p",)


def hotkey_letters() -> tuple[str, ...]:
    """Return configured Ctrl+Alt+letter clipboard-paste hotkey letters.

    粘贴触发现在只有 GNOME 全局快捷键一条路（scripts/clipboard-paste.sh），
    引擎不再拦截按键；这个解析仅存的消费者是 voice_hotkey 的 raw 热键去重
    （避免 Ctrl+Alt+B 配置撞上 Ctrl+Alt+P）。接受 ``Ctrl+Alt+P`` 或 ``p`` 形式
    的 ``VOICE_IME_CLIPBOARD_HOTKEYS``（``VOICE_IME_CLIPBOARD_HOTKEY`` 为单键
    兼容别名）。
    """
    raw = os.environ.get("VOICE_IME_CLIPBOARD_HOTKEYS") or os.environ.get("VOICE_IME_CLIPBOARD_HOTKEY") or ""
    letters: list[str] = []
    for item in re.split(r"[,;\s]+", raw):
        token = item.strip()
        if not token:
            continue
        key = token.rsplit("+", 1)[-1].strip().lower()
        if len(key) == 1 and "a" <= key <= "z" and key not in letters:
            letters.append(key)
    return tuple(letters or _DEFAULT_HOTKEYS)


def content_fingerprint(content: str) -> str:
    """粘贴链路检查点共用的内容指纹：sha1 前 8 位 + 行数 + 字符数。

    CP1（clipboard-paste.sh 写暂存文件）、CP2（引擎 paste-check / paste-file
    读回）、CP3a（commit_text 调用）各自独立计算同一份指纹；跨进程对照指纹
    即可确认内容在每一跳完好、行数一致（多行是否被破坏在这一眼可见）。
    """
    if not content:
        return "sha=<empty> lines=0 len=0"
    digest = hashlib.sha1(content.encode("utf-8")).hexdigest()[:8]
    lines = content.count("\n") + 1
    return f"sha={digest} lines={lines} len={len(content)}"


def read_clipboard_text() -> str:
    """Read text from the system clipboard."""
    value, _source, _details = read_clipboard_text_with_source()
    return value


def read_clipboard_text_with_source() -> tuple[str, str, str]:
    """Read text and return diagnostic source details.

    Prefer ``xclip`` through XWayland when ``DISPLAY`` is available: on GNOME
    Wayland（Fedora 44+）, a fresh native Wayland clipboard client
    (``wl-paste``) triggers compositor input-context focus flicker that
    silently drops the following IME commit——Ctrl+Alt+P 粘贴自系统升级后
    静默失效的根因（2026-08-28 排查定案）。XWayland 的选择转发由常驻 X
    服务承担，读取不新建 Wayland 客户端（实测零 FocusOut）。X11 不可用时
    回退 wl-paste / GTK / xsel。
    """
    readers = [
        ("xclip", _read_xclip),
        ("wl-paste", _read_wl_paste),
        ("gtk", _read_gtk_clipboard),
        ("xsel", _read_xsel),
    ]
    details: list[str] = []
    empty_seen = False
    for name, reader in readers:
        value = reader()
        if value is None:
            details.append(f"{name}=unavailable")
            continue
        details.append(f"{name}=len:{len(value)}")
        if value:
            return value, name, ", ".join(details)
        # Some backends can report an empty string while another backend can
        # still read the text.  Treat empty as a soft failure until all fallbacks
        # have been tried.
        empty_seen = True
    return ("", "empty" if empty_seen else "unavailable", ", ".join(details))


def _read_gtk_clipboard() -> str | None:
    try:
        import gi  # noqa: WPS433

        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk, Gtk  # type: ignore  # noqa: WPS433

        try:
            Gtk.init_check([])
        except Exception:
            pass
        if Gdk.Display.get_default() is None:
            return None
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        value = clipboard.wait_for_text()
        return value if isinstance(value, str) else None
    except Exception:
        return None


def _read_command(argv: list[str]) -> str | None:
    executable = shutil.which(argv[0])
    candidates = [argv]
    if executable and executable != argv[0]:
        candidates.insert(0, [executable, *argv[1:]])
    if argv[0] == "wl-paste" and "/usr/bin/wl-paste" not in {item[0] for item in candidates}:
        candidates.append(["/usr/bin/wl-paste", *argv[1:]])
    for candidate in candidates:
        try:
            return subprocess.check_output(candidate, stderr=subprocess.DEVNULL, text=True, timeout=1.5)
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired, UnicodeDecodeError):
            continue
    return None


def _read_wl_paste() -> str | None:
    return _read_command(["wl-paste", "--type", "text"])


def _read_xclip() -> str | None:
    if not os.environ.get("DISPLAY"):
        return None
    return _read_command(["xclip", "-selection", "clipboard", "-o"])


def _read_xsel() -> str | None:
    return _read_command(["xsel", "--clipboard", "--output"])
