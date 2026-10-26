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

from .paths import ProjectIndex, mention_token, path_candidates
from .terminal import quiet_cpr
from .width import terminal_height

try:  # 可选依赖，缺了也能跑
    from prompt_toolkit import PromptSession
    from prompt_toolkit.application import get_app, run_in_terminal
    from prompt_toolkit.completion import Completer, Completion, ThreadedCompleter
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
SUSPENDED = object()    # read() 被 suspend() 收起时的返回值
INPUT_MIN_ROWS = 5  # 输入框高度上限是半屏，但再矮的终端也至少给这么多行


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


class MentionCompleter(Completer):
    """``@`` 后面补文件路径（见 paths.py）。root 可以是函数：会话换了目录时现取"""

    def __init__(self, root: str | Callable[[], str]) -> None:
        self.root = root
        self.index = ProjectIndex()

    def get_completions(self, document, complete_event):
        token = mention_token(document.text_before_cursor)
        if token is None:
            return
        partial, typed = token
        root = self.root() if callable(self.root) else self.root
        for candidate in path_candidates(root, partial, self.index):
            yield Completion(candidate.insert, start_position=-typed, display=candidate.display,
                             display_meta="目录" if candidate.is_dir else "")


class _Completers(Completer):
    """光标在 @词 里补路径，否则补命令"""

    def __init__(self, slash: Completer | None, mentions: Completer | None) -> None:
        self.slash, self.mentions = slash, mentions

    def get_completions(self, document, complete_event):
        if self.mentions is not None and mention_token(document.text_before_cursor) is not None:
            yield from self.mentions.get_completions(document, complete_event)
        elif self.slash is not None:
            yield from self.slash.get_completions(document, complete_event)

    async def get_completions_async(self, document, complete_event):
        # 路径补全要遍历项目目录（在家目录这种大目录下能有几百毫秒），放到线程里，不卡打字
        if self.mentions is not None and mention_token(document.text_before_cursor) is not None:
            async for completion in ThreadedCompleter(self.mentions).get_completions_async(
                    document, complete_event):
                yield completion
        elif self.slash is not None:
            async for completion in self.slash.get_completions_async(document, complete_event):
                yield completion


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
# 浅色背景：亮青、浅灰在白底上看不清
MENU_STYLE_LIGHT = {
    **MENU_STYLE,
    "completion-menu.completion.current": "bg:default fg:#005f87 bold",
    "completion-menu.meta.completion": "bg:default fg:#6c6c6c",
    "completion-menu.meta.completion.current": "bg:default fg:#303030",
    "scrollbar.button": "bg:#a8a8a8",
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
    - ``above``：画在输入框上方的内容（运行中的活动区：转圈、思考、工具、任务清单），每次重画
      重新取；外面状态变了调 ``invalidate()``。配合 LiveRegion.host，运行中输入框也能常驻。
    - ``on_escape``：按 Esc（菜单没打开时）调用，如中断正在运行的一轮。
    - ``on_interrupt``：ctrl+c 时先问它（参数是输入框里的字），返回 True 表示处理掉了
      （如空行时中断运行），否则照旧：有字清空、空行退出。
    - ``suspend()``：把正在读的这一行收起来（返回已输入的字），让别的界面（选择器）用屏幕；
      之后用 ``read(default=...)`` 接着读。

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
            above: Callable[[], str] | None = None,
            on_escape: Callable[[], Any] | None = None,
            on_interrupt: Callable[[str], bool] | None = None,
            background: str = "dark",
            files_root: str | Callable[[], str] | None = None,
            on_paste_image: Callable[[], str | None] | None = None,
    ) -> None:
        self.session: Any = None
        self.completer: SlashCompleter | None = None
        self.mentions: MentionCompleter | None = None
        self._active = False
        self._done = asyncio.Event()
        self._done.set()
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

        in_mention = Condition(self._in_mention)

        @bindings.add("enter", filter=in_input & has_completions & in_mention)
        @bindings.add("tab", filter=in_input & in_mention)
        def _(event) -> None:
            """@ 菜单里：回车 / Tab 选中路径而不是发送；目录接着往下补，文件后面留个空格"""
            buffer = event.current_buffer
            state = buffer.complete_state
            if state is None or not state.completions:
                buffer.start_completion(select_first=True)
                return
            completion = state.current_completion or state.completions[0]
            buffer.apply_completion(completion)
            if completion.text.rstrip('"').endswith("/"):
                buffer.start_completion(select_first=False)
            else:
                buffer.insert_text(" ")

        if on_paste_image is not None:
            async def paste_image(event) -> None:
                """剪贴板里的截图存成文件，在光标处插入 @路径（发送时作为图片附上）"""
                buffer = event.current_buffer
                text = await asyncio.get_running_loop().run_in_executor(None, on_paste_image)
                if text:
                    before = buffer.document.text_before_cursor
                    buffer.insert_text(("" if not before or before[-1].isspace() else " ") + text + " ")
                else:
                    event.app.output.bell()     # 剪贴板里不是图片

            # ctrl+v；Windows Terminal 等把 ctrl+v 留给自己粘贴文字，那里用 alt+v
            bindings.add("c-v", filter=in_input)(paste_image)
            bindings.add("escape", "v", filter=in_input)(paste_image)

        @bindings.add("c-c", filter=in_input)
        def _(event) -> None:
            """有字时清空这一行，空行时才退出（同 Claude Code）；运行中空行是中断（on_interrupt）"""
            if on_interrupt is not None and on_interrupt(event.current_buffer.text):
                return
            if event.current_buffer.text:
                event.current_buffer.reset()
            else:
                event.app.exit(exception=KeyboardInterrupt, style="class:aborting")

        if on_escape is not None:
            # 不用 eager：Alt+Enter 是 Esc 再跟回车，eager 会把它拆成"中断 + 提交"。
            # 等 ttimeoutlen（下面调到 50ms）没有后续键再算单独的 Esc
            @bindings.add("escape", filter=in_input & ~has_completions)
            def _(event) -> None:
                on_escape()

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
        if files_root is not None:
            self.mentions = MentionCompleter(files_root)
        completer = _Completers(self.completer, self.mentions) if (self.completer or self.mentions) else None
        self.session = PromptSession(
            history=history,
            key_bindings=bindings,
            multiline=False,
            completer=completer,
            # 边打边补；和 ↑ 翻历史搜索冲突，二选一时保留补全（有补全器时才开）
            complete_while_typing=completer is not None,
            enable_history_search=completer is None,
            erase_when_done=erase_when_done,
            style=Style.from_dict(MENU_STYLE_LIGHT if background == "light" else MENU_STYLE),
            # 固定预留会让输入行下面一直空着一大块；菜单空间由 _input_height 按需撑开
            reserve_space_for_menu=0,
        )
        quiet_cpr(self.session.app)
        if on_escape is not None:
            self.session.app.ttimeoutlen = 0.05
        if above is not None:
            # 输入框上方：运行中的活动区。没内容时不占行
            block = ConditionalContainer(
                Window(FormattedTextControl(lambda: ANSI(above())), dont_extend_height=True),
                filter=~is_done & Condition(lambda: bool(above())),
            )
            self.session.layout.container.children.insert(0, block)
        if footer is not None:
            # PromptSession 只提供贴在屏幕最底行的 bottom_toolbar；这里把一行插在输入区正下方
            # （布局最外层 HSplit 的第 0 个孩子是输入区，补全菜单浮在输入区内，所以菜单在这行上面）
            line = ConditionalContainer(
                # 不定高：按内容行数（细线 + 模式提示可以是两行）
                Window(FormattedTextControl(lambda: ANSI(footer())), dont_extend_height=True),
                filter=~is_done,
            )
            index = 2 if above is not None else 1
            self.session.layout.container.children.insert(index, line)
        input_window = next(w for w in self.session.layout.find_all_windows()
                            if getattr(w.content, "buffer", None) is self.session.default_buffer)
        if footer is not None:
            # 输入窗口默认会撑满下方剩余的空间，这行就被挤到屏幕最底下（和 bottom_toolbar 一样）
            input_window.dont_extend_height = to_filter(True)
        # 高度由 _input_height 决定：有命令候选时撑开放菜单；粘贴大段文字时不超过半屏，框内滚动
        input_window.height = self._input_height
        if completer is not None:
            self.session.default_buffer.on_completions_changed += _highlight_first

    def _input_height(self) -> Dimension:
        """输入窗口的高度：平时按内容，但最多半屏——粘贴一大段时不会把整个屏幕占满，
        超出的在框里滚动（光标所在处可见）。有命令候选时撑开放菜单（不用
        reserve_space_for_menu 固定预留，否则输入行下面一直空着一大块）。

        按输入的原文算候选数：↑↓ 浏览时 prompt_toolkit 会把选中的候选写进输入框，
        按那段文字算菜单会缩成一行"""
        buffer = self.session.default_buffer
        if get_app().is_done:
            return Dimension()
        cap = max(INPUT_MIN_ROWS, terminal_height() // 2)
        state = buffer.complete_state
        count = 0
        if self.completer is not None:
            text = state.original_document.text if state is not None else buffer.text
            count = len(self.completer.matches(text))
        if not count and self.mentions is not None and state is not None and state.completions \
                and mention_token(state.original_document.text_before_cursor) is not None:
            count = len(state.completions)      # @ 菜单：路径候选是异步算的，按算出来的撑开
        if count:
            return Dimension(min=min(buffer.document.line_count, cap) + min(count, MENU_ROWS))
        return Dimension(max=cap)

    def _in_mention(self) -> bool:
        return self.mentions is not None and \
            mention_token(self.session.default_buffer.document.text_before_cursor) is not None

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

    @property
    def active(self) -> bool:
        """正在读（输入框在屏幕上）"""
        return self._active

    async def read(self, prompt: str | Callable[[], str] = "> ", default: str = "") -> Any:
        """读一行。上层不要再回显——这里已经显示过了。被 suspend() 收起时返回 SUSPENDED。
        prompt 可以是函数：每次重画重新取（输入框上沿的细线跟着窗口宽度变）"""
        if self.session is not None:
            self._active = True
            self._done.clear()
            message = (lambda: ANSI(prompt())) if callable(prompt) else ANSI(prompt)
            try:
                # patch_stdout：流式输出和输入行共存，输出不会把正在输入的内容冲掉
                with patch_stdout(raw=True):
                    return await self.session.prompt_async(message, default=default)
            finally:
                self._active = False
                self._done.set()
        text = prompt() if callable(prompt) else prompt
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: input(text))

    async def suspend(self) -> str:
        """收起正在读的这一行，等输入框真的离开屏幕再返回；返回已经输入的字"""
        if not self._active or self.session is None:
            return ""
        text = self.session.default_buffer.text
        app = self.session.app
        if app.is_running and not app.future.done():
            app.exit(result=SUSPENDED)
        await self._done.wait()
        return text

    def invalidate(self) -> None:
        """外面的状态变了（活动区、模式），重画输入框这块"""
        if self._active and self.session is not None:
            self.session.app.invalidate()

    def set_text(self, text: str) -> None:
        """往正在读的输入框里放字（如没送达的插话放回来）"""
        if self._active and self.session is not None:
            self.session.default_buffer.text = text
            self.session.default_buffer.cursor_position = len(text)


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
