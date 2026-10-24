"""pi-tui 输入组件测试：命令补全、选择器

    python tests/test_widgets.py
    pytest tests/test_widgets.py

只测纯逻辑（候选、选择器状态和渲染）；prompt_toolkit 没装时跳过补全器那一条。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pi_tui import (
    HAS_PROMPT_TOOLKIT,
    PickerItem,
    PickerState,
    SlashCommand,
    Theme,
    display_width,
    match_commands,
    strip_ansi,
)
from pi_tui.theme import ColorMode

FAILURES: list[str] = []


def under_pytest() -> bool:
    # 必须在调用时判断：import（收集）阶段 pytest 还没设置这个环境变量
    return "PYTEST_CURRENT_TEST" in os.environ


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILURES.append(f"{name}: {detail}")
    print(f"  FAIL {name} {detail}")
    if under_pytest():
        raise AssertionError(f"{name}: {detail}")


COMMANDS = [
    SlashCommand("model", "切换模型"),
    SlashCommand("compact", "压缩上下文"),
    SlashCommand("cost", "用量", aliases=("usage",)),
    SlashCommand("context", "上下文"),
    SlashCommand("status", "会话信息", aliases=("session",)),
]


def names(found: list[SlashCommand]) -> list[str]:
    return [c.name for c in found]


def test_match_commands() -> None:
    check("补全: 只输 / 列出全部", names(match_commands("/", COMMANDS)) == names(COMMANDS))
    check("补全: 前缀匹配", names(match_commands("/co", COMMANDS)) == ["compact", "cost", "context"])
    check("补全: 大小写不敏感", names(match_commands("/MO", COMMANDS)) == ["model"])
    check("补全: 别名排在名字前缀之后",
          names(match_commands("/s", COMMANDS)) == ["status", "cost"],
          str(names(match_commands("/s", COMMANDS))))
    check("补全: 名字中间包含也算", names(match_commands("/tex", COMMANDS)) == ["context"])
    check("补全: 别名能找到命令", names(match_commands("/usa", COMMANDS)) == ["cost"])
    check("补全: 敲了空格（在输参数）不再补", match_commands("/model ", COMMANDS) == [])
    check("补全: 不是 / 开头不补", match_commands("model", COMMANDS) == [])
    check("补全: 无匹配为空", match_commands("/usr/bin", COMMANDS) == [])
    extra = [SlashCommand("planner"), SlashCommand("plan")]
    check("补全: 名字完全相同的排最前", names(match_commands("/plan", extra)) == ["plan", "planner"])
    check("补全: 别名完全相同的也排最前",
          names(match_commands("/session", [SlashCommand("sessions"), *COMMANDS]))[0] == "status")


def test_completer() -> None:
    if not HAS_PROMPT_TOOLKIT:
        print("  skip 没装 prompt_toolkit")
        return
    from pi_tui import SlashCompleter
    from prompt_toolkit.document import Document

    dynamic = list(COMMANDS)
    completer = SlashCompleter(lambda: dynamic)
    found = list(completer.get_completions(Document("/co"), None))
    check("补全器: 替换整段输入", [c.text for c in found] == ["/compact", "/cost", "/context"]
          and all(c.start_position == -3 for c in found))
    check("补全器: 说明放在 meta", found[0].display_meta_text == "压缩上下文")
    dynamic.append(SlashCommand("plan", "计划模式"))
    check("补全器: 命令表现取（扩展后加的也能补）",
          [c.text for c in completer.get_completions(Document("/pl"), None)] == ["/plan"])


def items() -> list[PickerItem]:
    levels = ["off", "low", "high"]
    return [
        PickerItem("claude", "Anthropic", choices=levels, choice=1, current=True),
        PickerItem("deepseek", "兼容端点", note="不支持思考"),
        PickerItem("gpt", "OpenAI", choices=levels),
    ]


def test_picker_state() -> None:
    state = PickerState(items(), index=0, choice_label="思考")
    state.up()
    check("选择器: ↑ 在顶部绕到底部", state.index == 2)
    state.down()
    check("选择器: ↓ 在底部绕回顶部", state.index == 0)
    state.right()
    state.right()
    check("选择器: → 切到下一档，到头停住", state.item.choices[state.item.choice] == "high")
    state.left()
    state.left()
    state.left()
    check("选择器: ← 到头停住", state.item.choice == 0)
    state.down()
    state.right()
    result = state.result()
    check("选择器: 没有次要选项的行 ←→ 无效，结果 choice 为 None",
          result.item.label == "deepseek" and result.choice is None)
    state.up()
    result = state.result()
    check("选择器: 结果带上次要选项", result.index == 0 and result.choice == "off")
    check("选择器: 初始下标越界会夹住", PickerState(items(), index=9).index == 2)


def test_picker_render() -> None:
    plain = Theme(ColorMode.NONE)
    state = PickerState(items(), index=0, choice_label="思考")
    lines = [strip_ansi(line) for line in state.render("选择模型", 100, plain)]
    rows = [line for line in lines if line.startswith(("│ ❯", "│  "))]
    check("面板: 和上面的内容空一行", lines[0] == "", str(lines))
    check("面板: 上边框写标题", lines[1].startswith("╭─ 选择模型 ─"), lines[1])
    check("面板: 下边框是按键说明（数字直接选、←→ 次要选项）", lines[-1].startswith("╰─ ↑↓ 选择")
          and "1-3 直接选" in lines[-1] and "←→ 思考" in lines[-1], lines[-1])
    check("面板: 每项一行、带编号", len(lines) == 6 and len(rows) == 3
          and "1. claude" in rows[0] and "3. gpt" in rows[2], str(lines))
    check("面板: 不超过屏宽", all(display_width(line) <= 100 for line in lines),
          str([display_width(line) for line in lines]))
    check("渲染: 选中行有 ❯ 和 ◀ ▶", rows[0].startswith("│ ❯") and "◀ low ▶" in rows[0], rows[0])
    check("渲染: 当前项标 ✓", "✓ 当前" in rows[0])
    check("渲染: 不支持的行显示说明", "不支持思考" in rows[1], rows[1])
    check("渲染: 未选中行只显示档位", "思考 off" in rows[2] and "◀" not in rows[2], rows[2])
    narrow = [strip_ansi(line) for line in state.render("选择模型", 40, plain)]
    check("渲染: 窄屏时省掉说明", "Anthropic" not in narrow[2], narrow[2])
    no_choices = PickerState([PickerItem("a"), PickerItem("b")])
    footer = strip_ansi(no_choices.render("会话", 80, plain)[-1])
    check("渲染: 没有次要选项时不提示 ←→", "←→" not in footer, footer)

    question = PickerState([PickerItem("统一改（推荐）", "list 也显示 10.00"), PickerItem("其他")])
    lines = [strip_ansi(line) for line in question.render(
        "list 命令的金额也统一显示两位小数吗？这一句故意写得很长，窄屏时要折行显示", 40, plain,
        header="需要你选择 · 1/2")]
    check("提问面板: 上边框写明需要选择和进度", lines[1].startswith("╭─ 需要你选择 · 1/2 ─"), lines[1])
    body = [line for line in lines[2:] if line.startswith("│ ") and "1." not in line and "2." not in line]
    check("提问面板: 问题在面板里，长问题折行", len(body) >= 2 and body[0].startswith("│ list 命令")
          and all(display_width(line) <= 40 for line in lines), str(lines))

    lines = [strip_ansi(line) for line in question.render("允许执行这条命令吗？", 40, plain,
                                                         header="需要你确认 · 执行命令",
                                                         detail=["rm -rf build " + "x" * 60])]
    check("细节: 问题下面缩进显示，超宽截短", lines[3].startswith("│   rm -rf build") and lines[4] == "│"
          and all(display_width(line) <= 40 for line in lines), str(lines))
    check("数字键: 选中对应项", question.select(2) and question.index == 1)
    check("数字键: 超出范围不动", not question.select(3) and not question.select(0) and question.index == 1)


def test_live_region_host() -> None:
    """有宿主（常驻输入框）时活动区交给宿主画、定稿内容交给宿主打印，自己不往终端写光标控制码"""
    import io

    from pi_tui.live import LiveRegion

    class Host:
        def __init__(self) -> None:
            self.live: list[str] = []
            self.printed: list[str] = []

        def set_live(self, lines: list[str]) -> None:
            self.live = list(lines)

        def print_above(self, text: str) -> None:
            self.printed.append(text)

    stream = io.StringIO()
    region = LiveRegion(stream=stream)
    region.host = host = Host()
    region.update(["⠋ 思考中"])
    check("宿主: 活动区交给宿主", host.live == ["⠋ 思考中"] and region.lines == ["⠋ 思考中"])
    region.append(["● 第一行"], live=["预览"])
    check("宿主: 定稿内容打印、活动区换成预览", host.printed == ["● 第一行"] and host.live == ["预览"])
    region.finalize(["收尾"])
    check("宿主: 收起活动区再打印", host.live == [] and host.printed[-1] == "收尾")
    check("宿主: 自己不写终端", stream.getvalue() == "", repr(stream.getvalue()))


def test_viewer_state() -> None:
    """全屏查看器的状态：选段落、展开收起、翻页、搜索（命中正文时自动展开）、渲染正好占满屏"""
    from pi_tui import Section, ViewerState

    plain = Theme(ColorMode.NONE)
    sections = [Section(f"第 {i} 段", [f"正文 {i}-{j}" for j in range(5)]) for i in range(8)]
    sections.append(Section("没有正文的段"))
    state = ViewerState(sections, "上一轮的过程")
    lines = [strip_ansi(line) for line in state.render(60, 10, plain)]
    check("查看器: 正好占满屏（标题 + 内容 + 状态栏）", len(lines) == 10 and lines[0].startswith("上一轮的过程  9 段")
          and lines[-1].startswith("第 1/9 段"), str(lines))
    check("查看器: 选中行有 ❯，收起的有 ▸", lines[1] == "❯ ▸ 第 0 段" and lines[2] == "  ▸ 第 1 段", str(lines))
    state.down()
    state.toggle()
    lines = [strip_ansi(line) for line in state.render(60, 10, plain)]
    check("查看器: 展开后正文缩进显示", "❯ ▾ 第 1 段" in lines and "    │ 正文 1-0" in lines, str(lines))
    state.cursor = 8
    state.toggle()
    check("查看器: 没有正文的段不能展开", not sections[8].expanded)
    lines = [strip_ansi(line) for line in state.render(60, 10, plain)]
    check("查看器: 光标走到下面时跟着滚动", "❯   没有正文的段" in lines and "❯ ▸ 第 0 段" not in lines, str(lines))
    state.home()
    state.page(1, 10)
    check("查看器: 翻页后光标跟到屏幕里", state.cursor > 0 and state.offset > 0, f"{state.cursor} {state.offset}")
    state.expand_all(False)
    state.search("正文 6-3")
    check("查看器: 搜索命中正文时跳过去并展开", state.cursor == 6 and sections[6].expanded and state.matches == [6])
    state.search("第")
    first = state.cursor
    state.next_match()
    check("查看器: n 下一个、绕回", state.cursor == first + 1 and len(state.matches) == 8)
    state.next_match(-1)
    state.next_match(-1)
    check("查看器: N 上一个、绕回", state.cursor == 7, str(state.cursor))
    state.search("没有这个词")
    footer = strip_ansi(state.render(60, 10, plain)[-1])
    check("查看器: 搜不到时状态栏说明", "没找到" in footer, footer)
    state.expand_all(True)
    check("查看器: e 全部展开（没正文的不算）", all(s.expanded for s in sections[:8]) and not sections[8].expanded)


def main() -> int:
    print("命令补全")
    test_match_commands()
    test_completer()
    print("选择器")
    test_picker_state()
    test_picker_render()
    test_live_region_host()
    test_viewer_state()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} 处失败：")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
