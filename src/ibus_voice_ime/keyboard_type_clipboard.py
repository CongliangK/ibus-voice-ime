#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Type clipboard text through a virtual keyboard.

This is an out-of-IME fallback for web pages that block paste events or ignore
IBus ``commit_text`` calls.  It reads text from the desktop clipboard and emits
real keyboard events via Linux ``uinput``.  By default every printable character
is entered through Linux's Unicode input sequence (Ctrl+Shift+U, hex code,
Enter), which avoids the current Chinese IME converting ASCII keystrokes into
pinyin composition.
"""
from __future__ import annotations

import argparse
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

try:
    import fcntl
except Exception:  # pragma: no cover - Linux-only script
    fcntl = None  # type: ignore[assignment]

# THIS_DIR = .../src/ibus_voice_ime; SRC_DIR = .../src (package root on sys.path).
THIS_DIR = Path(__file__).resolve().parent
SRC_DIR = THIS_DIR.parent
sys.path.insert(0, str(SRC_DIR))

try:
    from ibus_voice_ime import clipboard_paste
except Exception:  # pragma: no cover
    clipboard_paste = None  # type: ignore[assignment]

LOG_PATH = Path(os.environ.get("VOICE_IME_KEYBOARD_PASTE_LOG", "~/.local/share/ibus-voice-ime/keyboard-paste.log")).expanduser()

EV_SYN = 0x00
EV_KEY = 0x01
SYN_REPORT = 0

UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502

BUS_USB = 0x03

KEY = {
    "ESC": 1,
    "1": 2,
    "2": 3,
    "3": 4,
    "4": 5,
    "5": 6,
    "6": 7,
    "7": 8,
    "8": 9,
    "9": 10,
    "0": 11,
    "MINUS": 12,
    "EQUAL": 13,
    "BACKSPACE": 14,
    "TAB": 15,
    "Q": 16,
    "W": 17,
    "E": 18,
    "R": 19,
    "T": 20,
    "Y": 21,
    "U": 22,
    "I": 23,
    "O": 24,
    "P": 25,
    "LEFTBRACE": 26,
    "RIGHTBRACE": 27,
    "ENTER": 28,
    "LEFTCTRL": 29,
    "A": 30,
    "S": 31,
    "D": 32,
    "F": 33,
    "G": 34,
    "H": 35,
    "J": 36,
    "K": 37,
    "L": 38,
    "SEMICOLON": 39,
    "APOSTROPHE": 40,
    "GRAVE": 41,
    "LEFTSHIFT": 42,
    "BACKSLASH": 43,
    "Z": 44,
    "X": 45,
    "C": 46,
    "V": 47,
    "B": 48,
    "N": 49,
    "M": 50,
    "COMMA": 51,
    "DOT": 52,
    "SLASH": 53,
    "RIGHTSHIFT": 54,
    "LEFTALT": 56,
    "SPACE": 57,
    "F12": 88,
}

DIRECT_CHARS: dict[str, tuple[int, bool]] = {}
for letter, code in {
    "q": KEY["Q"], "w": KEY["W"], "e": KEY["E"], "r": KEY["R"], "t": KEY["T"],
    "y": KEY["Y"], "u": KEY["U"], "i": KEY["I"], "o": KEY["O"], "p": KEY["P"],
    "a": KEY["A"], "s": KEY["S"], "d": KEY["D"], "f": KEY["F"], "g": KEY["G"],
    "h": KEY["H"], "j": KEY["J"], "k": KEY["K"], "l": KEY["L"], "z": KEY["Z"],
    "x": KEY["X"], "c": KEY["C"], "v": KEY["V"], "b": KEY["B"], "n": KEY["N"], "m": KEY["M"],
}.items():
    DIRECT_CHARS[letter] = (code, False)
    DIRECT_CHARS[letter.upper()] = (code, True)
for char, code in zip("1234567890", [KEY[str(i)] for i in range(1, 10)] + [KEY["0"]]):
    DIRECT_CHARS[char] = (code, False)
for char, base in {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
}.items():
    DIRECT_CHARS[char] = (KEY[base], True)
DIRECT_CHARS.update({
    " ": (KEY["SPACE"], False),
    "-": (KEY["MINUS"], False), "_": (KEY["MINUS"], True),
    "=": (KEY["EQUAL"], False), "+": (KEY["EQUAL"], True),
    "[": (KEY["LEFTBRACE"], False), "{": (KEY["LEFTBRACE"], True),
    "]": (KEY["RIGHTBRACE"], False), "}": (KEY["RIGHTBRACE"], True),
    "\\": (KEY["BACKSLASH"], False), "|": (KEY["BACKSLASH"], True),
    ";": (KEY["SEMICOLON"], False), ":": (KEY["SEMICOLON"], True),
    "'": (KEY["APOSTROPHE"], False), '"': (KEY["APOSTROPHE"], True),
    "`": (KEY["GRAVE"], False), "~": (KEY["GRAVE"], True),
    ",": (KEY["COMMA"], False), "<": (KEY["COMMA"], True),
    ".": (KEY["DOT"], False), ">": (KEY["DOT"], True),
    "/": (KEY["SLASH"], False), "?": (KEY["SLASH"], True),
})

HEX_KEYS = {ch: DIRECT_CHARS[ch][0] for ch in "0123456789abcdef"}


def log(message: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except Exception:
        pass


def read_clipboard() -> str:
    if clipboard_paste is not None:
        try:
            value = clipboard_paste.read_clipboard_text()
            if value:
                return value
        except Exception as exc:
            log(f"clipboard_paste failed: {exc}")
    for argv in (["wl-paste", "--type", "text"], ["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"]):
        try:
            value = subprocess.check_output(argv, stderr=subprocess.DEVNULL, text=True, timeout=2.0)
        except Exception:
            continue
        if value:
            return value
    return ""


class UInputKeyboard:
    def __init__(self, device: str = "/dev/uinput", key_delay: float = 0.004):
        if fcntl is None:
            raise RuntimeError("fcntl unavailable; this script only supports Linux uinput")
        self.device = device
        self.key_delay = key_delay
        self.fd: int | None = None

    def __enter__(self) -> "UInputKeyboard":
        self.fd = os.open(self.device, os.O_WRONLY | os.O_NONBLOCK)
        assert self.fd is not None
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_SYN)
        for code in sorted(set(KEY.values())):
            fcntl.ioctl(self.fd, UI_SET_KEYBIT, code)

        name = b"ibus-voice-ime-keyboard-paste"[:79]
        uidev = struct.pack("80sHHHHI", name, BUS_USB, 0x1209, 0x5049, 1, 0)
        uidev += struct.pack("i" * 64, *([0] * 64))  # absmax
        uidev += struct.pack("i" * 64, *([0] * 64))  # absmin
        uidev += struct.pack("i" * 64, *([0] * 64))  # absfuzz
        uidev += struct.pack("i" * 64, *([0] * 64))  # absflat
        os.write(self.fd, uidev)
        fcntl.ioctl(self.fd, UI_DEV_CREATE)
        time.sleep(0.2)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        if self.fd is not None:
            try:
                fcntl.ioctl(self.fd, UI_DEV_DESTROY)
            finally:
                os.close(self.fd)
                self.fd = None

    def event(self, event_type: int, code: int, value: int) -> None:
        assert self.fd is not None
        sec = int(time.time())
        usec = int((time.time() - sec) * 1_000_000)
        os.write(self.fd, struct.pack("llHHi", sec, usec, event_type, code, value))

    def sync(self) -> None:
        self.event(EV_SYN, SYN_REPORT, 0)

    def key(self, code: int, down: bool) -> None:
        self.event(EV_KEY, code, 1 if down else 0)
        self.sync()
        time.sleep(self.key_delay)

    def tap(self, code: int, shift: bool = False) -> None:
        if shift:
            self.key(KEY["LEFTSHIFT"], True)
        self.key(code, True)
        self.key(code, False)
        if shift:
            self.key(KEY["LEFTSHIFT"], False)

    def type_direct_char(self, char: str) -> None:
        if char == "\n":
            self.tap(KEY["ENTER"])
            return
        if char == "\t":
            self.tap(KEY["TAB"])
            return
        code_shift = DIRECT_CHARS.get(char)
        if code_shift is None:
            self.type_unicode_char(char)
            return
        self.tap(*code_shift)

    def type_unicode_char(self, char: str) -> None:
        # Linux Unicode input: Ctrl+Shift+U, hexadecimal codepoint, Space.
        # Use Space instead of Enter so if a target does not enter Unicode-input
        # mode the failure is less destructive (no accidental form submit/newline).
        codepoint = ord(char)
        if codepoint == 0:
            return
        hex_value = f"{codepoint:x}"
        self.key(KEY["LEFTCTRL"], True)
        self.key(KEY["LEFTSHIFT"], True)
        self.tap(KEY["U"])
        self.key(KEY["LEFTSHIFT"], False)
        self.key(KEY["LEFTCTRL"], False)
        time.sleep(self.key_delay * 2)
        for digit in hex_value:
            self.tap(HEX_KEYS[digit])
        self.tap(KEY["SPACE"])

    def type_text(self, text: str, mode: str = "unicode") -> None:
        for char in text:
            if char == "\r":
                continue
            if mode == "direct":
                self.type_direct_char(char)
            elif mode == "smart" and (char == "\n" or char == "\t" or (char in DIRECT_CHARS and ord(char) < 128)):
                self.type_direct_char(char)
            else:
                if char == "\n":
                    self.tap(KEY["ENTER"])
                elif char == "\t":
                    self.tap(KEY["TAB"])
                else:
                    self.type_unicode_char(char)


def permission_hint(device: str) -> str:
    return f"""无法写入 {device}。请先给当前用户 uinput 权限，例如：

sudo modprobe uinput
sudo setfacl -m u:$USER:rw /dev/uinput

如需永久生效，可创建 udev 规则或把该命令加入登录脚本。"""


def _run_quiet(argv: list[str], timeout: float = 1.5) -> str:
    return subprocess.check_output(argv, stderr=subprocess.DEVNULL, text=True, timeout=timeout).strip()


def current_ibus_engine() -> str:
    try:
        return _run_quiet(["ibus", "engine"])
    except Exception:
        return ""


def switch_ibus_engine(engine: str) -> bool:
    if not engine:
        return False
    try:
        subprocess.run(["ibus", "engine", engine], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1.5, check=False)
        return True
    except Exception as exc:
        log(f"switch engine to {engine!r} failed: {exc}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Type clipboard text through a Linux uinput virtual keyboard")
    parser.add_argument("--text", help="type this text instead of reading clipboard")
    parser.add_argument("--delay-ms", type=int, default=int(os.environ.get("VOICE_IME_KEYBOARD_PASTE_DELAY_MS", "1000")))
    parser.add_argument("--max-chars", type=int, default=int(os.environ.get("VOICE_IME_KEYBOARD_PASTE_MAX_CHARS", "20000")))
    parser.add_argument("--mode", choices=("unicode", "smart", "direct"), default=os.environ.get("VOICE_IME_KEYBOARD_PASTE_MODE", "smart"))
    parser.add_argument("--key-delay-ms", type=float, default=float(os.environ.get("VOICE_IME_KEYBOARD_PASTE_KEY_DELAY_MS", "4")))
    parser.add_argument("--device", default=os.environ.get("VOICE_IME_UINPUT_DEVICE", "/dev/uinput"))
    parser.add_argument("--typing-engine", default=os.environ.get("VOICE_IME_KEYBOARD_PASTE_TYPING_ENGINE", "xkb:us::eng"), help="temporarily switch IBus to this engine while typing; empty disables switching")
    parser.add_argument("--no-engine-switch", action="store_true", help="do not switch IBus engine before typing")
    parser.add_argument("--check", action="store_true", help="only check uinput access")
    args = parser.parse_args()

    if args.check:
        if not os.path.exists(args.device):
            print(f"missing {args.device}", file=sys.stderr)
            return 2
        if not os.access(args.device, os.W_OK):
            print(permission_hint(args.device), file=sys.stderr)
            return 2
        print(f"OK: {args.device} is writable")
        return 0

    content = args.text if args.text is not None else read_clipboard()
    if not content:
        log("clipboard is empty")
        return 1
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    if args.max_chars > 0 and len(content) > args.max_chars:
        content = content[: args.max_chars]
        log(f"truncated to {args.max_chars} chars")

    delay = max(0, args.delay_ms) / 1000.0
    original_engine = ""
    switched_engine = False
    log(
        f"start typing chars={len(content)} mode={args.mode} delay={delay}s "
        f"typing_engine={args.typing_engine!r}"
    )
    time.sleep(delay)
    try:
        if not args.no_engine_switch and args.typing_engine:
            original_engine = current_ibus_engine()
            if original_engine != args.typing_engine:
                switched_engine = switch_ibus_engine(args.typing_engine)
                log(f"ibus engine switch: {original_engine!r} -> {args.typing_engine!r}, ok={switched_engine}")
                time.sleep(0.25)
        with UInputKeyboard(args.device, key_delay=max(0.0, args.key_delay_ms / 1000.0)) as keyboard:
            keyboard.type_text(content, mode=args.mode)
    except PermissionError:
        msg = permission_hint(args.device)
        log(msg)
        print(msg, file=sys.stderr)
        return 2
    except FileNotFoundError:
        msg = f"找不到 {args.device}；请先 sudo modprobe uinput"
        log(msg)
        print(msg, file=sys.stderr)
        return 2
    except Exception as exc:
        log(f"typing failed: {exc}")
        print(f"keyboard paste failed: {exc}", file=sys.stderr)
        return 2
    finally:
        if switched_engine and original_engine:
            time.sleep(0.1)
            restored = switch_ibus_engine(original_engine)
            log(f"ibus engine restore: {original_engine!r}, ok={restored}")
    log(f"done typing chars={len(content)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
