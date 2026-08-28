"""色彩能力与调色板。

丑陋的第一大来源是硬编码背景色：在浅色终端上，你精心调的深色卡片会变成
一块脏斑。这里的规则是 **只设前景色，永不设背景色**，让终端自己的主题
透出来。层次靠亮度（dim / 正常 / bold）表达，不靠堆颜色。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from enum import IntEnum


class ColorMode(IntEnum):
    NONE = 0
    ANSI16 = 1
    ANSI256 = 2
    TRUECOLOR = 3


def detect_color_mode(stream=None) -> ColorMode:
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):  # https://no-color.org
        return ColorMode.NONE
    if os.environ.get("FORCE_COLOR"):
        return ColorMode.TRUECOLOR
    if not hasattr(stream, "isatty") or not stream.isatty():
        return ColorMode.NONE  # 重定向到文件时输出纯文本
    term = os.environ.get("TERM", "")
    if term in ("dumb", ""):
        return ColorMode.NONE
    if os.environ.get("COLORTERM") in ("truecolor", "24bit"):
        return ColorMode.TRUECOLOR
    if "256" in term:
        return ColorMode.ANSI256
    return ColorMode.ANSI16


@dataclass(frozen=True)
class Color:
    """一个颜色的三档表示，按终端能力降级。"""

    rgb: tuple[int, int, int]
    c256: int
    c16: int

    def fg(self, mode: ColorMode) -> str:
        if mode is ColorMode.NONE:
            return ""
        if mode is ColorMode.TRUECOLOR:
            r, g, b = self.rgb
            return f"\033[38;2;{r};{g};{b}m"
        if mode is ColorMode.ANSI256:
            return f"\033[38;5;{self.c256}m"
        return f"\033[{self.c16}m"


# 低饱和度调色板：亮红亮绿那种默认 ANSI 色是"廉价终端感"的主要来源。
# 这几个色在深色和浅色终端上都能读。
ACCENT = Color((95, 135, 215), 68, 34)      # 冷蓝，唯一的强调色
SUCCESS = Color((95, 175, 95), 71, 32)      # 柔和绿
WARN = Color((215, 135, 95), 173, 33)       # 陶土橙
ERROR = Color((215, 95, 95), 167, 31)       # 砖红
THINKING = Color((135, 135, 175), 103, 35)  # 灰紫
MUTED = Color((128, 128, 128), 244, 90)     # 中灰

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
ITALIC = "\033[3m"


class Theme:
    """所有着色都走这里，方便统一降级和测试。"""

    def __init__(self, mode: ColorMode | None = None) -> None:
        self.mode = mode if mode is not None else detect_color_mode()

    @property
    def enabled(self) -> bool:
        return self.mode is not ColorMode.NONE

    def paint(self, text: str, color: Color | None = None, *, bold=False, dim=False, italic=False) -> str:
        if not self.enabled or not text:
            return text
        prefix = ""
        if color is not None:
            prefix += color.fg(self.mode)
        if bold:
            prefix += BOLD
        if dim:
            prefix += DIM
        if italic:
            prefix += ITALIC
        return f"{prefix}{text}{RESET}" if prefix else text

    # 语义化快捷方式：调用处不该出现具体颜色
    def accent(self, t: str, **kw) -> str:
        return self.paint(t, ACCENT, **kw)

    def success(self, t: str, **kw) -> str:
        return self.paint(t, SUCCESS, **kw)

    def warn(self, t: str, **kw) -> str:
        return self.paint(t, WARN, **kw)

    def error(self, t: str, **kw) -> str:
        return self.paint(t, ERROR, **kw)

    def thinking(self, t: str, **kw) -> str:
        return self.paint(t, THINKING, **kw)

    def muted(self, t: str, **kw) -> str:
        return self.paint(t, MUTED, dim=True, **kw)

    def bold(self, t: str) -> str:
        return self.paint(t, None, bold=True)


DEFAULT_THEME = Theme()
