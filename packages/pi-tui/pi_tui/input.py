"""输入行。

用 prompt_toolkit（如果装了），否则退化成 input()。差别很大：

- 宽字符正确处理：中文一次退格删掉一个字，而不是要按两下
- 行编辑与历史：← → 改前面的字，↑ 翻上一条
- 异步：模型流式输出时仍然可以打字
- 不重复回显：由输入层自己控制，上层不再手动 echo 一遍
- 命令补全：行首输入 / 弹出命令菜单，↑↓ 选择，回车直接执行高亮的那条

装法：pip install prompt_toolkit
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # 可选依赖，缺了也能跑
    from prompt_toolkit import PromptSession
    from prompt_toolkit.application import run_in_terminal
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.filters import has_completions
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.patch_stdout import patch_stdout
    from prompt_toolkit.styles import Style

    HAS_PROMPT_TOOLKIT = True
except ImportError:  # pragma: no cover
    HAS_PROMPT_TOOLKIT = False
    Completer = object  # type: ignore[assignment,misc]

MENU_ROWS = 10      # 命令菜单最多同时显示几行，更多的滚动


@dataclass(frozen=True)
class SlashCommand:
    """补全菜单里的一条命令。name 不带 /；aliases 也能匹配，但菜单里只显示 name"""
    name: str
    description: str = ""
    aliases: tuple[str, ...] = ()


Commands = Iterable[SlashCommand] | Callable[[], Iterable[SlashCommand]]


def match_commands(text: str, commands: Iterable[SlashCommand]) -> list[SlashCommand]:
    """输入框内容 → 候选命令。只在行首是 / 且还没敲空格（还在输命令名）时给候选。

    排序：名字前缀匹配 > 别名前缀匹配 > 名字里包含，各组内保持传入顺序。
    """
    if not text.startswith("/") or any(ch.isspace() for ch in text):
        return []
    query = text[1:].lower()
    prefix: list[SlashCommand] = []
    alias: list[SlashCommand] = []
    contains: list[SlashCommand] = []
    for command in commands:
        name = command.name.lower()
        if name.startswith(query):
            prefix.append(command)
        elif any(a.lower().startswith(query) for a in command.aliases):
            alias.append(command)
        elif query in name:
            contains.append(command)
    return prefix + alias + contains


def _resolve(commands: Commands) -> list[SlashCommand]:
    return list(commands() if callable(commands) else commands)


class SlashCompleter(Completer):
    """prompt_toolkit 补全器：只补命令名，参数部分不管。commands 可以是列表或返回列表的函数
    （扩展、提示模板会在运行中变化，用函数每次现取）"""

    def __init__(self, commands: Commands) -> None:
        self.commands = commands

    def matches(self, text: str) -> list[SlashCommand]:
        return match_commands(text, _resolve(self.commands))

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        for command in self.matches(text):
            yield Completion(f"/{command.name}", start_position=-len(text),
                             display=f"/{command.name}", display_meta=command.description)


# 菜单不用 prompt_toolkit 默认的灰底白块：透明底，高亮行用主题的青色
MENU_STYLE = {
    "completion-menu": "bg:default",
    "completion-menu.completion": "bg:default fg:ansidefault",
    "completion-menu.completion.current": "bg:default fg:#56c8d6 bold",
    "completion-menu.meta.completion": "bg:default fg:#8a8a8a",
    "completion-menu.meta.completion.current": "bg:default fg:#c8c8c8",
    "scrollbar.background": "bg:default",
    "scrollbar.button": "bg:#5a5a5a",
}


class LineReader:
    """一行输入。Enter 提交，Shift/Alt+Enter 换行，↑↓ 翻历史。

    - ``key_handlers``：额外快捷键（prompt_toolkit 的键名，如 ``"c-o"``）→ 回调。回调在输入行
      上方打印内容，打完输入行原样恢复，正在输入的字不丢。
    - ``erase_when_done``：提交后擦掉输入框，由上层用自己的样式回显这句话。
    - ``commands``：行首输入 / 时弹出的命令菜单（见 SlashCompleter）。菜单打开时回车会先补全
      高亮的那条（没高亮就取第一条）再提交，所以 ``/mo`` 回车就是 ``/model``。

    不用 prompt_toolkit 的 bottom_toolbar：它固定在屏幕最底行，输入行和它之间会空出一大块，
    提交后这块空白还留在 scrollback 里。提示信息放进输入框上方的分隔线即可。
    """

    def __init__(
            self,
            history_file: str | Path | None = None,
            key_handlers: dict[str, Callable[[], None]] | None = None,
            erase_when_done: bool = False,
            commands: Commands | None = None,
    ) -> None:
        self.session: Any = None
        self.completer: SlashCompleter | None = None
        if not HAS_PROMPT_TOOLKIT:
            return

        bindings = KeyBindings()

        @bindings.add("escape", "enter")     # Alt+Enter 换行
        def _(event) -> None:
            event.current_buffer.insert_text("\n")

        @bindings.add("enter", filter=has_completions)
        def _(event) -> None:
            buffer = event.current_buffer
            state = buffer.complete_state
            completion = state.current_completion or state.completions[0]
            buffer.apply_completion(completion)
            buffer.validate_and_handle()

        for key, handler in (key_handlers or {}).items():
            bindings.add(key)(_print_above(handler))

        history = None
        if history_file:
            path = Path(history_file).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            history = FileHistory(str(path))

        if commands is not None:
            self.completer = SlashCompleter(commands)
        self.session = PromptSession(
            history=history,
            key_bindings=bindings,
            multiline=False,
            completer=self.completer,
            # 边打边补；和 ↑ 翻历史搜索冲突，二选一时保留补全（有补全器时才开）
            complete_while_typing=self.completer is not None,
            enable_history_search=self.completer is None,
            erase_when_done=erase_when_done,
            style=Style.from_dict(MENU_STYLE),
            # 菜单要在输入行下方留出空行。固定预留会让输入行下面一直空着一大块，
            # 所以平时为 0，输入 / 有候选时才按候选数撑开（见 _fit_menu）
            reserve_space_for_menu=0,
        )
        if self.completer is not None:
            self.session.default_buffer.on_text_changed += self._fit_menu

    def _fit_menu(self, buffer: Any) -> None:
        count = len(self.completer.matches(buffer.text)) if self.completer else 0
        self.session.reserve_space_for_menu = min(count, MENU_ROWS)

    @property
    def rich(self) -> bool:
        return self.session is not None

    async def read(self, prompt: str = "> ") -> str:
        """读一行。上层不要再回显——这里已经显示过了。"""
        if self.session is not None:
            self.session.reserve_space_for_menu = 0
            # patch_stdout：流式输出和输入行共存，输出不会把正在输入的内容冲掉
            with patch_stdout(raw=True):
                return await self.session.prompt_async(ANSI(prompt))
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: input(prompt))


def _print_above(handler: Callable[[], None]) -> Callable[[Any], None]:
    def run(event) -> None:
        run_in_terminal(handler)
    return run


def missing_dependency_hint(theme=None) -> str | None:
    """没装 prompt_toolkit 时给一句提示，让用户知道能变好。"""
    if HAS_PROMPT_TOOLKIT:
        return None
    text = "提示：pip install prompt_toolkit 可获得行编辑、历史记录和中文退格支持"
    return theme.muted(text) if theme else text
