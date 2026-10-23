"""
pi-ai 离线测试：假 transport 喂 SSE，不需要网络和 key。
python tests/test_offline.py
联网之前先把这个跑绿。这里挂了，联网只会浪费额度。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pi_ai import (  # noqa: E402
    AnthropicProvider,
    Context,
    DoneEvent,
    ErrorEvent,
    Model,
    ModelCost,
    OpenAIProvider,
    SimpleStreamOptions,
    SSEEvent,
    TextContent,
    Tool,
    ToolResultMessage,
    UserMessage,
    calculate_cost,
    message_from_dict,
    message_to_dict, LLMError,
)
from pi_ai.json_parse import parse_streaming_json  # noqa: E402
from pi_ai.types import Usage  # noqa: E402

FAILURES: list[str] = []

# pytest 下必须抛出来，否则失败会被吞掉变成假绿灯；
# 脚本模式下继续往下跑，一次看完所有问题。
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


class FakeTransport:
    """回放一串 SSE，并把发出去的请求体记下来供断言。"""

    def __init__(self, events: list[dict]) -> None:
        self.events = events
        self.payload: dict | None = None
        self.url: str | None = None
        self.headers: dict | None = None

    async def stream_sse(self, url, headers, payload, timeout=600.0):
        self.url, self.headers, self.payload = url, headers, payload
        for data in self.events:
            yield SSEEvent(data.get("type"), data)


ANTHROPIC_MODEL = Model(
    id="ark-code-latest",
    provider="ark",
    api="anthropic-messages",
    cost=ModelCost(input=1.0, output=2.0),
    reasoning=True,
    base_url="https://example.invalid/api/coding",
    thinking_level_map={"medium": 12288},
)
OPENAI_MODEL = Model(
    id="doubao-seed-code",
    provider="ark_oai",
    api="openai-completions",
    cost=ModelCost(input=1.0, output=2.0),
    base_url="https://example.invalid/api/v3",
)


async def drain(provider, model, context, options=None, api_key="k"):
    kinds, final = [], None
    async for event in provider.stream(model, context, options or SimpleStreamOptions(), api_key):
        kinds.append(event.type)
        if isinstance(event, (DoneEvent, ErrorEvent)):
            final = event.message
    return final, kinds


# ── anthropic 协议 ──────────────────────────────────────────────────


async def case_anthropic_text() -> None:
    transport = FakeTransport(
        [
            {"type": "message_start", "message": {"id": "m1", "usage": {"input_tokens": 100, "cache_read_input_tokens": 20}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "你好"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "世界"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 50}},
        ]
    )
    provider = AnthropicProvider(transport=transport)
    context = Context(system_prompt="sys", messages=[UserMessage(content="hi")])
    msg, kinds = await drain(provider, ANTHROPIC_MODEL, context)

    check("anthropic: 文本拼接", msg.text() == "你好世界", msg.text())
    check("anthropic: 停止原因", msg.stop_reason == "stop", msg.stop_reason)
    check("anthropic: 用量", msg.usage.input == 100 and msg.usage.output == 50)
    check("anthropic: 缓存读取", msg.usage.cache_read == 20)
    check("anthropic: 成本", abs(msg.usage.cost.total - (100e-6 + 100e-6)) < 1e-9, str(msg.usage.cost.total))
    check("anthropic: 事件序", kinds[:3] == ["start", "text_start", "text_delta"], str(kinds[:3]))
    check("anthropic: URL 拼接", transport.url.endswith("/api/coding/v1/messages"), transport.url)
    check("anthropic: 鉴权头", transport.headers.get("x-api-key") == "k")
    check("anthropic: system 单独字段", transport.payload.get("system") == "sys")


async def case_anthropic_tools_and_thinking() -> None:
    transport = FakeTransport(
        [
            {"type": "message_start", "message": {"usage": {"input_tokens": 1}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "想一下"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "t1", "name": "get_weather"}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"city"'}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": ': "杭州"}'}},
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 9}},
        ]
    )
    provider = AnthropicProvider(transport=transport)
    tool = Tool(name="get_weather", description="d", parameters={"type": "object", "properties": {}})
    context = Context(messages=[UserMessage(content="天气")], tools=[tool])
    options = SimpleStreamOptions(reasoning="medium")
    msg, kinds = await drain(provider, ANTHROPIC_MODEL, context, options)

    calls = msg.tool_calls()
    check("anthropic: 工具调用解析", len(calls) == 1 and calls[0].arguments == {"city": "杭州"}, str(calls))
    check("anthropic: 工具停止原因", msg.stop_reason == "toolUse", msg.stop_reason)
    check("anthropic: 思考块", msg.content[0].thinking == "想一下")
    check("anthropic: 签名累积", msg.content[0].signature == "sig")
    check("anthropic: thinking 参数", transport.payload.get("thinking") == {"type": "enabled", "budget_tokens": 12288}, str(transport.payload.get("thinking")))
    check("anthropic: max_tokens 抬高", transport.payload["max_tokens"] > 12288)
    check("anthropic: 工具 schema 下发", transport.payload["tools"][0]["name"] == "get_weather")
    check("anthropic: 增量事件齐全", "thinking_delta" in kinds and "toolcall_delta" in kinds and "toolcall_end" in kinds)


def test_anthropic_payload_shapes() -> None:
    from pi_ai.providers.anthropic import build_payload

    context = Context(
        messages=[
            UserMessage(content="q"),
            message_from_dict(
                {
                    "role": "assistant",
                    "content": [{"type": "toolCall", "id": "t1", "name": "read", "arguments": {"path": "a"}}],
                    "provider": "ark",
                }
            ),
            ToolResultMessage(tool_call_id="t1", tool_name="read", content=[TextContent(text="内容")]),
            ToolResultMessage(tool_call_id="t2", tool_name="read", content=[TextContent(text="内容2")]),
        ]
    )
    payload = build_payload(ANTHROPIC_MODEL, context, SimpleStreamOptions())
    roles = [m["role"] for m in payload["messages"]]
    check("anthropic: 消息角色序", roles == ["user", "assistant", "user"], str(roles))
    check("anthropic: 相邻工具结果合并", len(payload["messages"][2]["content"]) == 2)
    check("anthropic: tool_use 块", payload["messages"][1]["content"][0]["type"] == "tool_use")

    tools = [Tool(name="read", description="r", parameters={"type": "object", "properties": {}})]
    with_tools = Context(messages=[UserMessage(content="q")], tools=tools)
    payload = build_payload(ANTHROPIC_MODEL, with_tools, SimpleStreamOptions(tool_choice="none"))
    check("anthropic: tool_choice none", payload.get("tool_choice") == {"type": "none"}, str(payload.get("tool_choice")))
    payload = build_payload(ANTHROPIC_MODEL, with_tools, SimpleStreamOptions(tool_choice="required"))
    check("anthropic: tool_choice required → any", payload.get("tool_choice") == {"type": "any"})
    payload = build_payload(ANTHROPIC_MODEL, with_tools, SimpleStreamOptions())
    check("anthropic: 默认不传 tool_choice", "tool_choice" not in payload)
    payload = build_payload(ANTHROPIC_MODEL, Context(messages=[UserMessage(content="q")]),
                            SimpleStreamOptions(tool_choice="none"))
    check("anthropic: 无工具时不传 tool_choice", "tool_choice" not in payload)


# ── openai 协议 ─────────────────────────────────────────────────────


async def case_openai_text_tools_reasoning() -> None:
    def chunk(delta, finish=None):
        return {"choices": [{"delta": delta, "finish_reason": finish}]}

    transport = FakeTransport(
        [
            chunk({"reasoning_content": "先想想"}),
            chunk({"content": "答案是"}),
            chunk({"content": "42"}),
            chunk({"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "get_weather", "arguments": '{"ci'}}]}),
            chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'ty": "杭州"}'}}]}),
            chunk({}, "tool_calls"),
            {"choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 7, "prompt_tokens_details": {"cached_tokens": 5}}},
        ]
    )
    provider = OpenAIProvider(provider_id="ark_oai", base_url="https://example.invalid/api/v3", transport=transport)
    context = Context(system_prompt="sys", messages=[UserMessage(content="hi")])
    options = SimpleStreamOptions(extra={"thinking": {"type": "enabled"}})
    msg, kinds = await drain(provider, OPENAI_MODEL, context, options)

    check("openai: 文本", msg.text() == "答案是42", msg.text())
    check("openai: reasoning_content 映射", any(getattr(c, "thinking", None) == "先想想" for c in msg.content))
    calls = msg.tool_calls()
    check("openai: 跨块参数拼装", calls and calls[0].arguments == {"city": "杭州"}, str(calls))
    check("openai: 停止原因", msg.stop_reason == "toolUse", msg.stop_reason)
    check("openai: 用量", msg.usage.input == 30 and msg.usage.cache_read == 5)
    check("openai: 流末补 toolcall_end", "toolcall_end" in kinds, str(kinds))
    check("openai: system 进 messages", transport.payload["messages"][0]["role"] == "system")
    check("openai: 方舟私有参数透传", transport.payload.get("thinking") == {"type": "enabled"}, str(transport.payload.get("thinking")))
    check("openai: URL 拼接", transport.url.endswith("/api/v3/chat/completions"), transport.url)
    check("openai: Bearer 头", transport.headers.get("authorization") == "Bearer k")


def test_openai_payload_shapes() -> None:
    from pi_ai.providers.openai import build_payload

    context = Context(
        messages=[
            UserMessage(content="q"),
            message_from_dict({"role": "assistant", "content": [{"type": "toolCall", "id": "t1", "name": "read", "arguments": {"path": "a"}}]}),
            ToolResultMessage(tool_call_id="t1", tool_name="read", content=[TextContent(text="内容")]),
        ]
    )
    payload = build_payload(OPENAI_MODEL, context, SimpleStreamOptions())
    roles = [m["role"] for m in payload["messages"]]
    check("openai: 消息角色序", roles == ["user", "assistant", "tool"], str(roles))
    check("openai: tool_calls arguments 是字符串", isinstance(payload["messages"][1]["tool_calls"][0]["function"]["arguments"], str))
    check("openai: tool_call_id 回填", payload["messages"][2]["tool_call_id"] == "t1")

    tools = [Tool(name="read", description="r", parameters={"type": "object", "properties": {}})]
    with_tools = Context(messages=[UserMessage(content="q")], tools=tools)
    payload = build_payload(OPENAI_MODEL, with_tools, SimpleStreamOptions(tool_choice="none"))
    check("openai: tool_choice none", payload.get("tool_choice") == "none", str(payload.get("tool_choice")))
    payload = build_payload(OPENAI_MODEL, Context(messages=[UserMessage(content="q")]),
                            SimpleStreamOptions(tool_choice="none"))
    check("openai: 无工具时不传 tool_choice", "tool_choice" not in payload)


def test_prompt_cache_breakpoints() -> None:
    import dataclasses

    from pi_ai.providers.anthropic import build_payload

    official = dataclasses.replace(ANTHROPIC_MODEL, provider="anthropic", base_url=None, reasoning=False)
    tools = [Tool(name=n, description=n, parameters={"type": "object", "properties": {}})
             for n in ("read", "bash")]
    context = Context(
        system_prompt="你是编码助手",
        tools=tools,
        messages=[
            UserMessage(content="第一问"),
            message_from_dict({"role": "assistant", "provider": "anthropic", "content": [
                {"type": "toolCall", "id": "t1", "name": "read", "arguments": {"path": "a"}}]}),
            ToolResultMessage(tool_call_id="t1", tool_name="read", content=[TextContent(text="内容")]),
            message_from_dict({"role": "assistant", "content": [{"type": "text", "text": "读完了"}]}),
            UserMessage(content="第二问"),
        ],
    )

    def marks(payload):
        found = []
        if isinstance(payload.get("system"), list):
            found += ["system" for b in payload["system"] if "cache_control" in b]
        found += [f"tool:{t['name']}" for t in payload.get("tools", []) if "cache_control" in t]
        for i, m in enumerate(payload["messages"]):
            found += [f"msg{i}:{b['type']}" for b in m["content"] if "cache_control" in b]
        return found

    payload = build_payload(official, context, SimpleStreamOptions())
    got = marks(payload)
    check("缓存: 官方 Anthropic 自动开启",
          got == ["system", "tool:bash", "msg2:tool_result", "msg4:text"], str(got))
    check("缓存: 断点不超过 4 个", len(got) <= 4)
    check("缓存: system 改成带断点的块", payload["system"][0]["text"] == "你是编码助手")

    third_party = build_payload(ANTHROPIC_MODEL, context, SimpleStreamOptions())
    check("缓存: 第三方兼容端点默认不加", marks(third_party) == [] and third_party["system"] == "你是编码助手",
          str(marks(third_party)))
    opted_in = build_payload(dataclasses.replace(ANTHROPIC_MODEL, prompt_cache=True), context,
                             SimpleStreamOptions())
    check("缓存: 第三方端点可显式开启", len(marks(opted_in)) == 4, str(marks(opted_in)))
    opted_out = build_payload(dataclasses.replace(official, prompt_cache=False), context,
                              SimpleStreamOptions())
    check("缓存: 官方也可显式关闭", marks(opted_out) == [])

    bare = build_payload(official, Context(messages=[UserMessage(content="hi")]), SimpleStreamOptions())
    check("缓存: 没有 system / 工具时只标消息", marks(bare) == ["msg0:text"], str(marks(bare)))


# ── 错误路径与工具函数 ──────────────────────────────────────────────


async def case_error_paths() -> None:
    provider = AnthropicProvider(transport=FakeTransport([]))
    context = Context(messages=[UserMessage(content="hi")])
    msg, kinds = await drain(provider, ANTHROPIC_MODEL, context, api_key=None)
    check("错误: 缺 key 走 ErrorEvent", msg.stop_reason == "error" and "error" in kinds)

    transport = FakeTransport([{"type": "error", "error": {"message": "rate limited"}}])
    msg, _ = await drain(AnthropicProvider(transport=transport), ANTHROPIC_MODEL, context)
    check("错误: 服务端 error 事件", msg.stop_reason == "error" and "rate limited" in (msg.error_message or ""))

async def case_network_error() -> None:
    class BrokenTransport:
        async def stream_sse(self, url, headers, payload, timeout=600.0):
            raise LLMError("连不上 https://x/v1/messages：检查网络")
            yield  # 让它是个 async generator

    provider = AnthropicProvider(transport=BrokenTransport())
    msg, kinds = await drain(provider, ANTHROPIC_MODEL,
                             Context(messages=[UserMessage(content="hi")]))
    check("错误: 网络异常转成 ErrorEvent", msg.stop_reason == "error" and "error" in kinds)
    check("错误: 消息里有可读原因", "连不上" in (msg.error_message or ""))

def test_json_parse() -> None:
    check("截断 JSON: 完整", parse_streaming_json('{"a": 1}') == {"a": 1})
    check("截断 JSON: 半个值", parse_streaming_json('{"a": "杭') == {"a": "杭"})
    check("截断 JSON: 嵌套未闭合", parse_streaming_json('{"a": {"b": [1,2') == {"a": {"b": [1, 2]}})
    check("截断 JSON: 空串", parse_streaming_json("") == {})


def test_serde_and_cost() -> None:
    original = message_from_dict(
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "hi"}, {"type": "toolCall", "id": "1", "name": "read", "arguments": {"p": 1}}],
            "usage": {"input": 5, "output": 6, "cost": {"total": 0.1}},
            "stopReason": "toolUse",
        }
    )
    again = message_from_dict(json.loads(json.dumps(message_to_dict(original))))
    check("serde: 往返", again.tool_calls()[0].arguments == {"p": 1} and again.usage.input == 5)
    cost = calculate_cost(ANTHROPIC_MODEL, Usage(input=1_000_000, output=1_000_000))
    check("cost: 计算", abs(cost.total - 3.0) < 1e-9, str(cost.total))


def test_error_classification() -> None:
    from pi_ai import AssistantMessage, describe_llm_error, is_context_overflow

    anthropic_body = json.dumps({"type": "error", "error": {
        "type": "invalid_request_error", "message": "prompt is too long: 210000 tokens > 200000 maximum"}})
    text = describe_llm_error(LLMError("HTTP 400 from x", 400, anthropic_body))
    check("错误描述: 取出响应体里的原因", text.startswith("HTTP 400: prompt is too long") and
          "invalid_request_error" in text, text)
    openai_body = json.dumps({"error": {"message": "This model's maximum context length is 65536 tokens",
                                        "code": "context_length_exceeded"}})
    text = describe_llm_error(LLMError("HTTP 400 from x", 400, openai_body))
    check("错误描述: 带上错误码", "(context_length_exceeded)" in text, text)
    check("错误描述: 非 JSON 响应体", describe_llm_error(LLMError("x", 502, "<html>Bad   Gateway</html>"))
          == "HTTP 502: <html>Bad Gateway</html>")
    check("错误描述: 网络错误原样", describe_llm_error(LLMError("连不上 x")) == "连不上 x")

    overflow = [
        "HTTP 400: prompt is too long: 210000 tokens > 200000 maximum",
        "HTTP 400: This model's maximum context length is 65536 tokens (context_length_exceeded)",
        "HTTP 400: input length and `max_tokens` exceed context limit: 190000 + 16384 > 200000",
        "HTTP 413: request entity too large",
        "HTTP 400: Input is too long for requested model.",
        "HTTP 400: Prompt 超长，请减少输入 (1261)",
        "HTTP 400: 输入的 token 数超出模型最大上下文长度",
        "The input exceeds the context window of this model",
    ]
    for text in overflow:
        check(f"超长识别: {text[:40]}", is_context_overflow(text), text)
    not_overflow = [
        "HTTP 429: Rate limit reached: too many tokens per minute",
        "HTTP 401: invalid x-api-key",
        "连不上 https://api.example.com",
        "HTTP 500: internal error",
        "",
    ]
    for text in not_overflow:
        check(f"超长误判: {text[:40] or '(空)'}", not is_context_overflow(text), text)

    message = AssistantMessage(stop_reason="error", error_message=overflow[0])
    check("超长识别: 接受消息对象", is_context_overflow(message))
    message.stop_reason = "stop"
    check("超长识别: 非出错消息不算", not is_context_overflow(message))


async def case_http_error_body() -> None:
    class RejectingTransport:
        async def stream_sse(self, url, headers, payload, timeout=600.0):
            raise LLMError("HTTP 400 from x", 400, json.dumps(
                {"error": {"type": "invalid_request_error", "message": "prompt is too long"}}))
            yield  # pragma: no cover

    provider = AnthropicProvider(transport=RejectingTransport())
    context = Context(messages=[UserMessage(content="hi")])
    events = [e async for e in provider.stream(ANTHROPIC_MODEL, context, SimpleStreamOptions(), api_key="k")]
    msg = events[-1].message
    check("错误描述: provider 把响应体写进 error_message",
          msg.stop_reason == "error" and "prompt is too long" in (msg.error_message or ""), str(msg.error_message))


def test_http_error_body() -> None:
    asyncio.run(case_http_error_body())


# ── pytest 兼容层 ───────────────────────────────────────────────────
# pytest 不装插件跑不了 async def，这里套一层同步壳；
# 直接 python tests/test_offline.py 时走下面的 main()，不受影响。


def test_anthropic_text() -> None:
    asyncio.run(case_anthropic_text())


def test_anthropic_tools_and_thinking() -> None:
    asyncio.run(case_anthropic_tools_and_thinking())


def test_openai_text_tools_reasoning() -> None:
    asyncio.run(case_openai_text_tools_reasoning())


def test_error_paths() -> None:
    asyncio.run(case_error_paths())


async def main() -> int:
    print("anthropic-messages")
    await case_anthropic_text()
    await case_anthropic_tools_and_thinking()
    test_anthropic_payload_shapes()
    test_prompt_cache_breakpoints()
    print("openai-completions")
    await case_openai_text_tools_reasoning()
    test_openai_payload_shapes()
    print("其他")
    await case_error_paths()
    test_json_parse()
    test_serde_and_cost()
    test_error_classification()
    await case_http_error_body()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} 处失败：")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("全部通过，可以联网测了")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
