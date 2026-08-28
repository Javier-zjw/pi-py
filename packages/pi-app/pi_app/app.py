"""把 AgentSession 和 SessionRenderer 接起来的最小应用。

这是整个项目唯一一个"什么都认识"的地方，所以它必须薄——业务逻辑一行都
不该出现在这里，只有装配和输入循环。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from pi_coding_agent import SessionManager, create_agent_session, create_agent_session_services
from pi_coding_agent.text import sanitize
from pi_tui import LineReader, Theme, missing_dependency_hint, terminal_width

from .renderer import SessionRenderer

BUILTIN_COMMANDS = {"help", "model", "think", "tools", "compact", "usage", "session", "exit", "quit"}

HELP = """
  /help              显示本帮助
  /model             列出可用模型
  /model <spec>      切换模型，如 /model ark-planing/glm-5.2
  /think <level>     思考档位：off minimal low medium high xhigh max
  /tools             列出当前启用的工具
  /compact [说明]    立即压缩上下文，可附加压缩重点
  /usage             本次会话的 token 与费用
  /session           当前会话文件路径
  /exit              退出
"""


async def run(session, renderer: SessionRenderer) -> int:
    renderer.status.model = session.model.key if session.model else "none"
    renderer.status.thinking = session.thinking_level
    renderer.status.hint = "/help"
    renderer.header(session.cwd, renderer.status.model, session.thinking_level)
    session.subscribe(renderer.on_event)

    reader = LineReader(history_file=Path.home() / ".pi" / "agent" / "history")
    hint = missing_dependency_hint(renderer.theme)
    if hint:
        print(hint)
    turns = 0
    while True:
        try:
            raw = await reader.read("\033[1m> \033[0m")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        line = sanitize(raw).strip()
        if not line:
            continue

        if line.startswith("/") and line[1:].partition(" ")[0] in BUILTIN_COMMANDS:
            cmd, _, rest = line[1:].partition(" ")
            if cmd in ("exit", "quit"):
                break
            if cmd == "help":
                print(renderer.theme.muted(HELP))
            elif cmd == "model":
                if rest:
                    model = session.model_runtime.resolve(rest.strip())
                    if model:
                        session.set_model(model)
                        renderer.status.model = model.key
                    else:
                        print(renderer.theme.error(f"未知模型：{rest}"))
                else:
                    for m in session.model_runtime.available_models():
                        mark = "*" if session.model and m.key == session.model.key else " "
                        print(f" {mark} {m.key}")
            elif cmd == "think":
                session.set_thinking_level(rest.strip() or "off")
                renderer.status.thinking = session.thinking_level
            elif cmd == "tools":
                for tool in session.agent.state.tools:
                    print(f"  {tool.name:<10} {tool.description.splitlines()[0][:70]}")
            elif cmd == "compact":
                result = await session.compact(rest or None)
                if result:
                    print(renderer.theme.muted(f"  已压缩 {result.tokens_before} tokens"))
            elif cmd == "usage":
                u = session.usage()
                print(renderer.theme.muted(f"  ↑{u.input} ↓{u.output} ¥{u.cost.total:.4f}"))
            elif cmd == "session":
                print(renderer.theme.muted(f"  {session.session_file or '(内存)'}"))
            # if cmd != "help":
            #     print(renderer.status_line())
            if cmd in ("model", "think"):
                print(renderer.status_line())
            continue

        # 不再调 renderer.prompt_echo(line)：输入行已经把它显示过一遍了，
        # 再打印一次就是同一句话出现两遍
        renderer.state.own_prompt = line
        turns += 1
        try:
            await session.prompt(line)
        except KeyboardInterrupt:
            session.abort()
        except Exception as exc:
            print(renderer.theme.error(f"  ✗ {type(exc).__name__}: {exc}"))
        # print(renderer.status_line())

    model = session.model
    renderer.summary(
        model.key if model else "none",
        turns,
        session.usage().input,
        model.context_window if model else 0,
    )
    return 0


async def main_async(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pi-tui", description="pi 的终端界面")
    parser.add_argument("-C", "--cwd", default=".")
    parser.add_argument("-m", "--model")
    parser.add_argument("-t", "--think", default="off")
    parser.add_argument("--no-thinking", action="store_true", help="不显示思考内容")
    parser.add_argument("--no-diff", action="store_true", help="不显示 diff")
    parser.add_argument("--no-session", action="store_true")
    parser.add_argument("--agent-dir")
    args = parser.parse_args(argv)

    cwd = str(Path(args.cwd).expanduser().resolve())
    services = create_agent_session_services(cwd=cwd, agent_dir=args.agent_dir)
    manager = SessionManager.in_memory(cwd) if args.no_session else SessionManager.create(cwd, args.agent_dir)
    session = create_agent_session(
        services=services, session_manager=manager, model=args.model, thinking_level=args.think
    )
    if session.model is None:
        print("没有可用模型：设置 ANTHROPIC_API_KEY 或写 ~/.pi/agent/auth.json", file=sys.stderr)
        return 1

    renderer = SessionRenderer(
        theme=Theme(), show_thinking=not args.no_thinking, show_diff=not args.no_diff
    )
    try:
        result = await run(session, renderer)
    finally:
        renderer.stop()
        session.dispose()
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(main_async(argv))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())