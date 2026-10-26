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
from types import SimpleNamespace

HERE = Path(__file__).resolve()
PACKAGES = HERE.parents[2]
for _p in (HERE.parents[1], PACKAGES / "pi-tui", PACKAGES / "pi-coding-agent",
           PACKAGES / "pi-coding-agent" / "tests", PACKAGES / "pi-agent", PACKAGES / "pi-ai"):
    sys.path.insert(0, str(_p))

from fakes import FAKE_MODEL, Sandbox, fake_models  # noqa: E402

from pi_agent import CustomMessage
from pi_ai import TextContent, ThinkingContent, ToolCall, Usage, UserMessage  # noqa: E402
from pi_tui import Theme, display_width  # noqa: E402
from pi_tui.theme import ColorMode  # noqa: E402

from pi_app.renderer import SLOT_LINES, SessionRenderer  # noqa: E402
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
        check("不落盘: 不打印工具参数", "ls -la" not in text and "missing.txt" not in text, text)
        check("不落盘: 不打印工具结果", "# demo" not in text and "No such file" not in text)
        check("不落盘: 不打印思考", "先列目录" not in text and "思考" not in text, text)
        summary = [line for line in lines if line.startswith("✻ ")]
        check("用时: 每轮一行，在最上面", len(summary) == 1 and lines[0] == summary[0], str(lines))
        check("用时: 带工具次数（工具名在活动区和 ctrl+o 里看）", "4 次工具" in summary[0]
              and "bash" not in summary[0], summary[0])
        check("用时: 失败数醒目标出", "1 失败" in summary[0], summary[0])
        check("用时: 提示怎么看详情", "ctrl+o 详情" in summary[0])
        check("用时: 不带文字标签", "用时" not in text and "上下文" not in text, text)
        answer = next((i for i, line in enumerate(lines) if line.startswith("● ")), None)
        check("回答: 以 ● 开头，紧跟在用时下面", answer == 1, str(lines))
        check("回答: 按 Markdown 排版，列表缩进两格", answer is not None
              and lines[answer + 1].startswith("  • README.md"), str(lines[answer:answer + 3]))
        check("留白: 用时行和回答之间空一行", "ctrl+o 详情\n\n● " in text, repr(text))
        check("统计: 最后一行只有 token", lines[-1] == "↑4.1k · ↓344", lines[-1])
        check("统计: 不再每次调模型都打一行", "↑4130" not in text)


async def case_interim_text() -> None:
    """工具前的过渡话不当回答；回答首尾和中间多余的空行去掉"""
    script = [
        [ThinkingContent(thinking="先看目录。"),
         TextContent(text="好的，让我先查看一下项目结构。\n\n\n"),
         ToolCall(id="t1", name="bash", arguments={"command": "ls"})],
        [ThinkingContent(thinking="可以回答了。"),
         TextContent(text="\n\n这是一个示例项目。\n\n\n\n第二段。\n\n\n")],
    ]
    with Sandbox() as cwd:
        renderer, out, _ = await run_turn(cwd, script)
        text = out.getvalue()
        check("过渡话: 不写进 scrollback", "让我先查看" not in text, text)
        check("过渡话: 只有一个 ●", text.count("●") == 1, text)
        check("回答: 开头空行去掉", "● 这是一个示例项目。" in text, repr(text))
        check("回答: 连续空行压成一行", "这是一个示例项目。\n\n  第二段。\n" in text, repr(text))
        check("回答: 段尾空行不写出，和统计之间只空一行", "第二段。\n\n↑" in text, repr(text))
        steps = [kind for kind, _ in renderer.last_turn.steps]
        check("展开: 按顺序记下思考、过渡话、工具、思考",
              steps == ["thinking", "text", "tool", "thinking"], str(steps))
        before = len(text)
        renderer.expand_last_turn()
        expanded = out.getvalue()[before:]
        check("展开: 过渡话也能看到", "◦ 好的，让我先查看一下项目结构。" in expanded, expanded)
        check("展开: 顺序和发生顺序一致",
              expanded.index("先看目录") < expanded.index("让我先查看") < expanded.index("✓ bash")
              < expanded.index("可以回答了"), expanded)


def ev(kind: str, **fields):
    return SimpleNamespace(type=kind, **fields)


def delta(kind: str, text: str):
    return ev("message_update", assistant_message_event=SimpleNamespace(type=kind, delta=text))


def test_live_slot() -> None:
    """思考、过渡话、工具共用活动区的一块位置，后来的顶掉先来的；第一行是本轮用时"""
    renderer = SessionRenderer(theme=Theme(ColorMode.NONE), stream=io.StringIO())
    live = lambda: "\n".join(renderer._live_lines())  # noqa: E731
    renderer.on_event(ev("agent_start"))
    renderer.state.turn_started = time.time() - 3.0
    renderer.on_event(ev("turn_start"))
    renderer.on_event(delta("thinking_delta", "\n\n用户想看代码，"))
    renderer.on_event(delta("thinking_delta", "我先列目录。"))
    text = live()
    check("活动区: 第一行是用时和状态", text.split("\n")[0].endswith("3.0s · 思考中"), text)
    check("活动区: 显示思考原文，去掉开头空行", "\n  用户想看代码，我先列目录。" in text, repr(text))

    renderer.on_event(ev("tool_execution_start", tool_call_id="t1", tool_name="bash",
                         arguments={"command": "ls -la"}))
    text = live()
    check("活动区: 工具顶掉思考", "bash" in text and "列目录" not in text, text)
    check("活动区: 工具带上关键参数", "bash  ls -la" in text, text)
    check("活动区: 用时行带工具次数", "执行工具 · 1 次工具" in text, text)
    renderer.on_event(ev("tool_execution_end", tool_call_id="t1", tool_name="bash", is_error=False,
                         result=SimpleNamespace(content=[], details=None)))
    check("活动区: 工具跑完显示 ✓，等下一步", "✓ bash" in live(), live())

    renderer.on_event(ev("turn_start"))
    renderer.on_event(delta("thinking_delta", "README 最关键。"))
    text = live()
    check("活动区: 新的思考顶掉工具", "README 最关键" in text and "bash" not in text, text)
    renderer.on_event(delta("text_delta", "让我读一下 README。"))
    text = live()
    check("活动区: 过渡话顶掉思考", "让我读一下" in text and "README 最关键" not in text, text)

    long_thought = "\n".join(f"第 {i} 行思考" for i in range(20))
    renderer.on_event(ev("turn_start"))
    renderer.on_event(delta("thinking_delta", long_thought))
    text = live()
    check("活动区: 长思考只显示最新几行", "第 19 行思考" in text and "第 0 行思考" not in text
          and len(text.split("\n")) == 1 + SLOT_LINES, text)

    renderer.on_event(delta("text_delta", "\n".join(f"回答第 {i} 行" for i in range(10))))
    check("长文字: 超过活动区就当回答写出", renderer.state.answering
          and "● 回答第 0 行" in renderer.stream.getvalue(), renderer.stream.getvalue())
    check("长文字: 用时写在回答上面", renderer.stream.getvalue().index("✻ ")
          < renderer.stream.getvalue().index("●"))

    quiet = SessionRenderer(theme=Theme(ColorMode.NONE), stream=io.StringIO(), show_thinking=False)
    quiet.on_event(ev("agent_start"))
    quiet.on_event(delta("thinking_delta", "不该出现"))
    check("--no-thinking: 活动区不显示思考原文", "不该出现" not in "\n".join(quiet._live_lines())
          and "思考中" in "\n".join(quiet._live_lines()))


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
        check("输入框: 下沿也是细线（下面一行是当前模式）",
              renderer.input_footer().split("\n")[0].strip("─ ") == "")

    out = io.StringIO()
    fresh = SessionRenderer(theme=Theme(ColorMode.NONE), stream=out)
    fresh.expand_last_turn()
    check("展开: 还没有轮次时给提示", "没有可展开" in out.getvalue())


def test_prune_and_notice() -> None:
    """清理旧工具输出不再一行一行刷屏：计数并进「✻ 用时」行；扩展的提醒显示成一行说明"""
    out = io.StringIO()
    renderer = SessionRenderer(theme=Theme(ColorMode.NONE), stream=out)
    renderer.on_event(ev("context_pruned", count=3, tokens_before=40_000, tokens_after=30_000))
    renderer.on_event(ev("agent_start"))
    renderer.on_event(ev("context_pruned", count=1, tokens_before=35_000, tokens_after=33_000))
    check("清理: 运行中显示在活动区第一行", "清理 2 次" in renderer._live_lines()[0], renderer._live_lines()[0])
    check("清理: 不往 scrollback 里一行一行写", "已清理" not in out.getvalue(), out.getvalue())
    renderer.on_event(ev("message_end", message=CustomMessage(
        custom_type="tool_budget", content="[系统提醒] 给模型看的全文",
        details={"notice": "已调用 40 次工具，提醒模型尽快给出回答"})))
    text = out.getvalue()
    check("提醒: 显示一行说明", "⚑ 已调用 40 次工具" in text, text)
    check("提醒: 不把给模型的全文当用户的话回显", "给模型看的全文" not in text and "❯" not in text, text)
    renderer.on_event(ev("message_end", message=CustomMessage(
        custom_type="tool_budget", content="[系统提醒] 对照一下方向……",
        details={"notice": "第 15 次工具调用：提醒模型对照用户的请求", "quiet": True})))
    check("重锚: quiet 的提醒不写进 scrollback", "第 15 次工具调用" not in out.getvalue(), out.getvalue())
    renderer.on_event(ev("agent_end", reason="stop"))
    summary = next(line for line in out.getvalue().splitlines() if line.startswith("✻ "))
    check("清理: 次数和省下的 tokens 并进用时行", "清理旧输出 2 次 −12.0k" in summary, summary)
    check("重锚: ctrl+o 里看得到", ("text", "↺ 第 15 次工具调用：提醒模型对照用户的请求")
          in renderer.last_turn.steps, str(renderer.last_turn.steps))
    renderer.on_event(ev("agent_start"))
    renderer.on_event(ev("agent_end", reason="stop"))
    check("清理: 下一轮重新计数", "清理" not in out.getvalue().splitlines()[-3:][0], out.getvalue())


def plan_events(renderer, call_id: str, plan: list[tuple[str, str]], is_error: bool = False) -> None:
    """模拟一次 update_plan：成功时扩展发 todo_updated，被拒绝时只有工具结束"""
    steps = [{"step": step, "status": status} for step, status in plan]
    renderer.on_event(ev("tool_execution_start", tool_call_id=call_id, tool_name="update_plan",
                         arguments={}))
    if not is_error:
        renderer.on_event(ev("todo_updated", plan=steps, explanation=""))
    renderer.on_event(ev("tool_execution_end", tool_call_id=call_id, tool_name="update_plan",
                         is_error=is_error, result=SimpleNamespace(content=[], details=None)))


def test_plan_display() -> None:
    """update_plan 不算工具：运行中勾选单常驻活动区，回答开始前写在用时行下面，展开时按顺序出现"""
    out = io.StringIO()
    renderer = SessionRenderer(theme=Theme(ColorMode.NONE), stream=out)
    renderer.on_event(ev("agent_start"))
    plan_events(renderer, "p1", [("读取项目结构", "completed"), ("分析压缩模块", "in_progress"),
                                 ("总结", "pending")])
    live = "\n".join(renderer._live_lines())
    check("计划: 活动区显示勾选单（带进度和序号）", "☰ 任务 1/3" in live and "✓ 1. 读取项目结构" in live
          and "▸ 2. 分析压缩模块" in live and "○ 3. 总结" in live, live)
    check("计划: 不算工具调用", renderer.state.tool_total == 0 and "次工具" not in live, live)
    renderer.on_event(ev("tool_execution_start", tool_call_id="t1", tool_name="read",
                         arguments={"path": "a.py"}))
    live = "\n".join(renderer._live_lines())
    check("计划: 执行别的工具时勾选单还在", "▸ 2. 分析压缩模块" in live and "read" in live, live)
    renderer.on_event(ev("tool_execution_end", tool_call_id="t1", tool_name="read", is_error=False,
                         result=SimpleNamespace(content=[], details=None)))
    plan_events(renderer, "p2", [("全部", "in_progress"), ("一起", "in_progress")], is_error=True)
    check("计划: 被拒绝的更新不显示", "▸ 2. 分析压缩模块" in "\n".join(renderer._live_lines()))
    plan_events(renderer, "p3", [("读取项目结构", "completed"), ("分析压缩模块", "completed"),
                                 ("总结", "in_progress")])
    renderer.on_event(delta("text_delta", "结论。"))
    renderer.on_event(ev("message_end", message=SimpleNamespace(
        role="assistant", content=[], stop_reason="stop", usage=None, text=lambda: "结论。")))
    renderer.on_event(ev("agent_end", reason="stop"))
    lines = out.getvalue().splitlines()
    summary = next(i for i, line in enumerate(lines) if line.startswith("✻ "))
    check("计划: 用时行只数真正的工具", "1 次工具" in lines[summary] and "2 次" not in lines[summary],
          lines[summary])
    check("计划: 最终勾选单写在用时行下面、回答上面",
          lines[summary + 1:summary + 5] == ["  ☰ 任务 2/3", "    ✓ 1. 读取项目结构",
                                             "    ✓ 2. 分析压缩模块", "    ▸ 3. 总结"]
          and out.getvalue().index("▸ 3. 总结") < out.getvalue().index("● 结论"), str(lines))
    before = len(out.getvalue())
    renderer.expand_last_turn()
    expanded = out.getvalue()[before:]
    check("计划: 展开时按顺序出现", expanded.index("☰ 计划") < expanded.index("✓ read")
          < expanded.rindex("☰ 计划"), expanded)


def test_plan_mode_display() -> None:
    """计划模式：输入框下显示模式；提交的计划单独成块；批准后清单跨轮常驻、逐项打勾"""
    out = io.StringIO()
    renderer = SessionRenderer(theme=Theme(ColorMode.NONE), stream=out)
    check("模式: 默认显示执行模式和切换方式", "执行模式" in renderer.input_footer()
          and "shift+tab" in renderer.input_footer())
    renderer.on_event(ev("mode_changed", mode="plan", previous="chat"))
    footer = renderer.input_footer().split("\n")
    check("模式: 切到计划模式后下沿第二行显示", len(footer) == 2 and "⏸ 计划模式" in footer[1], str(footer))
    check("模式: 切模式不往 scrollback 写东西（可能正在输入框里）", out.getvalue() == "")

    renderer.on_event(ev("agent_start"))
    renderer.on_event(ev("tool_execution_start", tool_call_id="s1", tool_name="submit_plan", arguments={}))
    renderer.on_event(ev("plan_proposed", plan={
        "title": "加超时配置", "summary": "## 思路\n把超时做成**可配置**。",
        "steps": ["读 cfg.py", "改成读环境变量", "跑测试"],
        "tasks": [{"title": "读 cfg.py", "files": ["cfg.py"], "done_when": "确认 TIMEOUT 的用法"},
                  {"title": "改成读环境变量", "files": ["cfg.py"], "done_when": "PI_TIMEOUT=5 时生效"},
                  {"title": "跑测试", "files": [], "done_when": "pytest 全部通过"}],
        "files": ["cfg.py"], "questions": []}))
    renderer.on_event(ev("tool_execution_end", tool_call_id="s1", tool_name="submit_plan", is_error=False,
                         result=SimpleNamespace(content=[], details=None)))
    renderer.on_event(ev("agent_end", reason="terminated"))
    text = out.getvalue()
    lines = text.splitlines()
    check("计划块: 标题行", any(line.startswith("╭ 计划 加超时配置") for line in lines), text)
    check("计划块: Markdown 排版", "│ 思路" in text and "│ 把超时做成可配置。" in text, text)
    check("计划块: 任务表（序号、任务、完成标准、涉及文件）", "任务（3 项）" in text
          and any("# " in line and "任务" in line and "完成标准" in line and "涉及文件" in line for line in lines)
          and any("│ 2 │ 改成读环境变量" in line and "PI_TIMEOUT=5 时生效" in line for line in lines), text)
    check("计划块: 在用时行下面", text.index("✻ ") < text.index("╭ 计划"))
    check("计划块: submit_plan 不算工具调用", "次工具" not in text, text)
    check("计划块: 提交即停不显示「结束原因」", "结束原因" not in text, text)

    renderer.on_event(ev("mode_changed", mode="chat", previous="plan"))
    renderer.on_event(ev("todo_updated", plan=[{"step": "读 cfg.py", "status": "in_progress"},
                                               {"step": "改成读环境变量", "status": "pending"},
                                               {"step": "跑测试", "status": "pending"}], explanation=""))
    renderer.on_event(ev("agent_start"))
    live = "\n".join(renderer._live_lines())
    check("执行: 批准时生成的清单在执行这一轮一开始就显示", "▸ 1. 读 cfg.py" in live and "○ 3. 跑测试" in live, live)
    plan_events(renderer, "u1", [("读 cfg.py", "completed"), ("改成读环境变量", "in_progress"),
                                 ("跑测试", "pending")])
    live = "\n".join(renderer._live_lines())
    check("执行: 做完一项实时打勾", "✓ 1. 读 cfg.py" in live and "▸ 2. 改成读环境变量" in live, live)
    start = len(out.getvalue())
    renderer.on_event(ev("agent_end", reason="stop"))
    tail = out.getvalue()[start:]
    check("执行: 一轮结束清单没做完时提示", "还有 2 项没完成" in tail, tail)

    renderer.on_event(ev("agent_start"))
    check("执行: 下一轮清单还在", "▸ 2. 改成读环境变量" in "\n".join(renderer._live_lines()))
    start = len(out.getvalue())
    renderer.on_event(ev("todo_updated", plan=[{"step": "读 cfg.py", "status": "completed"},
                                               {"step": "跑测试", "status": "in_progress"}],
                         explanation="改成读环境变量已在上一轮顺手完成"))
    check("执行: 调整清单时留一行原因", "↻ 调整计划：改成读环境变量已在上一轮顺手完成" in out.getvalue()[start:],
          out.getvalue()[start:])

    many = [{"step": f"第 {i} 项", "status": "completed" if i < 9 else "pending"} for i in range(1, 13)]
    many[8]["status"] = "in_progress"
    renderer.on_event(ev("todo_updated", plan=many, explanation=""))
    live = "\n".join(renderer._live_lines())
    check("执行: 项太多时围绕正在做的那项显示", "☰ 任务 8/12" in live and "▸  9. 第 9 项" in live
          and "前 4 项已完成" in live and "○ 12. 第 12 项" in live and "第 1 项" not in live, live)
    early = [{"step": f"第 {i} 项", "status": "completed" if i < 3 else "pending"} for i in range(1, 13)]
    early[2]["status"] = "in_progress"
    renderer.on_event(ev("todo_updated", plan=early, explanation=""))
    live = "\n".join(renderer._live_lines())
    check("执行: 后面的项太多时截掉并说明", "✓  2. 第 2 项" in live and "▸  3. 第 3 项" in live
          and "… 还有 3 项" in live and "前 1 项已完成" in live, live)
    renderer.reset()
    check("换会话: 清单清掉", renderer.plan is None)


def test_plan_guard_segment() -> None:
    """回答之后被提醒继续（计划没做完）：后面算新的一段，最终回答前重新写用时行和最新清单"""
    out = io.StringIO()
    renderer = SessionRenderer(theme=Theme(ColorMode.NONE), stream=out)
    steps = ["改 report", "改 storage"]

    def answer(text: str) -> None:
        renderer.on_event(delta("text_delta", text))
        renderer.on_event(ev("message_end", message=SimpleNamespace(
            role="assistant", content=[], stop_reason="stop", usage=None, text=lambda: text)))

    def tool(call_id: str) -> None:
        renderer.on_event(ev("tool_execution_start", tool_call_id=call_id, tool_name="bash", arguments={}))
        renderer.on_event(ev("tool_execution_end", tool_call_id=call_id, tool_name="bash", is_error=False,
                             result=SimpleNamespace(content=[], details=None)))

    renderer.on_event(ev("agent_start"))
    plan_events(renderer, "u0", [(steps[0], "in_progress"), (steps[1], "pending")])
    tool("b1")
    plan_events(renderer, "u1", [(steps[0], "completed"), (steps[1], "in_progress")])
    answer("先到这里。")
    renderer.on_event(ev("message_end", message=SimpleNamespace(
        role="custom", details={"notice": "计划还有 1 项没完成，提醒模型继续（1/2）"}, text=lambda: "…")))
    tool("b2")
    tool("b3")
    plan_events(renderer, "u2", [(steps[0], "completed"), (steps[1], "completed")])
    answer("都做完了。")
    renderer.on_event(ev("agent_end", reason="stop"))
    lines = out.getvalue().splitlines()
    summaries = [i for i, line in enumerate(lines) if line.startswith("✻ ")]
    check("分段: 提醒之后另起一段用时行", len(summaries) == 2, str(lines))
    check("分段: 第二段只数自己的工具", "2 次工具" in lines[summaries[1]]
          and "1 次工具" in lines[summaries[0]], str([lines[i] for i in summaries]))
    check("分段: 最终回答上面是最新的清单", lines[summaries[1] + 1] == "  ✓ 任务 2/2"
          and lines.index("● 都做完了。") > summaries[1] + 2, str(lines))


def test_retry_display() -> None:
    """限流 / 过载重试：活动区显示原因和等多久，失败那次的思考作废；次数并进用时行"""
    out = io.StringIO()
    renderer = SessionRenderer(theme=Theme(ColorMode.NONE), stream=out)
    renderer.on_event(ev("agent_start"))
    renderer.on_event(ev("turn_start"))
    renderer.on_event(delta("thinking_delta", "想到一半"))
    renderer.on_event(ev("retry", attempt=1, max_attempts=3, delay=2.0,
                         error="HTTP 529: Overloaded (overloaded_error)"))
    live = "\n".join(renderer._live_lines())
    check("重试: 活动区显示原因和等待", "等待重试" in live and "HTTP 529: Overloaded" in live
          and "2s 后第 1/3 次" in live, live)
    check("重试: 失败那次的思考不再显示", "想到一半" not in live, live)
    check("重试: 不往 scrollback 写", out.getvalue() == "", repr(out.getvalue()))
    renderer.on_event(ev("turn_start"))
    check("重试: 重新开始后回到思考中", "思考中" in "\n".join(renderer._live_lines()))
    renderer.on_event(delta("text_delta", "好了"))
    renderer.on_event(ev("message_end", message=SimpleNamespace(
        role="assistant", content=[], stop_reason="stop", usage=None, text=lambda: "好了")))
    renderer.on_event(ev("agent_end", reason="stop"))
    summary = next(line for line in out.getvalue().splitlines() if line.startswith("✻ "))
    check("重试: 次数并进用时行", "重试 1 次" in summary, summary)


def test_tool_lines() -> None:
    """活动区每个工具一行：名字 + 关键参数 + 结果；一轮结束汇总改了哪些文件；状态栏"""
    out = io.StringIO()
    renderer = SessionRenderer(theme=Theme(ColorMode.NONE), stream=out)
    renderer.cwd = "/work"
    renderer.on_event(ev("agent_start"))

    def start(call_id, name, args):
        renderer.on_event(ev("tool_execution_start", tool_call_id=call_id, tool_name=name, arguments=args))

    def end(call_id, name, details=None, text="", error=False):
        renderer.on_event(ev("tool_execution_end", tool_call_id=call_id, tool_name=name, is_error=error,
                             result=SimpleNamespace(content=[SimpleNamespace(text=text)], details=details)))

    start("b1", "bash", {"command": "pytest -q\n  tests"})
    renderer.state.running["b1"].started -= 2.0
    live = "\n".join(renderer._live_lines())
    check("工具行: 在跑的显示命令和用时", "bash  pytest -q tests  2.0s" in live, live)
    end("b1", "bash", {"exit_code": 0})
    start("e1", "edit", {"path": "/work/src/app.py", "old_text": "a", "new_text": "b"})
    end("e1", "edit", {"path": "/work/src/app.py", "patch": "--- a\n+++ b\n-x\n+y\n+z\n"})
    start("w1", "write", {"path": "notes.md", "content": "一\n二\n三\n"})
    end("w1", "write", {"path": "/work/notes.md", "created": True})
    start("r1", "read", {"path": "/work/README.md", "offset": 10})
    end("r1", "read", {"path": "/work/README.md", "lines": 42})
    live = "\n".join(renderer._live_lines())
    check("工具行: 路径相对工作目录、edit 显示加减行数", "✓ edit  src/app.py  +2 −1" in live, live)
    check("工具行: write 显示新建几行", "✓ write  notes.md  新建 3 行" in live, live)
    check("工具行: read 显示行数和起始行", "✓ read  README.md:10  42 行" in live, live)
    check("工具行: 只显示最新 3 个，前面的折成一行", "… 前面 1 个（完成 1）" in live and "pytest" not in live, live)
    start("g1", "grep", {"pattern": "TODO", "path": "/work/src"})
    end("g1", "grep", text="exit 2: bad pattern\nmore", error=True)
    live = "\n".join(renderer._live_lines())
    check("工具行: 失败显示错误第一行", "✗ grep  TODO  src  exit 2: bad pattern" in live, live)
    start("e2", "edit", {"path": "src/app.py"})
    end("e2", "edit", {"path": "/work/src/app.py", "patch": "+w\n"})

    renderer.on_event(delta("text_delta", "改好了。"))
    renderer.on_event(ev("message_end", message=SimpleNamespace(
        role="assistant", content=[], stop_reason="stop", usage=None, text=lambda: "改好了。")))
    renderer.on_event(ev("agent_end", reason="stop"))
    lines = out.getvalue().splitlines()
    summary = next(line for line in lines if line.startswith("✻ "))
    check("用时行: 改了几个文件、总加减", "6 次工具 · 1 失败 · 改了 2 个文件 +6 −1" in summary, summary)
    files = lines[lines.index("● 改好了。") + 1:]
    check("改动汇总: 回答后每个文件一行，同一文件合并", "  src/app.py  +3 −1" in files
          and "  notes.md    +3 −0  新建" in files, str(files))

    renderer.status.model = "fake/glm-5.2"
    renderer.context_window, renderer.context_tokens = 200_000, 36_000
    renderer.status.cost = 0.0123
    footer = renderer.input_footer().split("\n")[1]
    check("状态栏: 模型 · 上下文占用 · 费用", footer.endswith("glm-5.2 · 上下文 18% · ¥0.012"), footer)


def test_web_tool_lines() -> None:
    """上网工具的一行：搜索显示查询词和条数、来源；读网页显示去掉协议的地址、标题和字数"""
    renderer = SessionRenderer(theme=Theme(ColorMode.NONE), stream=io.StringIO())
    renderer.on_event(ev("agent_start"))
    for call_id, name, args, details in (
            ("s1", "web_search", {"query": "pi  coding agent"}, {"count": 5, "backend": "bing"}),
            ("f1", "web_fetch", {"url": "https://www.example.com/docs/install"},
             {"title": "安装指南", "chars": 12034}),
            ("f2", "web_fetch", {"url": "https://example.com/a", "start": 20000}, {"title": "", "chars": 40000})):
        renderer.on_event(ev("tool_execution_start", tool_call_id=call_id, tool_name=name, arguments=args))
        renderer.on_event(ev("tool_execution_end", tool_call_id=call_id, tool_name=name, is_error=False,
                             result=SimpleNamespace(content=[SimpleNamespace(text="")], details=details)))
    live = "\n".join(renderer._live_lines())
    check("上网: 搜索显示查询词、条数和来源", "web_search  pi coding agent  5 条 · bing" in live, live)
    check("上网: 读网页显示地址、标题、字数", "web_fetch  example.com/docs/install  安装指南 · 12034 字" in live, live)
    check("上网: 接着读时显示起点", "web_fetch  example.com/a @20000  40000 字" in live, live)


def test_turn_sections() -> None:
    """查看器里的段落：按发生顺序，标题一行说清做了什么，正文是全文；失败的工具默认展开"""
    renderer = SessionRenderer(theme=Theme(ColorMode.NONE), stream=io.StringIO())
    renderer.cwd = "/work"
    renderer.on_event(ev("agent_start"))
    renderer.on_event(delta("thinking_delta", "先看看入口"))
    renderer.on_event(ev("tool_execution_start", tool_call_id="b1", tool_name="bash",
                         arguments={"command": "pytest -q"}))
    renderer.on_event(ev("tool_execution_end", tool_call_id="b1", tool_name="bash", is_error=True,
                         result=SimpleNamespace(content=[SimpleNamespace(text="1 failed\nE assert 1 == 2")],
                                                details={"exit_code": 1})))
    renderer.on_event(ev("tool_execution_start", tool_call_id="e1", tool_name="edit",
                         arguments={"path": "/work/app.py"}))
    renderer.on_event(ev("tool_execution_end", tool_call_id="e1", tool_name="edit", is_error=False,
                         result=SimpleNamespace(content=[], details={
                             "path": "/work/app.py", "patch": "--- a//work/app.py\n+++ b//work/app.py\n-x\n+y"})))
    renderer.on_event(ev("tool_execution_start", tool_call_id="r1", tool_name="read",
                         arguments={"path": "/work/big.txt"}))
    sections = renderer.turn_sections(renderer.turn)
    titles = [strip(s.title) for s in sections]
    check("段落: 思考、工具按顺序", len(sections) == 4 and titles[0].startswith("✻ 思考")
          and "先看看入口" in titles[0], str(titles))
    check("段落: 失败的命令标题带错误、默认展开、正文有命令和输出", titles[1].startswith("✗ bash  pytest -q")
          and sections[1].expanded and strip(sections[1].body[0]) == "$ pytest -q"
          and "E assert 1 == 2" in strip("\n".join(sections[1].body)), str(sections[1]))
    body = [strip(line) for line in sections[2].body]
    check("段落: edit 正文是 diff，去掉带路径的文件头", body == ["-x", "+y"] and not sections[2].expanded, str(body))
    check("段落: 还在跑的工具标 ⏺ 运行中", titles[3].startswith("⏺ read") and "运行中" in titles[3], titles[3])
    check("段落: 没有过程时为空", renderer.turn_sections(None) == [])


def test_welcome() -> None:
    """启动欢迎框：目录（家目录缩写成 ~）、模型和思考档位、模式；框下一行常用操作"""
    import os

    out = io.StringIO()
    renderer = SessionRenderer(theme=Theme(ColorMode.NONE), stream=out)
    renderer.permission = "自动接受文件修改"
    renderer.header(os.path.expanduser("~/proj"), "fake/glm-5.2", "high")
    lines = out.getvalue().splitlines()
    check("欢迎框: 有边框、每行等宽", lines[0].startswith("╭") and lines[6].startswith("╰")
          and len({display_width(line) for line in lines[:7]}) == 1, str(lines))
    text = "\n".join(lines)
    check("欢迎框: 目录、模型、档位、模式、权限", "目录  ~/proj" in text and "fake/glm-5.2 · 思考 high" in text
          and "执行模式 · 权限 自动接受文件修改" in text, text)
    check("欢迎框: 下面一行常用操作", "ctrl+o 上一轮详情" in lines[7] and "Esc 中断" in lines[7], lines[7])


def test_code_highlight() -> None:
    """代码块按语言着色；没写语言的不上色；表格可以比正文宽"""
    from pi_tui import highlight, render_markdown
    from pi_tui.theme import ColorMode as Mode

    t = Theme(Mode.ANSI256)
    code = highlight("def f(x=1):  # 注释", "py", t)
    check("着色: 关键字、函数名、数字、注释都有颜色", code.count("\x1b[") >= 4 and "# 注释" in strip(code), code)
    check("着色: 没写语言原样", highlight("# 标题", "", t) == "# 标题")
    check("着色: JSON 键和值颜色不同", highlight('{"k": "v"}', "json", t).count("\x1b[38;5;") == 2)
    check("着色: diff 加删行", "\x1b[" in highlight("+new", "diff", t) and "\x1b[" in highlight("-old", "diff", t))
    lines = render_markdown("```python\nimport os\n```", 40, t)
    check("着色: Markdown 代码块用上了", any("\x1b[" in line and "import" in strip(line) for line in lines), str(lines))
    table = "| 名称 | 说明 |\n|---|---|\n| a | " + "很长的说明" * 8 + " |"
    narrow = render_markdown(table, 40, Theme(ColorMode.NONE))
    wide = render_markdown(table, 40, Theme(ColorMode.NONE), table_width=100)
    check("表格: 可以比正文宽（少折几行）", len(wide) < len(narrow) and max(len(x) for x in wide) > 40,
          f"{len(narrow)} → {len(wide)}")


def strip(text: str) -> str:
    import re
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


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
    renderer.print_lines(["  ✓ 模型 x"])
    renderer.user_echo("下一句")
    check("留白: 新的提问和上面的输出之间空一行", out.getvalue().endswith("✓ 模型 x\n\n❯ 下一句\n"),
          repr(out.getvalue()))


def test_user_echo_long() -> None:
    """粘贴一大段：回显只留开头几行 + 说明（按终端高度），全文在 ctrl+o 里"""
    import pi_app.renderer as module

    real = module.terminal_height
    try:
        module.terminal_height = lambda: 24
        out = io.StringIO()
        renderer = SessionRenderer(theme=Theme(ColorMode.NONE), stream=out)
        text = "\n".join(f"第 {i} 行需求说明" for i in range(1, 31))
        renderer.user_echo(text)
        lines = out.getvalue().splitlines()
        check("长输入: 只留 7 行 + 一行说明", len(lines) == 8 and lines[0] == "❯ 第 1 行需求说明"
              and lines[6] == "  第 7 行需求说明" and "… 还有 23 行" in lines[7]
              and "ctrl+o 看全文" in lines[7], str(lines))

        renderer.on_event(ev("agent_start"))
        renderer.on_event(delta("text_delta", "好的。"))
        renderer.on_event(ev("message_end", message=SimpleNamespace(
            role="assistant", content=[], stop_reason="stop", usage=None, text=lambda: "好的。")))
        renderer.on_event(ev("agent_end", reason="stop"))
        start = len(out.getvalue())
        renderer.expand_last_turn()
        expanded = out.getvalue()[start:]
        check("长输入: ctrl+o 展开全文", "你的输入（全文）" in expanded and "│ 第 30 行需求说明" in expanded,
              expanded)

        renderer.user_echo("短句")
        renderer.on_event(ev("agent_start"))
        check("长输入: 下一句短的不带上一句的全文", renderer.turn.prompt == "")

        module.terminal_height = lambda: 9
        out = io.StringIO()
        renderer = SessionRenderer(theme=Theme(ColorMode.NONE), stream=out)
        renderer.user_echo("很长" * 400)
        lines = out.getvalue().splitlines()
        check("长输入: 矮终端少留几行；一整行超长也按折行后的行数算", len(lines) == 4
              and "… 还有" in lines[3] and all(display_width(line) <= renderer.width for line in lines),
              str(lines))
        renderer.user_echo("第一行\n第二行\n第三行\n第四行")
        check("长输入: 不超过上限的照常全显示", out.getvalue().endswith("  第四行\n"), out.getvalue()[-40:])
    finally:
        module.terminal_height = real


def test_footer_narrow() -> None:
    """窗口窄时输入框下沿按次要程度丢内容，不折行、不在半个字处被切掉，模式名总在"""
    import pi_app.renderer as module

    real = module.terminal_width
    try:
        renderer = SessionRenderer(theme=Theme(ColorMode.NONE), stream=io.StringIO())
        renderer.status.model = "fake/glm-5.2"
        renderer.context_window, renderer.context_tokens = 200_000, 36_000
        renderer.permission = "自动放行"
        renderer.agent_running = True
        shown = {}
        for cols in (140, 100, 64, 40, 24):
            module.terminal_width = lambda cols=cols: cols
            footer = renderer.input_footer().split("\n")
            shown[cols] = footer[1]
            check(f"窄窗口: {cols} 列下沿不超宽", len(footer) == 2
                  and all(display_width(line) <= cols for line in footer), str(footer))
        check("窄窗口: 宽时全显示", "回车插话" in shown[140] and "shift+tab" in shown[140]
              and "上下文 18%" in shown[140] and "⚠ 自动放行" in shown[140], shown[140])
        check("窄窗口: 先丢模式说明、保留状态栏", "shift+tab" not in shown[64]
              and "执行模式" in shown[64] and "上下文 18%" in shown[64], shown[64])
        check("窄窗口: 再缩运行提示、丢状态栏，模式和权限还在", "Esc 中断" in shown[40]
              and "回车插话" not in shown[40] and "执行模式" in shown[40]
              and "⚠ 自动放行" in shown[40] and "上下文" not in shown[40], shown[40])
        check("窄窗口: 实在放不下就截断加省略号", shown[24].endswith("…"), shown[24])
    finally:
        module.terminal_width = real


def test_collapsed_turn() -> None:
    asyncio.run(case_collapsed_turn())


def test_expand() -> None:
    asyncio.run(case_expand())


def test_interim_text() -> None:
    asyncio.run(case_interim_text())


def test_steering_echo() -> None:
    asyncio.run(case_steering_echo())


async def main() -> int:
    print("收起与统计")
    await case_collapsed_turn()
    print("过渡话与空行")
    await case_interim_text()
    print("活动区")
    test_live_slot()
    test_prune_and_notice()
    test_plan_display()
    test_plan_mode_display()
    test_plan_guard_segment()
    test_retry_display()
    print("展开")
    await case_expand()
    print("状态行与回显")
    test_tool_lines()
    test_web_tool_lines()
    test_turn_sections()
    test_welcome()
    test_code_highlight()
    await case_steering_echo()
    test_user_echo_multiline()
    test_user_echo_long()
    test_footer_narrow()

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
