"""pi-agent 联网验证：真模型 + 真工具，跑完整循环。

复用 pi-ai 那边的 .env 加载器，所以不用重复配置。

    python tests/manual/live_agent.py                    # 列出可用模型
    python tests/manual/live_agent.py -m ark_doubao      # 跑全部用例
    python tests/manual/live_agent.py -m ark_cc --only tools multi
    python tests/manual/live_agent.py -m ark_doubao --chat   # 交互式，带工具

离线测试全绿之后再跑这个。这里验证的是离线测不了的东西：真模型会不会
按 schema 正确调用工具、多轮工具链能不能自己走完、思考内容会不会回流。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[2]))                        # packages/pi-agent
sys.path.insert(0, str(HERE.parents[3] / "pi-ai"))              # packages/pi-ai
sys.path.insert(0, str(HERE.parents[3] / "pi-ai" / "tests" / "manual"))  # env_models

try:
    from env_models import build_models, default_alias, load_env, sanitize
except ImportError:
    sys.exit("找不到 pi-ai/tests/manual/env_models.py —— 先把 pi-ai 那套测试放好")

from pi_agent import (  # noqa: E402
    Agent,
    AgentState,
    AgentTool,
    AgentToolResult,
    BeforeToolCallResult,
    ToolContext,
)

G, R, Y, D, C, X = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[36m", "\033[0m"
PASS = FAIL = 0


def ok(name: str, detail: str = "") -> None:
    global PASS
    PASS += 1
    print(f"  {G}✓{X} {name} {D}{detail}{X}")


def bad(name: str, detail: str = "") -> None:
    global FAIL
    FAIL += 1
    print(f"  {R}✗{X} {name} {R}{detail}{X}")


# --------------------------------------------------------------------------- #
# 几个真工具（都在临时目录里，不文碰你的件）
# --------------------------------------------------------------------------- #

WORKDIR = Path(__file__).parent / "_scratch"


def make_tools() -> list[AgentTool]:
    WORKDIR.mkdir(exist_ok=True)

    async def calc(args, ctx: ToolContext) -> AgentToolResult:
        expr = args["expression"]
        if not all(c in "0123456789+-*/(). " for c in expr):
            return AgentToolResult.text("只支持四则运算", is_error=True)
        try:
            return AgentToolResult.text(str(eval(expr, {"__builtins__": {}}, {})))
        except Exception as exc:
            return AgentToolResult.text(f"算不出来: {exc}", is_error=True)

    async def write_note(args, ctx: ToolContext) -> AgentToolResult:
        path = WORKDIR / Path(args["name"]).name
        path.write_text(args["content"], "utf-8")
        return AgentToolResult.text(f"已写入 {path.name}（{len(args['content'])} 字）",
                                    details={"path": str(path)})

    async def read_note(args, ctx: ToolContext) -> AgentToolResult:
        path = WORKDIR / Path(args["name"]).name
        if not path.exists():
            return AgentToolResult.text(f"没有 {path.name} 这个文件", is_error=True)
        return AgentToolResult.text(path.read_text("utf-8"), details={"path": str(path)})

    async def list_notes(args, ctx: ToolContext) -> AgentToolResult:
        names = sorted(p.name for p in WORKDIR.iterdir() if p.is_file())
        return AgentToolResult.text("\n".join(names) or "(空)")

    return [
        AgentTool(
            name="calculate", label="计算",
            description="计算一个四则运算表达式。需要算数时必须用本工具，不要心算。",
            parameters={
                "type": "object",
                "properties": {"expression": {"type": "string", "description": "如 (3+4)*5"}},
                "required": ["expression"],
            },
            execute=calc,
        ),
        AgentTool(
            name="write_note", label="写入",
            description="把内容写进一个笔记文件。",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "文件名，如 note.txt"},
                    "content": {"type": "string"},
                },
                "required": ["name", "content"],
            },
            execute=write_note,
        ),
        AgentTool(
            name="read_note", label="读取",
            description="读一个笔记文件的内容。",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            execute=read_note,
        ),
        AgentTool(
            name="list_notes", label="列出",
            description="列出所有笔记文件。",
            parameters={"type": "object", "properties": {}},
            execute=list_notes,
        ),
    ]


def make_agent(models, spec, tools, **kw) -> tuple[Agent, list]:
    def stream_fn(model, context, options=None):
        if options is not None:
            options.extra = spec.stream_extra(spec.thinking_level)
        return models.stream_simple(model, context, options)

    agent = Agent(
        stream_fn=stream_fn,
        initial_state=AgentState(
            system_prompt="你是一个会用工具的助手。需要计算或读写文件时必须调用工具，"
                          "不要凭空回答。回答简短。",
            model=spec.model,
            thinking_level=spec.thinking_level,
            tools=tools,
        ),
        **kw,
    )
    events: list = []
    agent.subscribe(events.append)
    return agent, events


def trace(events: list) -> None:
    for e in events:
        if e.type == "tool_execution_start":
            print(f"    {C}→ {e.tool_name}{X} {D}{json.dumps(e.arguments, ensure_ascii=False)}{X}")
        elif e.type == "tool_execution_end":
            mark = f"{G}✓{X}" if not e.is_error else f"{R}✗{X}"
            text = e.result.content[0].text if e.result.content else ""
            print(f"    {mark} {D}{text.splitlines()[0][:70] if text else ''}{X}")


# --------------------------------------------------------------------------- #
# 用例
# --------------------------------------------------------------------------- #


async def case_no_tools(models, spec, args) -> bool:
    agent, events = make_agent(models, spec, [])
    await agent.prompt("用一句话说明什么是二分查找。")
    last = agent.state.messages[-1]
    if last.stop_reason == "error":
        bad("no_tools 出错", last.error_message or "")
        return False
    if not last.text().strip():
        bad("no_tools 回复为空", str([e.type for e in events][:8]))
        return False
    ok("no_tools", f"{len(last.text())} 字 · {agent.state.usage().input} in")
    return True


async def case_tools(models, spec, args) -> bool:
    tools = make_tools()
    agent, events = make_agent(models, spec, tools)
    await agent.prompt("帮我算一下 (127 + 373) * 4 等于多少。")
    trace(events)

    calls = [e for e in events if e.type == "tool_execution_start"]
    if not calls:
        bad("tools 模型没调用工具", agent.state.messages[-1].text()[:60])
        return False
    ok("tools 发起调用", calls[0].tool_name)
    if calls[0].tool_name == "calculate" and "expression" in calls[0].arguments:
        ok("tools 参数符合 schema", json.dumps(calls[0].arguments, ensure_ascii=False))
    else:
        bad("tools 参数不对", str(calls[0].arguments))
    results = [m for m in agent.state.messages if getattr(m, "role", "") == "toolResult"]
    if results and results[0].text().strip() == "2000":
        ok("tools 结果正确", "2000")
    else:
        bad("tools 结果不对", results[0].text() if results else "无结果")
    if "2000" in agent.state.messages[-1].text():
        ok("tools 模型用上了结果")
    else:
        bad("tools 模型没用结果", agent.state.messages[-1].text()[:60])
    return True


async def case_multi(models, spec, args) -> bool:
    """多轮工具链：写 → 读 → 回答。考验循环能否自己走完。"""
    tools = make_tools()
    agent, events = make_agent(models, spec, tools)
    await agent.prompt(
        "先把「三段式提交信：标题、正文、脚注」写进 rule.txt，"
        "然后读回来，告诉我文件里第一个词是什么。"
    )
    trace(events)

    turns = sum(1 for e in events if e.type == "turn_start")
    names = [e.tool_name for e in events if e.type == "tool_execution_start"]
    if turns >= 2:
        ok("multi 多轮循环", f"{turns} 轮 · 工具 {names}")
    else:
        bad("multi 只跑了一轮", str(names))
    if "write_note" in names and "read_note" in names:
        ok("multi 工具链完整")
    else:
        bad("multi 工具链不全", str(names))
    if (WORKDIR / "rule.txt").exists():
        ok("multi 文件真的写了", (WORKDIR / "rule.txt").read_text("utf-8")[:40])
    else:
        bad("multi 文件没写出来")
    return True


async def case_hook(models, spec, args) -> bool:
    """前置钩子在真模型上的效果：拦截后模型应该改变行为。"""
    blocked: list[str] = []

    async def deny_write(call, state):
        if call.name == "write_note":
            blocked.append(call.name)
            return BeforeToolCallResult(block=True, reason="当前处于只读模式，禁止写文件。")
        return BeforeToolCallResult()

    tools = make_tools()
    agent, events = make_agent(models, spec, tools, before_tool_call=deny_write)
    await agent.prompt("把「测试内容」写进 blocked.txt")
    trace(events)

    if blocked:
        ok("hook 拦截生效", f"拦下 {blocked}")
    else:
        bad("hook 模型没尝试写文件", "换个提示词再试")
    if not (WORKDIR / "blocked.txt").exists():
        ok("hook 文件确实没被创建")
    else:
        bad("hook 文件居然创建了")
        (WORKDIR / "blocked.txt").unlink()
    if agent.state.messages[-1].stop_reason != "error":
        ok("hook 模型收到拒绝后正常收尾", agent.state.messages[-1].text()[:40])
    return True


async def case_abort(models, spec, args) -> bool:
    """真流式过程中中断。"""
    tools = make_tools()
    agent, events = make_agent(models, spec, tools)
    task = asyncio.create_task(agent.prompt("详细讲讲快速排序的实现，越长越好。"))
    await asyncio.sleep(1.2)
    agent.abort()
    await asyncio.wait_for(task, timeout=20)

    end = events[-1]
    if end.reason == "aborted":
        ok("abort 中断生效", f"已产出 {len(agent.state.messages[-1].text())} 字")
    else:
        bad("abort 没中断", end.reason)
    if not agent.state.is_streaming:
        ok("abort 状态复位")
    else:
        bad("abort 状态没复位")
    return True


ALL_CASES = {
    "no_tools": case_no_tools,
    "tools": case_tools,
    "multi": case_multi,
    "hook": case_hook,
    "abort": case_abort,
}


async def chat(models, spec) -> None:
    """交互式：真模型 + 真工具，观察循环行为。"""
    agent, _ = make_agent(models, spec, make_tools())

    def on_event(e) -> None:
        if e.type == "message_update":
            inner = e.assistant_message_event
            if inner.type == "text_delta":
                sys.stdout.write(inner.delta)
                sys.stdout.flush()
            elif inner.type == "thinking_delta":
                sys.stdout.write(f"{D}{inner.delta}{X}")
                sys.stdout.flush()
        elif e.type == "tool_execution_start":
            print(f"\n{C}→ {e.tool_name}{X} {D}{json.dumps(e.arguments, ensure_ascii=False)}{X}")
        elif e.type == "tool_execution_end":
            mark = f"{G}✓{X}" if not e.is_error else f"{R}✗{X}"
            text = e.result.content[0].text if e.result.content else ""
            print(f"{mark} {D}{text.splitlines()[0][:70] if text else ''}{X}")
        elif e.type == "agent_end":
            u = agent.state.usage()
            print(f"\n{D}  {e.reason} · ↑{u.input} ↓{u.output} · ¥{u.cost.total:.4f}{X}\n")

    agent.subscribe(on_event)
    print(f"{Y}工具：calculate / write_note / read_note / list_notes{X}")
    print(f"{D}Ctrl+C 中断当前回答，Ctrl+D 退出{X}\n")
    loop = asyncio.get_running_loop()
    while True:
        try:
            line = sanitize(await loop.run_in_executor(None, lambda: input("你 > "))).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        try:
            await agent.prompt(line)
        except KeyboardInterrupt:
            agent.abort()
            print(f"\n{Y}已中断{X}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="pi-agent 联网验证")
    parser.add_argument("-m", "--model", help=".env 里的别名")
    parser.add_argument("--only", nargs="+", choices=list(ALL_CASES))
    parser.add_argument("--chat", action="store_true", help="交互模式")
    parser.add_argument("--env", help="指定 .env 路径")
    args = parser.parse_args()

    env = load_env(args.env)
    try:
        models, specs = build_models(env)
    except (RuntimeError, ValueError) as exc:
        print(f"{R}配置有问题：{exc}{X}")
        return 2

    if not args.model:
        print(f"{Y}可用模型：{X}")
        for spec in specs.values():
            flag = f"{G}有 key{X}" if spec.api_key else f"{R}缺 key{X}"
            print(f"  {spec.describe()}  [{flag}]")
        print(f"\n{D}加 -m <别名> 开跑{X}")
        return 0

    spec = specs[args.model]
    if not spec.api_key:
        print(f"{R}[{args.model}] 没配 API_KEY{X}")
        return 2

    if args.chat:
        await chat(models, spec)
        return 0

    print(f"\n{Y}▶ {spec.alias}{X}  {spec.model.api}  {spec.model.id}\n")
    for name in args.only or ALL_CASES:
        print(f"{Y}[{name}]{X}")
        try:
            await ALL_CASES[name](models, spec, args)
        except Exception as exc:
            bad(f"{name} 抛异常", f"{type(exc).__name__}: {exc}")
        print()

    print(f"{G}{PASS} 通过{X}  {R if FAIL else D}{FAIL} 失败{X}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
