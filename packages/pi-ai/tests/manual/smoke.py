"""pi-ai 联网冒烟测试。

    python tests/manual/smoke.py                 # 列出 .env 里配好的模型
    python tests/manual/smoke.py -m ark_cc       # 跑全部检查
    python tests/manual/smoke.py -m ark_cc --only tools thinking
    python tests/manual/smoke.py --all           # 每个模型都跑一遍
    python tests/manual/smoke.py -m ark_oai --think high --raw

六项检查，从易到难，前面挂了后面就没必要跑：
  1 text      连通、鉴权、SSE 解析、text_delta 顺序
  2 usage     用量与成本回填
  3 tools     工具 schema 下发、toolCall 解析、参数 JSON 拼装
  4 roundtrip 把 toolResult 塞回去再问一轮，验证 build_payload 的消息转换
  5 thinking  思考参数是否被服务端接受、thinking_delta 是否回流
  6 errors    错 key 时走 ErrorEvent 而不是抛异常
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from env_models import build_models, default_alias, load_env  # noqa: E402

from pi_ai import (  # noqa: E402
    Context,
    DoneEvent,
    ErrorEvent,
    SimpleStreamOptions,
    TextContent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    Tool,
    ToolCall,
    ToolCallEndEvent,
    ToolResultMessage,
    UserMessage,
)

G, R, Y, D, X = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
PASS, FAIL = 0, 0

WEATHER_TOOL = Tool(
    name="get_weather",
    description="查询某个城市当前的天气。需要查天气时必须调用本工具，不要凭空回答。",
    parameters={
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "城市名，例如 杭州"},
            "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
        },
        "required": ["city"],
    },
)


def ok(name: str, detail: str = "") -> None:
    global PASS
    PASS += 1
    print(f"  {G}✓{X} {name} {D}{detail}{X}")


def bad(name: str, detail: str = "") -> None:
    global FAIL
    FAIL += 1
    print(f"  {R}✗{X} {name} {R}{detail}{X}")


async def collect(models, spec, context, level=None, raw=False):
    """跑一次流，返回 (最终消息, 事件类型序列, 耗时)。"""
    level = level or spec.thinking_level
    options = SimpleStreamOptions(
        reasoning=level,
        max_tokens=spec.model.max_tokens,
        extra=spec.stream_extra(level),
    )
    kinds: list[str] = []
    final = None
    started = time.time()
    first_token = None
    async for event in models.stream_simple(spec.model, context, options):
        kinds.append(event.type)
        if first_token is None and event.type in ("text_delta", "thinking_delta"):
            first_token = time.time() - started
        if raw and isinstance(event, TextDeltaEvent):
            sys.stdout.write(event.delta)
            sys.stdout.flush()
        if raw and isinstance(event, ThinkingDeltaEvent):
            sys.stdout.write(f"{D}{event.delta}{X}")
            sys.stdout.flush()
        if isinstance(event, (DoneEvent, ErrorEvent)):
            final = event.message
    if raw:
        print()
    return final, kinds, time.time() - started, first_token


# ── 检查项 ──────────────────────────────────────────────────────────


async def check_text(models, spec, args) -> bool:
    context = Context(
        system_prompt="你是一个简洁的助手，回答不超过一句话。",
        messages=[UserMessage(content="用一句话说明什么是快速排序。")],
    )
    msg, kinds, elapsed, ttft = await collect(models, spec, context, "off", args.raw)
    if msg is None:
        return bad("text  流没有终止事件") or False
    if msg.stop_reason == "error":
        bad("text  服务端报错", msg.error_message or "")
        return False
    if not msg.text().strip():
        bad("text  回复为空", str(kinds[:8]))
        return False
    ok("text ", f"{len(msg.text())} 字 · 首字 {ttft:.2f}s · 总 {elapsed:.2f}s")
    if "text_delta" in kinds:
        ok("text  增量事件", f"{kinds.count('text_delta')} 个 delta")
    else:
        bad("text  没有 text_delta", "服务端可能没真流式，或 SSE 解析漏了")
    return True


async def check_usage(models, spec, args) -> bool:
    context = Context(messages=[UserMessage(content="只回复两个字：收到")])
    msg, _, _, _ = await collect(models, spec, context, "off")
    if msg is None or msg.stop_reason == "error":
        return bad("usage 前置调用失败") or False
    u = msg.usage
    if u.input > 0 and u.output > 0:
        cost = f" · ¥{u.cost.total:.6f}" if u.cost.total else ""
        ok("usage", f"in={u.input} out={u.output} cache={u.cache_read}{cost}")
    else:
        bad("usage 用量为 0", f"in={u.input} out={u.output}，检查 usage 字段名")
    if msg.stop_reason in ("stop", "length"):
        ok("usage stop_reason", msg.stop_reason)
    else:
        bad("usage stop_reason 异常", msg.stop_reason)
    return True


async def check_tools(models, spec, args):
    context = Context(
        system_prompt="你可以调用工具。",
        messages=[UserMessage(content="杭州现在天气怎么样？用摄氏度。")],
        tools=[WEATHER_TOOL],
    )
    msg, kinds, _, _ = await collect(models, spec, context, "off")
    if msg is None or msg.stop_reason == "error":
        bad("tools 调用失败", (msg.error_message if msg else "") or "")
        return None
    calls = msg.tool_calls()
    if not calls:
        bad("tools 没有产生工具调用", f"回复={msg.text()[:60]!r}")
        return None
    call = calls[0]
    ok("tools", f"{call.name} stop={msg.stop_reason}")
    if isinstance(call.arguments, dict) and call.arguments.get("city"):
        ok("tools 参数解析", json.dumps(call.arguments, ensure_ascii=False))
    else:
        bad("tools 参数没解析出来", repr(call.arguments)[:80])
    if "toolcall_end" in kinds:
        ok("tools toolcall_end 已发出")
    else:
        bad("tools 缺少 toolcall_end", "流结束时补发的逻辑有问题")
    return msg


async def check_roundtrip(models, spec, args, assistant_msg) -> bool:
    """把工具结果回填，验证 build_payload 的三种消息都能转对。"""
    if assistant_msg is None:
        print(f"  {Y}-{X} roundtrip 跳过（依赖 tools）")
        return False
    call: ToolCall = assistant_msg.tool_calls()[0]
    context = Context(
        system_prompt="你可以调用工具。",
        messages=[
            UserMessage(content="杭州现在天气怎么样？用摄氏度。"),
            assistant_msg,
            ToolResultMessage(
                tool_call_id=call.id,
                tool_name=call.name,
                content=[TextContent(text='{"temp": 18, "condition": "多云"}')],
            ),
        ],
        tools=[WEATHER_TOOL],
    )
    msg, _, _, _ = await collect(models, spec, context, "off", args.raw)
    if msg is None or msg.stop_reason == "error":
        bad("roundtrip 失败", (msg.error_message if msg else "") or "")
        return False
    text = msg.text()
    if "18" in text or "多云" in text:
        ok("roundtrip", f"模型用上了工具结果：{text[:40]!r}")
    else:
        bad("roundtrip 结果没被用上", text[:60])
    return True


async def check_thinking(models, spec, args) -> bool:
    level = args.think or spec.thinking_level
    if spec.thinking_style == "none" or level == "off":
        print(f"  {Y}-{X} thinking 跳过（THINKING_STYLE=none 或档位为 off）")
        return False
    context = Context(
        messages=[UserMessage(content="一个笼子里有鸡和兔共 20 只、脚 56 只，各几只？")]
    )
    body = spec.stream_extra(level)
    print(f"  {D}思考参数：{json.dumps(body, ensure_ascii=False) or '(由 provider 生成)'}{X}")
    msg, kinds, elapsed, _ = await collect(models, spec, context, level, args.raw)
    if msg is None or msg.stop_reason == "error":
        bad("thinking 服务端拒绝", (msg.error_message if msg else "")[:160])
        return False
    n = kinds.count("thinking_delta")
    if n:
        ok("thinking", f"{n} 个 thinking_delta · {elapsed:.1f}s · 档位 {level}")
    else:
        bad(
            "thinking 没有思维链回流",
            "参数被接受但没返回 reasoning_content——换 THINKING_STYLE 或换模型",
        )
    if "12" in msg.text() or "8" in msg.text():
        ok("thinking 答案合理")
    return True


async def check_errors(models, spec, args) -> bool:
    context = Context(messages=[UserMessage(content="hi")])
    options = SimpleStreamOptions(api_key="sk-definitely-invalid-key", max_tokens=16)
    try:
        final = None
        async for event in models.stream_simple(spec.model, context, options):
            if isinstance(event, (DoneEvent, ErrorEvent)):
                final = event.message
    except Exception as exc:
        bad("errors 抛异常了", f"{type(exc).__name__}: {exc}")
        return False
    if final is not None and final.stop_reason == "error":
        ok("errors", f"优雅降级：{(final.error_message or '')[:60]}")
    else:
        bad("errors 错 key 居然成功了", "确认 API_KEY 真的参与了鉴权")
    return True


# ── 编排 ────────────────────────────────────────────────────────────

ALL_CHECKS = ("text", "usage", "tools", "roundtrip", "thinking", "errors")


async def run_model(models, spec, args) -> None:
    print(f"\n{Y}▶ {spec.alias}{X}  {spec.model.api}  {spec.model.id}")
    print(f"  {D}{spec.model.base_url}{X}")
    only = args.only or ALL_CHECKS
    assistant = None
    if "text" in only:
        if not await check_text(models, spec, args):
            print(f"  {R}连通性都没过，后面不用跑了{X}")
            return
    if "usage" in only:
        await check_usage(models, spec, args)
    if "tools" in only or "roundtrip" in only:
        assistant = await check_tools(models, spec, args)
    if "roundtrip" in only:
        await check_roundtrip(models, spec, args, assistant)
    if "thinking" in only:
        await check_thinking(models, spec, args)
    if "errors" in only:
        await check_errors(models, spec, args)


async def main() -> int:
    parser = argparse.ArgumentParser(description="pi-ai 联网冒烟测试")
    parser.add_argument("-m", "--model", help=".env 里的别名")
    parser.add_argument("--all", action="store_true", help="跑遍所有配置的模型")
    parser.add_argument("--only", nargs="+", choices=ALL_CHECKS, help="只跑指定检查")
    parser.add_argument("--think", help="覆盖思考档位")
    parser.add_argument("--raw", action="store_true", help="把模型输出打到屏幕")
    parser.add_argument("--env", help="指定 .env 路径")
    args = parser.parse_args()

    env = load_env(args.env)
    try:
        models, specs = build_models(env)
    except (RuntimeError, ValueError) as exc:
        print(f"{R}配置有问题：{exc}{X}")
        return 2

    if not args.model and not args.all:
        print(f"{Y}已配置的模型：{X}")
        for spec in specs.values():
            flag = f"{G}有 key{X}" if spec.api_key else f"{R}缺 key{X}"
            print(f"  {spec.describe()}  [{flag}]")
        print(f"\n默认：{default_alias(env, specs)}")
        print(f"{D}加 -m <别名> 开跑，或 --all 全跑{X}")
        return 0

    targets = list(specs.values()) if args.all else [specs[args.model]]
    for spec in targets:
        if not spec.api_key:
            print(f"{R}[{spec.alias}] 没配 API_KEY，跳过{X}")
            continue
        await run_model(models, spec, args)

    print(f"\n{G}{PASS} 通过{X}  {R if FAIL else D}{FAIL} 失败{X}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
