#!/usr/bin/env python3
"""零依赖终端选择菜单；编号输入为一基，结果索引为零基。"""
import sys
import os
import signal
import termios
import tty
import select
from contextlib import contextmanager
from pathlib import Path
import runpy


HELP = """用法：tui-select.py [--multi] --title 标题 --default 索引 [--checked 索引列表] -- 选项…
默认索引与初始勾选索引从 0 开始；--checked 接受逗号或空格分隔的索引。
单选：↑↓ 或 j/k 移动，Home/End 跳转，数字 1-9 直选并确认，回车确认。
多选：空格或数字 1-9 切换勾选，回车确认；Esc/q 或 Ctrl+C 取消。
非终端：输入一基编号，多选用逗号/空格分隔；空行采用默认，输入结束即失败。
结果为零基索引，输出到标准输出；标题、菜单、帮助与错误只输出到标准错误。
热键录入：tui-select.py --capture --title 语音热键 [--expect-format 格式提示]
仅终端可直接录入；按住修饰键后再按一个字母，确认后输出规范化热键标签。
Super 键终端捕获不到，如需 Super+字母请手动输入；Esc 取消。
"""


def arguments(argv):
    title, default, checked, multi = "请选择", 0, set(), False
    capture, hint = False, ""
    while argv and argv[0] != "--":
        flag, argv = argv[0], argv[1:]
        if flag in ("-h", "--help"):
            sys.stderr.write(HELP)
            return None
        if flag == "--multi":
            multi = True
        elif flag == "--capture":
            capture = True
        elif flag in ("--title", "--default", "--checked", "--expect-format"):
            if not argv:
                raise ValueError("参数缺少值")
            value, argv = argv[0], argv[1:]
            if flag == "--title":
                title = value
            elif flag == "--default":
                default = int(value)
            elif flag == "--expect-format":
                hint = value
            else:
                checked = set(int(n) for n in value.replace(",", " ").split())
        else:
            raise ValueError("未知参数，请查看 --help")
    options = argv[1:] if argv else []
    if capture:
        if multi or checked or options or default != 0:
            raise ValueError("热键录入不能同时指定菜单选项")
        return title, hint
    if hint:
        raise ValueError("格式提示仅用于热键录入")
    if not options or not 0 <= default < len(options):
        raise ValueError("请提供选项及有效的默认索引")
    if any(not 0 <= n < len(options) for n in checked):
        raise ValueError("初始勾选索引超出范围")
    if checked and not multi:
        raise ValueError("初始勾选仅用于多选模式")
    return title, default, checked, multi, options


def clean(text):
    return "".join(c if c.isprintable() else " " for c in text)


def clip(text, columns):
    # 中文/全角字符占两列；组合附加符不占列。不依赖第三方 wcwidth。
    result, width = [], 0
    for character in clean(text):
        code = ord(character)
        size = 0 if 0x300 <= code <= 0x36f else (2 if (
            0x1100 <= code <= 0x115f or 0x2e80 <= code <= 0xa4cf or
            0xac00 <= code <= 0xd7af or 0xf900 <= code <= 0xfaff or
            0xfe10 <= code <= 0xfe6f or 0xff01 <= code <= 0xff60 or
            0xffe0 <= code <= 0xffe6 or 0x1f300 <= code <= 0x1faff or
            0x20000 <= code <= 0x3ffff) else 1)
        if width + size > columns:
            break
        result.append(character)
        width += size
    return "".join(result)


def read_line():
    # 不用 TextIOWrapper.readline：它会预读并吞掉下一题在共享管道中的输入。
    data = bytearray()
    while True:
        value = os.read(sys.stdin.fileno(), 1)
        if not value:
            if not data:
                raise EOFError
            break
        if value == b"\n":
            break
        data.extend(value)
    return data.decode("utf-8").strip()


def fallback(title, default, checked, multi, options):
    sys.stderr.write(clean(title) + "\n")
    for index, label in enumerate(options):
        marker = " [已勾选]" if multi and index in checked else (
            " [默认]" if not multi and index == default else "")
        sys.stderr.write(f"  {index + 1}) {clean(label)}{marker}\n")
    while True:
        sys.stderr.write("请输入编号（多选用逗号/空格分隔，0=清空全部，回车采用默认）：" if multi else
                         "请输入编号（回车采用默认）：")
        sys.stderr.flush()
        reply = read_line()
        if not reply:
            return sorted(checked) if multi else [default]
        tokens = reply.replace(",", " ").split()
        if multi and tokens == ["0"]:
            return []  # 显式清空：默认勾选非空时空集否则不可达（红队 2026-10-11）
        if (tokens and all(token.isascii() and token.isdecimal() for token in tokens)
                and (multi or len(tokens) == 1)):
            try:
                # 超长数字串会让 int() 抛 ValueError（CPython 4300 位限制），
                # 属输入非法而非参数错误，按重问处理（红队 2026-10-11）。
                selected = {int(token) - 1 for token in tokens}
            except ValueError:
                selected = {-1}
            if all(0 <= index < len(options) for index in selected):
                return sorted(selected)
        sys.stderr.write("输入不合法，请输入列表中的编号。\n")


def read_key(fd):
    # 推回缓冲优先：ESC O 消歧推回的字节（如紧跟的回车确认）必须被
    # 后续的确认菜单读到，否则菜单会在空缓冲上死等（红队 P2-2）。
    if _PUSHBACK:
        return bytes([_PUSHBACK.pop(0)])
    key = os.read(fd, 1)
    if not key:
        raise EOFError
    if key != b"\x1b":
        return key
    # 单独 Esc 等待极短时间区分方向键；不吞下一次普通按键。
    if not select.select([fd], [], [], 0.08)[0]:
        return key
    sequence = os.read(fd, 1)
    if sequence not in (b"[", b"O"):
        return key
    # CSI/SS3 的末字节即使是字母，也属于方向键或功能键而非热键。
    while len(sequence) < 64 and select.select([fd], [], [], 0.08)[0]:
        part = os.read(fd, 1)
        if not part:
            raise EOFError
        sequence += part
        if b"@" <= part <= b"~":
            break
    return key + sequence


def _consume_csi(fd):
    """CSI 序列已提交：耐心等待终结字节（0x40-0x7E）。

    提交后的 CSI 不再受 80ms 短窗约束（红队实测 120ms 延迟会失去帧
    边界、把尾字母泄漏成热键）；迟到或截断的序列整体丢弃，绝不把
    尾部字节交还字母捕获。
    """
    length = 0
    while length < 64:
        if not select.select([fd], [], [], 0.5)[0]:
            return
        part = os.read(fd, 1)
        if not part:
            raise EOFError
        length += 1
        # '[' 虽落在 ECMA-48 终结区间，但 \x1b[[A（Linux 控制台 F1）里
        # 它是中间字节——当作非终结继续消费，否则尾字母泄漏成主键。
        if b"@" <= part <= b"~" and part != b"[":
            return


# 单字节推回缓冲：ESC O 消歧时读到的非 SS3 终结字节还给后续读取，
# 避免"Alt+Shift+O 后紧跟回车确认"被吞（红队 P2-2）。
_PUSHBACK = bytearray()


def capture_read(fd):
    """热键捕获专用读取：返回 (key, ignored)。

    与菜单版 read_key 的差异：转义序列保持帧边界——方向键/功能键
    整体忽略（ignored=True），绝不让尾字母泄漏成热键；ESC O 的
    SS3/Alt+Shift+O 歧义用短窗消歧：后续字节是 P/Q/R/S（F1-F4 等
    SS3 终结）才整体忽略，否则推回缓冲并按 Alt+Shift+O 组合返回。
    """
    if _PUSHBACK:
        return bytes([_PUSHBACK.pop(0)]), False
    key = os.read(fd, 1)
    if not key:
        raise EOFError
    if key != b"\x1b":
        return key, False
    if not select.select([fd], [], [], 0.08)[0]:
        return key, False  # 单独 Esc：取消语义
    second = os.read(fd, 1)
    if not second:
        raise EOFError
    if second == b"\x1b":
        # 双 ESC：可能是 Alt+Esc，也可能是 Alt+方向键（ESC ESC [ A）。
        if select.select([fd], [], [], 0.08)[0]:
            third = os.read(fd, 1)
            if not third:
                raise EOFError
            if third == b"[":
                _consume_csi(fd)
            return b"", True  # 双 ESC 引出的序列整体忽略
        return b"\x1b\x1b", False  # 无后续：按无效组合提示重试
    if second == b"[":
        _consume_csi(fd)
        return b"", True  # 方向键/功能键整体忽略
    if second == b"O":
        if select.select([fd], [], [], 0.08)[0]:
            third = os.read(fd, 1)
            if not third:
                raise EOFError
            if third in (b"P", b"Q", b"R", b"S"):
                return b"", True  # SS3 功能键（F1-F4）整体忽略
            _PUSHBACK.extend(third)  # 不是 SS3 终结：推回，别吞确认键
        return b"\x1bO", False  # Alt+Shift+O
    return key + second, False  # ESC+普通键 = Alt 组合


@contextmanager
def terminal_mode():
    """菜单与热键录入共用 raw、信号取消和恢复路径。"""
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)

    # SIGTERM/SIGHUP 与 SIGINT 同待遇：默认动作会在 finally 恢复终端之前
    # 直接终止进程，遗留 raw 模式 + 隐藏光标（红队 2026-10-11 PTY 实测）。
    # 转成异常走统一的 finally 恢复路径；PEP 475 保证 os.read 会先跑完
    # 本 handler 再重试/抛出。退出前恢复调用方原有 disposition 而非 SIG_DFL。
    def _signal_cancel(signum, frame):
        raise KeyboardInterrupt

    handled_signals = {}
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            handled_signals[sig] = signal.signal(sig, _signal_cancel)
        except (ValueError, OSError):
            pass  # 非主线程等场景无法安装，维持默认行为

    try:
        # 不清空已排队的输入：录入字母与确认可能在同一次读取前到达。
        tty.setraw(fd, termios.TCSANOW)
        sys.stderr.write("\x1b[?25l")
        sys.stderr.flush()
        yield fd
    finally:
        # 输出失败也要恢复调用方信号 disposition。
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        finally:
            try:
                sys.stderr.write("\x1b[0m\x1b[?25h")
                sys.stderr.flush()
            finally:
                for sig, previous in handled_signals.items():
                    try:
                        signal.signal(sig, previous)
                    except (ValueError, OSError):
                        pass


def terminal(title, default, checked, multi, options):
    current, drawn = default, 0
    checked = set(checked)

    def draw():
        nonlocal drawn
        try:
            columns, rows = os.get_terminal_size(sys.stdout.fileno())
        except OSError:
            columns, rows = 80, 24
        if columns <= 0 or rows <= 0:
            # 0x0 pty（未设 winsize 的终端）会把菜单截成空标签且只画一行，
            # 用户完全看不到选项——退回常规尺寸而非静默不可用。
            columns, rows = 80, 24
        columns, rows = max(1, columns - 1), max(1, rows)
        # 行数严格不超过终端高度：1 行窗口只画当前项，2 行起才容纳标题；
        # 最后一行不写换行，避免每次重绘把内容滚出屏幕（红队栅格仿真）。
        include_title = rows >= 2
        count = min(len(options), max(1, rows - (1 if include_title else 0)))
        start = min(max(0, current - count + 1), len(options) - count)
        lines = [clip(title, columns)] if include_title else []
        for index in range(start, start + count):
            marker = "[✓] " if index in checked else "[ ] "
            label = f"{'>' if index == current else ' '} {index + 1}) " + (marker if multi else "") + options[index]
            text = clip(label, columns)
            lines.append("\x1b[7m" + text + "\x1b[0m" if index == current else text)
        if drawn:
            sys.stderr.write(f"\x1b[{drawn - 1}A")
        for position, line in enumerate(lines):
            tail = "\r\n" if position < len(lines) - 1 else ""
            sys.stderr.write("\r\x1b[2K" + line + tail)
        drawn = len(lines)
        sys.stderr.flush()

    with terminal_mode() as fd:
        while True:
            draw()
            key = read_key(fd)
            if key in (b"\x03", b"\x1b", b"q"):
                raise KeyboardInterrupt
            if key in (b"\r", b"\n"):
                return sorted(checked) if multi else [current]
            if key in (b"\x1b[A", b"\x1bOA", b"k"):
                current = (current - 1) % len(options)
            elif key in (b"\x1b[B", b"\x1bOB", b"j"):
                current = (current + 1) % len(options)
            elif key in (b"\x1b[H", b"\x1bOH", b"\x1b[1~", b"\x1b[7~"):
                current = 0
            elif key in (b"\x1b[F", b"\x1bOF", b"\x1b[4~", b"\x1b[8~"):
                current = len(options) - 1
            elif b"1" <= key <= b"9" and int(key) <= len(options):
                current = int(key) - 1
                if not multi:
                    return [current]
                checked.symmetric_difference_update({current})
            elif key == b" " and multi:
                checked.symmetric_difference_update({current})


def capture_label(key):
    """传统终端字节映射；最终标签仍以引擎规范化规则为准。"""
    modifiers = []
    if key.startswith(b"\x1b"):
        # 只有 capture_read 的 Alt 组合（ESC+单字节）会到这里；
        # 不完整 CSI 由读取器整体丢弃，永不返回（红队帧边界修复）。
        if len(key) != 2 or key[1:2] == b"[":
            return None
        modifiers.append("Alt")
        key = key[1:]
    # Tab/换行/回车与 Ctrl+I/J/M 同字节，按不可用主键处理。
    if len(key) != 1 or key in (b"\t", b"\n", b"\r"):
        return None
    value = key[0]
    if 1 <= value <= 26:
        modifiers.insert(0, "Ctrl")
        letter = chr(value + ord("A") - 1)
    elif ord("A") <= value <= ord("Z"):
        modifiers.append("Shift")
        letter = chr(value)
    elif ord("a") <= value <= ord("z"):
        letter = chr(value).upper()
    else:
        return None
    return "+".join(modifiers + [letter]) if modifiers else letter.lower()


def manual_hotkey(fd, normalize):
    # 保持同一 raw 上下文，让 Esc/信号取消与捕获模式完全一致；手动字符
    # 显式回显，不经过会预读下一题的 TextIOWrapper。
    while True:
        sys.stderr.write("\r\n请输入热键（例如 Ctrl+Alt+V 或 Super+V，Esc 取消）：")
        sys.stderr.flush()
        data = bytearray()
        while True:
            key, ignored = capture_read(fd)
            if ignored:
                continue
            if key in (b"\x1b", b"\x03"):
                raise KeyboardInterrupt
            if key in (b"\r", b"\n"):
                break
            if key in (b"\x7f", b"\x08"):
                if data:
                    data.pop()
                    sys.stderr.write("\b \b")
            elif len(key) == 1 and key >= b" ":
                data.extend(key)
                sys.stderr.flush()
                sys.stderr.buffer.write(key)
            sys.stderr.flush()
        try:
            return normalize(data.decode("utf-8"))
        except (ValueError, UnicodeError):
            sys.stderr.write("\r\n输入不合法，请重试；热键仅支持修饰键加单字母或裸字母。\r\n")


def capture_hotkey(title, hint):
    # 延迟加载向导现有校验函数；菜单模式保持零依赖，不复制引擎语义。
    normalize = runpy.run_path(str(Path(__file__).with_name("apply-wizard-config.py")))["normalize_hotkey"]
    with terminal_mode() as fd:
        while True:
            sys.stderr.write("\r\n" + clean(title) + "\r\n"
                             "（实验性）按住修饰键后再按一个字母完成录入；Esc 取消。\r\n"
                             "注意：终端或桌面可能先截获组合——按键无响应时请 Esc 取消后改选手动输入。\r\n"
                             "Super 键、纯修饰键组合（如 Ctrl+Super）终端捕获不到，请手动配置。\r\n"
                             "裸字母按引擎规则等价于 Ctrl+Alt+字母；大写字母识别为 Shift+字母。\r\n"
                             "Tab/回车与 Ctrl+I/J/M 同字节，无法区分；Ctrl+Shift 也可能丢失 Shift，请手动输入。\r\n")
            if hint:
                sys.stderr.write("格式提示：" + clean(hint) + "\r\n")
            sys.stderr.flush()
            while True:
                key, ignored = capture_read(fd)
                if ignored:
                    # 方向键/功能键已整体忽略，不打断录入节奏。
                    continue
                if key == b"\x1b":
                    raise KeyboardInterrupt
                label = capture_label(key)
                if label is not None:
                    label = normalize(label)
                    break
                sys.stderr.write("主键只支持字母，请重试。\r\n")
                sys.stderr.flush()
            sys.stderr.write("\r\n")
            selected = terminal("已捕获：" + label, 0, set(), False,
                                ["使用 " + label, "重录", "手动输入"])[0]
            if selected == 0:
                return label
            if selected == 2:
                return manual_hotkey(fd, normalize)


def main(result_stream=None):
    output = sys.stdout if result_stream is None else result_stream
    try:
        parsed = arguments(sys.argv[1:])
        if parsed is None:
            return 0
        if len(parsed) == 2:
            if not sys.stdin.isatty():
                sys.stderr.write("错误：直接按键录入需要终端；请在配置向导中手动输入热键文本。\n")
                return 2
            result = capture_hotkey(*parsed)
        else:
            chooser = terminal if sys.stdin.isatty() and sys.stdout.isatty() else fallback
            selected = chooser(*parsed)
            result = " ".join(str(index) for index in selected)
        print(result, file=output, flush=True)
        return 0
    except KeyboardInterrupt:
        sys.stderr.write("\n已取消选择。\n")
        return 130
    except EOFError:
        sys.stderr.write("\n输入已结束，选择失败；需要无人值守请使用向导 --defaults。\n")
        return 1
    except (ValueError, UnicodeError):
        sys.stderr.write("错误：参数或输入编码不合法，请查看 --help。\n")
        return 2
    except (OSError, termios.error):
        sys.stderr.write("错误：终端或输入输出不可用，选择失败。\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
