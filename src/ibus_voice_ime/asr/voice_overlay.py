# -*- coding: utf-8 -*-
"""Small GTK status window for voice input.

The overlay is best-effort.  If GTK is unavailable or the compositor refuses the
window, callers can keep using the IBus auxiliary/preedit status.
"""
from __future__ import annotations

import os
from typing import Callable

from ibus_voice_ime import config

from ibus_voice_ime.asr import voice_hotkey

import gi

try:  # GTK may be unavailable in minimal sessions.
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GLib, Gtk  # type: ignore
except Exception:  # pragma: no cover - environment dependent
    Gdk = None  # type: ignore
    GLib = None  # type: ignore
    Gtk = None  # type: ignore


class VoiceOverlay:
    def __init__(self, on_stop: Callable[[], None] | None = None, on_cancel: Callable[[], None] | None = None):
        self.available = Gtk is not None and config.env_bool("VOICE_IME_OVERLAY", True)
        self._on_stop = on_stop
        self._on_cancel = on_cancel
        self._window = None
        self._status = None
        self._detail = None
        self._bar = None
        self._stop_button = None
        self._cancel_button = None
        if self.available:
            self._build()

    def _build(self) -> None:
        assert Gtk is not None
        # Normal top-level window is the most reliable GTK window type on
        # GNOME/Wayland.  Some compositors still focus it despite notification
        # hints; when that happens we handle the voice hotkey inside the overlay
        # and hide it before committing text so focus can return to the previous app.
        self._window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self._window.set_title("语音输入")
        self._window.set_keep_above(True)
        self._window.set_resizable(False)
        self._window.set_decorated(False)
        self._window.set_default_size(320, 120)
        # 默认不接收键盘焦点：悬浮窗抢焦点会触发引擎实例切换（曾把 toggle
        # 停止变成叠新录音，2026-10-09 事故），且停止热键已由引擎的进程级
        # 所有权转发兜底，无需悬浮窗自己接键。要 Esc 取消/窗口内接键的用户
        # 可显式设 VOICE_IME_OVERLAY_CATCH_HOTKEY=1。
        catch_hotkey = config.env_bool("VOICE_IME_OVERLAY_CATCH_HOTKEY", False)
        self._window.set_accept_focus(catch_hotkey)
        self._window.set_focus_on_map(catch_hotkey)
        self._window.set_can_focus(catch_hotkey)
        self._window.set_skip_taskbar_hint(True)
        if Gdk is not None:
            self._window.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self._window.connect("delete-event", self._handle_delete)
        self._window.connect("key-press-event", self._handle_key_press)

        outer = Gtk.Frame()
        outer.set_shadow_type(Gtk.ShadowType.OUT)
        self._window.add(outer)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(12)
        outer.add(box)

        self._status = Gtk.Label(label="🎙️ 准备录音")
        self._status.set_xalign(0)
        box.pack_start(self._status, False, False, 0)

        self._detail = Gtk.Label(label="")
        self._detail.set_xalign(0)
        box.pack_start(self._detail, False, False, 0)

        self._bar = Gtk.ProgressBar()
        self._bar.set_show_text(False)
        box.pack_start(self._bar, False, False, 0)

        if config.env_bool("VOICE_IME_OVERLAY_BUTTONS", False):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            box.pack_start(row, False, False, 0)

            self._stop_button = Gtk.Button(label="停止")
            self._stop_button.set_can_focus(False)
            self._stop_button.connect("clicked", lambda *_: self._on_stop and self._on_stop())
            row.pack_start(self._stop_button, True, True, 0)

            self._cancel_button = Gtk.Button(label="取消")
            self._cancel_button.set_can_focus(False)
            self._cancel_button.connect("clicked", lambda *_: self._on_cancel and self._on_cancel())
            row.pack_start(self._cancel_button, True, True, 0)
        else:
            hint = Gtk.Label(label=f"再按 {voice_hotkey.stop_hotkey_label()} 停止；或说完后静音自动识别")
            hint.set_xalign(0)
            box.pack_start(hint, False, False, 0)

    def _handle_delete(self, *_args):
        if self._on_cancel:
            self._on_cancel()
        return True

    def _handle_key_press(self, _widget, event):
        if Gdk is None:
            return False
        state = int(event.state)
        keyval = int(event.keyval)
        # 录音中按主热键（Ctrl+Alt+V）或原文热键（Ctrl+Alt+B）都能停止录音。
        if voice_hotkey.matches_ctrl_alt_letter(
            Gdk,
            keyval,
            state,
            int(Gdk.ModifierType.CONTROL_MASK),
            int(Gdk.ModifierType.MOD1_MASK),
        ) or voice_hotkey.matches_ctrl_alt_letter(
            Gdk,
            keyval,
            state,
            int(Gdk.ModifierType.CONTROL_MASK),
            int(Gdk.ModifierType.MOD1_MASK),
            letters=voice_hotkey.raw_hotkey_letters(),
        ):
            if self._on_stop:
                self._on_stop()
            return True
        if keyval == Gdk.KEY_Escape:
            if self._on_cancel:
                self._on_cancel()
            return True
        return False

    def show_recording(self, *, mode: str = "dictation", llm_enabled: bool = False, max_seconds: int | None = None) -> None:
        if not self.available:
            return
        assert self._window is not None and self._status is not None and self._detail is not None
        self._status.set_text("🎙️ 正在录音 00:00")
        llm = "LLM 开启" if llm_enabled else "LLM 关闭"
        limit = f" | 最多 {max_seconds} 秒" if max_seconds else ""
        self._detail.set_text(f"模式 {mode} | {llm}{limit}")
        self.set_level(0.0)
        if self._stop_button is not None:
            self._stop_button.set_sensitive(True)
            self._stop_button.set_label("停止")
        if self._cancel_button is not None:
            self._cancel_button.set_sensitive(True)
        self._window.show_all()
        self._position_window()

    def update_recording(self, *, elapsed: float, level: float) -> None:
        if not self.available:
            return
        assert self._status is not None
        minutes = int(elapsed) // 60
        seconds = int(elapsed) % 60
        self._status.set_text(f"🎙️ 正在录音 {minutes:02d}:{seconds:02d}")
        self.set_level(level)

    def show_processing(self, status: str, detail: str = "") -> None:
        if not self.available:
            return
        assert self._window is not None and self._status is not None and self._detail is not None
        self._status.set_text(status)
        self._detail.set_text(detail)
        if self._bar is not None:
            self._bar.pulse()
        if self._stop_button is not None:
            self._stop_button.set_sensitive(False)
            self._stop_button.set_label("处理中")
        if self._cancel_button is not None:
            self._cancel_button.set_sensitive(False)
        self._window.show_all()
        self._position_window()

    def show_error(self, message: str) -> None:
        if not self.available:
            return
        assert self._window is not None and self._status is not None and self._detail is not None
        self._status.set_text("⚠️ 语音输入失败")
        self._detail.set_text(message[:300])
        if self._bar is not None:
            self._bar.set_fraction(0.0)
        if self._stop_button is not None:
            self._stop_button.set_sensitive(False)
        if self._cancel_button is not None:
            self._cancel_button.set_sensitive(True)
            self._cancel_button.set_label("关闭")
        self._window.show_all()
        self._position_window()

    def set_level(self, level: float) -> None:
        if not self.available or self._bar is None:
            return
        self._bar.set_fraction(min(1.0, max(0.0, level)))

    def hide(self) -> None:
        if self.available and self._window is not None:
            self._window.hide()

    def destroy(self) -> None:
        if self.available and self._window is not None:
            self._window.destroy()
            self._window = None

    def _position_window(self) -> None:
        if not self.available or self._window is None or Gtk is None:
            return
        pos = config.env_str("VOICE_IME_OVERLAY_POSITION", "top-center").strip().lower()
        try:
            screen = self._window.get_screen()
            monitor = screen.get_primary_monitor()
            geo = screen.get_monitor_geometry(monitor)
            self._window.realize()
            width, height = self._window.get_size()
            if pos == "center":
                x = geo.x + (geo.width - width) // 2
                y = geo.y + (geo.height - height) // 2
            else:
                x = geo.x + (geo.width - width) // 2
                y = geo.y + 80
            self._window.move(max(0, x), max(0, y))
        except Exception:
            self._window.set_position(Gtk.WindowPosition.CENTER)
