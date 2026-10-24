"""
**智能体主循环**

近乎纯函数：接收智能体状态、`StreamFn`（上下文转事件流函数）与 `emit` 回调作为入参；持续执行回合，直到模型不再发起工具调用。自身不持有任何 IO 逻辑 —— 所有外部副作用均由外部注入。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from pi_ai import (
    AssistantMessage,
    Context,
    DoneEvent,
    ErrorEvent,
    Message,
    SimpleStreamOptions,
    TextContent,
    Tool,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)

from .types import (
    AfterToolCall,
    AgentEndEvent,
    AgentMessage,
    AgentStartEvent,
    AgentState,
    AgentToolResult,
    BeforeToolCall,
    ConvertToLlm,
    CustomMessage,
    Emit,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    StreamFn,
    ToolContext,
    ToolExecutionEndEvent,
    ToolExecutionMode,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    TurnEndEvent,
    TurnStartEvent,
    EndReason,
)

from .validation import ValidationError, validate_tool_arguments

INTERRUPTED_TOOL_TEXT = (
    "[没有执行结果：工具执行被中断（中途取消或进程退出），结果未知。"
    "如果它会修改文件或系统状态，请先检查现状，再决定是否重做]"
)


def interrupted_tool_result(call: ToolCall) -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id=call.id,
        tool_name=call.name,
        content=[TextContent(text=INTERRUPTED_TOOL_TEXT)],
        details={"interrupted": True},
        is_error=True,
    )


def find_missing_tool_results(messages: list[Any]) -> list[tuple[int, list[ToolCall]]]:
    """[(应插入的位置, 缺结果的工具调用)]。结果必须紧跟在发起调用的 assistant 之后"""
    missing: list[tuple[int, list[ToolCall]]] = []
    for i, m in enumerate(messages):
        if not isinstance(m, AssistantMessage):
            continue
        calls = m.tool_calls()
        if not calls:
            continue
        j = i + 1
        answered: set[str] = set()
        while j < len(messages) and isinstance(messages[j], ToolResultMessage):
            answered.add(messages[j].tool_call_id)
            j += 1
        lacking = [c for c in calls if c.id not in answered]
        if lacking:
            missing.append((j, lacking))
    return missing


def ensure_tool_results(messages: list[Any]) -> list[Any]:
    """
    给没有结果的工具调用补上"被中断"的结果。
    服务商要求每个 tool_call 后面都有对应结果，否则整个请求被拒——中途取消（模型刚
    发出调用就被中断）或进程在工具执行中退出，都会留下这种调用，之后每次请求都失败。
    """
    missing = find_missing_tool_results(messages)
    if not missing:
        return messages
    out = list(messages)
    for position, calls in reversed(missing):
        out[position:position] = [interrupted_tool_result(c) for c in calls]
    return out


def default_convert_to_llm(messages: list[AgentMessage]) -> list[Message]:

    out: list[Message] = []
    for m in messages:
        if isinstance(m, CustomMessage):
            if m.include_in_context:
                out.append(UserMessage(content=m.content, timestamp=m.timestamp))

        else:
            out.append(m)

    return ensure_tool_results(out)

@dataclass
class LoopConfig:
    stream_fn: StreamFn
    tool_execution: ToolExecutionMode = "parallel"
    max_turns: int | None = None
    before_tool_call: BeforeToolCall | None = None
    after_tool_call: AfterToolCall | None = None
    transform_context: Callable[[list[AgentMessage]], Awaitable[list[AgentMessage]]] | None = None
    convert_to_llm: ConvertToLlm = default_convert_to_llm
    """返回额外消息，将在下一轮开始前追加（引导消息）"""
    on_turn_end: Callable[[], Awaitable[list[AgentMessage]]] | None = None
    """返回额外消息，用于维持循环持续运行（后续消息）"""
    on_before_stop: Callable[[], Awaitable[list[AgentMessage]]] | None = None
    """模型调用出错时询问上层。返回 True 表示上层已处理（比如上下文超长已经压缩、限流已经等过），
    丢弃这条失败的回复、重试本轮；连续失败最多重试 max_error_retries 次（成功一次就重新计数）"""
    on_stream_error: Callable[[AssistantMessage], Awaitable[bool]] | None = None
    max_error_retries: int = 2
    stream_options: dict[str, Any] = field(default_factory=dict)


def _result_to_message(call: ToolCall, result: AgentToolResult) -> ToolResultMessage:

    return ToolResultMessage(
        tool_call_id=call.id,
        tool_name=call.name,
        content=list(result.content),
        details=result.details,
        usage=result.usage,
        is_error=result.is_error,
        timestamp=time.time()
    )

async def _run_one_tool(
        call: ToolCall,
        arguments: dict[str, Any],
        state: AgentState,
        emit: Emit,
        config: LoopConfig,
        cancel_event: asyncio.Event
) -> tuple[AgentToolResult, bool]:
    """执行单次经过校验、无拦截的工具调用。返回二元组：(执行结果，终止标记)"""
    tool = state.tool_by_name(call.name)
    assert tool is not None

    def on_update(partial: AgentToolResult) -> None:
        emit(ToolExecutionUpdateEvent(tool_call_id=call.id, tool_name=call.name, partial=partial))

    ctx = ToolContext(tool_call_id=call.id, cancel_event=cancel_event, on_update=on_update)

    try:
        result = await tool(arguments, ctx)
    except asyncio.CancelledError:
        result = AgentToolResult.text("Tool call aborted.", is_error=True)
    except Exception as exc:
        result = AgentToolResult.text(f"{type(exc).__name__}: {exc}", is_error=True)

    terminate = result.terminate
    if config.after_tool_call:
        hook = await config.after_tool_call(call, result, state)
        if hook:
            result = hook.result or result
            terminate = terminate or hook.terminate

    state.pending_tool_calls.discard(call.id)
    emit(
        ToolExecutionEndEvent(
            tool_call_id=call.id,
            tool_name=call.name,
            result=result,
            is_error=result.is_error
        )
    )

    return result, terminate

async def execute_tool_calls(
        calls: list[ToolCall],
        state: AgentState,
        emit: Emit,
        config: LoopConfig,
        cancel_event: asyncio.Event
) -> tuple[list[ToolResultMessage], bool]:
    """
    **执行一批工具调用**
    参数校验与 `before_tool_call` 钩子按顺序串行执行（因此钩子可以按次序感知整批调用）；工具实际执行遵循 `tool_execution` 调度逻辑。
    `tool_execution_end` 事件按照工具**完成先后顺序**触发，但最终返回的结果消息保持模型（助手）原始请求的顺序。
    """
    prepared: list[tuple[int, ToolCall, dict[str, Any]]] = []
    results: list[ToolResultMessage | None] = [None] * len(calls)
    terminate = False

    for index, call in enumerate(calls):
        emit(
            ToolExecutionStartEvent(
                tool_call_id=call.id, tool_name=call.name, arguments=call.arguments
            )
        )

        tool = state.tool_by_name(call.name)

        if tool is None:
            result = AgentToolResult.text(f"Unknown tool: {call.name}", is_error=True)
            emit(ToolExecutionEndEvent(call.id, call.name, result, True))
            results[index] = _result_to_message(call, result)
            continue

        try:
            arguments = validate_tool_arguments(tool.parameters, call.arguments)
        except ValidationError as exc:
            result = AgentToolResult.text(f"Invalid arguments: {exc}", is_error=True)
            emit(ToolExecutionEndEvent(call.id, call.name, result, True))
            results[index] = _result_to_message(call, result)
            continue

        if config.before_tool_call:
            hook = await config.before_tool_call(call, state)
            if hook and hook.arguments is not None:
                arguments = hook.arguments
            if hook and hook.block:
                result = AgentToolResult.text(
                    hook.reason or "Tool call blocked.", is_error=True
                )
                emit(ToolExecutionEndEvent(call.id, call.name, result, is_error=True))
                results[index] = _result_to_message(call, result)
                continue

        state.pending_tool_calls.add(call.id)
        prepared.append((index, call, arguments))

    async def run(index: int, call: ToolCall, arguments: dict[str, Any]) -> None:
        nonlocal terminate
        result, term = await _run_one_tool(call, arguments, state, emit, config, cancel_event)
        results[index] = _result_to_message(call, result)
        terminate = terminate or term

    if config.tool_execution == "sequential":
        for index, call, arguments in prepared:
            await run(index, call, arguments)
            if cancel_event.is_set():
                break

    elif prepared:
        await asyncio.gather(*(run(i, c, a) for i, c, a in prepared))

    for index, call, _args in prepared:
        if results[index] is None:
            results[index] = _result_to_message(
                call, AgentToolResult.text("Tool call aborted.", is_error=True)
            )

    return [r for r in results if r is not None], terminate

async def _stream_turn(
    state: AgentState,
    emit: Emit,
    config: LoopConfig,
    cancel_event: asyncio.Event,
) -> AssistantMessage:
    messages = list(state.messages)
    if config.transform_context:
        messages = await config.transform_context(messages)
    llm_messages = config.convert_to_llm(messages)

    context = Context(
        system_prompt=state.system_prompt,
        messages=llm_messages,
        tools=[Tool(name=t.name, description=t.description, parameters=t.parameters) for t in state.tools],
    )
    options = SimpleStreamOptions(reasoning=state.thinking_level, **config.stream_options)

    assert state.model is not None, "AgentState.model must be set before prompting"
    final: AssistantMessage | None = None
    started = False
    stream = config.stream_fn(state.model, context, options)
    async for event in stream:
        if not started:
            started = True
            state.streaming_message = getattr(event, "partial", None)
            if state.streaming_message is not None:
                emit(MessageStartEvent(message=state.streaming_message))
        emit(MessageUpdateEvent(assistant_message_event=event))
        if isinstance(event, (DoneEvent, ErrorEvent)):
            final = event.message
        if cancel_event.is_set():
            aclose = getattr(stream, "aclose", None)
            if aclose:
                await aclose()
            if final is None:
                final = state.streaming_message or AssistantMessage(
                    api=state.model.api, provider=state.model.provider, model=state.model.id
                )
            final.stop_reason = "aborted"
            break

    if final is None:
        final = AssistantMessage(
            api=state.model.api,
            provider=state.model.provider,
            model=state.model.id,
            stop_reason="error",
            error_message="stream ended without a terminal event"
        )

    state.streaming_message = None
    return final

MAX_ERROR_RETRIES = 2     # LoopConfig.max_error_retries 的默认值


async def run_agent_loop(
        state: AgentState,
        emit: Emit,
        config: LoopConfig,
        cancel_event: asyncio.Event | None = None
) -> list[AgentMessage]:
    """持续执行回合，直到模型不再发起工具调用。会向 ``state.messages`` 追加消息。"""

    cancel_event = cancel_event or asyncio.Event()
    new_messages: list[AgentMessage] = []
    emit(AgentStartEvent(messages=list(state.messages)))
    state.is_streaming = True
    state.error_message = None
    reason: EndReason = "stop"
    turn = 0
    error_retries = 0

    try:
        while True:
            if cancel_event.is_set():
                reason = "aborted"
                break
            if config.max_turns is not None and turn >= config.max_turns:
                reason = "max_turns"
                break
            turn += 1
            emit(TurnStartEvent(turn=turn))

            assistant = await _stream_turn(state, emit, config, cancel_event)
            if (
                    assistant.stop_reason == "error"
                    and config.on_stream_error is not None
                    and error_retries < config.max_error_retries
                    and not cancel_event.is_set()
            ):
                error_retries += 1
                if await config.on_stream_error(assistant):
                    # 失败的回复不进 transcript、不发 message_end（订阅方据此落盘），直接重试本轮
                    continue
            if assistant.stop_reason != "error":
                error_retries = 0       # 连续失败才累计：长任务里偶尔失败几次不该耗尽重试
            state.messages.append(assistant)
            new_messages.append(assistant)
            emit(MessageEndEvent(message=assistant))

            if assistant.stop_reason in ("error", "aborted"):
                state.error_message = assistant.error_message
                reason = "aborted" if assistant.stop_reason == "aborted" else "error"
                emit(TurnEndEvent(turn=turn, message=assistant, tool_results=[]))
                break

            calls = assistant.tool_calls()
            if not calls:
                emit(TurnEndEvent(turn=turn, message=assistant, tool_results=[]))
                extra: list[AgentMessage] = (
                    (await config.on_before_stop() or []) if config.on_before_stop else []
                )
                if extra:
                    state.messages.extend(extra)
                    new_messages.extend(extra)
                    for m in extra:
                        emit(MessageStartEvent(message=m))
                        emit(MessageEndEvent(message=m))
                    continue
                reason = "stop"
                break

            tool_results, terminate = await execute_tool_calls(
                calls, state, emit, config, cancel_event
            )
            state.messages.extend(tool_results)
            new_messages.extend(tool_results)
            emit(TurnEndEvent(turn=turn, message=assistant, tool_results=tool_results))

            if terminate:
                reason = "terminated"
                break

            if cancel_event.is_set():
                reason = "aborted"
                break

            if config.on_turn_end:
                steering: list[AgentMessage] = await config.on_turn_end() or []
                if steering:
                    state.messages.extend(steering)
                    new_messages.extend(steering)
                    for m in steering:
                        emit(MessageStartEvent(message=m))
                        emit(MessageEndEvent(message=m))
    finally:
        state.is_streaming = False
        state.streaming_message = None
        state.pending_tool_calls.clear()

    emit(AgentEndEvent(messages=new_messages, reason=reason))
    return new_messages

async def agent_loop_continue(
        state: AgentState,
        emit: Emit,
        config: LoopConfig,
        cancel_event: asyncio.Event | None = None
) -> list[AgentMessage]:
    """
    **以用户消息或工具结果消息收尾的会话记录恢复运行。**
    用于服务商临时故障后重试，避免重复发送用户提示内容。
    """
    if not state.messages:
        raise ValidationError("cannot continue an empty transcript")
    last = state.messages[-1]
    if isinstance(last, AssistantMessage):
        raise ValidationError("cannot continue: last message is an assistant message")

    return await run_agent_loop(state, emit, config, cancel_event)