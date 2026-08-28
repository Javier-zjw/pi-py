"""
pi-agent 离线测试：不需要网络、不需要 key、不需要 pytest。

    python tests/test_offline.py
    python tests/test_offline.py -v      # 打印每个用例的事件序列

这一层的全部外部依赖都是注入的（StreamFn、AgentTool、钩子），所以可以
100% 离线覆盖。scripted_stream 用二十行替换掉整个网络栈——这正是分层
设计带来的直接好处。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))                    # packages/pi-agent
sys.path.insert(0, str(HERE.parents[2] / "pi-ai"))          # packages/pi-ai

from pi_agent import (  # noqa: E402
    AfterToolCallResult,
    Agent,
    AgentState,
    AgentTool,
    AgentToolResult,
    BeforeToolCallResult,
    CustomMessage,
    LoopConfig,
    PendingMessageQueue,
    ToolContext,
    ValidationError,
    agent_loop_continue,
    agent_message_from_dict,
    agent_message_to_dict,
    default_convert_to_llm,
    run_agent_loop,
    validate_tool_arguments,
)
from pi_ai import (  # noqa: E402
    AssistantMessage,
    DoneEvent,
    ImageContent,
    Model,
    ModelCost,
    StartEvent,
    TextContent,
    TextDeltaEvent,
    ThinkingContent,
    ThinkingDeltaEvent,
    ToolCall,
    ToolCallEndEvent,
    ToolResultMessage,
    Usage,
    UserMessage,
)

VERBOSE = "-v" in sys.argv
UNDER_PYTEST = "PYTEST_CURRENT_TEST" in os.environ
FAILURES: list[str] = []

MODEL = Model(
    id="fake-1",
    provider="fake",
    api="anthropic-messages",
    name="Fake",
    cost=ModelCost(input=1.0, output=2.0),
    context_window=1000,
)


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILURES.append(f"{name}: {detail}")
    print(f"  FAIL {name} {detail}")
    if UNDER_PYTEST:
        raise AssertionError(f"{name}: {detail}")


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #


def scripted_stream(script: list[list], usage: Usage | None = None, delay: float = 0.0):
    """按剧本产生事件流。每个元素是一轮的内容块列表。"""
    turns = iter(script)

    def stream_fn(model, context, options=None):
        async def gen():
            blocks = next(turns, [TextContent(text="(剧本已用完)")])
            message = AssistantMessage(
                api=model.api,
                provider=model.provider,
                model=model.id,
                usage=usage or Usage(input=10, output=5),
            )
            yield StartEvent(partial=message)
            for block in blocks:
                if delay:
                    await asyncio.sleep(delay)
                message.content.append(block)
                index = len(message.content) - 1
                if isinstance(block, TextContent):
                    yield TextDeltaEvent(content_index=index, delta=block.text, partial=message)
                elif isinstance(block, ThinkingContent):
                    yield ThinkingDeltaEvent(
                        content_index=index, delta=block.thinking, partial=message
                    )
                elif isinstance(block, ToolCall):
                    yield ToolCallEndEvent(content_index=index, tool_call=block, partial=message)
            message.stop_reason = (
                "toolUse" if any(isinstance(b, ToolCall) for b in blocks) else "stop"
            )
            yield DoneEvent(reason=message.stop_reason, message=message)

        return gen()

    return stream_fn


def echo_tool(name: str = "echo", delay: float = 0.0, fail: bool = False) -> AgentTool:
    async def execute(args, ctx: ToolContext) -> AgentToolResult:
        if delay:
            await asyncio.sleep(delay)
        if fail:
            raise RuntimeError("工具内部炸了")
        return AgentToolResult.text(f"echo:{args['text']}")

    return AgentTool(
        name=name,
        description="回显",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        execute=execute,
    )


def make_agent(script: list[list], tools: list[AgentTool] | None = None, **kw) -> tuple[Agent, list]:
    agent = Agent(
        stream_fn=scripted_stream(script),
        initial_state=AgentState(model=MODEL, tools=tools or []),
        **kw,
    )
    events: list = []
    agent.subscribe(events.append)
    return agent, events


def kinds(events: list) -> list[str]:
    return [e.type for e in events]


def roles(agent: Agent) -> list[str]:
    return [getattr(m, "role", "?") for m in agent.state.messages]


# --------------------------------------------------------------------------- #
# 1. 参数校验
# --------------------------------------------------------------------------- #


def test_validation() -> None:
    schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "count": {"type": "integer", "default": 3},
            "mode": {"type": "string", "enum": ["a", "b"]},
            "flag": {"type": "boolean"},
        },
        "required": ["path"],
    }
    ok = validate_tool_arguments(schema, {"path": "a.py"})
    check("校验: 填充默认值", ok["count"] == 3, str(ok))

    coerced = validate_tool_arguments(schema, {"path": "a.py", "count": "7"})
    check("校验: 字符串数字被纠正", coerced["count"] == 7, repr(coerced["count"]))

    coerced = validate_tool_arguments(schema, {"path": "a.py", "flag": "true"})
    check("校验: 字符串布尔被纠正", coerced["flag"] is True)

    for bad, why in [
        ({}, "缺必填"),
        ({"path": 1}, "类型不符"),
        ({"path": "a", "mode": "z"}, "枚举越界"),
    ]:
        try:
            validate_tool_arguments(schema, bad)
            check(f"校验: 拒绝{why}", False, f"居然通过了 {bad}")
        except ValidationError:
            check(f"校验: 拒绝{why}", True)

    nested = {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {"type": "integer"}}},
    }
    try:
        validate_tool_arguments(nested, {"items": [1, "x"]})
        check("校验: 递归检查数组元素", False, "没抓到")
    except ValidationError:
        check("校验: 递归检查数组元素", True)


# --------------------------------------------------------------------------- #
# 2. 消息转换与序列化
# --------------------------------------------------------------------------- #


def test_convert_and_serde() -> None:
    messages = [
        UserMessage(content="hi"),
        CustomMessage(custom_type="ui", content="只给界面看", include_in_context=False),
        CustomMessage(custom_type="summary", content="给模型看"),
        AssistantMessage(content=[TextContent(text="ok")]),
    ]
    llm = default_convert_to_llm(messages)
    check("转换: 过滤纯 UI 消息", len(llm) == 3, str(len(llm)))
    check("转换: 自定义消息折成 user", llm[1].role == "user" and llm[1].text() == "给模型看")
    check("转换: 顺序不变", llm[0].text() == "hi" and llm[2].text() == "ok")

    custom = CustomMessage(
        custom_type="compaction_summary", content="摘要", display=False, details={"n": 1}
    )
    back = agent_message_from_dict(agent_message_to_dict(custom))
    check(
        "序列化: 自定义消息往返",
        back.custom_type == "compaction_summary" and back.display is False and back.details == {"n": 1},
        str(back),
    )
    normal = agent_message_from_dict(agent_message_to_dict(UserMessage(content="中文")))
    check("序列化: 普通消息交给 pi_ai", normal.text() == "中文")


# --------------------------------------------------------------------------- #
# 3. 队列
# --------------------------------------------------------------------------- #


def test_queue() -> None:
    q = PendingMessageQueue()
    check("队列: 空的取出空列表", q.take() == [])
    q.push(UserMessage(content="a"))
    q.push(UserMessage(content="b"))
    check("队列: 默认一次全取", len(q.take()) == 2)
    check("队列: 取完就空", len(q) == 0 and not q)

    q1 = PendingMessageQueue(mode="one-at-a-time")
    q1.push(UserMessage(content="a"))
    q1.push(UserMessage(content="b"))
    first = q1.take()
    check("队列: 逐条模式一次一条", len(first) == 1 and first[0].text() == "a")
    check("队列: 剩下的还在", len(q1) == 1)


# --------------------------------------------------------------------------- #
# 4. 循环基本流程
# --------------------------------------------------------------------------- #


async def case_single_turn() -> None:
    agent, events = make_agent([[TextContent(text="你好")]])
    result = await agent.prompt("在吗")

    check("单轮: 返回新消息", len(result) == 1 and result[0].role == "assistant", str(result))
    check("单轮: transcript", roles(agent) == ["user", "assistant"], str(roles(agent)))
    seq = kinds(events)
    check("单轮: 生命周期事件齐全",
          seq[:2] == ["message_start", "message_end"] and seq[-1] == "agent_end", str(seq))
    check("单轮: 有 agent_start / turn_start", "agent_start" in seq and seq.count("turn_start") == 1)
    check("单轮: 用量累加", agent.state.usage().input == 10)
    check("单轮: 结束后不在流式中", not agent.state.is_streaming)
    end = events[-1]
    check("单轮: 结束原因是 stop", end.reason == "stop", end.reason)
    if VERBOSE:
        print("   ", seq)


async def case_tool_turn() -> None:
    tool = echo_tool()
    agent, events = make_agent(
        [
            [ToolCall(id="c1", name="echo", arguments={"text": "一"})],
            [TextContent(text="做完了")],
        ],
        [tool],
    )
    await agent.prompt("跑一下")

    check("工具轮: transcript",
          roles(agent) == ["user", "assistant", "toolResult", "assistant"], str(roles(agent)))
    check("工具轮: 两个回合", kinds(events).count("turn_start") == 2)
    check("工具轮: 工具事件成对",
          kinds(events).count("tool_execution_start") == 1
          and kinds(events).count("tool_execution_end") == 1)
    result = agent.state.messages[2]
    check("工具轮: 结果回填", result.text() == "echo:一" and not result.is_error, result.text())
    check("工具轮: 用量跨轮累加", agent.state.usage().input == 20)
    if VERBOSE:
        print("   ", kinds(events))


async def case_consecutive_prompts() -> None:
    """连续调用两次 prompt —— 最基本的用法，也最容易被漏测。"""
    agent, _ = make_agent([[TextContent(text="第一轮")], [TextContent(text="第二轮")]])
    await agent.prompt("一")
    await agent.prompt("二")
    check("连续: 第二轮真的跑了",
          agent.state.messages[-1].text() == "第二轮", agent.state.messages[-1].text())
    check("连续: transcript 长度", len(agent.state.messages) == 4, str(len(agent.state.messages)))
    check("连续: 结束后回到空闲", not agent.state.is_streaming)
    await asyncio.wait_for(agent.wait_for_idle(), timeout=1)
    check("连续: wait_for_idle 不会卡住", True)


async def case_streaming_guard() -> None:
    agent, _ = make_agent([[TextContent(text="x")]])

    async def second():
        await asyncio.sleep(0)
        try:
            await agent.prompt("插队")
            return "没拦住"
        except RuntimeError:
            return "拦住了"

    tool = echo_tool(delay=0.02)
    agent2, _ = make_agent(
        [[ToolCall(id="c1", name="echo", arguments={"text": "慢"})], [TextContent(text="完")]],
        [tool],
    )
    task = asyncio.create_task(agent2.prompt("跑"))
    await asyncio.sleep(0.005)
    try:
        await agent2.prompt("插队")
        check("并发保护: 流式中拒绝 prompt", False, "居然允许了")
    except RuntimeError:
        check("并发保护: 流式中拒绝 prompt", True)
    await task


# --------------------------------------------------------------------------- #
# 5. 工具执行细节
# --------------------------------------------------------------------------- #


async def case_unknown_tool() -> None:
    agent, _ = make_agent(
        [[ToolCall(id="c1", name="根本不存在", arguments={})], [TextContent(text="ok")]],
        [echo_tool()],
    )
    await agent.prompt("go")
    check("未知工具: 不崩且回错误结果",
          "Unknown tool" in agent.state.messages[2].text(), agent.state.messages[2].text())
    check("未知工具: 标记为错误", agent.state.messages[2].is_error)


async def case_invalid_arguments() -> None:
    agent, _ = make_agent(
        [[ToolCall(id="c1", name="echo", arguments={})], [TextContent(text="ok")]],
        [echo_tool()],
    )
    await agent.prompt("go")
    check("参数非法: 转成错误结果而不是异常",
          "Invalid arguments" in agent.state.messages[2].text(), agent.state.messages[2].text())


async def case_tool_exception() -> None:
    agent, _ = make_agent(
        [[ToolCall(id="c1", name="echo", arguments={"text": "x"})], [TextContent(text="ok")]],
        [echo_tool(fail=True)],
    )
    await agent.prompt("go")
    text = agent.state.messages[2].text()
    check("工具抛异常: 循环不中断", "RuntimeError" in text and "炸了" in text, text)
    check("工具抛异常: 后续回合继续", agent.state.messages[-1].text() == "ok")


async def case_parallel_ordering() -> None:
    """并行的双序语义：事件按完成顺序，结果按模型请求顺序。"""
    async def slow(args, ctx):
        await asyncio.sleep(0.03 if args["text"] == "a" else 0.001)
        return AgentToolResult.text(args["text"])

    tool = AgentTool(
        name="echo",
        description="e",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        execute=slow,
    )
    agent, events = make_agent(
        [
            [
                ToolCall(id="a", name="echo", arguments={"text": "a"}),
                ToolCall(id="b", name="echo", arguments={"text": "b"}),
            ],
            [TextContent(text="fin")],
        ],
        [tool],
    )
    await agent.prompt("go")

    ends = [e.tool_call_id for e in events if e.type == "tool_execution_end"]
    results = [m.tool_call_id for m in agent.state.messages if getattr(m, "role", "") == "toolResult"]
    check("并行: 结束事件按完成顺序", ends == ["b", "a"], str(ends))
    check("并行: 结果消息按请求顺序", results == ["a", "b"], str(results))

    starts = [e.tool_call_id for e in events if e.type == "tool_execution_start"]
    check("并行: 开始事件按请求顺序", starts == ["a", "b"], str(starts))


async def case_sequential_mode() -> None:
    order: list[str] = []

    async def record(args, ctx):
        order.append(f"start:{args['text']}")
        await asyncio.sleep(0.02 if args["text"] == "a" else 0.001)
        order.append(f"end:{args['text']}")
        return AgentToolResult.text(args["text"])

    tool = AgentTool(
        name="echo",
        description="e",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        execute=record,
    )
    agent, _ = make_agent(
        [
            [
                ToolCall(id="a", name="echo", arguments={"text": "a"}),
                ToolCall(id="b", name="echo", arguments={"text": "b"}),
            ],
            [TextContent(text="fin")],
        ],
        [tool],
        tool_execution="sequential",
    )
    await agent.prompt("go")
    check("串行: 不重叠执行",
          order == ["start:a", "end:a", "start:b", "end:b"], str(order))


async def case_tool_streaming_update() -> None:
    async def streaming(args, ctx: ToolContext):
        for chunk in ("第一段", "第二段"):
            ctx.update(AgentToolResult.text(chunk))
        return AgentToolResult.text("最终")

    tool = AgentTool(
        name="stream", description="s", parameters={"type": "object", "properties": {}},
        execute=streaming,
    )
    agent, events = make_agent(
        [[ToolCall(id="c1", name="stream", arguments={})], [TextContent(text="ok")]], [tool]
    )
    await agent.prompt("go")
    updates = [e for e in events if e.type == "tool_execution_update"]
    check("流式工具: 收到中间更新", len(updates) == 2, str(len(updates)))
    check("流式工具: 更新内容正确", updates[0].partial.content[0].text == "第一段")
    check("流式工具: 最终结果覆盖", agent.state.messages[2].text() == "最终")


# --------------------------------------------------------------------------- #
# 6. 钩子
# --------------------------------------------------------------------------- #


async def case_before_hook() -> None:
    async def block_all(call, state):
        return BeforeToolCallResult(block=True, reason="不许调")

    ran = []

    async def never(args, ctx):
        ran.append(1)
        return AgentToolResult.text("不该执行")

    tool = AgentTool(
        name="danger", description="d", parameters={"type": "object", "properties": {}},
        execute=never,
    )
    agent, _ = make_agent(
        [[ToolCall(id="c1", name="danger", arguments={})], [TextContent(text="ok")]],
        [tool], before_tool_call=block_all,
    )
    await agent.prompt("go")
    check("前置钩子: 真的拦住了", not ran, "工具居然执行了")
    check("前置钩子: 理由回给模型", "不许调" in agent.state.messages[2].text())

    async def rewrite(call, state):
        return BeforeToolCallResult(arguments={"text": "被改写"})

    agent2, _ = make_agent(
        [[ToolCall(id="c1", name="echo", arguments={"text": "原始"})], [TextContent(text="ok")]],
        [echo_tool()], before_tool_call=rewrite,
    )
    await agent2.prompt("go")
    check("前置钩子: 可改写参数",
          agent2.state.messages[2].text() == "echo:被改写", agent2.state.messages[2].text())


async def case_after_hook() -> None:
    async def replace(call, result, state):
        return AfterToolCallResult(result=AgentToolResult.text("被替换"))

    agent, _ = make_agent(
        [[ToolCall(id="c1", name="echo", arguments={"text": "x"})], [TextContent(text="ok")]],
        [echo_tool()], after_tool_call=replace,
    )
    await agent.prompt("go")
    check("后置钩子: 可替换结果", agent.state.messages[2].text() == "被替换")

    async def stop(call, result, state):
        return AfterToolCallResult(terminate=True)

    agent2, events = make_agent(
        [[ToolCall(id="c1", name="echo", arguments={"text": "x"})], [TextContent(text="不该出现")]],
        [echo_tool()], after_tool_call=stop,
    )
    await agent2.prompt("go")
    check("后置钩子: terminate 提前结束",
          events[-1].reason == "terminated", events[-1].reason)
    check("后置钩子: 第二轮没跑",
          all(m.text() != "不该出现" for m in agent2.state.messages))


async def case_transform_context() -> None:
    seen: list[int] = []

    async def transform(messages):
        seen.append(len(messages))
        return messages[-1:]  # 只保留最后一条

    agent, _ = make_agent([[TextContent(text="ok")]], transform_context=transform)
    agent.state.messages.append(UserMessage(content="老消息"))
    await agent.prompt("新消息")
    check("上下文转换: 被调用", seen == [2], str(seen))
    check("上下文转换: 不影响真实 transcript", len(agent.state.messages) == 3, str(roles(agent)))


# --------------------------------------------------------------------------- #
# 7. 队列与中断
# --------------------------------------------------------------------------- #


async def case_steering() -> None:
    agent, events = make_agent(
        [[ToolCall(id="c1", name="echo", arguments={"text": "x"})], [TextContent(text="第二轮")]],
        [echo_tool()],
    )
    agent.steer("改主意了")
    await agent.prompt("go")

    texts = [m.text() for m in agent.state.messages if getattr(m, "role", "") == "user"]
    check("引导: 被注入 transcript", "改主意了" in texts, str(texts))
    idx = [i for i, m in enumerate(agent.state.messages) if m.text() == "改主意了"][0]
    check("引导: 插在工具结果之后", agent.state.messages[idx - 1].role == "toolResult")
    pairs = [k for k in kinds(events) if k in ("message_start", "message_end")]
    check("引导: 消息事件成对",
          pairs.count("message_start") == pairs.count("message_end"), str(pairs))


async def case_follow_up() -> None:
    agent, _ = make_agent([[TextContent(text="第一轮")], [TextContent(text="第二轮")]])
    agent.follow_up("还有个事")
    await agent.prompt("go")
    texts = [m.text() for m in agent.state.messages]
    check("后续: 模型停下时才注入", "还有个事" in texts, str(texts))
    check("后续: 触发了第二轮", "第二轮" in texts, str(texts))


async def case_abort() -> None:
    started = asyncio.Event()

    async def slow(args, ctx: ToolContext):
        started.set()
        for _ in range(100):
            if ctx.cancelled:
                return AgentToolResult.text("我看到取消了", is_error=True)
            await asyncio.sleep(0.01)
        return AgentToolResult.text("跑完了")

    tool = AgentTool(
        name="slow", description="s", parameters={"type": "object", "properties": {}},
        execute=slow,
    )
    agent, events = make_agent(
        [[ToolCall(id="c1", name="slow", arguments={})], [TextContent(text="不该出现")]], [tool]
    )
    task = asyncio.create_task(agent.prompt("go"))
    await asyncio.wait_for(started.wait(), timeout=1)
    agent.abort()
    await asyncio.wait_for(task, timeout=2)

    check("中断: 工具能感知 cancel_event",
          "取消" in agent.state.messages[2].text(), agent.state.messages[2].text())
    check("中断: 结束原因是 aborted", events[-1].reason == "aborted", events[-1].reason)
    check("中断: 没有跑第二轮",
          all(m.text() != "不该出现" for m in agent.state.messages))
    check("中断: 状态复位", not agent.state.is_streaming and not agent.state.pending_tool_calls)


async def case_max_turns() -> None:
    tool = echo_tool()
    script = [[ToolCall(id=f"c{i}", name="echo", arguments={"text": str(i)})] for i in range(10)]
    agent = Agent(
        stream_fn=scripted_stream(script),
        initial_state=AgentState(model=MODEL, tools=[tool]),
        max_turns=3,
    )
    events: list = []
    agent.subscribe(events.append)
    await agent.prompt("go")
    check("回合上限: 生效", kinds(events).count("turn_start") == 3,
          str(kinds(events).count("turn_start")))
    check("回合上限: 结束原因", events[-1].reason == "max_turns", events[-1].reason)


# --------------------------------------------------------------------------- #
# 8. 错误路径与恢复
# --------------------------------------------------------------------------- #


async def case_stream_error() -> None:
    def broken_stream(model, context, options=None):
        async def gen():
            message = AssistantMessage(api=model.api, provider=model.provider, model=model.id)
            yield StartEvent(partial=message)
            message.stop_reason = "error"
            message.error_message = "provider 挂了"
            from pi_ai import ErrorEvent

            yield ErrorEvent(error="provider 挂了", message=message)

        return gen()

    agent = Agent(stream_fn=broken_stream, initial_state=AgentState(model=MODEL))
    events: list = []
    agent.subscribe(events.append)
    await agent.prompt("go")
    check("流出错: 循环优雅退出", events[-1].reason == "error", events[-1].reason)
    check("流出错: 错误写进 state", agent.state.error_message == "provider 挂了")


async def case_no_terminal_event() -> None:
    """provider 异常退出、一个终止事件都没发——必须有兜底。"""
    def truncated(model, context, options=None):
        async def gen():
            message = AssistantMessage(api=model.api, provider=model.provider, model=model.id)
            yield StartEvent(partial=message)
            yield TextDeltaEvent(content_index=0, delta="半句", partial=message)

        return gen()

    agent = Agent(stream_fn=truncated, initial_state=AgentState(model=MODEL))
    events: list = []
    agent.subscribe(events.append)
    await agent.prompt("go")
    last = agent.state.messages[-1]
    check("无终止事件: 有兜底消息", last.role == "assistant" and last.stop_reason == "error",
          f"{last.role}/{last.stop_reason}")
    check("无终止事件: 不会返回 None", events[-1].reason == "error")


async def case_loop_continue() -> None:
    state = AgentState(model=MODEL, messages=[UserMessage(content="hi")])
    config = LoopConfig(stream_fn=scripted_stream([[TextContent(text="续上了")]]))
    result = await agent_loop_continue(state, lambda e: None, config)
    check("续跑: 从 user 结尾可以继续", result[-1].text() == "续上了")

    state2 = AgentState(model=MODEL, messages=[AssistantMessage(content=[TextContent(text="x")])])
    try:
        await agent_loop_continue(state2, lambda e: None, config)
        check("续跑: assistant 结尾应拒绝", False, "居然允许了")
    except ValueError:
        check("续跑: assistant 结尾应拒绝", True)

    try:
        await agent_loop_continue(AgentState(model=MODEL), lambda e: None, config)
        check("续跑: 空 transcript 应拒绝", False, "居然允许了")
    except ValueError:
        check("续跑: 空 transcript 应拒绝", True)


# --------------------------------------------------------------------------- #
# 9. 事件订阅
# --------------------------------------------------------------------------- #


async def case_subscription() -> None:
    agent, _ = make_agent([[TextContent(text="a")], [TextContent(text="b")]])
    seen: list[str] = []
    unsubscribe = agent.subscribe(lambda e: seen.append(e.type))
    await agent.prompt("一")
    count = len(seen)
    unsubscribe()
    await agent.prompt("二")
    check("订阅: 取消后不再收到", len(seen) == count, f"{count} → {len(seen)}")

    def broken(event):
        raise RuntimeError("监听器炸了")

    agent2, _ = make_agent([[TextContent(text="ok")]])
    agent2.subscribe(broken)
    await agent2.prompt("go")
    check("订阅: 监听器异常不影响主流程",
          agent2.state.messages[-1].text() == "ok")


async def case_images_in_prompt() -> None:
    agent, _ = make_agent([[TextContent(text="看到了")]])
    await agent.prompt("这是什么", [ImageContent(data="AAAA", mime_type="image/png")])
    first = agent.state.messages[0]
    check("图片: 组成多模态消息",
          isinstance(first.content, list) and len(first.content) == 2, str(first.content))
    check("图片: 文本在前", first.content[0].text == "这是什么")


# --------------------------------------------------------------------------- #


# ── pytest 兼容层 ───────────────────────────────────────────────────
# PyCharm 看到 test_*.py 会默认用 pytest 跑，而 pytest 不装插件跑不了
# async def。这里给每个异步用例套一层同步壳；直接 python tests/test_offline.py
# 时走下面的 main()，不受影响。


def test_single_turn() -> None:
    asyncio.run(case_single_turn())


def test_tool_turn() -> None:
    asyncio.run(case_tool_turn())


def test_consecutive_prompts() -> None:
    asyncio.run(case_consecutive_prompts())


def test_streaming_guard() -> None:
    asyncio.run(case_streaming_guard())


def test_unknown_tool() -> None:
    asyncio.run(case_unknown_tool())


def test_invalid_arguments() -> None:
    asyncio.run(case_invalid_arguments())


def test_tool_exception() -> None:
    asyncio.run(case_tool_exception())


def test_parallel_ordering() -> None:
    asyncio.run(case_parallel_ordering())


def test_sequential_mode() -> None:
    asyncio.run(case_sequential_mode())


def test_tool_streaming_update() -> None:
    asyncio.run(case_tool_streaming_update())


def test_before_hook() -> None:
    asyncio.run(case_before_hook())


def test_after_hook() -> None:
    asyncio.run(case_after_hook())


def test_transform_context() -> None:
    asyncio.run(case_transform_context())


def test_steering() -> None:
    asyncio.run(case_steering())


def test_follow_up() -> None:
    asyncio.run(case_follow_up())


def test_abort() -> None:
    asyncio.run(case_abort())


def test_max_turns() -> None:
    asyncio.run(case_max_turns())


def test_stream_error() -> None:
    asyncio.run(case_stream_error())


def test_no_terminal_event() -> None:
    asyncio.run(case_no_terminal_event())


def test_loop_continue() -> None:
    asyncio.run(case_loop_continue())


def test_subscription() -> None:
    asyncio.run(case_subscription())


def test_images_in_prompt() -> None:
    asyncio.run(case_images_in_prompt())

async def main() -> int:
    print("参数校验");         test_validation()
    print("消息转换/序列化");  test_convert_and_serde()
    print("队列");             test_queue()
    print("循环基本流程")
    await case_single_turn()
    await case_tool_turn()
    await case_consecutive_prompts()
    await case_streaming_guard()
    print("工具执行")
    await case_unknown_tool()
    await case_invalid_arguments()
    await case_tool_exception()
    await case_parallel_ordering()
    await case_sequential_mode()
    await case_tool_streaming_update()
    print("钩子")
    await case_before_hook()
    await case_after_hook()
    await case_transform_context()
    print("队列与中断")
    await case_steering()
    await case_follow_up()
    await case_abort()
    await case_max_turns()
    print("错误路径")
    await case_stream_error()
    await case_no_terminal_event()
    await case_loop_continue()
    print("订阅与输入")
    await case_subscription()
    await case_images_in_prompt()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} 处失败：")
        for f in FAILURES:
            print(f"  - {f}")
        print("\n对照 tests/CHECKLIST.md 定位对应的实现位置")
        return 1
    print("全部通过 —— pi-agent 可以交付给 coding-agent 层了")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
