# -*- coding: utf-8 -*-
"""Clipboard helpers for IME-level paste.

The goal is to read the desktop clipboard and let ``engine.py`` submit the
content through IBus ``commit_text``.  This avoids browser/application paste
handlers because the target app receives normal IME committed text rather than a
``Ctrl+V``/paste event.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Any

_DEFAULT_HOTKEYS = ("p",)


def hotkey_letters() -> tuple[str, ...]:
    """Return configured Ctrl+Alt+letter clipboard-paste hotkeys.

    Accepts values such as ``Ctrl+Alt+P`` or simply ``p`` from
    ``VOICE_IME_CLIPBOARD_HOTKEYS``.  ``VOICE_IME_CLIPBOARD_HOTKEY`` is kept as
    a single-hotkey convenience alias.
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


def hotkey_label() -> str:
    return " / ".join(f"Ctrl+Alt+{letter.upper()}" for letter in hotkey_letters())


def matches_ctrl_alt_letter(namespace: Any, keyval: int, state: int, control_mask: int, alt_mask: int) -> bool:
    """Return whether a key event matches a configured paste hotkey."""
    if not ((int(state) & int(control_mask)) and (int(state) & int(alt_mask))):
        return False
    for letter in hotkey_letters():
        lower = getattr(namespace, f"KEY_{letter}", None)
        upper = getattr(namespace, f"KEY_{letter.upper()}", None)
        if keyval == lower or keyval == upper:
            return True
    return False


def read_clipboard_text() -> str:
    """Read text from the system clipboard."""
    value, _source, _details = read_clipboard_text_with_source()
    return value


def read_clipboard_text_with_source() -> tuple[str, str, str]:
    """Read text and return diagnostic source details.

    Prefer ``wl-paste`` under Wayland.  GTK clipboard reads from inside an IBus
    engine can report an empty string or block on some focused clients, while
    ``wl-paste`` reads the compositor clipboard directly.  Keep GTK and X11
    tools as fallbacks.
    """
    readers = [
        ("wl-paste", _read_wl_paste),
        ("gtk", _read_gtk_clipboard),
        ("xclip", _read_xclip),
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
    return _read_command(["xclip", "-selection", "clipboard", "-o"])


def _read_xsel() -> str | None:
    return _read_command(["xsel", "--clipboard", "--output"])
