"""终端本身的准备和提醒：Windows 控制台打开 ANSI 支持；需要用户时响铃 / 发系统通知。"""

from __future__ import annotations

import sys


def prepare_terminal() -> None:
    """Windows：老控制台（conhost）默认不认 ANSI 转义，颜色和光标控制会变成乱码；
    打开虚拟终端处理、输出用 UTF-8。Windows Terminal 本来就支持，再开一次也没坏处。其他系统什么都不做"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleOutputCP(65001)
        handle = kernel32.GetStdHandle(-11)         # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)    # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:  # noqa: BLE001 —— 打不开就算了，最多颜色不对
        pass


def notify(message: str = "", *, bell: bool = True, desktop: bool = False) -> None:
    """提醒用户：响铃（终端标签页会标记、Dock 图标跳动，看终端设置）；desktop 时再发一条系统通知
    （OSC 9，iTerm2 / WezTerm / Ghostty / kitty 支持，不认的终端会忽略）。
    直接写真正的终端（sys.__stdout__）：输入框开着时 sys.stdout 是按行缓冲的代理"""
    stream = sys.__stdout__
    if stream is None or not stream.isatty():
        return
    out = ""
    if desktop and message:
        out += f"\033]9;{message}\007"
    if bell:
        out += "\a"
    if out:
        stream.write(out)
        stream.flush()
