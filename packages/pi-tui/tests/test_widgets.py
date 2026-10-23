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
    state = PickerState(items(), index=0, choice_label="思考")
    lines = [strip_ansi(line) for line in state.render("选择模型", 100, Theme(ColorMode.NONE))]
    check("渲染: 标题行带按键说明", "选择模型" in lines[0] and "←→ 思考" in lines[0], lines[0])
    check("渲染: 每项一行", len(lines) == 4, str(lines))
    check("渲染: 选中行有 ❯ 和 ◀ ▶", lines[1].startswith("❯") and "◀ low ▶" in lines[1], lines[1])
    check("渲染: 当前项标 ✓", "✓ 当前" in lines[1])
    check("渲染: 不支持的行显示说明", "不支持思考" in lines[2], lines[2])
    check("渲染: 未选中行只显示档位", "思考 off" in lines[3] and "◀" not in lines[3], lines[3])
    narrow = [strip_ansi(line) for line in state.render("选择模型", 40, Theme(ColorMode.NONE))]
    check("渲染: 窄屏时省掉说明", "Anthropic" not in narrow[1], narrow[1])
    no_choices = PickerState([PickerItem("a"), PickerItem("b")])
    header = strip_ansi(no_choices.render("会话", 80, Theme(ColorMode.NONE))[0])
    check("渲染: 没有次要选项时不提示 ←→", "←→" not in header, header)


def main() -> int:
    print("命令补全")
    test_match_commands()
    test_completer()
    print("选择器")
    test_picker_state()
    test_picker_render()
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
