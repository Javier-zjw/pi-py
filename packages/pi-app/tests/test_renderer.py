"""pi-tui 渲染器测试：过程收起、结论突出

    python tests/test_renderer.py
    python tests/test_renderer.py -v      # 打印渲染结果
    pytest tests/test_renderer.py

用真的 AgentSession + 假模型跑一轮，检查终端上实际输出了什么。不联网、不需要 key。
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve()
PACKAGES = HERE.parents[2]
for _p in (HERE.parents[1], PACKAGES / "pi-tui", PACKAGES / "pi-coding-agent",
           PACKAGES / "pi-coding-agent" / "tests", PACKAGES / "pi-agent", PACKAGES / "pi-ai"):
    sys.path.insert(0, str(_p))

from fakes import FAKE_MODEL, Sandbox, fake_models  # noqa: E402

from pi_ai import TextContent, ThinkingContent, ToolCall, Usage, UserMessage  # noqa: E402
from pi_tui import Theme  # noqa: E402
from pi_tui.theme import ColorMode  # noqa: E402

from pi_app.renderer import SessionRenderer, ToolRecord  # noqa: E402
from pi_coding_agent import ModelRuntime, SessionManager  # noqa: E402
from pi_coding_agent.agent_session import AgentSession, create_agent_session_services  # noqa: E402

VERBOSE = "-v" in sys.argv
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


SCRIPT = [
    [ThinkingContent(thinking="用户想看代码，我先列目录，再读关键文件。"),
     ToolCall(id="t1", name="bash", arguments={"command": "ls -la"})],
    [ToolCall(id="t2", name="read", arguments={"path": "README.md"}),
     ToolCall(id="t3", name="read", arguments={"path": "app.py"})],
    [ToolCall(id="t4", name="bash", arguments={"command": "cat missing.txt"})],
    [TextContent(text="当前目录是一个小项目：\n- README.md：说明文档\n- app.py：入口")],
]


async def run_turn(cwd: Path, script: list[list], show_thinking: bool = True
                   ) -> tuple[SessionRenderer, io.StringIO, AgentSession]:
    (cwd / "README.md").write_text("# demo\n说明文档\n", "utf-8")
    (cwd / "app.py").write_text("def main():\n    print('hi')\n", "utf-8")
    models, _ = fake_models(script, usage=Usage(input=4130, output=86))
    services = create_agent_session_services(cwd=cwd, agent_dir=cwd / "agent",
                                             model_runtime=ModelRuntime(models, cwd / "agent"))
    session = AgentSession(services, SessionManager.in_memory(cwd), model=FAKE_MODEL,
                           tools=["bash", "read"])
    out = io.StringIO()
    renderer = SessionRenderer(theme=Theme(ColorMode.NONE), stream=out, show_thinking=show_thinking)
    session.subscribe(renderer.on_event)
    renderer.state.own_prompt = "查看一下当前代码"
    await session.prompt("查看一下当前代码")
    renderer.stop()
    if VERBOSE:
        print(out.getvalue())
    return renderer, out, session


async def case_collapsed_turn() -> None:
    with Sandbox() as cwd:
        _renderer, out, _ = await run_turn(cwd, list(SCRIPT))
        text = out.getvalue()
        lines = [line for line in text.splitlines() if line.strip()]
        check("收起: 不打印工具参数", "ls -la" not in text and "missing.txt" not in text, text)
        check("收起: 不打印工具结果", "# demo" not in text and "No such file" not in text)
        summary = [line for line in lines if "个工具调用" in line]
        check("收起: 工具收成一行", len(summary) == 1, str(summary))
        check("收起: 摘要带名字和次数", "bash ×2" in summary[0] and "read ×2" in summary[0], summary[0])
        check("收起: 失败数醒目标出", "1 失败" in summary[0], summary[0])
        check("收起: 提示怎么展开", "ctrl+o 展开" in summary[0])
        check("思考: 实时显示思考原文", "✻ 思考" in text and "  用户想看代码，我先列目录，再读关键文件。" in lines,
              text)
        check("思考: 显示原文时不再打「思考了」", "思考了" not in text, text)
        answer = next((i for i, line in enumerate(lines) if line.startswith("● ")), None)
        check("回答: 以 ● 开头", answer is not None, text)
        check("回答: 续行缩进两格", answer is not None and lines[answer + 1].startswith("  - README.md"),
              str(lines[answer:answer + 3]))
        check("回答: 工具摘要在回答之前", text.index("个工具调用") < text.index("● "))
        footers = [line for line in lines if "↑" in line and "↓" in line]
        check("统计: 每轮只有一行", len(footers) == 1, str(footers))
        check("统计: 上下文取最后一次、输出累加",
              "↑4.1k" in footers[0] and "↓344" in footers[0] and "4 次工具" in footers[0],
              footers[0])
        check("统计: 不带文字标签，以用时开头",
              footers[0].split(" · ")[0].endswith("s") and not any(
                  word in footers[0] for word in ("用时", "上下文", "输出", "✻")), footers[0])
        check("统计: 不再每次调模型都打一行", "↑4130" not in text)


async def case_thinking_order() -> None:
    """多次调模型：思考 → 工具摘要 → 思考 → 回答，按真实顺序；思考的多余空行被压掉"""
    script = [
        [ThinkingContent(thinking="\n\n先看目录。\n\n\n\n再决定读哪个文件。\n\n"),
         ToolCall(id="t1", name="bash", arguments={"command": "ls"})],
        [ThinkingContent(thinking="README 最关键。"),
         ToolCall(id="t2", name="read", arguments={"path": "README.md"})],
        [ThinkingContent(thinking="可以回答了。"), TextContent(text="这是一个示例项目。")],
    ]
    with Sandbox() as cwd:
        _renderer, out, _ = await run_turn(cwd, script)
        text = out.getvalue()
        lines = text.splitlines()
        marks = [text.index(m) for m in ("先看目录", "1 个工具调用 · bash", "README 最关键",
                                         "1 个工具调用 · read", "可以回答了", "● 这是一个示例项目")]
        check("顺序: 思考 → 工具 → 思考 → 工具 → 思考 → 回答", marks == sorted(marks), text)
        check("思考: 每次调用一个标题", text.count("✻ 思考") == 3, text)
        check("思考: 开头的空行去掉", lines[lines.index("✻ 思考") + 1] == "  先看目录。", text)
        check("思考: 连续空行压成一行，续行缩进",
              "  先看目录。\n  \n  再决定读哪个文件。\n" in text, repr(text))
        check("思考: 段尾空行不写出", "再决定读哪个文件。\n▸" in text, repr(text))

    with Sandbox() as cwd:
        _renderer, out, _ = await run_turn(cwd, list(SCRIPT), show_thinking=False)
        text = out.getvalue()
        check("--no-thinking: 收成一行时长", "思考了" in text and "先列目录" not in text
              and "✻ 思考" not in text, text)


async def case_expand() -> None:
    with Sandbox() as cwd:
        renderer, out, _ = await run_turn(cwd, list(SCRIPT))
        before = len(out.getvalue())
        renderer.expand_last_turn()
        expanded = out.getvalue()[before:]
        check("展开: 有标题", "上一轮详情" in expanded, expanded[:80])
        check("展开: 思考全文", "先列目录" in expanded)
        check("展开: 每个工具的参数", "ls -la" in expanded and "README.md" in expanded
              and "cat missing.txt" in expanded, expanded)
        check("展开: 工具结果", "# demo" in expanded and "def main" in expanded)
        check("展开: 失败的工具标 ✗", "✗ bash" in expanded, expanded)
        prompt = renderer.input_prompt()
        check("输入框: 上沿是不带文字的细线", prompt.split("\n")[0].strip("─ ") == ""
              and "ctrl+o" not in prompt and "tok" not in prompt, prompt)
        check("输入框: 下沿也是细线", renderer.input_footer().strip("─ ") == "")

    out = io.StringIO()
    fresh = SessionRenderer(theme=Theme(ColorMode.NONE), stream=out)
    fresh.expand_last_turn()
    check("展开: 还没有轮次时给提示", "没有可展开" in out.getvalue())


def test_running_status() -> None:
    """运行中只显示正在执行的那个工具：名字 + 时长，不带参数"""
    renderer = SessionRenderer(theme=Theme(ColorMode.NONE), stream=io.StringIO())
    renderer.state.turn_started = time.time()
    record = ToolRecord("t1", "bash", {"command": "pytest -q tests"}, time.time() - 2.0)
    renderer.state.running["t1"] = record
    status = renderer._status_text()
    check("状态行: 显示工具名", "bash" in status, status)
    check("状态行: 不显示参数", "pytest" not in status, status)
    check("状态行: 显示已执行时长", "2.0s" in status, status)
    renderer.state.running["t2"] = ToolRecord("t2", "read", {"path": "a.py"}, time.time())
    status = renderer._status_text()
    check("状态行: 并行时只显示最新的一个", "read" in status and "bash" not in status
          and "等 2 个" in status, status)
    renderer.state.running.clear()
    check("状态行: 等模型时显示思考中", "思考中" in renderer._status_text())


async def case_steering_echo() -> None:
    """途中插进来的话用 ❯ 显示；自己提交的那句不重复显示"""
    with Sandbox() as cwd:
        renderer, out, _session = await run_turn(cwd, [[TextContent(text="好")]])
        check("回显: 自己提交的那句不重复", out.getvalue().count("查看一下当前代码") == 0, out.getvalue())
        renderer.on_event(type("E", (), {"type": "message_end",
                                         "message": UserMessage(content="顺便看下测试")})())
        check("回显: 插话用 ❯ 显示", "❯ 顺便看下测试" in out.getvalue(), out.getvalue())


def test_user_echo_multiline() -> None:
    out = io.StringIO()
    renderer = SessionRenderer(theme=Theme(ColorMode.NONE), stream=out)
    renderer.user_echo("第一行\n第二行")
    check("回显: 多行对齐", out.getvalue() == "❯ 第一行\n  第二行\n", repr(out.getvalue()))


def test_collapsed_turn() -> None:
    asyncio.run(case_collapsed_turn())


def test_expand() -> None:
    asyncio.run(case_expand())


def test_thinking_order() -> None:
    asyncio.run(case_thinking_order())


def test_steering_echo() -> None:
    asyncio.run(case_steering_echo())


async def main() -> int:
    print("收起与统计")
    await case_collapsed_turn()
    print("思考")
    await case_thinking_order()
    print("展开")
    await case_expand()
    print("状态行与回显")
    test_running_status()
    await case_steering_echo()
    test_user_echo_multiline()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} 处失败：")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("全部通过 —— 终端渲染可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
