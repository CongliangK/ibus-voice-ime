#!/usr/bin/env python3
"""零依赖终端选择菜单；编号输入为一基，结果索引为零基。"""
import sys
import os
import signal
import termios
import tty
import select


HELP = """用法：tui-select.py [--multi] --title 标题 --default 索引 [--checked 索引列表] -- 选项…
默认索引与初始勾选索引从 0 开始；--checked 接受逗号或空格分隔的索引。
单选：↑↓ 或 j/k 移动，Home/End 跳转，数字 1-9 直选并确认，回车确认。
多选：空格或数字 1-9 切换勾选，回车确认；Esc/q 或 Ctrl+C 取消。
非终端：输入一基编号，多选用逗号/空格分隔；空行采用默认，输入结束即失败。
结果为零基索引，输出到标准输出；标题、菜单、帮助与错误只输出到标准错误。
"""


def arguments(argv):
    title, default, checked, multi = "请选择", 0, set(), False
    while argv and argv[0] != "--":
        flag, argv = argv[0], argv[1:]
        if flag in ("-h", "--help"):
            sys.stderr.write(HELP)
            return None
        if flag == "--multi":
            multi = True
        elif flag in ("--title", "--default", "--checked"):
            if not argv:
                raise ValueError("参数缺少值")
            value, argv = argv[0], argv[1:]
            if flag == "--title":
                title = value
            elif flag == "--default":
                default = int(value)
            else:
                checked = set(int(n) for n in value.replace(",", " ").split())
        else:
            raise ValueError("未知参数，请查看 --help")
    options = argv[1:] if argv else []
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
    while len(sequence) < 12 and select.select([fd], [], [], 0.08)[0]:
        part = os.read(fd, 1)
        sequence += part
        if b"@" <= part <= b"~":
            break
    return key + sequence


def terminal(title, default, checked, multi, options):
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    current, drawn = default, 0
    checked = set(checked)

    # SIGTERM/SIGHUP 与 SIGINT 同待遇：默认动作会在 finally 恢复终端之前
    # 直接终止进程，遗留 raw 模式 + 隐藏光标（红队 2026-10-11 PTY 实测）。
    # 转成异常走统一的 finally 恢复路径；PEP 475 保证 os.read 会先跑完
    # 本 handler 再重试/抛出。退出前恢复调用方原有 disposition 而非 SIG_DFL。
    def _signal_cancel(signum, frame):
        raise KeyboardInterrupt

    handled_signals = {}
    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            handled_signals[sig] = signal.signal(sig, _signal_cancel)
        except (ValueError, OSError):
            pass  # 非主线程等场景无法安装，维持默认行为

    def draw():
        nonlocal drawn
        try:
            columns, rows = os.get_terminal_size(sys.stdout.fileno())
        except OSError:
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

    try:
        tty.setraw(fd)
        sys.stderr.write("\x1b[?25l")
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
    finally:
        # 先恢复 termios；即使菜单输出异常也不得遗留 raw 模式。
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stderr.write("\x1b[0m\x1b[?25h")
        sys.stderr.flush()
        for sig, previous in handled_signals.items():
            try:
                signal.signal(sig, previous)
            except (ValueError, OSError):
                pass


def main(result_stream=None):
    output = sys.stdout if result_stream is None else result_stream
    try:
        parsed = arguments(sys.argv[1:])
        if parsed is None:
            return 0
        chooser = terminal if sys.stdin.isatty() and sys.stdout.isatty() else fallback
        selected = chooser(*parsed)
        print(" ".join(str(index) for index in selected), file=output, flush=True)
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
