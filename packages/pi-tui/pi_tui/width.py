"""宽度计算。

中文、emoji 占两列，ANSI 转义序列占零列。用 len() 排版的表格在中文面前
一定是歪的——这是"Python TUI 很丑"最常见的具体死因。stdlib 的
unicodedata 就够了，不需要 wcwidth。
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import unicodedata

ANSI_RE = re.compile(r"\033\[[0-9;]*[a-zA-Z]|\033\][^\007]*\007")

# 零宽：组合记号、变体选择符、零宽连接符
_ZERO_WIDTH_CATEGORIES = {"Mn", "Me", "Cf"}


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def char_width(ch: str) -> int:
    if unicodedata.category(ch) in _ZERO_WIDTH_CATEGORIES:
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def display_width(text: str) -> int:
    """文本在终端上占多少列，忽略 ANSI。"""
    return sum(char_width(c) for c in strip_ansi(text))


def truncate(text: str, limit: int, ellipsis: str = "…") -> str:
    """按显示宽度截断（不会把宽字符切一半）。"""
    if display_width(text) <= limit:
        return text
    budget = limit - display_width(ellipsis)
    out, used = [], 0
    for ch in strip_ansi(text):
        w = char_width(ch)
        if used + w > budget:
            break
        out.append(ch)
        used += w
    return "".join(out) + ellipsis


def pad(text: str, width: int, align: str = "left") -> str:
    """按显示宽度补空格，中英混排也能对齐。"""
    gap = max(0, width - display_width(text))
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def wrap(text: str, width: int) -> list[str]:
    """按显示宽度折行，中文按字断，英文按词断。"""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current, used = "", 0
        for token in _tokenize(paragraph):
            w = display_width(token)
            if used + w > width and current:
                lines.append(current.rstrip())
                current, used = "", 0
                if token.isspace():
                    continue
            current += token
            used += w
        if current:
            lines.append(current.rstrip())
    return lines


def _tokenize(text: str):
    """英文按空格切词，CJK 逐字切——CJK 没有词间空格，按词切会不折行。"""
    buffer = ""
    for ch in text:
        if char_width(ch) == 2:
            if buffer:
                yield buffer
                buffer = ""
            yield ch
        elif ch.isspace():
            buffer += ch
            yield buffer
            buffer = ""
        else:
            buffer += ch
    if buffer:
        yield buffer


def _terminal_size(default: tuple[int, int]) -> tuple[int, int]:
    """直接问终端（ioctl）：shutil.get_terminal_size 先看 COLUMNS / LINES 环境变量，
    导出过的话拖动窗口后它还是老数，和 prompt_toolkit 看到的不一致。
    用 sys.__stdout__：输入框开着时 sys.stdout 被 patch_stdout 换成了代理"""
    try:
        size = os.get_terminal_size(sys.__stdout__.fileno())
        if size.columns > 0 and size.lines > 0:
            return size.columns, size.lines
    except (AttributeError, OSError, ValueError):
        pass
    size = shutil.get_terminal_size(default)
    return size.columns, size.lines


def terminal_width(default: int = 80, maximum: int = 0) -> int:
    """当前终端宽度（每次现取，窗口拖动后马上变）。

    默认不设上限——用户把窗口拉宽就是想让内容跟着宽。需要限制正文栏宽时，
    在调用处传 maximum，而不是在这里一刀切。
    """
    columns = _terminal_size((default, 24))[0]
    return min(columns, maximum) if maximum else columns


def terminal_height(default: int = 24) -> int:
    """当前终端高度（行数）"""
    return _terminal_size((80, default))[1]


def prose_width(maximum: int = 100) -> int:
    """正文栏宽：长文本铺满整行很难读，这里才需要上限。"""
    return terminal_width(maximum=maximum)
