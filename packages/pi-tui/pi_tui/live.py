"""活动区：屏幕底部若干行的差分重绘。

三个关键点，都是上一版踩过的坑：

1. **按实际占用行数擦除**。一行超过终端宽度会占两行甚至三行，按 len(lines)
   往上移光标会移少，屏幕上留下残渣——看起来就是"抖"。
2. **正文不走活动区**。模型输出的正文直接写 stdout 追加，活动区只放状态行。
   把正文塞进活动区会导致内容一长就重绘失败，而且必须等全部收完才能画，
   完全没有流式感。
3. **自带心跳**。转圈动画不能只在收到事件时才重画，否则工具跑 30 秒就定格。
"""

from __future__ import annotations

import asyncio
import math
import sys
import time
from typing import Callable, TextIO

from .theme import DEFAULT_THEME, Theme
from .width import display_width, terminal_width

CURSOR_UP = "\033[{n}A"
CLEAR_BELOW = "\033[J"
CLEAR_LINE = "\033[2K"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"


def rendered_rows(lines: list[str], width: int) -> int:
    """这些行在终端上实际占多少行（考虑自动折行）。"""
    rows = 0
    for line in lines:
        w = display_width(line)
        rows += max(1, math.ceil(w / width)) if w else 1
    return rows


class LiveRegion:
    """屏幕底部的可重绘区域。

    历史内容正常追加进 scrollback（可滚动、可复制），只有底部状态行在动。
    """

    def __init__(
        self,
        stream: TextIO | None = None,
        theme: Theme = DEFAULT_THEME,
        min_interval: float = 0.05,
    ) -> None:
        self.stream = stream or sys.stdout
        self.theme = theme
        self.min_interval = min_interval
        self._lines: list[str] = []
        self._rows = 0
        self._last_draw = 0.0
        self._hidden_cursor = False
        self._at_line_start = True

    @property
    def interactive(self) -> bool:
        return hasattr(self.stream, "isatty") and self.stream.isatty()

    # -- 内部 ---------------------------------------------------------- #

    def _erase(self) -> None:
        if not self._rows or not self.interactive:
            self._lines, self._rows = [], 0
            return
        # 按实际占用行数往上移，不是按逻辑行数
        self.stream.write("\r" + CLEAR_LINE)
        if self._rows > 1:
            self.stream.write(CURSOR_UP.format(n=self._rows - 1))
        self.stream.write(CLEAR_BELOW)
        self._lines, self._rows = [], 0

    def _ensure_line_start(self) -> None:
        """正文流式写入可能停在半行上，追加历史前先换行。"""
        if not self._at_line_start:
            self.stream.write("\n")
            self._at_line_start = True

    # -- 对外 ---------------------------------------------------------- #

    def update(self, lines: list[str], force: bool = False) -> None:
        """重绘状态行。限帧，避免闪烁。"""
        if not self.interactive:
            return
        now = time.monotonic()
        if not force and self._lines and now - self._last_draw < self.min_interval:
            return
        self._last_draw = now
        if not self._hidden_cursor:
            self.stream.write(HIDE_CURSOR)
            self._hidden_cursor = True
        self._erase()
        self._ensure_line_start()
        if lines:
            self.stream.write("\n".join(lines))
            self._lines = lines
            self._rows = rendered_rows(lines, terminal_width())
        self.stream.flush()

    def append(self, lines: list[str]) -> None:
        """在活动区上方插入永久内容，活动区保持在最下面。"""
        saved = list(self._lines)
        self._erase()
        self._ensure_line_start()
        if lines:
            self.stream.write("\n".join(lines) + "\n")
        if saved:
            self.update(saved, force=True)
        else:
            self.stream.flush()

    def stream_text(self, text: str) -> None:
        """把正文原样写出去（流式输出的主通道）。

        先擦掉活动区：正文是要留在 scrollback 里的，不能和状态行混在一起。
        """
        if not text:
            return
        self._erase()
        self.stream.write(text)
        self._at_line_start = text.endswith("\n")
        self.stream.flush()

    def finalize(self, lines: list[str] | None = None) -> None:
        """收起活动区，可选地写入最终内容。"""
        self._erase()
        self._ensure_line_start()
        if lines:
            self.stream.write("\n".join(lines) + "\n")
        if self._hidden_cursor and self.interactive:
            self.stream.write(SHOW_CURSOR)
            self._hidden_cursor = False
        self.stream.flush()

    def clear(self) -> None:
        self.finalize()


class Ticker:
    """定时回调，给转圈动画提供心跳。

    没有它的话，动画只在收到事件时才前进——工具跑 30 秒就会定格在同一帧，
    用户会以为卡死了。
    """

    def __init__(self, callback: Callable[[], None], interval: float = 0.1) -> None:
        self.callback = callback
        self.interval = interval
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # 不在事件循环里（比如单测），静默跳过
        self._task = loop.create_task(self._run())

    async def _run(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.interval)
                try:
                    self.callback()
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None


class TextStreamer:
    """匀速吐字缓冲。

    provider 的 SSE 事件是成批到的——一次可能给 20 个字，然后停 300 毫秒。
    收到就立刻整块写出去，视觉上就是"蹦"。这里把 token 先存进缓冲，再由
    心跳按固定节奏一个个吐出来。

    吐字速度自适应：缓冲积压越多吐得越快，保证永远不落后于模型（目标是
    drain_seconds 内清空当前缓冲），同时下限保证至少一个字，不会卡住。
    """

    def __init__(
        self,
        write: Callable[[str], None],
        interval: float = 0.016,      # ~60fps
        drain_seconds: float = 0.35,  # 缓冲内容在这么久内吐完
        max_chunk: int = 16,
    ) -> None:
        self.write = write
        self.interval = interval
        self.drain_seconds = drain_seconds
        self.max_chunk = max_chunk
        self._buffer: list[str] = []
        self._task: asyncio.Task | None = None

    @property
    def pending(self) -> int:
        return sum(len(chunk) for chunk in self._buffer)

    def feed(self, text: str) -> None:
        if not text:
            return
        self._buffer.append(text)
        self._start()

    def _start(self) -> None:
        if self._task and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.flush()      # 不在事件循环里（单测），退化成直接写
            return
        self._task = loop.create_task(self._run())

    def _take(self, count: int) -> str:
        out: list[str] = []
        while count > 0 and self._buffer:
            head = self._buffer[0]
            if len(head) <= count:
                out.append(head)
                count -= len(head)
                self._buffer.pop(0)
            else:
                out.append(head[:count])
                self._buffer[0] = head[count:]
                count = 0
        return "".join(out)

    async def _run(self) -> None:
        try:
            idle = 0
            while True:
                await asyncio.sleep(self.interval)
                if not self._buffer:
                    idle += 1
                    if idle > 30:      # 半秒没内容就歇了，等下次 feed 唤醒
                        return
                    continue
                idle = 0
                ticks = max(1, int(self.drain_seconds / self.interval))
                count = max(1, min(self.max_chunk, -(-self.pending // ticks)))
                self.write(self._take(count))
        except asyncio.CancelledError:
            pass

    def flush(self) -> None:
        """立刻吐完剩余内容。

        在插入工具行、结束回答之前必须调用，否则顺序会乱——正文会跑到
        工具日志后面去。
        """
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        rest = self._take(self.pending)
        if rest:
            self.write(rest)

    def stop(self) -> None:
        self.flush()


class Spinner:
    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, theme: Theme = DEFAULT_THEME) -> None:
        self.theme = theme
        self._start = time.monotonic()

    def reset(self) -> None:
        self._start = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._start

    def frame(self) -> str:
        return self.FRAMES[int(self.elapsed * 12) % len(self.FRAMES)]

    def render(self, label: str, detail: str = "") -> str:
        """状态行：转圈 + 秒数固定在左边，详情放最后。

        秒数放最后会被长度不定的详情推着乱跳，看着很躁。
        """
        t = self.theme
        head = f"{t.accent(self.frame())} {t.muted(f'{self.elapsed:>4.0f}s')}  {t.muted(label)}"
        return f"{head}  {t.muted(detail)}" if detail else head


class StatusBar:
    def __init__(self, theme: Theme = DEFAULT_THEME) -> None:
        self.theme = theme
        self.model = ""
        self.thinking = "off"
        self.tokens = 0
        self.cost = 0.0
        self.hint = ""

    def render(self, width: int) -> str:
        left = self.theme.muted(f"{self.model}  ·  think {self.thinking}")
        bits = []
        if self.tokens:
            bits.append(f"{self.tokens // 1000}k tok")
        if self.cost:
            bits.append(f"¥{self.cost:.3f}")
        if self.hint:
            bits.append(self.hint)
        right = self.theme.muted("  ·  ".join(bits))
        gap = max(1, width - display_width(left) - display_width(right))
        return left + " " * gap + right