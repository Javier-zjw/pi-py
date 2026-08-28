"""
有状态的`Agent`：封装会话记录、事件、以及循环配套队列。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Iterator
from pi_ai import ImageContent, Model, TextContent, ThinkingLevel, UserMessage

from .loop import LoopConfig, default_convert_to_llm, run_agent_loop
from .queue import PendingMessageQueue
from .types import (
    AfterToolCall,
    AgentEvent,
    AgentMessage,
    AgentState,
    AgentTool,
    BeforeToolCall,
    ConvertToLlm,
    Emit,
    MessageEndEvent,
    MessageStartEvent,
    QueueMode,
    StreamFn,
    ToolExecutionMode,
    TransformContext
)

class Agent:
    """托管整个对话会话。单个Agent对应一套会话记录，同一时刻仅运行一个主循环"""

    def __init__(
            self,
            stream_fn: StreamFn,
            initial_state: AgentState | None = None,
            *,
            tool_execution: ToolExecutionMode = "parallel",
            max_turns: int | None = None,
            queue_mode: QueueMode = "all",
            before_tool_call: BeforeToolCall | None = None,
            after_tool_call: AfterToolCall | None = None,
            transform_context: TransformContext | None = None,
            convert_to_llm: ConvertToLlm = default_convert_to_llm,
            stream_options: dict[str, Any] | None = None,
    ) -> None:
        self.state = initial_state or AgentState()
        self.stream_fn = stream_fn
        self.tool_execution = tool_execution
        self.max_turns = max_turns
        self.before_tool_call = before_tool_call
        self.after_tool_call = after_tool_call
        self.transform_context = transform_context
        self.convert_to_llm = convert_to_llm
        self.stream_options = stream_options or {}

        self.steering_queue = PendingMessageQueue(mode=queue_mode)
        self.follower_queue = PendingMessageQueue(mode=queue_mode)

        self._listeners: list[Callable[[AgentEvent], None]] = []
        self._cancel = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()

    def subscribe(self, listener: Callable[[AgentEvent], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def _emit(self, event: AgentEvent) -> None:
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                pass

    @property
    def emit(self) -> Emit:
        return self._emit

    def set_model(self, model: Model) -> None:
        self.state.model = model

    def set_thinking_level(self, level: ThinkingLevel) -> None:
        self.state.thinking_level = level

    def set_tools(self, tools: Iterator[AgentTool]) -> None:
        self.state.tools = list(tools)

    def add_tool(self, tool: AgentTool) -> None:
        self.state.tools = [t for t in self.state.tools if t.name != tool.name] + [tool]

    def _config(self) -> LoopConfig:
        async def on_turn_end() -> list[AgentMessage]:
            return self.steering_queue.take()

        async def on_before_stop() -> list[AgentMessage]:
            return self.follower_queue.take() or self.steering_queue.take()

        return LoopConfig(
            stream_fn=self.stream_fn,
            tool_execution=self.tool_execution,
            max_turns=self.max_turns,
            before_tool_call=self.before_tool_call,
            after_tool_call=self.after_tool_call,
            transform_context=self.transform_context,
            convert_to_llm=self.convert_to_llm,
            on_turn_end=on_turn_end,
            on_before_stop=on_before_stop,
            stream_options=self.stream_options
        )

    @staticmethod
    def user_message(
            text: str,
            images: Iterator[ImageContent] | None = None
    ) -> UserMessage:
        images = list(images or [])
        if not images:
            return UserMessage(content=text, timestamp=time.time())

        content: list[Any] = [TextContent(text=text)] if text else []
        content.extend(images)
        return UserMessage(content=content, timestamp=time.time())

    async def prompt(
            self,
            text: str | AgentMessage,
            images: Iterator[ImageContent] | None = None
    ) -> list[AgentMessage]:
        """
        发送一条消息并执行至整轮流程完成。
        流式输出过程中，请改用 :meth:`steer` 或 :meth:`follow_up`。
        """
        if self.state.is_streaming:
            raise RuntimeError("agent is streaming; use steer() or follow_up()")
        message = text if not isinstance(text, str) else self.user_message(text, images)
        self.state.messages.append(message)
        self._emit_complete(message)

        self._cancel = asyncio.Event()
        self._idle.clear()
        try:
            return await run_agent_loop(self.state, self._emit, self._config(), self._cancel)
        finally:
            self._idle.set()

    def _emit_complete(self, message: UserMessage) -> None:
        """
        完整消息：用户输入、steering、follow-up。
        没有流式阶段，但仍成对发 Start + End，这样持久化和渲染只需要
        监听 message_end 一个事件，不必为注入消息写特例分支。
        """

        self._emit(MessageStartEvent(message=message))
        self._emit(MessageEndEvent(message=message))

    def steer(self, text: str | AgentMessage, images: Iterator[ImageContent] | None = None) -> None:
        """等待当前轮次所有工具调用完成后再推送"""
        message = text if not isinstance(text, str) else self.user_message(text, images)
        self.steering_queue.push(message)

    def follow_up(self, text: str | AgentMessage, images: Iterator[ImageContent] | None = None) -> None:
        """当智能体即将终止运行时，才进行消息推送"""
        message = text if not isinstance(text, str) else self.user_message(text, images)
        self.follower_queue.push(message)

    def abort(self) -> None:
        self._cancel.set()

    async def wait_for_idle(self) -> None:
        await self._idle.wait()

    @property
    def is_streaming(self) -> bool:
        return self.state.is_streaming
