"""输入行。

用 prompt_toolkit（如果装了），否则退化成 input()。差别很大：

- 宽字符正确处理：中文一次退格删掉一个字，而不是要按两下
- 行编辑与历史：← → 改前面的字，↑ 翻上一条
- 异步：模型流式输出时仍然可以打字
- 不重复回显：由输入层自己控制，上层不再手动 echo 一遍

装法：pip install prompt_toolkit
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

try:  # 可选依赖，缺了也能跑
    from prompt_toolkit import PromptSession
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.patch_stdout import patch_stdout

    HAS_PROMPT_TOOLKIT = True
except ImportError:  # pragma: no cover
    HAS_PROMPT_TOOLKIT = False


class LineReader:
    """一行输入。Enter 提交，Shift/Alt+Enter 换行，↑↓ 翻历史。"""

    def __init__(self, history_file: str | Path | None = None) -> None:
        self.session: Any = None
        if not HAS_PROMPT_TOOLKIT:
            return

        bindings = KeyBindings()

        @bindings.add("escape", "enter")     # Alt+Enter 换行
        def _(event) -> None:
            event.current_buffer.insert_text("\n")

        history = None
        if history_file:
            path = Path(history_file).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            history = FileHistory(str(path))

        self.session = PromptSession(
            history=history,
            key_bindings=bindings,
            multiline=False,
            enable_history_search=True,
            complete_while_typing=False,
        )

    @property
    def rich(self) -> bool:
        return self.session is not None

    async def read(self, prompt: str = "> ") -> str:
        """读一行。上层不要再回显——这里已经显示过了。"""
        if self.session is not None:
            # patch_stdout：流式输出和输入行共存，输出不会把正在输入的内容冲掉
            with patch_stdout(raw=True):
                return await self.session.prompt_async(ANSI(prompt))
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: input(prompt))


def missing_dependency_hint(theme=None) -> str | None:
    """没装 prompt_toolkit 时给一句提示，让用户知道能变好。"""
    if HAS_PROMPT_TOOLKIT:
        return None
    text = "提示：pip install prompt_toolkit 可获得行编辑、历史记录和中文退格支持"
    return theme.muted(text) if theme else text
