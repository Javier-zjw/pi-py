"""选择器：↑↓ 选行，←→ 切换这一行的次要选项（比如模型的思考档位），Enter 确认，Esc 取消。

状态和渲染（PickerState）不依赖 prompt_toolkit，可以离线测试；交互部分有 prompt_toolkit
时用一个非全屏的小应用（选完整块擦掉，不留在 scrollback 里），没有就退化成输入编号。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from .theme import DEFAULT_THEME, Theme
from .width import display_width, pad, truncate

try:  # 可选依赖
    from prompt_toolkit.application import Application
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout, Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    HAS_PROMPT_TOOLKIT = True
except ImportError:  # pragma: no cover
    HAS_PROMPT_TOOLKIT = False


@dataclass
class PickerItem:
    label: str
    description: str = ""
    choices: list[str] = field(default_factory=list)   # ←→ 切换的次要选项；空表示没有
    choice: int = 0
    note: str = ""          # 没有次要选项时右侧显示的说明，如"不支持思考"
    current: bool = False   # 当前正在用的那一项，标出来
    value: Any = None


@dataclass
class PickResult:
    index: int
    item: PickerItem
    choice: str | None


class PickerState:
    def __init__(self, items: list[PickerItem], index: int = 0, choice_label: str = "") -> None:
        if not items:
            raise ValueError("picker needs at least one item")
        self.items = items
        self.index = max(0, min(index, len(items) - 1))
        self.choice_label = choice_label

    @property
    def item(self) -> PickerItem:
        return self.items[self.index]

    def up(self) -> None:
        self.index = (self.index - 1) % len(self.items)

    def down(self) -> None:
        self.index = (self.index + 1) % len(self.items)

    def left(self) -> None:
        if self.item.choices:
            self.item.choice = max(0, self.item.choice - 1)

    def right(self) -> None:
        if self.item.choices:
            self.item.choice = min(len(self.item.choices) - 1, self.item.choice + 1)

    def result(self) -> PickResult:
        item = self.item
        choice = item.choices[item.choice] if item.choices else None
        return PickResult(self.index, item, choice)

    def render(self, title: str, width: int, theme: Theme = DEFAULT_THEME) -> list[str]:
        t = theme
        has_choices = any(i.choices for i in self.items)
        keys = "↑↓ 选择" + (f" · ←→ {self.choice_label}" if has_choices else "") + " · Enter 确认 · Esc 取消"
        lines = [f"{t.bold(title)}  {t.muted(keys)}"]
        label_width = min(max(display_width(i.label) for i in self.items), max(width // 2, 20))
        for i, item in enumerate(self.items):
            selected = i == self.index
            mark = t.accent("❯", bold=True) if selected else " "
            label = pad(truncate(item.label, label_width), label_width)
            label = t.accent(label, bold=True) if selected else label
            current = t.success(" ✓ 当前") if item.current else "       "
            if item.choices:
                value = item.choices[item.choice]
                if selected:
                    side = f"{t.muted(self.choice_label)} {t.muted('◀')} {t.highlight(value, bold=True)} {t.muted('▶')}"
                else:
                    side = t.muted(f"{self.choice_label} {value}")
            else:
                side = t.muted(item.note)
            line = f"{mark} {label}{current}" + (f"  {side}" if side else "")
            if item.description:
                room = width - display_width(line) - 4
                if room > 8:
                    line += f"  {t.muted(truncate(item.description, room))}"
            lines.append(line)
        return lines


async def pick(
        title: str,
        items: list[PickerItem],
        *,
        index: int = 0,
        choice_label: str = "",
        theme: Theme = DEFAULT_THEME,
        width: int = 80,
) -> PickResult | None:
    """交互选择。取消返回 None。"""
    state = PickerState(items, index, choice_label)
    if HAS_PROMPT_TOOLKIT:
        return await _pick_rich(state, title, theme, width)
    return await _pick_plain(state, title, theme)


async def _pick_rich(state: PickerState, title: str, theme: Theme, width: int) -> PickResult | None:
    bindings = KeyBindings()

    @bindings.add("up")
    @bindings.add("c-p")
    def _(event) -> None:
        state.up()

    @bindings.add("down")
    @bindings.add("c-n")
    def _(event) -> None:
        state.down()

    @bindings.add("left")
    def _(event) -> None:
        state.left()

    @bindings.add("right")
    @bindings.add("tab")
    def _(event) -> None:
        state.right()

    @bindings.add("enter")
    def _(event) -> None:
        event.app.exit(result=state.result())

    @bindings.add("escape", eager=True)
    @bindings.add("c-c")
    @bindings.add("q")
    def _(event) -> None:
        event.app.exit(result=None)

    control = FormattedTextControl(lambda: ANSI("\n".join(state.render(title, width, theme))))
    app: Application = Application(
        layout=Layout(Window(control, dont_extend_height=True, always_hide_cursor=True)),
        key_bindings=bindings,
        full_screen=False,
        erase_when_done=True,     # 选完整块擦掉，由调用方打一行结论
    )
    return await app.run_async()


async def _pick_plain(state: PickerState, title: str, theme: Theme) -> PickResult | None:
    """没有 prompt_toolkit：列出编号，输入编号选择"""
    loop = asyncio.get_running_loop()
    print(theme.bold(title))
    for i, item in enumerate(state.items, 1):
        current = theme.success(" ✓ 当前") if item.current else ""
        print(f"  {i}. {item.label}{current}  {theme.muted(item.note or item.description)}")
    raw = (await loop.run_in_executor(None, lambda: input("编号（回车取消）："))).strip()
    if not raw.isdigit() or not 1 <= int(raw) <= len(state.items):
        return None
    state.index = int(raw) - 1
    item = state.item
    if item.choices:
        options = " / ".join(item.choices)
        raw = (await loop.run_in_executor(
            None, lambda: input(f"{state.choice_label}（{options}，回车保持 {item.choices[item.choice]}）："))).strip()
        if raw in item.choices:
            item.choice = item.choices.index(raw)
    return state.result()
