#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A minimal custom IBus input method with optional local voice input.

Hotkey: Ctrl+Alt+V records audio and commits recognized text.
Ctrl+Alt+B is the raw-transcript variant: the same recording pipeline, but the
recognized text is never sent through LLM post-processing.
"""
from __future__ import annotations

import argparse
import html
import locale
import os
import re
import shutil
import signal
import socket
import sys
import threading
import time
import traceback
from pathlib import Path

try:
    import gi

    gi.require_version("GLib", "2.0")
    gi.require_version("IBus", "1.0")
    from gi.repository import GLib, IBus  # noqa: E402
except ImportError as _gi_exc:  # 缺 PyGObject 时给可读指引，而不是裸 traceback 死循环拉起
    print(
        f"无法加载 PyGObject/IBus（{_gi_exc}）。安装后重试：\n"
        "  Fedora:        sudo dnf install python3-gobject ibus\n"
        "  Debian/Ubuntu: sudo apt install python3-gi ibus\n"
        "（若用项目 venv，请按 README 以 --system-site-packages 创建后重跑 ./install.sh）",
        file=sys.stderr,
        flush=True,
    )
    sys.exit(2)

# Make local imports work when launched by ibus-daemon.
# THIS_DIR = .../src/ibus_voice_ime; SRC_DIR = .../src (the package root on sys.path).
THIS_DIR = Path(__file__).resolve().parent
SRC_DIR = THIS_DIR.parent.parent
sys.path.insert(0, str(SRC_DIR))

from ibus_voice_ime import clipboard_paste, config, core  # noqa: E402
from ibus_voice_ime.asr import audio_session, qwen_asr_runtime, voice, voice_hotkey, voice_overlay  # noqa: E402
from ibus_voice_ime.memory import chinese_memory, english_memory  # noqa: E402
from ibus_voice_ime.rime import rime_backend  # noqa: E402
from ibus_voice_ime.text import llm_postprocess  # noqa: E402

COMPONENT_NAME = "org.freedesktop.IBus.VoiceCustom"
ENGINE_NAME = "voice-custom"
ENGINE_LONGNAME = "自定义语音输入法"
ENGINE_SYMBOL = "语"
ENGINE_PATH_PREFIX = "/org/freedesktop/IBus/Engine/VoiceCustom"
# IBus activates components by calling CreateEngine on the standard factory
# object path.  Using a custom path makes SetGlobalEngine fail.
FACTORY_PATH = IBus.PATH_FACTORY

def _modifier_mask(*names: str) -> int:
    mask = 0
    for name in names:
        value = getattr(IBus.ModifierType, name, None)
        if value is not None:
            mask |= int(value)
    return mask


# Application/global shortcuts must pass through the IME.  On GNOME/XKB the
# Windows/Super key in shortcuts such as Shift+Super+S often arrives as MOD4,
# not SUPER, so include both forms (plus META/HYPER for other mappings).
APP_SHORTCUT_MASK = _modifier_mask(
    "CONTROL_MASK", "MOD1_MASK", "SUPER_MASK", "META_MASK", "HYPER_MASK", "MOD4_MASK"
)
IGNORE_MASK = _modifier_mask(
    "RELEASE_MASK", "CONTROL_MASK", "MOD1_MASK", "SHIFT_MASK", "SUPER_MASK", "MOD4_MASK", "LOCK_MASK"
)
INPUT_CODE_PREEDIT_SUFFIX_RE = re.compile(r"[-A-Za-z0-9_+.#' \u00fc\u00dc]+$")

PUNCT = {
    IBus.KEY_comma: "，",
    IBus.KEY_period: "。",
    IBus.KEY_question: "？",
    IBus.KEY_exclam: "！",
    IBus.KEY_semicolon: "；",
    IBus.KEY_colon: "：",
}

ASCII_TOGGLE_KEYS = (IBus.KEY_Shift_L, IBus.KEY_Shift_R)

CANDIDATE_PREV_KEYS = (IBus.KEY_Up, IBus.KEY_Left, IBus.KEY_KP_Up, IBus.KEY_KP_Left)
CANDIDATE_NEXT_KEYS = (IBus.KEY_Down, IBus.KEY_Right, IBus.KEY_KP_Down, IBus.KEY_KP_Right)
CANDIDATE_PAGE_PREV_KEYS = (IBus.KEY_Page_Up, IBus.KEY_KP_Page_Up)
CANDIDATE_PAGE_NEXT_KEYS = (IBus.KEY_Page_Down, IBus.KEY_KP_Page_Down)
try:
    LOOKUP_PAGE_SIZE = max(1, config.env_int("VOICE_IME_CANDIDATE_PAGE_SIZE", 5, minimum=1))
except ValueError:
    LOOKUP_PAGE_SIZE = 5


def _env_bool(name: str, default: bool) -> bool:
    return config.env_bool(name, default)


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    return config.env_int(name, default, minimum=minimum)


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    return config.env_float(name, default, minimum=minimum)


def text(s: str) -> IBus.Text:
    return IBus.Text.new_from_string(s)


def engine_exec_command() -> str:
    # Repo root is two levels above src/ (THIS_DIR = .../src/ibus_voice_ime).
    repo_root = THIS_DIR.parent.parent
    launcher = repo_root / "run-engine.sh"
    if launcher.exists():
        return f"{launcher} --ibus"
    # sys.executable 在被 argv[0] 劫持的宿主进程（AppImage 等）里指向错误解释器；
    # PATH 上的 python3 更可靠。
    python = shutil.which("python3") or sys.executable
    return f"{python} {THIS_DIR / 'engine.py'} --ibus"


def log_error(message: str) -> None:
    """Append diagnostics to a user-visible log file.

    IBus launches engines with stdout/stderr often redirected to /dev/null, so
    tracebacks printed with traceback.print_exc() are otherwise lost.
    """
    try:
        log_path = Path(config.env_str("VOICE_IME_ERROR_LOG", "~/.local/share/ibus-voice-ime/error.log")).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
        # 长会话内的日志洪水治理：错误路径本身就低频，顺手做保留最近 N 行。
        from ibus_voice_ime.log_trim import trim_to_last_lines

        trim_to_last_lines(log_path)
    except Exception:
        pass


_FOCUSED_ENGINE = None
# 进程级语音录音所有权：IBus 为每个输入上下文创建独立引擎实例，而语音状态
# （_voice_state/_audio_session）存在实例上。焦点切换（悬浮窗抢焦点、应用切换）
# 会让停止热键落在一个新建的空闲实例上——它不知道有录音在跑，会把"停止"误判
# 成"开始"，叠出第二个弹窗并泄漏 arecord（2026-10-09 报障）。所有权全局化后，
# 任何实例收到热键都先转发给真正持有录音的实例。
_VOICE_OWNER: "VoiceCustomEngine | None" = None


def _claim_voice_owner(engine: "VoiceCustomEngine") -> None:
    global _VOICE_OWNER
    _VOICE_OWNER = engine


def _release_voice_owner(engine: "VoiceCustomEngine") -> None:
    global _VOICE_OWNER
    if _VOICE_OWNER is engine:
        _VOICE_OWNER = None


def _forward_voice_hotkey_to_owner(recipient: "VoiceCustomEngine", raw: bool) -> bool:
    """Route a voice hotkey to the engine instance that owns the live recording.

    Returns True when the hotkey was forwarded (recipient must do nothing more).
    """
    owner = _VOICE_OWNER
    if owner is None or owner is recipient:
        return False
    if getattr(owner, "_voice_state", "idle") == "idle" and not getattr(owner, "_voice_busy", False):
        return False
    log_error(
        f"语音热键转发：按键实例未持有录音，转发给持有实例（owner_state={owner._voice_state}）"
    )
    try:
        owner._handle_voice_hotkey(raw)
    except Exception as exc:
        # 持有实例可能已被 ibus-daemon 销毁（对象失效）；释放所有权避免永久卡死。
        log_error(f"语音热键转发失败（释放所有权）：{exc}")
        _release_voice_owner(owner)
    return True


_IPC_SERVER_STARTED = False
_IPC_SERVER_LOCK = threading.Lock()


def voice_ipc_socket_path() -> Path:
    explicit = config.env_str("VOICE_IME_IPC_SOCKET", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if runtime_dir:
        return Path(runtime_dir) / "ibus-voice-ime" / "voice.sock"
    return Path("~/.local/share/ibus-voice-ime/voice.sock").expanduser()


def _start_voice_ipc_server() -> None:
    """Start a small user-local IPC endpoint for desktop global shortcuts.

    Some terminal programs do not forward Ctrl+Alt+V to IBus.  A GNOME custom
    shortcut can call voice-toggle.sh, which sends "toggle" to this socket; the
    focused engine then runs the same voice-hotkey path as a normal IBus key.
    """
    global _IPC_SERVER_STARTED
    if not config.env_bool("VOICE_IME_IPC", True):
        return
    with _IPC_SERVER_LOCK:
        if _IPC_SERVER_STARTED:
            return
        _IPC_SERVER_STARTED = True
    threading.Thread(target=_voice_ipc_server, name="voice-ime-ipc", daemon=True).start()


def _engine_can_receive_ipc(engine: object | None) -> bool:
    """Whether an IPC command has an engine object to target.

    Do not reject an engine just because ``get_connection()`` reports ``None``:
    with some IBus/PyGObject/browser combinations that is a false negative for
    global-shortcut initiated calls, and it made Ctrl+Alt+P fail in browsers
    even though the same engine can still receive ``commit_text`` calls.
    """
    return engine is not None


def _voice_ipc_server() -> None:
    path = voice_ipc_socket_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except Exception:
            pass
        if path.exists():
            path.unlink()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(path))
            try:
                os.chmod(path, 0o600)
            except Exception:
                pass
            server.listen(5)
            log_error(f"输入法 IPC 已启动：{path}")
            while True:
                conn, _addr = server.accept()
                with conn:
                    try:
                        _serve_ipc_connection(conn)
                    except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                        # 单个坏客户端（连接后立即关闭等）不能杀死整个 accept
                        # 循环——否则 GNOME 全局热键兜底链路会静默失效。
                        log_error(f"IPC 单连接处理失败（已忽略，服务继续）：{exc}")
    except Exception:
        log_error("语音 IPC 启动失败\n" + traceback.format_exc())


def _serve_ipc_connection(conn) -> None:
    # 注意：此处的局部变量 raw 是接收到的 IPC 命令文本，与「原文语音输入」
    # （raw 模式）只是同名；raw 模式命令是下面的 "toggle-raw" / "voice-raw"。
    raw = conn.recv(128).decode("utf-8", "ignore").strip().lower()
    target = _FOCUSED_ENGINE
    if raw in {"", "toggle", "hotkey", "voice"}:
        if not _engine_can_receive_ipc(target):
            log_error(f"IPC 语音请求失败：没有可用焦点引擎 command={raw!r}")
            conn.sendall(b"NO_FOCUS\n")
        else:
            GLib.idle_add(target._handle_voice_hotkey)
            conn.sendall(b"OK\n")
    elif raw in {"toggle-raw", "voice-raw"}:
        if not _engine_can_receive_ipc(target):
            log_error(f"IPC 原文语音请求失败：没有可用焦点引擎 command={raw!r}")
            conn.sendall(b"NO_FOCUS\n")
        else:
            # 与上面同款 idle 分发；额外传 raw=True 表示「原文语音输入」。
            GLib.idle_add(target._handle_voice_hotkey, True)
            conn.sendall(b"OK\n")
    elif raw.startswith("paste-file "):
        if not _engine_can_receive_ipc(target):
            log_error(f"IPC 粘贴文件请求失败：没有可用焦点引擎 command={raw[:80]!r}")
            conn.sendall(b"NO_FOCUS\n")
        else:
            rest = raw[len("paste-file ") :].strip()
            # 可选第三段 delay_ms：诊断脚本用它做时序二分（对齐语音路径的
            # 200ms、复现默认的 1000ms 等）。不带该参数时行为与旧协议一致。
            path_arg, _, extra = rest.partition(" ")
            delay_arg = int(extra) if extra.isdigit() else None
            GLib.idle_add(target._handle_clipboard_file_paste, path_arg, delay_arg)
            conn.sendall(b"OK\n")
    elif raw.startswith("paste-check "):
        # CP2 检查点专用：只读暂存文件并回传内容指纹，不提交。用于把
        # "文件→引擎"（CP2）与"引擎→应用"（CP3）两段故障隔离开。
        # 纯文件 IO + 日志，不触碰 IBus/GLib，可在 IPC 线程直接执行。
        path_arg = raw[len("paste-check ") :].strip()
        try:
            content = Path(path_arg).expanduser().read_text(encoding="utf-8")
        except Exception as exc:
            log_error(f"CP2 失败：paste-check 读文件出错 path={path_arg!r}：{exc}")
            conn.sendall(f"ERR read: {exc}\n".encode("utf-8", "ignore"))
        else:
            fingerprint = clipboard_paste.content_fingerprint(content)
            log_error(f"CP2 通过：paste-check 已读到文件 {fingerprint} path={path_arg}")
            conn.sendall(f"OK {fingerprint}\n".encode("utf-8", "ignore"))
    else:
        conn.sendall(b"ERROR unknown command\n")


def make_component() -> IBus.Component:
    exec_cmd = engine_exec_command()
    component = IBus.Component(
        name=COMPONENT_NAME,
        description="Custom IBus voice input method prototype",
        version="0.1.0",
        license="MIT",
        author="local user",
        homepage="https://github.com/CongliangK/ibus-voice-ime",
        command_line=exec_cmd,
        textdomain="ibus-voice-ime",
    )
    component.add_engine(
        IBus.EngineDesc(
            name=ENGINE_NAME,
            longname=ENGINE_LONGNAME,
            description=f"Python 自定义输入法原型，{voice_hotkey.hotkey_label()} 语音输入",
            language="zh",
            license="MIT",
            author="local user",
            icon="input-keyboard",
            layout="default",
            symbol=ENGINE_SYMBOL,
            rank=80,
        )
    )
    return component


def component_xml() -> str:
    exec_cmd = engine_exec_command()
    return f'''<?xml version="1.0" encoding="utf-8"?>
<component>
  <name>{COMPONENT_NAME}</name>
  <description>Custom IBus voice input method prototype</description>
  <exec>{html.escape(exec_cmd)}</exec>
  <version>0.1.0</version>
  <author>local user</author>
  <license>MIT</license>
  <homepage>https://github.com/CongliangK/ibus-voice-ime</homepage>
  <textdomain>ibus-voice-ime</textdomain>
  <engines>
    <engine>
      <name>{ENGINE_NAME}</name>
      <language>zh</language>
      <license>MIT</license>
      <author>local user</author>
      <icon>input-keyboard</icon>
      <layout>default</layout>
      <longname>{ENGINE_LONGNAME}</longname>
      <description>Python 自定义输入法原型，{html.escape(voice_hotkey.hotkey_label())} 语音输入</description>
      <rank>80</rank>
      <symbol>{ENGINE_SYMBOL}</symbol>
    </engine>
  </engines>
</component>
'''


class VoiceCustomEngine(IBus.Engine):
    __gtype_name__ = "IBusEngineVoiceCustom"

    def __init__(self, bus: IBus.Bus, object_path: str):
        kwargs = dict(engine_name=ENGINE_NAME, connection=bus.get_connection(), object_path=object_path)
        if hasattr(IBus.Engine.props, "has_focus_id"):
            kwargs["has_focus_id"] = True
        super().__init__(**kwargs)
        # 不在此设置 _FOCUSED_ENGINE：新实例未必持有焦点，无条件劫持会让 IPC
        # 热键分发给"最新的空闲实例"而不是真正有焦点的实例（配合悬浮窗抢焦点
        # 的实例切换，曾把 toggle 停止变成叠新录音）。焦点归属由 do_focus_in /
        # do_process_key_event 维护。
        self._bus = bus
        self._buffer = ""
        self._candidates: list[core.Candidate] = []
        # Keep the candidate popup to one visible page.  If more than
        # LOOKUP_PAGE_SIZE candidates are appended, IBus/GNOME shows paging
        # arrow buttons in the popup; we handle page turning ourselves instead.
        self._lookup = IBus.LookupTable.new(page_size=LOOKUP_PAGE_SIZE, cursor_pos=0, cursor_visible=True, round=False)
        self._lookup.set_orientation(IBus.Orientation.VERTICAL)
        self._cursor_area: tuple[int, int, int, int] | None = None
        self._composition_aux_visible = False
        self._aux_generation = 0
        self._voice_busy = False
        self._voice_state = "idle"  # idle | recording | processing
        # 本次 toggle 录音是否为「原文语音输入」（Ctrl+Alt+B）：跳过 LLM 后处理。
        self._voice_raw = False
        self._audio_session: audio_session.AudioSession | None = None
        self._overlay: voice_overlay.VoiceOverlay | None = None
        self._voice_seen_sound = False
        self._voice_last_sound_at = 0.0
        self._chinese = chinese_memory.ChineseMemory()
        self._english = english_memory.EnglishMemory()
        self._raw_input = ""
        self._chinese_candidates: list[chinese_memory.ChineseCandidate] = []
        self._english_candidates: list[english_memory.EnglishCandidate] = []
        self._display_items: list[tuple[str, int]] = []
        self._display_cursor_index = 0
        self._rime_candidate_count = 0
        self._last_input_leak_log_at = 0.0
        self._rime_page_no = 0
        self._rime_is_last_page = True
        self._ascii_mode = config.env_str("VOICE_IME_START_ASCII", "0").strip().lower() in {"1", "true", "yes", "on"}
        self._pending_shift_toggle: int | None = None
        self._rime: rime_backend.RimeSession | None = None
        _start_voice_ipc_server()
        if config.env_str("VOICE_IME_KEYBOARD_BACKEND", "rime").lower() != "demo":
            try:
                self._rime = rime_backend.RimeSession()
            except Exception:
                # Keep the engine usable even if librime/schema setup is broken.
                tb = traceback.format_exc()
                log_error("Rime 初始化失败\n" + tb)
                traceback.print_exc()

    def do_process_key_event(self, keyval: int, keycode: int, state: int) -> bool:  # noqa: D401
        """IBus key event entry point."""
        global _FOCUSED_ENGINE
        _FOCUSED_ENGINE = self
        if state & IBus.ModifierType.RELEASE_MASK:
            return self._handle_shift_toggle_release(keyval)

        if self._handle_shift_toggle_press(keyval, state):
            return True
        self._cancel_pending_shift_toggle(keyval)

        # Voice hotkey.  Works even when there is a preedit buffer.
        # 主热键（默认 Ctrl+Alt+V）优先匹配；raw 热键（默认 Ctrl+Alt+B）在
        # 主热键不匹配时才尝试，两者互不重叠且命中后都提前 return。
        if voice_hotkey.matches_ctrl_alt_letter(
            IBus,
            keyval,
            state,
            int(IBus.ModifierType.CONTROL_MASK),
            int(IBus.ModifierType.MOD1_MASK),
        ):
            self._handle_voice_hotkey()
            return True

        # 「原文语音输入」热键：识别结果跳过 LLM 后处理，只走确定性规整。
        if voice_hotkey.matches_ctrl_alt_letter(
            IBus,
            keyval,
            state,
            int(IBus.ModifierType.CONTROL_MASK),
            int(IBus.ModifierType.MOD1_MASK),
            letters=voice_hotkey.raw_hotkey_letters(),
        ):
            self._handle_voice_hotkey(raw=True)
            return True

        if self._ascii_mode:
            return False

        # Let application/global shortcuts go to the application/desktop before
        # Rime sees them.  This fixes GNOME screenshot Shift+Super+S being
        # consumed as a normal "s" key by the input method.
        if state & APP_SHORTCUT_MASK:
            return False

        if self._rime is not None:
            # In Chinese mode Rime maps '/' to '、'.  For command workflows
            # like pi's /new, default to a literal ASCII slash when there is no
            # active composition.
            if self._should_forward_ascii_slash(keyval, state):
                return False
            if not (state & APP_SHORTCUT_MASK):
                if self._handle_display_navigation_key(keyval):
                    return True
            if self._handle_rime_overlay_selection(keyval):
                return True
            if keyval in CANDIDATE_PAGE_PREV_KEYS or keyval in CANDIDATE_PAGE_NEXT_KEYS:
                self._display_cursor_index = 0
            if keyval in (IBus.KEY_Return, IBus.KEY_KP_Enter) and self._raw_input:
                # Rime's Enter behavior is raw-code commit.  Keep that behavior,
                # and additionally remember ASCII words for future candidates.
                raw_token = self._raw_input
                raw = self._raw_with_literal_prefix(raw_token)
                learned = self._english.learn(raw_token) if raw == raw_token else False
                self._rime.clear()
                self._raw_input = ""
                self._chinese_candidates = []
                self._english_candidates = []
                self._display_items = []
                self._display_cursor_index = 0
                self._refresh_rime()
                self.commit_text(text(raw))
                if learned:
                    self._show_aux(f"已记住英文词：{raw}", 1200)
                return True

            tracked_input_key = self._track_raw_input_before_rime(keyval, state)
            handled = self._rime.process_key(keyval, state)
            self._commit_rime_pending()
            self._refresh_rime()
            # Some Rime states/backends can return False for plain input-code
            # keys even though this engine is presenting its own preedit and
            # candidate list from _raw_input.  If we return False in that case,
            # the application receives the latin code too, producing text like
            # "muqian目前".  Once we have accepted an input-code/editing key into
            # _raw_input, keep it inside the IME.
            if tracked_input_key and not handled:
                now = time.monotonic()
                if now - self._last_input_leak_log_at > 2.0:
                    self._last_input_leak_log_at = now
                    log_error(f"键盘输入保护：Rime 未接管 keyval={keyval}, raw={self._raw_input!r}，已阻止原始字母漏到应用")
            return bool(handled or tracked_input_key)

        # Let other Ctrl/Alt/Super shortcuts go to applications.
        if state & APP_SHORTCUT_MASK:
            return False

        if self._buffer and self._handle_demo_navigation_key(keyval):
            return True

        if keyval == IBus.KEY_Escape:
            if self._buffer:
                self._clear()
                return True
            return False

        if keyval == IBus.KEY_BackSpace:
            if self._buffer:
                self._buffer = self._buffer[:-1]
                self._refresh()
                return True
            return False

        if keyval in (IBus.KEY_space, IBus.KEY_Return, IBus.KEY_KP_Enter):
            if self._buffer:
                self._commit_candidate(self._lookup.get_cursor_pos())
                return True
            return False

        if IBus.KEY_1 <= keyval <= IBus.KEY_9 and self._buffer:
            idx = keyval - IBus.KEY_1
            if idx < len(self._candidates):
                self._commit_candidate(idx)
                return True

        if keyval in PUNCT:
            if self._buffer:
                self._commit_candidate(self._lookup.get_cursor_pos(), clear_only=True)
                self.commit_text(text(PUNCT[keyval]))
                return True
            return False

        ch = IBus.keyval_to_unicode(keyval)
        if isinstance(ch, str) and ch:
            # This prototype uses latin letters as input code.  Replace this rule
            # if your custom method needs different keys.
            if ch.isascii() and (ch.isalpha() or ch == "'"):
                self._buffer += ch.lower()
                self._refresh()
                return True

        return False

    def do_candidate_clicked(self, index: int, button: int, state: int) -> None:
        if self._rime is not None:
            self._select_display_candidate(index)
            return
        if self._buffer and 0 <= index < len(self._candidates):
            self._commit_candidate(index)

    def do_page_down(self) -> None:
        if self._rime is not None:
            self._display_cursor_index = 0
            self._rime.process_key(IBus.KEY_Page_Down, 0)
            self._refresh_rime()
            return
        self._lookup.page_down()
        if self._use_inline_candidates():
            self._refresh()
        else:
            self.update_lookup_table(self._lookup, bool(self._buffer))

    def do_page_up(self) -> None:
        if self._rime is not None:
            self._display_cursor_index = 0
            self._rime.process_key(IBus.KEY_Page_Up, 0)
            self._refresh_rime()
            return
        self._lookup.page_up()
        if self._use_inline_candidates():
            self._refresh()
        else:
            self.update_lookup_table(self._lookup, bool(self._buffer))

    def do_cursor_down(self) -> None:
        if self._rime is not None:
            self._move_display_cursor(1)
            return
        self._lookup.cursor_down()
        if self._use_inline_candidates():
            self._refresh()
        else:
            self.update_lookup_table(self._lookup, bool(self._buffer))

    def do_cursor_up(self) -> None:
        if self._rime is not None:
            self._move_display_cursor(-1)
            return
        self._lookup.cursor_up()
        if self._use_inline_candidates():
            self._refresh()
        else:
            self.update_lookup_table(self._lookup, bool(self._buffer))

    def do_reset(self) -> None:
        self._clear()

    def do_focus_in(self) -> None:
        global _FOCUSED_ENGINE
        _FOCUSED_ENGINE = self

    def do_focus_out(self) -> None:
        # Keep _FOCUSED_ENGINE as the last active engine.  The GNOME custom
        # shortcut helper may run while a transient overlay has focus, but it
        # still needs to stop the recording owned by the previous application.
        self._clear()

    def do_set_cursor_location(self, x: int, y: int, w: int, h: int) -> None:
        # Keep the real cursor rectangle for overlap-risk detection.  We still
        # try to bias the inherited cursor area upward, but GNOME/IBus candidate
        # placement is mostly controlled by the focused client/panel; the robust
        # readability fix is the auxiliary preedit mirror below.
        self._cursor_area = (x, y, w, h)
        adjusted_y = y
        if self._lookup_would_open_above():
            line_px = h if h > 8 else _env_int("VOICE_IME_PREEDIT_LINE_PX", 36, minimum=1)
            gap_px = _env_int("VOICE_IME_CANDIDATE_GAP_PX", 8, minimum=0)
            adjusted_y = max(0, y - line_px - gap_px)
        try:
            IBus.Engine.do_set_cursor_location(self, x, adjusted_y, w, h)
        except Exception:
            pass

    def _monitor_bottom_for_cursor(self) -> int | None:
        if self._cursor_area is None:
            return None
        x, y, w, h = self._cursor_area
        try:
            gi.require_version("Gdk", "3.0")
            from gi.repository import Gdk  # noqa: WPS433

            display = Gdk.Display.get_default()
            if display is None:
                return None
            monitor = display.get_monitor_at_point(x + max(0, w // 2), y + max(0, h // 2))
            if monitor is not None:
                geometry = monitor.get_geometry()
                return int(geometry.y + geometry.height)
            screen = display.get_default_screen()
            return int(screen.get_height()) if screen is not None else None
        except Exception:
            return None

    def _lookup_would_open_above(self) -> bool:
        if not _env_bool("VOICE_IME_CANDIDATE_ADAPTIVE_ANCHOR", True):
            return False
        if self._cursor_area is None:
            return False
        bottom = self._monitor_bottom_for_cursor()
        if bottom is None:
            return False
        _x, y, _w, h = self._cursor_area
        space_below = max(0, bottom - (y + max(1, h)))
        row_px = _env_int("VOICE_IME_CANDIDATE_ROW_PX", 58, minimum=1)
        padding_px = _env_int("VOICE_IME_CANDIDATE_PADDING_PX", 28, minimum=0)
        return space_below < LOOKUP_PAGE_SIZE * row_px + padding_px

    def _preedit_mirror_enabled(self) -> bool:
        """Whether to mirror the current latin input code in the candidate panel.

        GNOME/IBus positions the candidate panel from cursor geometry reported by
        the focused client.  Some terminal/TUI clients report a rectangle near
        the baseline; when the input line is at the bottom of the screen, GNOME
        opens the popup upward and its bottom edge can cover the inline preedit.
        Engines cannot reliably move that popup after the client reports the
        geometry, so the safer fix is _use_inline_candidates().  This mirror is
        kept as a fallback for explicit popup mode.
        """
        mode = config.env_str("VOICE_IME_PREEDIT_MIRROR", "off").strip().lower()
        if mode in {"0", "false", "no", "off", "disabled", "none"}:
            return False
        if mode in {"auto", "adaptive"}:
            return self._lookup_would_open_above()
        return True

    def _set_composition_auxiliary(self, code: str) -> None:
        if code and self._preedit_mirror_enabled():
            self._aux_generation += 1
            self.update_auxiliary_text(text(f"输入码：{code}"), True)
            self._composition_aux_visible = True
            return
        if self._composition_aux_visible:
            self._aux_generation += 1
            self.update_auxiliary_text(text(""), False)
            self._composition_aux_visible = False

    def _candidate_ui_mode(self) -> str:
        return config.env_str("VOICE_IME_CANDIDATE_UI", "popup").strip().lower()

    def _use_inline_candidates(self) -> bool:
        """Render candidates inside preedit instead of the IBus popup.

        Inline mode is kept as an explicit opt-in fallback: it hides the IBus
        popup and appends compact candidates to the preedit string.  The default
        is the normal vertical IBus/GNOME lookup table.
        """
        mode = self._candidate_ui_mode()
        if mode in {"inline", "preedit", "terminal", "safe"}:
            return True
        # Default and legacy "auto" both use the normal IBus/GNOME lookup
        # table now; inline candidates are opt-in only.
        return False

    def _inline_preedit(self, preedit: str, labels: list[str]) -> str:
        if not preedit or not labels:
            return preedit
        max_items = _env_int("VOICE_IME_INLINE_CANDIDATES", LOOKUP_PAGE_SIZE, minimum=1)
        max_label_chars = _env_int("VOICE_IME_INLINE_LABEL_CHARS", 10, minimum=1)
        max_total_chars = _env_int("VOICE_IME_INLINE_MAX_CHARS", 96, minimum=20)
        parts: list[str] = []
        for i, label in enumerate(labels[:max_items]):
            compact = re.sub(r"\s+", " ", label).strip()
            compact = compact.split("〔", 1)[0].strip()
            if len(compact) > max_label_chars:
                compact = compact[:max_label_chars] + "…"
            marker = "▶" if i == self._display_cursor_index else ""
            parts.append(f"{marker}{i + 1}.{compact}")
        suffix = "  〔" + "  ".join(parts) + "〕"
        budget = max_total_chars - len(preedit)
        if budget <= 4:
            return preedit
        if len(suffix) > budget:
            suffix = suffix[: max(0, budget - 1)] + "…"
        return preedit + suffix

    def _refresh_visible_candidates(self) -> None:
        if self._rime is not None:
            self._refresh_rime()
        else:
            self._refresh()

    def _lookup_layout(self) -> tuple[int, IBus.Orientation]:
        # Keep the vertical candidate page stable when the IBus popup is used.
        return LOOKUP_PAGE_SIZE, IBus.Orientation.VERTICAL

    def _configure_lookup_layout(self) -> int:
        page_size, orientation = self._lookup_layout()
        if self._lookup.get_page_size() != page_size:
            self._lookup.set_page_size(page_size)
        if self._lookup.get_orientation() != orientation:
            self._lookup.set_orientation(orientation)
        return page_size

    def _shift_toggle_enabled(self) -> bool:
        return config.env_bool("VOICE_IME_SHIFT_TOGGLE_ASCII", True)

    def _handle_shift_toggle_press(self, keyval: int, state: int) -> bool:
        if keyval not in ASCII_TOGGLE_KEYS or not self._shift_toggle_enabled():
            return False
        # Do not steal Ctrl/Alt/Super combinations such as Ctrl+Shift or
        # Shift+Super shortcuts.  A plain Shift press is consumed and toggles on
        # key release only if no other key was used while Shift was held.
        if state & APP_SHORTCUT_MASK:
            self._pending_shift_toggle = None
            return False
        self._pending_shift_toggle = keyval
        return True

    def _handle_shift_toggle_release(self, keyval: int) -> bool:
        if keyval not in ASCII_TOGGLE_KEYS or self._pending_shift_toggle != keyval:
            if keyval in ASCII_TOGGLE_KEYS:
                self._pending_shift_toggle = None
            return False
        self._pending_shift_toggle = None
        self._toggle_ascii_mode()
        return True

    def _cancel_pending_shift_toggle(self, keyval: int) -> None:
        if self._pending_shift_toggle is not None and keyval not in ASCII_TOGGLE_KEYS:
            self._pending_shift_toggle = None

    def _toggle_ascii_mode(self) -> None:
        switching_to_ascii = not self._ascii_mode
        literal_commit = ""
        if switching_to_ascii:
            literal_commit = self._literal_composition_for_ascii_toggle()
        self._ascii_mode = switching_to_ascii
        self._clear()
        if literal_commit:
            self.commit_text(text(literal_commit))
        self._show_aux("已切换到英文模式" if self._ascii_mode else "已切换到中文模式", 1200)

    def _literal_composition_for_ascii_toggle(self) -> str:
        """Return raw latin composition to commit before switching to ASCII mode."""
        if self._rime is not None:
            if not self._raw_input:
                return ""
            raw_token = self._raw_input
            raw = self._raw_with_literal_prefix(raw_token)
            if raw == raw_token:
                self._english.learn(raw_token)
            return raw
        return self._buffer

    def _should_forward_ascii_slash(self, keyval: int, state: int) -> bool:
        if config.env_str("VOICE_IME_ASCII_SLASH", "1") == "0":
            return False
        if keyval not in (IBus.KEY_slash, IBus.KEY_KP_Divide):
            return False
        if state & APP_SHORTCUT_MASK:
            return False
        if self._raw_input:
            return False
        return not (self._rime and self._rime.context().composing)

    def _track_raw_input_before_rime(self, keyval: int, state: int) -> bool:
        if state & APP_SHORTCUT_MASK:
            return False
        if keyval == IBus.KEY_Escape:
            consumed = bool(self._raw_input or self._display_items)
            self._raw_input = ""
            self._display_cursor_index = 0
            return consumed
        if keyval == IBus.KEY_BackSpace:
            if not self._raw_input:
                return False
            self._raw_input = self._raw_input[:-1]
            self._display_cursor_index = 0
            return True
        if self._raw_input and self._display_items and IBus.KEY_1 <= keyval <= IBus.KEY_9:
            return True
        ch = IBus.keyval_to_unicode(keyval)
        if isinstance(ch, str) and ch and len(ch) == 1 and ch.isascii():
            # Do not start an IME composition with a digit.  Numbers should be
            # delivered to the application literally unless the user is already
            # composing a code/candidate selection.  Without this guard, typing
            # plain numbers is swallowed into _raw_input and is hard to commit.
            if not self._raw_input and ch.isdigit():
                return False
            if ch.isalnum() or ch in "_+.#-":
                self._raw_input += ch
                self._display_cursor_index = 0
                return True
            if not ch.isspace():
                consumed = bool(self._raw_input)
                self._raw_input = ""
                self._display_cursor_index = 0
                return consumed
        return False

    def _display_candidate_count(self) -> int:
        return len(self._display_items)

    def _handle_display_navigation_key(self, keyval: int) -> bool:
        if not self._display_items:
            return False
        # In the Rime-backed candidate menu, Up/Down moves the highlighted
        # candidate, while Left/Right turns pages.  This matches the usual IME
        # expectation for a vertical 5-candidate lookup table.
        if keyval in (IBus.KEY_Left, IBus.KEY_KP_Left):
            if self._rime is not None and self._rime_page_no > 0:
                self._turn_rime_page(IBus.KEY_Page_Up, cursor_to_end=False)
            return True
        if keyval in (IBus.KEY_Right, IBus.KEY_KP_Right):
            if self._rime is not None and not self._rime_is_last_page:
                self._turn_rime_page(IBus.KEY_Page_Down, cursor_to_end=False)
            return True
        if keyval in (IBus.KEY_Up, IBus.KEY_KP_Up):
            self._move_display_cursor(-1)
            return True
        if keyval in (IBus.KEY_Down, IBus.KEY_KP_Down):
            self._move_display_cursor(1)
            return True
        return False

    def _move_display_cursor(self, delta: int) -> None:
        count = self._display_candidate_count()
        if count <= 0:
            return
        next_index = self._display_cursor_index + delta
        if self._rime is not None:
            if next_index < 0 and self._rime_page_no > 0:
                self._turn_rime_page(IBus.KEY_Page_Up, cursor_to_end=True)
                return
            if next_index >= count and not self._rime_is_last_page:
                self._turn_rime_page(IBus.KEY_Page_Down, cursor_to_end=False)
                return
        self._display_cursor_index = next_index % count
        self._lookup.set_cursor_pos(self._display_cursor_index)
        if self._use_inline_candidates():
            self._refresh_visible_candidates()
        else:
            self.update_lookup_table(self._lookup, True)

    def _turn_rime_page(self, keyval: int, cursor_to_end: bool = False) -> None:
        if self._rime is None:
            return
        self._display_cursor_index = 0
        self._rime.process_key(keyval, 0)
        self._commit_rime_pending()
        self._refresh_rime()
        count = self._display_candidate_count()
        if count <= 0:
            return
        self._display_cursor_index = count - 1 if cursor_to_end else 0
        self._lookup.set_cursor_pos(self._display_cursor_index)
        if self._use_inline_candidates():
            self._refresh_rime()
        else:
            self.update_lookup_table(self._lookup, True)

    def _handle_demo_navigation_key(self, keyval: int) -> bool:
        count = len(self._candidates)
        if count <= 0:
            return False
        if keyval in CANDIDATE_PREV_KEYS:
            delta = -1
        elif keyval in CANDIDATE_NEXT_KEYS:
            delta = 1
        elif keyval in CANDIDATE_PAGE_PREV_KEYS:
            delta = -self._lookup.get_page_size()
        elif keyval in CANDIDATE_PAGE_NEXT_KEYS:
            delta = self._lookup.get_page_size()
        else:
            return False
        self._lookup.set_cursor_pos((self._lookup.get_cursor_pos() + delta) % count)
        if self._use_inline_candidates():
            self._refresh()
        else:
            self.update_lookup_table(self._lookup, True)
        return True

    def _handle_rime_overlay_selection(self, keyval: int) -> bool:
        if not self._display_items:
            return False
        if keyval == IBus.KEY_space:
            return self._select_display_candidate(self._display_cursor_index)
        if IBus.KEY_1 <= keyval <= IBus.KEY_9:
            return self._select_display_candidate(keyval - IBus.KEY_1)
        return False

    def _select_display_candidate(self, index: int) -> bool:
        if not (0 <= index < len(self._display_items)):
            return False
        kind, inner_index = self._display_items[index]
        if kind == "chinese":
            self._commit_chinese_candidate(inner_index)
            return True
        if kind == "english":
            self._commit_english_candidate(inner_index)
            return True
        if kind == "rime" and self._rime is not None:
            self._rime.process_key(IBus.KEY_1 + inner_index, 0)
            # Selecting one segment in a long composition can leave the rest of
            # the composition active.  Start the next candidate menu from the
            # first row instead of preserving the previously selected row.
            self._display_cursor_index = 0
            self._commit_rime_pending()
            self._refresh_rime()
            return True
        return False

    def _literal_prefix(self) -> str:
        if self._rime is None or not self._raw_input:
            return ""
        preedit = self._rime.context().preedit
        if not preedit:
            return ""
        prefix = INPUT_CODE_PREEDIT_SUFFIX_RE.sub("", preedit)
        return prefix if prefix != preedit else ""

    def _raw_with_literal_prefix(self, raw: str) -> str:
        return self._literal_prefix() + raw

    def _commit_chinese_candidate(self, index: int) -> None:
        cand = self._chinese_candidates[index]
        raw = self._raw_input
        commit = self._raw_with_literal_prefix(cand.text)
        self._chinese.learn(raw, cand.text)
        if self._rime is not None:
            self._rime.clear()
        self._raw_input = ""
        self._chinese_candidates = []
        self._english_candidates = []
        self._display_items = []
        self._display_cursor_index = 0
        self._refresh_rime()
        self.commit_text(text(commit))

    def _commit_english_candidate(self, index: int) -> None:
        cand = self._english_candidates[index]
        commit = self._raw_with_literal_prefix(cand.text)
        if commit == cand.text:
            self._english.learn(cand.text)
        if self._rime is not None:
            self._rime.clear()
        self._raw_input = ""
        self._chinese_candidates = []
        self._english_candidates = []
        self._display_items = []
        self._display_cursor_index = 0
        self._refresh_rime()
        self.commit_text(text(commit))

    def _refresh_rime(self) -> None:
        if self._rime is None:
            return
        ctx = self._rime.context()
        if not ctx.composing and not self._raw_input:
            self._lookup.clear()
            self._chinese_candidates = []
            self._english_candidates = []
            self._display_items = []
            self._display_cursor_index = 0
            self._rime_candidate_count = 0
            self._rime_page_no = 0
            self._rime_is_last_page = True
            self.update_preedit_text(text(""), 0, False)
            self._set_composition_auxiliary("")
            self.hide_lookup_table()
            return

        self._chinese_candidates = self._chinese.suggest(self._raw_input)
        self._english_candidates = self._english.suggest(self._raw_input)
        rime_candidates = list(ctx.candidates)
        self._rime_candidate_count = len(rime_candidates)
        self._rime_page_no = ctx.page_no
        self._rime_is_last_page = ctx.is_last_page

        # Learned Chinese and English candidates share the same priority model:
        # exact match first, then higher frequency, then more recent use.  This
        # lets repeated English commits overtake Chinese, and repeated Chinese
        # commits overtake English for the same code.
        #
        # Memory candidates are injected only on Rime's first page.  Otherwise a
        # high-frequency English token (for example "Open Code") would occupy
        # the same slot on every page turn and hide one real Rime candidate per
        # page.
        memory_display: list[tuple[str, int, object]] = []
        if self._rime_page_no == 0:
            memory_display.extend(("chinese", i, cand) for i, cand in enumerate(self._chinese_candidates))
            memory_display.extend(("english", i, cand) for i, cand in enumerate(self._english_candidates))

        def memory_rank(item: tuple[str, int, object]) -> tuple[int, int, float, int, int]:
            kind, index, cand = item
            exact = kind == "chinese" or bool(getattr(cand, "exact", False))
            return (
                0 if exact else 1,
                -int(getattr(cand, "freq", 0)),
                -float(getattr(cand, "last", 0.0)),
                0 if kind == "chinese" else 1,
                index,
            )

        memory_display.sort(key=memory_rank)

        # Learned memory usually outranks Rime.  But for codes ending in an
        # unfinished/abbreviated syllable (bangw -> bang + w, nih -> ni + h), a
        # previously learned shorter commit like "帮" must not hide Rime's
        # longer intended candidate "帮我".  In that case let Rime lead and put
        # non-duplicate memory entries after it.
        memory_first = not (
            english_memory.is_plausible_pinyin_code(self._raw_input)
            and not english_memory.is_probably_pinyin(self._raw_input)
        )
        rime_texts = {cand.text for cand in rime_candidates}
        if memory_first:
            memory_texts = {cand.text for _kind, _index, cand in memory_display}
            filtered_rime = [(i, cand) for i, cand in enumerate(rime_candidates) if cand.text not in memory_texts]
            filtered_memory = memory_display
        else:
            filtered_rime = [(i, cand) for i, cand in enumerate(rime_candidates)]
            filtered_memory = [item for item in memory_display if item[2].text not in rime_texts]

        display: list[tuple[str, int, object]] = []
        if memory_first:
            display.extend(filtered_memory)
        display.extend(("rime", i, cand) for i, cand in filtered_rime)
        if not memory_first:
            display.extend(filtered_memory)

        # Only send one page of rows to IBus.  This prevents the GTK candidate
        # panel from drawing built-in paging arrows; Rime page keys still work
        # through _turn_rime_page().
        page_size = self._configure_lookup_layout()
        display = display[:page_size]
        self._display_items = [(kind, index) for kind, index, _cand in display]

        preedit = ctx.preedit or self._raw_input
        self._lookup.clear()
        labels: list[str] = []
        for _kind, _index, cand in display:
            label = cand.text if not cand.comment else f"{cand.text} 〔{cand.comment}〕"
            labels.append(label)
            self._lookup.append_candidate(text(label))

        count = self._display_candidate_count()
        if count:
            self._display_cursor_index = max(0, min(self._display_cursor_index, count - 1))
            self._lookup.set_cursor_pos(self._display_cursor_index)
        else:
            self._display_cursor_index = 0

        inline = count > 0 and self._use_inline_candidates()
        shown_preedit = self._inline_preedit(preedit, labels) if inline else preedit
        self.update_preedit_text(text(shown_preedit), min(ctx.cursor_pos, len(preedit)), bool(shown_preedit))
        if inline:
            self._set_composition_auxiliary("")
            self.hide_lookup_table()
        else:
            self._set_composition_auxiliary(self._raw_input or preedit)
            self.update_lookup_table(self._lookup, count > 0)

    def _commit_rime_pending(self) -> None:
        if self._rime is None:
            return
        commit = self._rime.get_commit()
        if commit:
            raw = self._raw_input
            self._chinese.learn(raw, commit)
            self._raw_input = ""
            self._chinese_candidates = []
            self._english_candidates = []
            self._display_items = []
            self._display_cursor_index = 0
            self.commit_text(text(commit))

    def _refresh(self) -> None:
        if not self._buffer:
            self._clear()
            return
        page_size = self._configure_lookup_layout()
        self._candidates = core.suggest(self._buffer)[:page_size]
        self._lookup.clear()
        labels: list[str] = []
        for cand in self._candidates:
            label = cand.text if not cand.comment else f"{cand.text} 〔{cand.comment}〕"
            labels.append(label)
            self._lookup.append_candidate(text(label))
        cursor = max(0, min(self._lookup.get_cursor_pos(), len(self._candidates) - 1)) if self._candidates else 0
        self._lookup.set_cursor_pos(cursor)
        self._display_cursor_index = cursor
        inline = bool(self._candidates) and self._use_inline_candidates()
        shown_preedit = self._inline_preedit(self._buffer, labels) if inline else self._buffer
        self.update_preedit_text(text(shown_preedit), len(self._buffer), True)
        if inline:
            self._set_composition_auxiliary("")
            self.hide_lookup_table()
        else:
            self._set_composition_auxiliary(self._buffer)
            self.update_lookup_table(self._lookup, bool(self._candidates))

    def _clear(self) -> None:
        if self._rime is not None:
            self._rime.clear()
        self._raw_input = ""
        self._chinese_candidates = []
        self._english_candidates = []
        self._display_items = []
        self._display_cursor_index = 0
        self._rime_candidate_count = 0
        self._rime_page_no = 0
        self._rime_is_last_page = True
        self._pending_shift_toggle = None
        self._buffer = ""
        self._candidates = []
        self._lookup.clear()
        self.update_preedit_text(text(""), 0, False)
        self._set_composition_auxiliary("")
        self.hide_lookup_table()

    def _commit_candidate(self, index: int, clear_only: bool = False) -> None:
        if not self._candidates:
            commit = self._buffer
        else:
            index = max(0, min(index, len(self._candidates) - 1))
            commit = self._candidates[index].text
        self._clear()
        if not clear_only:
            self.commit_text(text(commit))
        else:
            self.commit_text(text(commit))

    def _show_aux(self, message: str, millis: int = 2500) -> None:
        self._composition_aux_visible = False
        self._aux_generation += 1
        generation = self._aux_generation
        self.update_auxiliary_text(text(message), True)

        def clear_if_current() -> bool:
            if self._aux_generation == generation:
                self.update_auxiliary_text(text(""), False)
            return False

        GLib.timeout_add(millis, clear_if_current)

    def _clipboard_prepare_delay_ms(self) -> int:
        # 300ms 与语音提交节奏一致；镜像方案下粘贴时刻已无焦点抖动，
        # 不再需要为"等焦点稳定"保留 1 秒（2026-08-28 排查结论）。
        default_ms = int(_env_float("VOICE_IME_CLIPBOARD_PREPARE_DELAY_SECONDS", 0.3, minimum=0.0) * 1000)
        return _env_int("VOICE_IME_CLIPBOARD_PREPARE_DELAY_MS", default_ms, minimum=0)

    def _handle_clipboard_file_paste(self, path_text: str, delay_ms: int | None = None) -> bool:
        """Commit clipboard text prepared by the external Ctrl+Alt+P helper.

        This mirrors the voice path as closely as possible: the helper owns
        clipboard access and the user-visible notification, while the engine only
        receives already-prepared text and commits it through _commit_voice_result.
        ``delay_ms`` is a diagnostic override (scripts/diagnose-paste.sh) for
        bisecting the timing window; ``None`` keeps the configured default.
        """
        try:
            path = Path(path_text).expanduser()
            content = path.read_text(encoding="utf-8")
        except Exception:
            log_error("输入法粘贴：读取外部剪贴板文件失败\n" + traceback.format_exc())
            return False
        if not content:
            log_error(f"输入法粘贴：外部剪贴板文件为空 path={path_text!r}")
            return False
        content = content.replace("\r\n", "\n").replace("\r", "\n")
        # CP2 检查点：文本已从暂存文件回到引擎。指纹与 CP1（clipboard-paste.sh）
        # 对照可确认这一跳内容完好、行数一致（多行是否被破坏在这一眼可见）。
        log_error(f"CP2 通过：外部文件已读回引擎 {clipboard_paste.content_fingerprint(content)} path={path_text!r}")
        max_chars = _env_int("VOICE_IME_CLIPBOARD_MAX_CHARS", 20000, minimum=1)
        truncated = False
        if len(content) > max_chars:
            content = content[:max_chars]
            truncated = True
        delay = self._clipboard_prepare_delay_ms() if delay_ms is None else max(0, int(delay_ms))
        # 与语音链路同通道的可见反馈：光标附近显示「正在粘贴」，确认热键已
        # 触发（桌面通知依赖通知守护进程且各环境表现不一；aux text 走的是
        # 与「正在录音」完全相同的 IBus 通道）。时长覆盖提交等待窗口。
        hint_ms = _env_int("VOICE_IME_CLIPBOARD_PREPARE_HINT_MS", delay + 800, minimum=600)
        self._show_aux("📋 正在粘贴……", hint_ms)
        # Keep the engine's own mode flags sane even if the target page rejects
        # the commit.  We intentionally do not call _clear() here because it
        # emits extra IBus UI signals; the paste-file path should be as close as
        # possible to the stable voice-result commit path.
        self._ascii_mode = False
        self._pending_shift_toggle = None
        if delay > 0:
            GLib.timeout_add(delay, self._commit_clipboard_file_text, content, truncated)
        else:
            self._commit_clipboard_file_text(content, truncated)
        return False

    def _commit_clipboard_file_text(self, content: str, truncated: bool) -> bool:
        # 语音完成路径同款顺序：先清辅助提示再提交，避免合成提交周围的多余
        # IBus 信号干扰敏感客户端；代际 +1 同时取消未到期的自动清除。
        self._aux_generation += 1
        self._composition_aux_visible = False
        self.update_auxiliary_text(text(""), False)
        self._commit_voice_result(content)
        # CP3a 检查点：commit_text 已在 GLib 主线程调用完毕（引擎视角的终点）。
        # 文字是否真正到达焦点应用不在引擎可观测范围内——投递观测（CP3b）
        # 由 scripts/diagnose-paste.sh 用 dbus-monitor 旁路完成。
        log_error(
            f"CP3a 通过：commit_text 已调用 {clipboard_paste.content_fingerprint(content)} truncated={truncated}"
        )
        return False

    def _voice_env_enabled(self, name: str, default: bool = False) -> bool:
        return config.env_bool(name, default)

    def _flush_composition_before_voice(self) -> None:
        if self._rime is not None:
            raw = self._raw_input
            commit = self._rime.commit_composition()
            if commit:
                self._chinese.learn(raw, commit)
                self._raw_input = ""
                self.commit_text(text(commit))
            self._refresh_rime()
        elif self._buffer:
            self._commit_candidate(self._lookup.get_cursor_pos())

    def _handle_voice_hotkey(self, raw: bool = False) -> None:
        """语音热键入口：raw=True 表示「原文语音输入」（跳过 LLM 后处理）。

        录音进行中时，无论按的是主热键还是 raw 热键都只停止当前录音；
        空闲时才按 raw 标志以对应模式开始新的录音。
        """
        log_error(f"语音热键触发：state={self._voice_state}, busy={self._voice_busy}, raw={raw}")
        # 本实例未持有录音时，热键语义（停止/提示处理中）只对持有实例成立：
        # 先转发，防止焦点切换后的空闲实例把"停止"当"开始"。
        if _forward_voice_hotkey_to_owner(self, raw):
            return
        mode = config.env_str("VOICE_IME_TRIGGER_MODE", "toggle").strip().lower()
        if mode == "fixed":
            # fixed 模式同样尊重 raw 标志：V/B 的差异（是否跳过 LLM 后处理）
            # 与 toggle 模式一致，只是录音方式仍是旧的固定时长。
            self._start_voice_input(raw=raw)
            return
        if self._voice_state == "recording":
            self._stop_toggle_voice_recording()
            return
        if self._voice_state == "processing" or self._voice_busy:
            self._show_aux("🎙️ 正在处理上一段语音……")
            return
        self._begin_toggle_voice_recording(raw=raw)

    def _begin_toggle_voice_recording(self, raw: bool = False) -> None:
        if self._voice_busy:
            self._show_aux("🎙️ 正在处理上一段语音……")
            return
        self._flush_composition_before_voice()
        max_seconds = _env_int("VOICE_IME_MAX_RECORD_SECONDS", 300)
        rms_full_scale = _env_float("VOICE_IME_OVERLAY_RMS_FULL_SCALE", 3000.0)
        try:
            session = audio_session.AudioSession(max_seconds=max_seconds, rms_full_scale=rms_full_scale)
            session.start()
        except Exception as exc:
            self._show_aux("语音录音失败：" + str(exc), 6000)
            return

        self._clear_voice_watchdog()
        self._voice_generation = getattr(self, "_voice_generation", 0) + 1
        self._audio_session = session
        self._voice_state = "recording"
        self._voice_busy = True
        self._voice_raw = raw
        self._voice_seen_sound = False
        self._voice_last_sound_at = time.monotonic()
        _claim_voice_owner(self)
        log_error(f"开始 toggle 语音录音 raw={raw}")
        # Pre-warm the ASR model onto the GPU concurrently with recording so
        # that by the time the user stops talking the model is already loaded
        # and the first /transcribe is near-instant.  Best-effort: any failure
        # is logged and ignored — recording/transcription must not depend on it.
        self._warm_voice_asr()
        # Do not keep a preedit composition during long recording; on Wayland
        # some clients then stop forwarding the second shortcut to IBus.
        self.update_auxiliary_text(text(f"🎙️ 正在录音，再按 {voice_hotkey.stop_hotkey_label()} 停止……"), True)

        self._overlay = voice_overlay.VoiceOverlay(
            on_stop=self._stop_toggle_voice_recording,
            on_cancel=self._cancel_toggle_voice_recording,
        )
        self._overlay.show_recording(
            mode=config.env_str("VOICE_IME_VOICE_MODE", "dictation"),
            # raw 模式先短路，避免无谓读取 llm.json 判断 LLM 是否开启。
            llm_enabled=not raw and llm_postprocess.enabled(),
            max_seconds=max_seconds,
        )
        GLib.timeout_add(100, self._update_recording_status)

    def _warm_voice_asr(self) -> None:
        """Spawn a daemon thread to pre-load the ASR model onto the GPU.

        Only the Qwen3-ASR backend exposes a ``/warm`` endpoint; for other
        backends this is a silent no-op.  The warmup runs concurrently with
        recording (which is itself on a background thread) and never blocks the
        IBus main loop.  All failures are caught and logged — transcription
        will still work by acquiring the model on demand at ``/transcribe``.
        """
        if not qwen_asr_runtime.selected():
            return

        def _do_warm() -> None:
            try:
                ok = qwen_asr_runtime.warm()
                log_error(f"ASR 预热完成：{'成功' if ok else '跳过/失败（best-effort）'}")
            except Exception as exc:  # never let warmup escape to the thread
                log_error(f"ASR 预热异常（忽略）：{exc}")

        threading.Thread(target=_do_warm, name="voice-asr-warmup", daemon=True).start()

    def _update_recording_status(self) -> bool:
        if self._voice_state != "recording" or self._audio_session is None:
            return False
        session = self._audio_session
        elapsed = session.elapsed()
        level = session.level()
        minutes = int(elapsed) // 60
        seconds = int(elapsed) % 60
        self.update_auxiliary_text(text(f"🎙️ 正在录音 {minutes:02d}:{seconds:02d}，再按 {voice_hotkey.stop_hotkey_label()} 停止……"), True)
        if self._overlay is not None:
            self._overlay.update_recording(elapsed=elapsed, level=level)
        self._maybe_auto_stop_toggle_recording(elapsed, level)
        if self._voice_state != "recording":
            return False
        if session.error():
            self._cancel_toggle_voice_recording("录音失败：" + session.error())
            return False
        if not session.is_recording():
            # Max-duration reached or recorder ended; process what we have.
            self._stop_toggle_voice_recording()
            return False
        return True

    def _maybe_auto_stop_toggle_recording(self, elapsed: float, level: float) -> None:
        if not self._voice_env_enabled("VOICE_IME_TOGGLE_SILENCE_AUTO_STOP", True):
            return
        now = time.monotonic()
        threshold = _env_float("VOICE_IME_TOGGLE_SOUND_LEVEL", 0.04)
        min_seconds = _env_float("VOICE_IME_TOGGLE_MIN_RECORD_SECONDS", 0.8)
        silence_seconds = _env_float("VOICE_IME_TOGGLE_SILENCE_SECONDS", 2.5)
        no_speech_timeout = _env_float("VOICE_IME_TOGGLE_NO_SPEECH_TIMEOUT", 8.0)
        if level >= threshold:
            self._voice_seen_sound = True
            self._voice_last_sound_at = now
            return
        if elapsed < min_seconds:
            return
        if self._voice_seen_sound and now - self._voice_last_sound_at >= silence_seconds:
            log_error("toggle 语音录音：静音自动停止")
            self._stop_toggle_voice_recording()
        elif not self._voice_seen_sound and elapsed >= no_speech_timeout:
            log_error("toggle 语音录音：未检测到声音，自动停止")
            self._stop_toggle_voice_recording()

    def _stop_toggle_voice_recording(self) -> None:
        if self._voice_state != "recording" or self._audio_session is None:
            return
        log_error("停止 toggle 语音录音，开始处理")
        session = self._audio_session
        self._voice_state = "processing"
        # Hide the overlay immediately.  If it got focus, this gives the window
        # manager a chance to restore focus to the original application before
        # commit_text runs after STT/LLM processing.
        if self._overlay is not None:
            self._overlay.hide()
            self._overlay = None
        self.update_auxiliary_text(text("🧠 正在识别语音……"), True)

        token = self._arm_voice_watchdog()
        skip_llm = self._voice_raw

        def worker() -> None:
            try:
                wav_path = session.stop()
                # skip_llm 传给 transcribe：volc-bigmodel 后端据此强制关闭云端
                # DDC 语义平滑，保证 raw 模式拿到未经云端改写的识别原文。
                raw = voice.transcribe(wav_path, skip_llm=skip_llm)
                log_error(f"STT 完成，长度={len(raw)}")
                # raw 模式（Ctrl+Alt+B）彻底跳过 LLM 后处理分支（min_chars
                # 逻辑因此不参与）；悬浮窗/状态 detail 也不显示 LLM 开启。
                # 先判断 raw 再读 llm.json，raw 模式下避免无谓的配置读取。
                llm_active = not skip_llm and llm_postprocess.enabled()
                # raw 模式只做确定性规整，提示文案不能暗示 LLM 参与。
                processing_hint = "📋 正在规整文本……" if skip_llm else "✨ 正在整理文本……"
                GLib.idle_add(self._voice_callback, token, self._show_voice_processing, processing_hint, self._voice_processing_detail(llm=llm_active))
                result = voice.postprocess(raw, skip_llm=skip_llm)
                log_error(f"后处理完成，长度={len(result)}")
                session.cleanup()
                GLib.idle_add(self._voice_callback, token, self._finish_voice_input, result, None)
            except Exception as exc:
                tb = traceback.format_exc()
                log_error("语音输入失败\n" + tb)
                traceback.print_exc()
                try:
                    session.cleanup()
                except Exception:
                    pass
                GLib.idle_add(self._voice_callback, token, self._finish_voice_input, "", str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def _cancel_toggle_voice_recording(self, message: str = "已取消语音输入") -> None:
        self._clear_voice_watchdog()
        self._voice_generation = getattr(self, "_voice_generation", 0) + 1
        log_error("取消 toggle 语音录音：" + message)
        if self._voice_state == "recording" and self._audio_session is not None:
            try:
                self._audio_session.cancel()
            except Exception:
                pass
        self._audio_session = None
        self._voice_state = "idle"
        self._voice_busy = False
        self._voice_raw = False
        _release_voice_owner(self)
        self.update_preedit_text(text(""), 0, False)
        self.update_auxiliary_text(text(""), False)
        if self._overlay is not None:
            self._overlay.hide()
        self._show_aux(message, 2000)

    def _voice_processing_detail(self, llm: bool = False) -> str:
        if llm and llm_postprocess.enabled():
            return llm_postprocess.active_model_label()
        backend = config.env_str("VOICE_IME_ASR_BACKEND", "qwen3-asr").strip().lower() or "qwen3-asr"
        if backend in {"qwen", "qwen3", "qwen3-asr", "qwen-asr"}:
            model = config.env_str("VOICE_IME_QWEN_ASR_MODEL", "1.7b")
            return f"Qwen3-ASR {model}"
        if backend in {"mimo-cloud", "mimo-cloud-asr"}:
            return f"MiMo 云端 {config.env_str('VOICE_IME_MIMO_CLOUD_ASR_MODEL', 'mimo-v2.5-asr')}"
        if backend in {"volc", "volc-bigmodel", "volc-bigmodel-asr"}:
            return "火山引擎 bigmodel ASR"
        if backend == "mimo-asr":
            return "MiMo-V2.5-ASR 本地"
        model = config.env_str("VOICE_IME_WHISPER_MODEL", "large-v3")
        device = config.env_str("VOICE_IME_WHISPER_DEVICE", "cuda")
        compute = config.env_str("VOICE_IME_WHISPER_COMPUTE", "float16")
        index = config.env_str("VOICE_IME_WHISPER_DEVICE_INDEX", "0")
        return f"faster-whisper {model} / {device}:{index} / {compute}"

    def _show_voice_processing(self, status: str, detail: str = "") -> bool:
        self.update_auxiliary_text(text(status), True)
        if self._overlay is not None:
            self._overlay.show_processing(status, detail)
        return False

    def _start_voice_input(self, raw: bool = False) -> None:
        """Legacy fixed-duration recording mode (raw=True skips LLM post-processing)."""
        if self._voice_busy:
            self._show_aux("🎙️ 正在识别上一段语音……")
            return
        self._flush_composition_before_voice()
        self._voice_busy = True
        self._voice_state = "processing"
        _claim_voice_owner(self)
        seconds = int(config.env_str("VOICE_IME_RECORD_SECONDS", "5"))
        self.update_preedit_text(text(f"🎙️ 录音 {seconds} 秒……"), 0, True)
        self._overlay = voice_overlay.VoiceOverlay(on_cancel=lambda: None)
        self._overlay.show_processing(f"🎙️ 录音 {seconds} 秒……", "固定时长录音")

        token = self._arm_voice_watchdog(extra_seconds=seconds)

        def worker() -> None:
            try:
                result = voice.record_and_transcribe(seconds, skip_llm=raw)
                GLib.idle_add(self._voice_callback, token, self._finish_voice_input, result, None)
            except Exception as exc:  # keep engine alive
                tb = traceback.format_exc()
                log_error("语音输入失败\n" + tb)
                traceback.print_exc()
                GLib.idle_add(self._voice_callback, token, self._finish_voice_input, "", str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def _clear_voice_watchdog(self) -> None:
        source = getattr(self, "_voice_watchdog_source", None)
        self._voice_watchdog_source = None
        if source is not None:
            GLib.source_remove(source)

    def _arm_voice_watchdog(self, extra_seconds: float = 0) -> int:
        self._clear_voice_watchdog()
        self._voice_generation = getattr(self, "_voice_generation", 0) + 1
        token = self._voice_generation
        # Includes startup, queued warmup, ASR and postprocessing. Transport
        # timeouts alone do not cover hung plugins, session.stop or a slow body.
        default = 2 * qwen_asr_runtime._timeout("VOICE_IME_QWEN_ASR_START_TIMEOUT", 120.0) + qwen_asr_runtime._timeout("VOICE_IME_QWEN_ASR_TIMEOUT", 120.0) + 60
        timeout = qwen_asr_runtime._timeout("VOICE_IME_PROCESSING_TIMEOUT", min(default + extra_seconds, 1800.0))
        self._voice_watchdog_source = GLib.timeout_add(int(timeout * 1000), self._voice_watchdog_expired, token, timeout)
        return token

    def _voice_callback(self, token: int, callback, *args) -> bool:
        if token != getattr(self, "_voice_generation", 0) or not self._voice_busy:
            return False  # discarded late completion; never commit into a new dictation
        callback(*args)
        return False

    def _voice_watchdog_expired(self, token: int, timeout: float) -> bool:
        if (token != getattr(self, "_voice_generation", 0) or not self._voice_busy
                or self._voice_state != "processing"):
            return False
        # This source is already dispatching and will return False; do not remove
        # it again in finish. Stale callbacks must never clear a newer source.
        self._voice_watchdog_source = None
        self._voice_generation += 1
        proc = qwen_asr_runtime._PROCESS
        # UI recovery runs on GLib immediately; child reaping never blocks IBus.
        self._finish_voice_input("", f"语音处理超时（{timeout:g}s），已恢复空闲，可再次按热键重试。请检查 ASR 日志。")
        threading.Thread(target=qwen_asr_runtime.recover_timeout, args=(proc,), daemon=True).start()
        return False

    def _finish_voice_input(self, result: str, error: str | None) -> bool:
        self._clear_voice_watchdog()
        self._voice_generation = getattr(self, "_voice_generation", 0) + 1
        # Accepted results keep their independent delayed commit below; advancing
        # the worker generation must not discard a successful pending commit.
        self._voice_busy = False
        self._voice_state = "idle"
        self._audio_session = None
        self._voice_raw = False
        _release_voice_owner(self)
        log_error(f"语音输入完成：error={bool(error)}, result_len={len(result or '')}")
        self.update_preedit_text(text(""), 0, False)
        self.update_auxiliary_text(text(""), False)
        if error:
            if self._overlay is not None:
                self._overlay.show_error(error)
                GLib.timeout_add(4000, lambda: (self._overlay.hide() if self._overlay else None, False)[1])
            self._show_aux("语音输入失败：" + error, 6000)
        elif result:
            if self._overlay is not None:
                self._overlay.hide()
            delay = int(config.env_str("VOICE_IME_COMMIT_DELAY_MS", "200"))
            GLib.timeout_add(max(0, delay), self._commit_voice_result, result)
        else:
            if self._overlay is not None:
                self._overlay.hide()
            self._show_aux("没有识别到文字")
        return False

    def _commit_voice_result(self, result: str) -> bool:
        self.commit_text(text(result))
        return False


class EngineFactory(IBus.Factory):
    __gtype_name__ = "IBusFactoryVoiceCustom"

    def __init__(self, bus: IBus.Bus):
        super().__init__(object_path=FACTORY_PATH, connection=bus.get_connection())
        self._bus = bus
        self._id = 0

    def do_create_engine(self, engine_name: str) -> IBus.Engine:
        self._id += 1
        return VoiceCustomEngine(self._bus, f"{ENGINE_PATH_PREFIX}/{self._id}")


class IMApp:
    def __init__(self, exec_by_ibus: bool):
        self.loop = GLib.MainLoop()
        self.bus = IBus.Bus()
        self.bus.connect("disconnected", lambda _bus: self.loop.quit())
        self.factory = EngineFactory(self.bus)
        if exec_by_ibus:
            self.bus.request_name(COMPONENT_NAME, 0)
        else:
            self.bus.register_component(make_component())

    def run(self) -> None:
        self.loop.run()


def main() -> int:
    try:
        locale.setlocale(locale.LC_ALL, "")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description=ENGINE_LONGNAME)
    parser.add_argument("--ibus", action="store_true", help="launched by ibus-daemon")
    parser.add_argument("--xml", action="store_true", help="print component XML")
    args = parser.parse_args()

    if args.xml:
        print(component_xml(), end="")
        return 0

    IBus.init()
    app = IMApp(exec_by_ibus=args.ibus)
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda *_: (app.loop.quit(), False)[1])
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
