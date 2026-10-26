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



def quiet_cpr(app) -> None:
    """不打印 prompt_toolkit 的 CPR 警告。

    prompt_toolkit 每次打开输入框都会问终端光标在哪（CPR），等 2 秒没收到回答就判定"不支持"，
    打印 "WARNING: your terminal doesn't support cursor position requests (CPR)."。终端其实支持、
    只是那一下回答慢了（刚开窗口、机器忙、回答到的时候输入正好被摘开——后台往屏幕上打印时会这样）
    也会误报。判定为不支持也不影响使用（只是少知道光标下面还剩几行），迟到的回答一来它会自己
    改回"支持"，所以只去掉这句提示。

    不放宽 2 秒的判定时间：没判定之前，每次关掉输入框它都要为没回来的查询再等最多 1 秒，
    放宽了反而让真不支持 CPR 的终端在开头那段时间里每次回车都卡一下。"""
    renderer = getattr(app, "renderer", None)
    if renderer is not None:
        renderer.cpr_not_supported_callback = None
