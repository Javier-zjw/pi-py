"""流式 Markdown 渲染测试

    python tests/test_markdown.py
    pytest tests/test_markdown.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pi_tui import MarkdownStream, Theme, display_width, render_markdown, strip_ansi, wrap_ansi
from pi_tui.theme import RESET, ColorMode

FAILURES: list[str] = []
PLAIN = Theme(ColorMode.NONE)
COLOR = Theme(ColorMode.TRUECOLOR)


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


SAMPLE = """

看完了。当前仓库是 **pi-py**——[pi agent](https://github.com/earendil-works/pi) 的 Python 重写，最新提交是 `4b5b701`。

## 结构



```
pi-app ──→ pi-ai  # **不是粗体**
```

| 包 | 行数 | 内容 |
|---|---:|:---:|
| pi-ai | 2,100 | 单次调用 |
| pi-coding-agent | 9,800 | 会话 |

- 第一点 *斜体* 和 ~~删除~~
  - 嵌套
1. 有序
- [x] 完成
- [ ] 未完成

> 引用

---
结尾。


"""


def render(text: str, width: int = 60, theme: Theme = PLAIN) -> list[str]:
    return render_markdown(text, width, theme, "● ", "  ")


def test_blocks() -> None:
    lines = render(SAMPLE)
    text = "\n".join(lines)
    check("首行带 ●，开头空行去掉", lines[0].startswith("● 看完了"), lines[0])
    check("结尾空行去掉", lines[-1] == "  结尾。", lines[-1])
    check("连续空行压成一个", "\n\n\n" not in text, text)
    check("粗体去掉星号", "pi-py" in text and "**pi-py**" not in text, text)
    wide = "\n".join(render(SAMPLE, width=200))
    check("链接显示文字和地址", "pi agent (https://github.com/earendil-works/pi)" in wide, wide)
    check("行内码去掉反引号", "4b5b701" in text and "`" not in text, text)
    check("标题去掉 #，前面空一行", "\n\n  结构\n" in text and "##" not in text, text)
    check("代码块: 去掉围栏、左侧竖线、原样不做行内样式",
          "  │ pi-app ──→ pi-ai  # **不是粗体**" in lines, text)
    check("列表: • 和嵌套 ◦", "  • 第一点 斜体 和 删除" in lines and "    ◦ 嵌套" in lines, text)
    check("列表: 有序保留序号", "  1. 有序" in lines, text)
    check("任务列表: ☑ ☐", "  ☑ 完成" in lines and "  ☐ 未完成" in lines, text)
    check("引用: 左侧竖线", "  │ 引用" in lines, text)
    check("分隔线", any(line.strip() and set(line.strip()) == {"─"} for line in lines), text)


def test_table() -> None:
    lines = render(SAMPLE)
    top = next(i for i, line in enumerate(lines) if "┌" in line)
    bottom = next(i for i, line in enumerate(lines) if "└" in line)
    table = lines[top:bottom + 1]
    rows = [line for line in table if "│" in line and "─" not in line]
    check("表格: 有边框", any("┌" in line for line in lines) and any("└" in line for line in lines),
          "\n".join(lines))
    widths = {display_width(line) for line in table}
    check("表格: 每行一样宽（中文对齐）", len(widths) == 1, "\n".join(table))
    check("表格: 右对齐列", "│  行数 │" in rows[0] and "│ 2,100 │" in rows[1], "\n".join(rows))
    check("表格: 居中列", "│   会话   │" in rows[2], rows[2])
    check("表格: 表头加粗", "\033[1m包" in render(SAMPLE, theme=COLOR)[top + 1], "")

    narrow = render("| 名字 | 说明 |\n|---|---|\n| a | " + "很长的说明文字" * 6 + " |\n", width=40)
    check("表格: 太宽时格子里折行，不超宽", all(display_width(line) <= 40 for line in narrow)
          and len(narrow) > 5, "\n".join(narrow))
    fake = render("| 不是表格 |\n普通一行")
    check("没有分隔行的竖线行按普通文字", "│" not in "\n".join(fake[:1]) and "不是表格" in fake[0], str(fake))


def test_streaming() -> None:
    whole = render(SAMPLE)
    stream = MarkdownStream(60, PLAIN, "● ", "  ")
    got: list[str] = []
    for ch in SAMPLE:
        got += stream.feed(ch)
    got += stream.finish()
    check("流式: 一个字一个字喂，结果和一次性渲染一样", got == whole,
          "\n".join(f"{a!r} | {b!r}" for a, b in zip(got, whole) if a != b)[:600])

    stream = MarkdownStream(60, PLAIN, "● ", "  ")
    done = stream.feed("第一段完整。\n第二段还没写")
    check("流式: 收齐的行先定稿", done == ["● 第一段完整。"], str(done))
    check("流式: 半行在 preview 里，带续行前缀", stream.preview() == ["  第二段还没写"],
          str(stream.preview()))
    check("流式: preview 不消耗状态", stream.preview() == ["  第二段还没写"])
    done = stream.feed("完。\n| a | b |\n|---|---|\n| 1 | 2 |\n")
    check("流式: 表格收齐前不定稿", done == ["  第二段还没写完。"], str(done))
    check("流式: 没收齐的表格在 preview 里", any("│ 1 " in line for line in stream.preview()),
          str(stream.preview()))
    rest = stream.finish()
    check("流式: 结束时写出表格", any("│ 1 " in line for line in rest), str(rest))
    first = MarkdownStream(60, PLAIN, "● ", "  ")
    check("流式: 第一行之前的 preview 用 ● 前缀", first.feed("你好") == [] and first.preview() == ["● 你好"])


def test_inline() -> None:
    text = "\n".join(render("snake_case_name 和 2 * 3 * 4 和 a*b*c 都不是斜体"))
    check("行内: 下划线变量名、乘号不当斜体", "snake_case_name" in text and "2 * 3 * 4" in text
          and "a*b*c" in text, text)
    escaped = "\n".join(render(r"\*不是强调\* 和 \`不是代码\`"))
    check("行内: 反斜杠转义", "*不是强调*" in escaped and "`不是代码`" in escaped, escaped)
    colored = render("**粗体里有 `代码` 还有后半**", theme=COLOR)[0]
    tail = colored.split("代码")[1]
    check("行内: 嵌套样式结束后补回外层粗体", "\033[1m" in tail.split("还有")[0], repr(colored))
    heading = render("## 标题里 `代码` 之后", theme=COLOR)[0]
    after = heading.split("代码")[1]
    check("标题: 内层样式之后标题样式还在", "\033[1m" in after.split("之后")[0], repr(heading))


def test_wrap() -> None:
    text = "当前仓库是 pi-py——TypeScript 版的分层 Python 重写，工作区干净（无未提交改动），最新提交是 4b5b701（pi-tui 的布局）。"
    for width in (20, 31, 47):
        lines = wrap_ansi(text, width)
        check(f"折行({width}): 不超宽", all(display_width(line) <= width for line in lines),
              str([display_width(line) for line in lines]))
        check(f"折行({width}): 不丢字", "".join(lines).replace(" ", "") == text.replace(" ", ""))
    bad = [w for w in range(12, 61)
           if any(strip_ansi(line)[:1] in "，。、）：" for line in wrap_ansi(text, w)[1:])]
    check("折行: 任何宽度下标点都不放行首", not bad, str(bad))
    styled = wrap_ansi(COLOR.info("一二三四五六七八九十" * 2), 10)
    check("折行: 样式跨行时每行都闭合、下一行重新打开",
          all(line.endswith(RESET) for line in styled)
          and all(line.startswith("\033[38;2") for line in styled), repr(styled))
    check("折行: 超长 URL 硬断", all(display_width(line) <= 20
          for line in wrap_ansi("https://github.com/earendil-works/pi/blob/main/README.md", 20)))
    lines = render("- " + "很长的列表项" * 12, width=40)
    check("列表: 折行悬挂缩进对齐到文字", all(line.startswith("    ") for line in lines[1:])
          and lines[0].startswith("● • "), str(lines))


def main() -> int:
    print("块")
    test_blocks()
    test_table()
    print("流式")
    test_streaming()
    print("行内与折行")
    test_inline()
    test_wrap()
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
