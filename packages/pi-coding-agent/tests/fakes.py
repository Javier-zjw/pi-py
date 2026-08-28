"""测试用的假 provider 和小工具，供 test_runtime.py / test_agent_session.py 共用。

这些替身之所以能存在，靠的是分层：pi-agent 只认 StreamFn，pi-ai 只认
Provider 协议，所以整条链路都能在不联网的情况下跑通。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
for _p in (HERE.parents[1], HERE.parents[2] / "pi-agent", HERE.parents[2] / "pi-ai"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from pi_ai import (  # noqa: E402
    AssistantMessage,
    DoneEvent,
    Model,
    ModelCost,
    Models,
    StartEvent,
    TextContent,
    TextDeltaEvent,
    ThinkingContent,
    ThinkingDeltaEvent,
    ToolCall,
    ToolCallEndEvent,
    Usage,
)

UNDER_PYTEST = "PYTEST_CURRENT_TEST" in os.environ

FAKE_MODEL = Model(
    id="fake-1",
    provider="fake",
    api="anthropic-messages",
    name="Fake One",
    cost=ModelCost(input=1.0, output=2.0),
    context_window=1000,
    max_tokens=256,
)

BIG_MODEL = Model(
    id="fake-big",
    provider="fake",
    api="anthropic-messages",
    name="Fake Big",
    cost=ModelCost(input=1.0, output=2.0),
    context_window=200_000,
)


class Sandbox:
    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix="pi-test-"))
        return self.path

    def __exit__(self, *exc) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def scripted_stream(script: list[list], usage: Usage | None = None):
    """按剧本产生事件流。每个元素是一轮的内容块列表。"""
    turns = iter(script)

    def stream_fn(model, context, options=None):
        async def gen():
            blocks = next(turns, [TextContent(text="(剧本用完了)")])
            message = AssistantMessage(
                api=model.api, provider=model.provider, model=model.id,
                usage=usage or Usage(input=10, output=5),
            )
            yield StartEvent(partial=message)
            for block in blocks:
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


class FakeProvider:
    """满足 pi_ai.Provider 协议的最小实现。"""

    api = "anthropic-messages"

    def __init__(self, script: list[list], provider_id: str = "fake",
                 usage: Usage | None = None) -> None:
        self.id = provider_id
        self.name = provider_id
        self.fn = scripted_stream(script, usage)
        self.calls: list = []

    def stream(self, model, context, options=None, api_key=None):
        self.calls.append({"model": model, "context": context, "options": options})
        return self.fn(model, context, options)


def fake_models(script: list[list], models: list[Model] | None = None,
                usage: Usage | None = None) -> tuple[Models, FakeProvider]:
    provider = FakeProvider(script, usage=usage)
    registry = Models(models=models or [FAKE_MODEL])
    registry.set_provider(provider)
    return registry, provider
