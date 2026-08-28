"""
命令行：交互式REPL、打印输出模式、JSON事件流模式。
刻意使用朴素标准输出；本版本不实现TUI交互界面层。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from pi_agent import (
    AgentEndEvent,
    AgentEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
)
from pi_ai import TextDeltaEvent, ThinkingDeltaEvent

from .agent_session import AgentSession, create_agent_session, create_agent_session_services
from .session.manager import SessionManager
from .text import sanitize

DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"


def make_printer(show_thinking: bool = False):
    state = {"thinking": False}

    def on_event(event: AgentEvent) -> None:
        if isinstance(event, MessageUpdateEvent):
            inner = event.assistant_message_event
            if isinstance(inner, TextDeltaEvent):
                if state["thinking"]:
                    sys.stdout.write(RESET + "\n")
                    state["thinking"] = False
                sys.stdout.write(inner.delta)
                sys.stdout.flush()
            elif isinstance(inner, ThinkingDeltaEvent) and show_thinking:
                if not state["thinking"]:
                    sys.stdout.write(DIM)
                    state["thinking"] = True
                sys.stdout.write(inner.delta)
                sys.stdout.flush()
        elif isinstance(event, ToolExecutionStartEvent):
            args = json.dumps(event.arguments, ensure_ascii=False)
            if len(args) > 120:
                args = args[:117] + "..."
            sys.stdout.write(f"\n{CYAN}→ {event.tool_name}{RESET} {DIM}{args}{RESET}\n")
            sys.stdout.flush()
        elif isinstance(event, ToolExecutionEndEvent):
            mark = f"{RED}✗{RESET}" if event.is_error else f"{CYAN}✓{RESET}"
            preview = event.result.content[0].text if event.result.content else ""
            preview = (preview or "").split("\n")[0][:100]
            sys.stdout.write(f"{mark} {DIM}{preview}{RESET}\n")
            sys.stdout.flush()
        elif isinstance(event, AgentEndEvent):
            sys.stdout.write("\n")
            sys.stdout.flush()

    return on_event


def json_printer(event: AgentEvent) -> None:
    payload = {"type": event.type}
    if isinstance(event, MessageUpdateEvent):
        inner = event.assistant_message_event
        if isinstance(inner, TextDeltaEvent):
            payload = {"type": "text_delta", "delta": inner.delta}
        else:
            return
    elif isinstance(event, ToolExecutionStartEvent):
        payload = {"type": event.type, "tool": event.tool_name, "arguments": event.arguments}
    elif isinstance(event, ToolExecutionEndEvent):
        payload = {"type": event.type, "tool": event.tool_name, "isError": event.is_error}
    print(json.dumps(payload, ensure_ascii=False), flush=True)


HELP = """Commands:
  /help              show this help
  /model [spec]      show or switch model (provider/model-id)
  /think <level>     off|minimal|low|medium|high|xhigh|max
  /tools             list active tools
  /skills            list discovered skills
  /commands          list slash commands from prompts and extensions
  /compact [notes]   compact the context now
  /usage             token and cost totals
  /session           session file path
  /exit              quit
Anything else is sent to the agent."""


async def handle_command(session: AgentSession, line: str) -> bool:
    """Returns False when the REPL should exit."""
    name, _, rest = line[1:].partition(" ")
    rest = rest.strip()
    api = session.resources.get_extension_api()

    if name in ("exit", "quit"):
        return False
    if name == "help":
        print(HELP)
    elif name == "model":
        if rest:
            model = session.model_runtime.resolve(rest)
            if not model:
                print(f"{RED}unknown model: {rest}{RESET}")
            else:
                session.set_model(model)
                print(f"model → {model.provider}/{model.id}")
        else:
            for m in session.model_runtime.available_models():
                marker = "*" if session.model and m.key == session.model.key else " "
                print(f" {marker} {m.key}")
    elif name == "think":
        session.set_thinking_level(rest or "off")  # type: ignore[arg-type]
        print(f"thinking → {session.thinking_level}")
    elif name == "tools":
        for tool in session.agent.state.tools:
            print(f"  {tool.name:<10} {tool.description.splitlines()[0][:80]}")
    elif name == "skills":
        for skill in session.resources.get_skills():
            print(f"  {skill.name:<20} {skill.description[:80]}")
    elif name == "commands":
        for prompt in session.resources.get_prompts():
            print(f"  /{prompt.name:<18} {prompt.description[:70]} (prompt)")
        for command in api.commands.values():
            print(f"  /{command.name:<18} {command.description[:70]} (extension)")
    elif name == "compact":
        result = await session.compact(rest or None)
        if result:
            print(f"{DIM}compacted {result.tokens_before} tokens → summary{RESET}")
    elif name == "usage":
        usage = session.usage()
        print(
            f"  in {usage.input} / out {usage.output} / cache {usage.cache_read} "
            f"→ ${usage.cost.total:.4f}"
        )
    elif name == "session":
        print(f"  {session.session_file or '(in memory)'}")
    elif name in api.commands:
        output = await api.commands[name].handler(rest)
        if output:
            print(output)
    else:
        # not a builtin: maybe a prompt template
        await session.prompt(line)
    return True


async def run_interactive(session: AgentSession, show_thinking: bool) -> None:
    session.subscribe(make_printer(show_thinking))
    model = session.model
    print(f"{BOLD}pi-coding-agent{RESET} {DIM}{session.cwd}{RESET}")
    print(f"{DIM}model: {model.key if model else 'none'} · /help for commands{RESET}\n")

    loop = asyncio.get_running_loop()
    while True:
        try:
            raw = await loop.run_in_executor(None, lambda: input(f"{BOLD}> {RESET}"))
        except (EOFError, KeyboardInterrupt):
            print()
            return
        # locale 不是 UTF-8 时，中文输入会带孤立代理字符，进上下文前先修
        line = sanitize(raw).strip()
        if not line:
            continue
        if line.startswith("/"):
            name = line[1:].partition(" ")[0]
            builtin = name in {
                "help", "model", "think", "tools", "skills", "commands",
                "compact", "usage", "session", "exit", "quit",
            }
            if builtin or name in session.resources.get_extension_api().commands:
                if not await handle_command(session, line):
                    return
                continue
        try:
            await session.prompt(line)
        except KeyboardInterrupt:
            session.abort()
            print(f"\n{YELLOW}aborted{RESET}")
        except Exception as exc:
            print(f"{RED}{type(exc).__name__}: {exc}{RESET}")


async def run_print(session: AgentSession, message: str, as_json: bool) -> None:
    session.subscribe(json_printer if as_json else make_printer(False))
    await session.prompt(message)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pi", description="A minimal coding agent")
    parser.add_argument("message", nargs="*", help="prompt; omit for interactive mode")
    parser.add_argument("-C", "--cwd", default=".", help="working directory")
    parser.add_argument("-m", "--model", help="provider/model-id")
    parser.add_argument("-t", "--think", default="off", help="thinking level")
    parser.add_argument("--tools", help="comma-separated tool allowlist")
    parser.add_argument("--exclude-tools", default="", help="comma-separated tool denylist")
    parser.add_argument("--thinking", action="store_true", help="show thinking output")
    parser.add_argument("--mode", choices=["interactive", "print", "json"], default=None)
    parser.add_argument("--continue", dest="continue_recent", action="store_true")
    parser.add_argument("--resume", help="session .jsonl to open")
    parser.add_argument("--no-session", action="store_true", help="do not persist")
    parser.add_argument("--agent-dir", help="config dir, defaults to ~/.pi/agent")
    return parser


async def main_async(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # 非 UTF-8 locale 下 Python 用 surrogateescape 解码 argv，中文任务描述
    # 会带坏字符，一路飘到 json 序列化才炸
    args.message = [sanitize(m) for m in args.message]
    cwd = sanitize(str(Path(args.cwd).expanduser().resolve()))

    services = create_agent_session_services(cwd=cwd, agent_dir=args.agent_dir)
    for note in services.diagnostics:
        print(f"{YELLOW}! {note}{RESET}", file=sys.stderr)

    if args.no_session:
        session_manager = SessionManager.in_memory(cwd)
    elif args.resume:
        session_manager = SessionManager.open(args.resume, args.agent_dir)
    elif args.continue_recent:
        session_manager = SessionManager.continue_recent(cwd, args.agent_dir)
    else:
        session_manager = SessionManager.create(cwd, args.agent_dir)

    session = create_agent_session(
        services=services,
        session_manager=session_manager,
        model=args.model,
        thinking_level=args.think,
        tools=args.tools.split(",") if args.tools else None,
        exclude_tools=[t for t in args.exclude_tools.split(",") if t],
    )

    if session.model is None:
        print(
            f"{RED}No model available.{RESET} Set ANTHROPIC_API_KEY or OPENAI_API_KEY, "
            "or add ~/.pi/agent/auth.json.",
            file=sys.stderr,
        )
        return 1

    message = " ".join(args.message).strip()
    mode = args.mode or ("print" if message else "interactive")
    try:
        if mode == "interactive":
            await run_interactive(session, args.thinking)
        else:
            if not message:
                message = sanitize(sys.stdin.read()).strip()
            await run_print(session, message, as_json=(mode == "json"))
    finally:
        session.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(main_async(argv))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())