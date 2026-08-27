#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""蓝牙耳机 profile 切换后自动恢复播放的守护进程。

背景
----
语音输入法用 ``arecord`` 录音时会请求系统默认输入设备。当默认输入是蓝牙
耳机的麦克风时，PipeWire/WirePlumber 必须把耳机从 A2DP（高音质纯输出）切到
HSP/HFP（带麦克风的通话模式）—— 蓝牙协议规定二者互斥。切换瞬间 A2DP 的
输出 sink 被销毁，挂在它上面的浏览器、Spotify 等播放器的音频流中断，多数
播放器会因此自动暂停，且不会在切回 A2DP 后自动恢复。

本守护进程监听蓝牙耳机的 profile 变化，在耳机从 HFP 切回 A2DP 时，用
``playerctl`` 恢复**切换前正在播放**的播放器。只恢复快照里状态为 Playing
的，不会误启动用户手动暂停的歌单。

真相来源：``pactl list cards`` 里蓝牙卡的"活动配置/Active Profile"字段。
这是 PipeWire/PulseAudio 维护的真实状态——sink 的创建/销毁就由它驱动，所以
直接读它最可靠。（BlueZ 的 Device1 接口上没有 ActiveProfile，WirePlumber
也不暴露 DBus，因此用 pactl 轮询而非 DBus 信号订阅。）
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402  # GLib.MainLoop 驱动周期轮询

POLL_SECONDS = max(1, int(os.environ.get("VOICE_IME_BT_RESTORE_POLL", "2")))
# 切回 A2DP 后等播放器流稳定再发 play，避免过早调用被忽略。
RESTORE_DELAY_MS = max(0, int(os.environ.get("VOICE_IME_BT_RESTORE_DELAY_MS", "800")))


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# playerctl 封装
# --------------------------------------------------------------------------- #
def _run(cmd: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def list_players() -> list[str]:
    rc, out, _ = _run(["playerctl", "--list-all"])
    if rc != 0:
        return []
    names = []
    for line in out.splitlines():
        name = line.strip()
        # playerctl 偶尔在没有任何播放器时输出 "(null)"，忽略。
        if name and name != "(null)":
            names.append(name)
    return names


def player_status(name: str) -> str:
    rc, out, _ = _run(["playerctl", "--player", name, "status"])
    if rc != 0:
        return ""
    return out.strip()


def get_playing_players() -> set[str]:
    """返回当前 status 为 Playing 的播放器名集合。"""
    playing: set[str] = set()
    for name in list_players():
        if player_status(name) == "Playing":
            playing.add(name)
    return playing


def play(name: str) -> bool:
    rc, _, err = _run(["playerctl", "--player", name, "play"])
    if rc == 0:
        return True
    _log(f"恢复播放失败 {name}：{err.strip() or '未知错误'}")
    return False


# --------------------------------------------------------------------------- #
# 蓝牙 profile 探测（pactl list cards）
# --------------------------------------------------------------------------- #
# 中文 locale 下是"活动配置"，C/英文 locale 下是"Active Profile"。
_PROFILE_RE = re.compile(
    r"^\s*(?:活动配置|Active Profile)\s*[：:]\s*<?([^\s<]+)", re.M
)
_CARD_RE = re.compile(r"^\s*(?:名称|Name)\s*[：:]\s*(\S+)", re.M)


def is_hfp_active() -> bool:
    """任一已连接蓝牙卡处于带麦的 HSP/HFP profile 即认为"录音中"。

    读 ``pactl list cards``，对每个 bluez_card 解析其活动 profile，命中
    headset/hfp/hsp/hands 关键字即返回 True。pactl 缺失或无蓝牙卡时返回 False。
    """
    if shutil.which("pactl") is None:
        return False
    rc, out, _ = _run(["pactl", "list", "cards"])
    if rc != 0:
        return False

    # 按卡块开头切分。中文 locale 下是"卡 #NNN"或"卡片 #NNN"，英文是"Card #NNN"。
    # 用一个能同时覆盖三者的边界正则；re.split 在拿不到匹配时返回整段为单块，
    # 所以下面再用 bluez + profile 行精确判定，避免误取非蓝牙卡的 profile。
    for block in re.split(r"\n(?=卡(?:片)?\s*#|Card\s*#)", out):
        if "bluez" not in block:
            continue
        m = _PROFILE_RE.search(block)
        if not m:
            continue
        profile = m.group(1).lower()
        if any(k in profile for k in ("headset", "hfp", "hsp", "hands")):
            return True
    return False


# --------------------------------------------------------------------------- #
# 状态机
# --------------------------------------------------------------------------- #
class RestoreMachine:
    """A2DP ↔ HFP 状态机，负责快照与恢复。

    快照策略：在 A2DP 状态下每个周期持续刷新"正在播放"集合（保鲜）；一旦切到
    HFP 就冻结快照（HFP 下音乐已停，不能覆盖）；切回 A2DP 时恢复冻结快照里
    仍然存在的播放器，并把快照设为我们刚恢复的集合，保持状态一致。
    """

    def __init__(self) -> None:
        # 初次运行时无法判断"之前"状态，保守地置为 A2DP 并在首个周期填充快照。
        self.prev_hfp = False
        self.last_playing: set[str] = set()
        self._initialized = False

    def tick(self) -> None:
        cur_hfp = is_hfp_active()

        if not cur_hfp:
            if self._initialized and self.prev_hfp:
                # HFP -> A2DP：恢复冻结的快照。
                self._restore()
            else:
                # 正常 A2DP 周期：刷新快照（保鲜）。
                fresh = get_playing_players()
                if fresh != self.last_playing:
                    if fresh:
                        _log(f"播放快照更新：{', '.join(sorted(fresh))}")
                    self.last_playing = fresh
        # HFP 下不更新快照，冻结等待切回。

        self.prev_hfp = cur_hfp
        self._initialized = True

    def _restore(self) -> None:
        if not self.last_playing:
            _log("切回 A2DP，但快照为空，无需恢复。")
            return
        existing = set(list_players())
        to_restore = [n for n in self.last_playing if n in existing]
        gone = self.last_playing - existing
        if gone:
            _log(f"快照中已退出的播放器（跳过）：{', '.join(sorted(gone))}")
        if not to_restore:
            _log("切回 A2DP，但快照里的播放器均已退出。")
            self.last_playing = set()
            return
        # 延迟一点再恢复，让 A2DP sink 与播放器流稳定。用线程睡眠避免阻塞主循环。
        def _delayed() -> None:
            if RESTORE_DELAY_MS > 0:
                time.sleep(RESTORE_DELAY_MS / 1000.0)
            self._do_restore(to_restore)

        threading.Thread(target=_delayed, daemon=True).start()
        # 快照记为我们正在恢复的，保持一致；后续周期会按真实状态刷新。
        self.last_playing = set(to_restore)

    @staticmethod
    def _do_restore(names: list[str]) -> None:
        for name in names:
            try:
                if play(name):
                    _log(f"已恢复播放：{name}")
            except Exception as exc:
                # 单个播放器恢复失败不应影响其余的恢复。
                _log(f"恢复播放异常 {name}（已忽略）：{exc}")


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main() -> int:
    if shutil.which("playerctl") is None:
        _log("未找到 playerctl，请先安装：sudo dnf install playerctl")
        return 1
    if shutil.which("pactl") is None:
        _log("未找到 pactl（pipewire-pulseaudio 或 pulseaudio-utils）。")
        return 1

    _machine = RestoreMachine()
    _log(
        f"蓝牙播放恢复守护进程已启动（轮询 {POLL_SECONDS}s，恢复延迟 {RESTORE_DELAY_MS}ms）"
    )

    # 首次跑一次填充快照，之后周期轮询。
    _machine.tick()
    GLib.timeout_add_seconds(POLL_SECONDS, _poll_tick, _machine)
    GLib.MainLoop().run()
    return 0


def _poll_tick(machine: RestoreMachine) -> bool:
    try:
        machine.tick()
    except Exception as exc:  # 轮询绝不能让主循环挂掉
        _log(f"轮询异常（已忽略）：{exc}")
    return GLib.SOURCE_CONTINUE


if __name__ == "__main__":
    sys.exit(main())
