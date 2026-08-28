"""pi-ai 交互式对话壳。

pi-ai 本身是库，不带 CLI。这个脚本就是最小的"应用层"：读 .env、组 Context、
把事件流打到屏幕上。等你写完 pi-agent，多轮和工具执行会被那一层接管，
这里只剩渲染。

    python tests/manual/chat.py                      # 用 PI_DEFAULT_MODEL
    python tests/manual/chat.py -m ark_doubao        # 指定模型
    python tests/manual/chat.py -m ark_cc --think high
    python tests/manual/chat.py --tools              # 开工具演示
    python tests/manual/chat.py -q "一句话说明快排"    # 单轮，问完就退

对话中可用命令：
    /model [别名]   查看或切换模型
    /think <档位>   off|minimal|low|medium|high|xhigh|max
    /system <文本>  改系统提示（会清空历史）
    /usage          本次会话累计用量与成本
    /history        当前上下文里有哪些消息
    /raw            打印最后一条 assistant 消息的 JSON
    /save <文件>    把对话存成 json
    /clear          清空历史
    /exit           退出
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

from env_models import LEVELS, build_models, default_alias, load_env  # noqa: E402

from pi_ai import (  # noqa: E402
    Context,
    DoneEvent,
    ErrorEvent,
    SimpleStreamOptions,
    TextContent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingStartEvent,
    Tool,
    ToolCallStartEvent,
    ToolResultMessage,
    Usage,
    UserMessage,
    message_to_dict,
)

B, D, G, Y, R, C, X = (
    "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[36m", "\033[0m",
)

DEMO_TOOL = Tool(
    name="get_weather",
    description="查询某个城市当前的天气。需要查天气时必须调用本工具。",
    parameters={
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "城市名"},
            "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
        },
        "required": ["city"],
    },
)


async def render_stream(models, spec, context, level, on_first_token=None):
    """把事件流打到屏幕，返回最终的 AssistantMessage。

    这几十行就是 pi-ai 消费方要写的全部代码——事件循环 + 状态标记。
    """
    options = SimpleStreamOptions(
        reasoning=level, max_tokens=spec.model.max_tokens, extra=spec.stream_extra(level)
    )
    thinking_open = False
    started = time.time()
    first = None
    final = None

    async for event in models.stream_simple(spec.model, context, options):
        if isinstance(event, ThinkingStartEvent):
            thinking_open = True
            sys.stdout.write(f"{D}[思考] ")
        elif isinstance(event, ThinkingDeltaEvent):
            sys.stdout.write(event.delta)
        elif isinstance(event, ThinkingEndEvent):
            thinking_open = False
            sys.stdout.write(f"{X}\n")
        elif isinstance(event, TextDeltaEvent):
            if thinking_open:
                thinking_open = False
                sys.stdout.write(f"{X}\n")
            sys.stdout.write(event.delta)
        elif isinstance(event, ToolCallStartEvent):
            sys.stdout.write(f"\n{C}[工具] {event.tool_name}{X} ")
        elif isinstance(event, (DoneEvent, ErrorEvent)):
            final = event.message

        if first is None and event.type in ("text_delta", "thinking_delta"):
            first = time.time() - started
            if on_first_token:
                on_first_token(first)
        sys.stdout.flush()

    if thinking_open:
        sys.stdout.write(X)
    print()
    return final, time.time() - started, first


def print_stats(msg, elapsed, first, total: Usage) -> None:
    u = msg.usage
    bits = [f"in {u.input}", f"out {u.output}"]
    if u.cache_read:
        bits.append(f"cache {u.cache_read}")
    bits.append(f"{elapsed:.1f}s")
    if first:
        bits.append(f"首字 {first:.2f}s")
    if u.cost.total:
        bits.append(f"¥{u.cost.total:.5f}（累计 ¥{total.cost.total:.4f}）")
    print(f"{D}  {' · '.join(bits)} · stop={msg.stop_reason}{X}")


async def one_shot(models, spec, level, system, question) -> int:
    context = Context(system_prompt=system, messages=[UserMessage(content=question)])
    msg, elapsed, first = await render_stream(models, spec, context, level)
    if msg is None or msg.stop_reason == "error":
        print(f"{R}失败：{(msg.error_message if msg else '流没有终止事件')}{X}")
        return 1
    print_stats(msg, elapsed, first, msg.usage)
    return 0


async def repl(models, specs, alias, level, system, use_tools) -> int:
    spec = specs[alias]
    context = Context(system_prompt=system, messages=[], tools=[DEMO_TOOL] if use_tools else [])
    total = Usage()

    print(f"{B}pi-ai chat{X}  {D}{spec.model.api}{X}")
    print(f"  模型 {G}{alias}{X} → {spec.model.id}")
    print(f"  思考 {level}  ·  工具 {'开（演示）' if use_tools else '关'}")
    print(f"{D}  /help 看命令，/exit 退出{X}\n")

    loop = asyncio.get_running_loop()
    while True:
        try:
            line = (await loop.run_in_executor(None, lambda: input(f"{B}你 >{X} "))).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue

        # ── 命令 ────────────────────────────────────────────────
        if line.startswith("/"):
            cmd, _, rest = line[1:].partition(" ")
            rest = rest.strip()
            if cmd in ("exit", "quit", "q"):
                return 0
            if cmd == "help":
                print(__doc__.split("对话中可用命令：")[1])
            elif cmd == "model":
                if rest and rest in specs:
                    alias, spec = rest, specs[rest]
                    print(f"{G}切到 {alias} → {spec.model.id}{X}")
                elif rest:
                    print(f"{R}没有这个别名：{rest}{X}")
                else:
                    for a, s in specs.items():
                        mark = "*" if a == alias else " "
                        print(f" {mark} {a:<12} {s.model.api:<20} {s.model.id}")
            elif cmd == "think":
                if rest in LEVELS:
                    level = rest
                    print(f"{G}思考档位 → {level}{X}  {D}{json.dumps(spec.stream_extra(level), ensure_ascii=False) or '(由 provider 生成)'}{X}")
                else:
                    print(f"{R}档位只能是 {LEVELS}{X}")
            elif cmd == "system":
                context.system_prompt = rest or None
                context.messages.clear()
                print(f"{G}系统提示已更新，历史已清空{X}")
            elif cmd == "usage":
                print(
                    f"  in {total.input} · out {total.output} · cache {total.cache_read} "
                    f"· 累计 ¥{total.cost.total:.4f} · {len(context.messages)} 条消息"
                )
            elif cmd == "history":
                for i, m in enumerate(context.messages):
                    text = m.text().replace("\n", " ")[:60]
                    print(f"  {i:>2} {m.role:<11} {text}")
            elif cmd == "raw":
                last = next(
                    (m for m in reversed(context.messages) if m.role == "assistant"), None
                )
                print(json.dumps(message_to_dict(last), ensure_ascii=False, indent=2) if last else "(无)")
            elif cmd == "save":
                path = Path(rest or "chat.json")
                path.write_text(
                    json.dumps(
                        {
                            "system": context.system_prompt,
                            "messages": [message_to_dict(m) for m in context.messages],
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    "utf-8",
                )
                print(f"{G}已存到 {path}{X}")
            elif cmd == "clear":
                context.messages.clear()
                print(f"{G}历史已清空{X}")
            else:
                print(f"{R}未知命令：/{cmd}{X}")
            continue

        # ── 正常一轮 ────────────────────────────────────────────
        context.messages.append(UserMessage(content=line))
        try:
            msg, elapsed, first = await render_stream(models, spec, context, level)
        except KeyboardInterrupt:
            print(f"\n{Y}已中断（pi-ai 没有 abort 机制，这是 pi-agent 的职责）{X}")
            context.messages.pop()
            continue

        if msg is None:
            print(f"{R}流没有终止事件{X}")
            context.messages.pop()
            continue
        if msg.stop_reason == "error":
            print(f"{R}出错：{msg.error_message}{X}")
            context.messages.pop()  # 失败的一轮不进上下文，免得污染后续
            continue

        context.messages.append(msg)
        total = total + msg.usage
        print_stats(msg, elapsed, first, total)

        # 工具调用：pi-ai 只负责解析出来，执行是上一层的事
        for call in msg.tool_calls():
            print(f"{C}  模型请求调用 {call.name}({json.dumps(call.arguments, ensure_ascii=False)}){X}")
            print(f"{D}  pi-ai 不执行工具——这正是 pi-agent 存在的理由。{X}")
            result = (
                await loop.run_in_executor(
                    None, lambda: input(f"{D}  手动填结果（回车跳过）> {X}")
                )
            ).strip()
            if result:
                context.messages.append(
                    ToolResultMessage(
                        tool_call_id=call.id,
                        tool_name=call.name,
                        content=[TextContent(text=result)],
                    )
                )
                msg2, e2, f2 = await render_stream(models, spec, context, level)
                if msg2 and msg2.stop_reason != "error":
                    context.messages.append(msg2)
                    total = total + msg2.usage
                    print_stats(msg2, e2, f2, total)
            else:
                context.messages.pop()  # 没有工具结果，这条 assistant 留着会让下一轮报错


async def main() -> int:
    parser = argparse.ArgumentParser(description="pi-ai 交互式对话")
    parser.add_argument("-m", "--model", help=".env 里的别名")
    parser.add_argument("-t", "--think", help="思考档位，默认取 .env 里的配置")
    parser.add_argument("-s", "--system", default="你是一个简洁、准确的助手。")
    parser.add_argument("--tools", action="store_true", help="挂上演示工具")
    parser.add_argument("-q", "--question", help="单轮提问，问完退出")
    parser.add_argument("--env", help="指定 .env 路径")
    args = parser.parse_args()

    env = load_env(args.env)
    try:
        models, specs = build_models(env)
    except (RuntimeError, ValueError) as exc:
        print(f"{R}配置有问题：{exc}{X}")
        return 2

    alias = args.model or default_alias(env, specs)
    if alias not in specs:
        print(f"{R}没有别名 {alias}，可选：{list(specs)}{X}")
        return 2
    spec = specs[alias]
    if not spec.api_key:
        print(f"{R}[{alias}] 没配 API_KEY{X}")
        return 2
    level = args.think or spec.thinking_level

    if args.question:
        return await one_shot(models, spec, level, args.system, args.question)
    return await repl(models, specs, alias, level, args.system, args.tools)


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)