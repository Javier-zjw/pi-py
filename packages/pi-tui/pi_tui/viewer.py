"""全屏查看器：一列可折叠的段落（一轮里的思考、工具、输出……），看完按 q 回到原来的界面。

用在 ctrl+o："展开上一轮"不再把一大段内容倒进 scrollback（收不回去），而是临时开一个全屏界面
（终端的备用屏幕），关掉后原来的历史原样还在。

按键：↑↓（k j）选段落 · Enter / 空格 展开收起 · e 全部展开 · c 全部收起 ·
PgUp PgDn 翻页 · g G 到头 / 到尾 · / 搜索，n N 下一个 / 上一个 · q Esc 返回

状态和渲染（ViewerState）不依赖 prompt_toolkit，可以离线测试；交互部分是一个全屏小应用。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .theme import DEFAULT_THEME, Theme
from .width import display_width, strip_ansi, truncate

try:  # 可选依赖
    from prompt_toolkit.application import Application
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.layout import Layout, Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    HAS_PROMPT_TOOLKIT = True
except ImportError:  # pragma: no cover
    HAS_PROMPT_TOOLKIT = False

KEYS = "↑↓ 选择 · Enter 展开/收起 · e 全部展开 · c 全部收起 · / 搜索 · q 返回"


@dataclass
class Section:
    title: str                                      # 一行，可以带颜色
    body: list[str] = field(default_factory=list)   # 排好版的行（可以带颜色），展开时显示
    expanded: bool = False


class ViewerState:
    def __init__(self, sections: list[Section], title: str = "") -> None:
        self.sections = sections
        self.title = title
        self.cursor = 0
        self.offset = 0                 # 第一行显示的是展开后的第几行
        self.searching = False          # 正在输入搜索词
        self.query = ""
        self.matches: list[int] = []    # 命中的段落下标
        self.match = -1

    # -- 移动 ---------------------------------------------------------- #

    def up(self) -> None:
        self.cursor = max(self.cursor - 1, 0)

    def down(self) -> None:
        self.cursor = min(self.cursor + 1, len(self.sections) - 1)

    def home(self) -> None:
        self.cursor, self.offset = 0, 0

    def end(self) -> None:
        self.cursor = len(self.sections) - 1
        self.offset = 10 ** 9           # 渲染时夹到最大

    def page(self, rows: int, height: int) -> None:
        """翻页：滚动整屏，光标跟到屏幕里第一个段落"""
        self.offset = max(self.offset + rows * max(height - 3, 1), 0)
        starts = self._starts()
        visible = [i for i, start in enumerate(starts) if start >= self.offset]
        if visible:
            self.cursor = visible[0]

    def toggle(self) -> None:
        if self.sections and self.sections[self.cursor].body:
            self.sections[self.cursor].expanded = not self.sections[self.cursor].expanded

    def expand_all(self, expanded: bool = True) -> None:
        for section in self.sections:
            section.expanded = expanded and bool(section.body)

    # -- 搜索 ---------------------------------------------------------- #

    def search(self, query: str) -> None:
        self.query = query.strip()
        self.searching = False
        needle = self.query.lower()
        self.matches = [i for i, s in enumerate(self.sections) if needle and needle in
                        strip_ansi("\n".join([s.title, *s.body])).lower()]
        self.match = -1
        self.next_match()

    def next_match(self, step: int = 1) -> None:
        if not self.matches:
            return
        self.match = (self.match + step) % len(self.matches)
        self.cursor = self.matches[self.match]
        section = self.sections[self.cursor]
        if section.body and self.query.lower() not in strip_ansi(section.title).lower():
            section.expanded = True     # 命中在正文里：展开给人看

    # -- 渲染 ---------------------------------------------------------- #

    def _starts(self) -> list[int]:
        """每个段落标题在展开后的第几行"""
        starts, row = [], 0
        for section in self.sections:
            starts.append(row)
            row += 1 + (len(section.body) if section.expanded else 0)
        return starts

    def render(self, width: int, height: int, theme: Theme = DEFAULT_THEME) -> list[str]:
        """整屏：标题栏 + 内容 + 状态栏，正好 height 行"""
        t = theme
        body_rows = max(height - 2, 1)
        lines: list[str] = []
        cursor_row = 0
        needle = self.query.lower()
        for i, section in enumerate(self.sections):
            selected = i == self.cursor
            if selected:
                cursor_row = len(lines)
            marker = ("▾" if section.expanded else "▸") if section.body else " "
            mark = t.accent(f"❯ {marker}", bold=True) if selected else t.muted(f"  {marker}")
            title = truncate(section.title, max(width - 5, 10))
            if needle and i in self.matches:
                title = t.highlight(strip_ansi(title))
            lines.append(f"{mark} {title}")
            if section.expanded:
                bar = t.accent("│") if selected else t.muted("│")
                lines += [f"    {bar} {truncate(line, max(width - 7, 10))}" for line in section.body]
        # 光标所在的标题要在屏幕里；翻页之后光标已经跟过来了，这里只是兜底
        max_offset = max(len(lines) - body_rows, 0)
        if cursor_row < self.offset:
            self.offset = cursor_row
        elif cursor_row >= self.offset + body_rows:
            self.offset = cursor_row - body_rows + 1
        self.offset = min(max(self.offset, 0), max_offset)
        shown = lines[self.offset:self.offset + body_rows]
        shown += [""] * (body_rows - len(shown))

        head = f"{t.bold(self.title or '详情')}  {t.muted(f'{len(self.sections)} 段')}"
        head = truncate(head, width)
        if self.searching:
            foot = f"{t.accent('/', bold=True)} {self.query}{t.muted('▏ 回车搜索 · Esc 取消')}"
        else:
            where = f"第 {self.cursor + 1}/{len(self.sections)} 段"
            if self.query:
                found = f"{self.match + 1}/{len(self.matches)}" if self.matches else "没找到"
                where += f" · 搜索「{self.query}」{found}（n / N）"
            room = width - display_width(where) - 4
            foot = f"{t.muted(where)}  {t.muted(truncate(KEYS, room))}" if room > 10 else t.muted(where)
        return [head, *shown, foot]


async def view(sections: list[Section], title: str = "", *, theme: Theme = DEFAULT_THEME) -> None:
    """打开全屏查看器，按 q / Esc 关掉后返回。没有 prompt_toolkit 时什么都不做"""
    if not HAS_PROMPT_TOOLKIT or not sections:
        return
    state = ViewerState(sections, title)
    bindings = KeyBindings()
    searching = lambda: state.searching  # noqa: E731

    def size(event) -> tuple[int, int]:
        size = event.app.output.get_size()
        return size.columns, size.rows

    def on(*keys: str, search: bool = False):
        """普通模式的按键；搜索输入时这些键当字符输入"""
        def register(handler):
            for key in keys:
                bindings.add(key, filter=_condition(lambda: state.searching == search))(handler)
            return handler
        return register

    @on("q", "escape", "c-c")
    def _(event) -> None:
        event.app.exit()

    @on("up", "k")
    def _(event) -> None:
        state.up()

    @on("down", "j")
    def _(event) -> None:
        state.down()

    @on("enter", "space", "right", "left")
    def _(event) -> None:
        state.toggle()

    @on("e")
    def _(event) -> None:
        state.expand_all(True)

    @on("c")
    def _(event) -> None:
        state.expand_all(False)

    @on("pageup", "b")
    def _(event) -> None:
        state.page(-1, size(event)[1])

    @on("pagedown", "f")
    def _(event) -> None:
        state.page(1, size(event)[1])

    @on("g", "home")
    def _(event) -> None:
        state.home()

    @on("G", "end")
    def _(event) -> None:
        state.end()

    @on("/")
    def _(event) -> None:
        state.searching, state.query = True, ""

    @on("n")
    def _(event) -> None:
        state.next_match(1)

    @on("N")
    def _(event) -> None:
        state.next_match(-1)

    @on("enter", search=True)
    def _(event) -> None:
        state.search(state.query)

    @on("escape", "c-c", search=True)
    def _(event) -> None:
        state.searching, state.query = False, ""

    @on("backspace", search=True)
    def _(event) -> None:
        state.query = state.query[:-1]

    @bindings.add(Keys.Any, filter=_condition(searching))
    def _(event) -> None:
        if event.data.isprintable():
            state.query += event.data

    def text():
        size = _app.output.get_size()
        return ANSI("\n".join(state.render(size.columns, size.rows, theme)))

    _app: Application = Application(
        layout=Layout(Window(FormattedTextControl(text), always_hide_cursor=True)),
        key_bindings=bindings,
        full_screen=True,
    )
    await _app.run_async()


def _condition(fn):
    from prompt_toolkit.filters import Condition
    return Condition(fn)
