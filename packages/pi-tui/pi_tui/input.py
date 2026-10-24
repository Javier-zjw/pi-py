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
    from prompt_toolkit.application import get_app, run_in_terminal
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.document import Document
    from prompt_toolkit.enums import DEFAULT_BUFFER
    from prompt_toolkit.filters import Condition, has_completions, has_focus, is_done, to_filter
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import ConditionalContainer, Dimension, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
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

    排序：名字或别名完全相同 > 名字前缀 > 别名前缀 > 名字里包含，各组内保持传入顺序。
    """
    if not text.startswith("/") or any(ch.isspace() for ch in text):
        return []
    query = text[1:].lower()
    exact: list[SlashCommand] = []
    prefix: list[SlashCommand] = []
    alias: list[SlashCommand] = []
    contains: list[SlashCommand] = []
    for command in commands:
        name = command.name.lower()
        if query and (name == query or query in (a.lower() for a in command.aliases)):
            exact.append(command)
        elif name.startswith(query):
            prefix.append(command)
        elif any(a.lower().startswith(query) for a in command.aliases):
            alias.append(command)
        elif query in name:
            contains.append(command)
    return exact + prefix + alias + contains


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


# 菜单不用 prompt_toolkit 默认的灰底白块：透明底；高亮行沿用默认的 reverse，配上主题青色成一条青色底条
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
    - ``key_actions``：不打印东西、只改状态的快捷键（如 ``"s-tab"`` 切换模式）→ 回调，
      回调后立刻重画（输入框上下的提示随之更新）。补全菜单打开时不抢菜单自己的按键。
    - ``footer``：编辑时画在输入行下方的内容（如输入框下沿的细线 + 当前模式），可以多行，
      每次重画都重新取；提交时不留下。
    - ``commands``：行首输入 / 时弹出的命令菜单（见 SlashCompleter）。菜单打开时回车会先补全
      高亮的那条（没高亮就取第一条）再提交，所以 ``/mo`` 回车就是 ``/model``。

    不用 prompt_toolkit 的 bottom_toolbar：它固定在屏幕最底行，输入行和它之间会空出一大块，
    提交后这块空白还留在 scrollback 里。提示信息放进输入框上方的分隔线即可。
    """

    def __init__(
            self,
            history_file: str | Path | None = None,
            key_handlers: dict[str, Callable[[], None]] | None = None,
            key_actions: dict[str, Callable[[], None]] | None = None,
            erase_when_done: bool = False,
            commands: Commands | None = None,
            footer: Callable[[], str] | None = None,
    ) -> None:
        self.session: Any = None
        self.completer: SlashCompleter | None = None
        if not HAS_PROMPT_TOOLKIT:
            return

        bindings = KeyBindings()

        @bindings.add("escape", "enter")     # Alt+Enter 换行
        def _(event) -> None:
            event.current_buffer.insert_text("\n")

        # 回车 / Tab 都不依赖菜单是否已经算出来（补全是异步的，打字快时按键会先到）：
        # 菜单里有选中项用选中项，否则当场匹配取第一条
        # 只管输入框；ctrl+r 搜索历史时的回车等仍走 prompt_toolkit 自己的绑定
        in_input = has_focus(DEFAULT_BUFFER)

        @bindings.add("enter", filter=in_input & Condition(lambda: self.completer is not None))
        def _(event) -> None:
            self._accept_command(event.current_buffer)
            event.current_buffer.validate_and_handle()

        @bindings.add("tab", filter=in_input & Condition(self._typing_command))
        def _(event) -> None:
            """补全命令名并留一个空格，接着输参数"""
            self._accept_command(event.current_buffer)
            event.current_buffer.insert_text(" ")

        @bindings.add("c-c", filter=in_input)
        def _(event) -> None:
            """有字时清空这一行，空行时才退出（同 Claude Code）"""
            if event.current_buffer.text:
                event.current_buffer.reset()
            else:
                event.app.exit(exception=KeyboardInterrupt, style="class:aborting")

        for key, handler in (key_handlers or {}).items():
            bindings.add(key)(_print_above(handler))
        for key, action in (key_actions or {}).items():
            bindings.add(key, filter=in_input & ~has_completions)(_act(action))

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
            # 固定预留会让输入行下面一直空着一大块；菜单空间由 _input_height 按需撑开
            reserve_space_for_menu=0,
        )
        if footer is not None:
            # PromptSession 只提供贴在屏幕最底行的 bottom_toolbar；这里把一行插在输入区正下方
            # （布局最外层 HSplit 的第 0 个孩子是输入区，补全菜单浮在输入区内，所以菜单在这行上面）
            line = ConditionalContainer(
                # 不定高：按内容行数（细线 + 模式提示可以是两行）
                Window(FormattedTextControl(lambda: ANSI(footer())), dont_extend_height=True),
                filter=~is_done,
            )
            self.session.layout.container.children.insert(1, line)
        input_window = next(w for w in self.session.layout.find_all_windows()
                            if getattr(w.content, "buffer", None) is self.session.default_buffer)
        if footer is not None:
            # 输入窗口默认会撑满下方剩余的空间，这行就被挤到屏幕最底下（和 bottom_toolbar 一样）
            input_window.dont_extend_height = to_filter(True)
        if self.completer is not None:
            # 菜单浮在输入窗口里，窗口要按候选数撑开（见 _input_height）
            input_window.height = self._input_height
            self.session.default_buffer.on_completions_changed += _highlight_first

    def _input_height(self) -> Dimension:
        """输入窗口的高度：平时一行；有命令候选时撑开放菜单（不用 reserve_space_for_menu
        固定预留，否则输入行下面一直空着一大块）。

        按输入的原文算候选数：↑↓ 浏览时 prompt_toolkit 会把选中的候选写进输入框，
        按那段文字算菜单会缩成一行"""
        buffer = self.session.default_buffer
        if get_app().is_done or self.completer is None:
            return Dimension()
        state = buffer.complete_state
        text = state.original_document.text if state is not None else buffer.text
        count = len(self.completer.matches(text))
        lines = buffer.document.line_count
        return Dimension(min=lines + min(count, MENU_ROWS)) if count else Dimension()

    def _typing_command(self) -> bool:
        text = self.session.default_buffer.document.text_before_cursor
        return bool(self.completer and self.completer.matches(text))

    def _accept_command(self, buffer: Any) -> None:
        """把正在输的命令名换成选中的候选（没选就是第一条）"""
        state = buffer.complete_state
        if state is not None and state.completions:
            buffer.apply_completion(state.current_completion or state.completions[0])
            return
        if buffer.document.text_after_cursor or self.completer is None:
            return
        matches = self.completer.matches(buffer.text)
        if matches:
            text = f"/{matches[0].name}"
            buffer.document = Document(text, len(text))

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


def _highlight_first(buffer: Any) -> None:
    """菜单一出来就高亮第一条（回车执行的就是它）。只改下标、不改输入框的字——
    go_to_completion 会把候选写进输入框，接着打字就成了 /helpm。
    已经输完整的唯一候选 prompt_toolkit 随后会丢掉，这时不能选，否则下标指向空列表"""
    state = buffer.complete_state
    if state is None or state.complete_index is not None or not state.completions:
        return
    if len(state.completions) == 1 and state.completions[0].text == buffer.document.text_before_cursor:
        return
    state.complete_index = 0


def _act(action: Callable[[], None]) -> Callable[[Any], None]:
    def run(event) -> None:
        action()
        event.app.invalidate()
    return run


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
