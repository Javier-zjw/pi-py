"""
簇 E · 组装测试：agent_session.py + cli.py

    python tests/test_agent_session.py
    python tests/test_agent_session.py -v
    pytest tests/test_agent_session.py     # PyCharm 默认走这个

三层第一次真正串起来：假 provider（pi-ai）→ 真循环（pi-agent）→ 真工具、
真会话文件（pi-coding-agent）。不联网、不需要 key。
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parents[2] / "pi-agent"))
sys.path.insert(0, str(HERE.parents[2] / "pi-ai"))

from fakes import BIG_MODEL, FAKE_MODEL, Sandbox, fake_models  # noqa: E402

from pi_agent import AgentToolResult, CustomMessage, ToolContext  # noqa: E402
from pi_ai import AssistantMessage, TextContent, ThinkingContent, ToolCall, Usage  # noqa: E402

from pi_coding_agent import ModelRuntime, SessionManager, define_tool  # noqa: E402
from pi_coding_agent.agent_session import (  # noqa: E402
    AgentSession,
    create_agent_session,
    create_agent_session_services,
)

VERBOSE = "-v" in sys.argv
UNDER_PYTEST = "PYTEST_CURRENT_TEST" in os.environ
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILURES.append(f"{name}: {detail}")
    print(f"  FAIL {name} {detail}")
    if UNDER_PYTEST:
        raise AssertionError(f"{name}: {detail}")


def make_session(
    cwd: Path,
    script: list[list],
    *,
    tools: list[str] | None = None,
    model=FAKE_MODEL,
    session_manager=None,
    usage: Usage | None = None,
    **kw,
) -> tuple[AgentSession, object]:
    """造一个用假 provider 驱动的真会话。"""
    models, provider = fake_models(script, models=[FAKE_MODEL, BIG_MODEL], usage=usage)
    runtime = ModelRuntime(models, cwd / "agent")
    services = create_agent_session_services(
        cwd=cwd, agent_dir=cwd / "agent", model_runtime=runtime
    )
    session = AgentSession(
        services,
        session_manager or SessionManager.in_memory(cwd),
        model=model,
        tools=tools if tools is not None else [],
        **kw,
    )
    return session, provider


def roles(session: AgentSession) -> list[str]:
    return [getattr(m, "role", "?") for m in session.messages]


# --------------------------------------------------------------------------- #
# 1. 端到端一轮
# --------------------------------------------------------------------------- #


async def case_simple_turn() -> None:
    with Sandbox() as cwd:
        session, provider = make_session(cwd, [[TextContent(text="你好，我在。")]])
        events: list[str] = []
        session.subscribe(lambda e: events.append(e.type))

        await session.prompt("在吗")

        check("单轮: transcript", roles(session) == ["user", "assistant"], str(roles(session)))
        check("单轮: 回答内容", session.messages[-1].text() == "你好，我在。")
        check("单轮: 事件流经会话", "agent_end" in events and "message_end" in events, str(events))
        check("单轮: 用量汇总", session.usage().input == 10, str(session.usage().input))
        check("单轮: 结束后不在流式中", not session.is_streaming)

        ctx = provider.calls[0]["context"]
        check("单轮: 系统提示已装配", "<environment>" in (ctx.system_prompt or ""),
              (ctx.system_prompt or "")[:40])
        check("单轮: 没配工具时不下发 tools", ctx.tools == [], str(ctx.tools))


async def case_tool_turn_with_real_tools() -> None:
    with Sandbox() as cwd:
        (cwd / "note.txt").write_text("藏在文件里的内容\n", "utf-8")
        session, provider = make_session(
            cwd,
            [
                [ToolCall(id="t1", name="read", arguments={"path": "note.txt"})],
                [TextContent(text="文件里写着：藏在文件里的内容")],
            ],
            tools=["read"],
        )
        await session.prompt("note.txt 里是什么")

        check("工具轮: 四条 transcript",
              roles(session) == ["user", "assistant", "toolResult", "assistant"],
              str(roles(session)))
        check("工具轮: 真的读了磁盘",
              "藏在文件里的内容" in session.messages[2].text(), session.messages[2].text()[:40])
        check("工具轮: 工具 schema 下发给模型",
              [t.name for t in provider.calls[0]["context"].tools] == ["read"],
              str(provider.calls[0]["context"].tools))
        check("工具轮: 第二次请求带上了工具结果",
              any(getattr(m, "role", "") == "toolResult"
                  for m in provider.calls[1]["context"].messages))


# --------------------------------------------------------------------------- #
# 2. 持久化
# --------------------------------------------------------------------------- #


async def case_persistence() -> None:
    with Sandbox() as cwd:
        manager = SessionManager(cwd=str(cwd), session_file=cwd / "s.jsonl")
        manager._write_header()
        session, _ = make_session(
            cwd,
            [
                [ToolCall(id="t1", name="read", arguments={"path": "a.txt"})],
                [TextContent(text="读完了")],
            ],
            tools=["read"],
            session_manager=manager,
        )
        (cwd / "a.txt").write_text("内容", "utf-8")
        await session.prompt("读一下 a.txt")

        entries = manager.get_entries()
        check("持久化: 每条消息一个条目", len(entries) == 4, str(len(entries)))
        on_disk = [getattr(e.message, "role", "?") for e in entries]
        check("持久化: 落盘顺序与上下文一致", on_disk == roles(session), str(on_disk))
        check("持久化: 没有重复写入",
              len({id(e.message) for e in entries}) == 4, str(len(entries)))

        reopened = SessionManager.open(cwd / "s.jsonl")
        texts = [m.text() for m in reopened.build_session_context()["messages"]]
        check("持久化: 重开后能还原对话", texts[-1] == "读完了", str(texts))


async def case_persist_injected_messages() -> None:
    """steering 消息进了上下文，就必须也进会话文件。"""
    with Sandbox() as cwd:
        manager = SessionManager(cwd=str(cwd), session_file=cwd / "s.jsonl")
        manager._write_header()
        session, _ = make_session(
            cwd,
            [
                [ToolCall(id="t1", name="noop", arguments={})],
                [TextContent(text="收到新指示")],
            ],
            tools=[],
            session_manager=manager,
            custom_tools=[define_tool(
                "noop", "什么都不做", {"type": "object", "properties": {}},
                lambda a, c: _ok(),
            )],
        )
        session.steer("改个方向")
        await session.prompt("开始")

        on_disk = [e.message.text() for e in manager.get_entries()]
        check("持久化: 注入消息也落盘", "改个方向" in on_disk, str(on_disk))
        check("持久化: 落盘数量与内存一致",
              len(manager.get_entries()) == len(session.messages),
              f"{len(manager.get_entries())} vs {len(session.messages)}")


async def _ok() -> AgentToolResult:
    return AgentToolResult.text("ok")


# --------------------------------------------------------------------------- #
# 3. 会话恢复与状态
# --------------------------------------------------------------------------- #


async def case_persist_dedup() -> None:
    """同一条消息被重复上报时只落盘一次。

    自从持久化统一到 message_end 之后，正常流程不会重复上报；这个守卫是防
    扩展或上层重复派发事件的。直接测它的契约，而不是绕一个不存在的路径。
    """
    from pi_agent import MessageEndEvent

    with Sandbox() as cwd:
        manager = SessionManager(cwd=str(cwd), session_file=cwd / "s.jsonl")
        manager._write_header()
        session, _ = make_session(cwd, [[TextContent(text="ok")]], session_manager=manager)

        message = AssistantMessage(content=[TextContent(text="同一条")], model="fake-1")
        session._on_agent_event(MessageEndEvent(message=message))
        session._on_agent_event(MessageEndEvent(message=message))
        check("持久化: 重复上报只落盘一次", len(manager.get_entries()) == 1,
              str(len(manager.get_entries())))


async def case_resume() -> None:
    with Sandbox() as cwd:
        path = cwd / "s.jsonl"
        manager = SessionManager(cwd=str(cwd), session_file=path)
        manager._write_header()
        session, _ = make_session(cwd, [[TextContent(text="第一次回答")]],
                                  session_manager=manager)
        await session.prompt("第一个问题")
        session.set_model(BIG_MODEL)
        session.set_thinking_level("high")

        reopened = SessionManager.open(path)
        session2, provider2 = make_session(
            cwd, [[TextContent(text="第二次回答")]],
            model=None, session_manager=reopened,
        )
        check("恢复: 历史消息回到上下文",
              [m.text() for m in session2.messages] == ["第一个问题", "第一次回答"],
              str([m.text() for m in session2.messages]))
        check("恢复: 模型从会话记录还原",
              session2.model and session2.model.id == BIG_MODEL.id,
              str(session2.model))
        check("恢复: 思考档位还原", session2.thinking_level == "high", session2.thinking_level)

        await session2.prompt("第二个问题")
        sent = [m.text() for m in provider2.calls[0]["context"].messages]
        check("恢复: 历史被发给模型", "第一个问题" in sent and "第二个问题" in sent, str(sent))


async def case_model_and_thinking_switch() -> None:
    with Sandbox() as cwd:
        manager = SessionManager(cwd=str(cwd), session_file=cwd / "s.jsonl")
        manager._write_header()
        session, provider = make_session(cwd, [[TextContent(text="ok")]],
                                         session_manager=manager)
        session.set_model(BIG_MODEL)
        session.set_thinking_level("medium")

        kinds = [e.type for e in manager.get_entries()]
        check("切换: 模型变更写进会话", "model_change" in kinds, str(kinds))
        check("切换: 档位变更写进会话", "thinking_level_change" in kinds, str(kinds))

        await session.prompt("试试")
        check("切换: 新模型生效", provider.calls[0]["model"].id == BIG_MODEL.id)
        check("切换: 档位传给 provider",
              provider.calls[0]["options"].reasoning == "medium",
              str(provider.calls[0]["options"].reasoning))


# --------------------------------------------------------------------------- #
# 4. 工具选择
# --------------------------------------------------------------------------- #


def test_tool_selection() -> None:
    with Sandbox() as cwd:
        models, _ = fake_models([[TextContent(text="x")]])
        runtime = ModelRuntime(models, cwd / "agent")
        services = create_agent_session_services(
            cwd=cwd, agent_dir=cwd / "agent", model_runtime=runtime
        )

        default = AgentSession(services, SessionManager.in_memory(cwd), model=FAKE_MODEL)
        names = {t.name for t in default.agent.state.tools}
        check("工具: 默认集合", names == {"read", "bash", "edit", "write"}, str(names))

        limited = AgentSession(services, SessionManager.in_memory(cwd), model=FAKE_MODEL,
                               tools=["read", "grep"])
        check("工具: 显式指定",
              {t.name for t in limited.agent.state.tools} == {"read", "grep"},
              str([t.name for t in limited.agent.state.tools]))

        excluded = AgentSession(services, SessionManager.in_memory(cwd), model=FAKE_MODEL,
                                exclude_tools=["bash", "write"])
        check("工具: 排除生效",
              {t.name for t in excluded.agent.state.tools} == {"read", "edit"},
              str([t.name for t in excluded.agent.state.tools]))

        custom = define_tool("mine", "自定义", {"type": "object", "properties": {}},
                             lambda a, c: _ok())
        with_custom = AgentSession(services, SessionManager.in_memory(cwd), model=FAKE_MODEL,
                                   tools=["read"], custom_tools=[custom])
        check("工具: 自定义工具加入",
              {t.name for t in with_custom.agent.state.tools} == {"read", "mine"},
              str([t.name for t in with_custom.agent.state.tools]))


def test_extension_tools_wired() -> None:
    """扩展注册的工具要自动进会话。"""
    with Sandbox() as cwd:
        ext = cwd / ".pi" / "extensions"
        ext.mkdir(parents=True)
        (ext / "demo.py").write_text('''
NAME = "demo"

def activate(pi):
    async def ping(args, ctx):
        from pi_agent import AgentToolResult
        return AgentToolResult.text("pong")

    pi.register_tool("ping", "测试用", {"type": "object", "properties": {}}, ping)
''', "utf-8")

        models, _ = fake_models([[TextContent(text="x")]])
        runtime = ModelRuntime(models, cwd / "agent")
        services = create_agent_session_services(
            cwd=cwd, agent_dir=cwd / "agent", model_runtime=runtime
        )
        session = AgentSession(services, SessionManager.in_memory(cwd), model=FAKE_MODEL,
                               tools=["read"])
        check("扩展: 注册的工具进入会话",
              "ping" in {t.name for t in session.agent.state.tools},
              str([t.name for t in session.agent.state.tools]))
        check("扩展: 会话绑回扩展 API",
              services.resources.get_extension_api().session is session)
        check("扩展: 加载无诊断信息", services.diagnostics == [], str(services.diagnostics))


def test_diagnostics_surface() -> None:
    with Sandbox() as cwd:
        ext = cwd / ".pi" / "extensions"
        ext.mkdir(parents=True)
        (ext / "bad.py").write_text("def activate(pi):\n    raise RuntimeError('炸了')\n", "utf-8")
        models, _ = fake_models([[TextContent(text="x")]])
        services = create_agent_session_services(
            cwd=cwd, agent_dir=cwd / "agent",
            model_runtime=ModelRuntime(models, cwd / "agent"),
        )
        check("诊断: 扩展报错被汇总", len(services.diagnostics) == 1, str(services.diagnostics))
        check("诊断: 含扩展名和原因",
              "bad" in services.diagnostics[0] and "炸了" in services.diagnostics[0],
              services.diagnostics[0])


# --------------------------------------------------------------------------- #
# 5. 压缩接入
# --------------------------------------------------------------------------- #


async def case_manual_compact() -> None:
    with Sandbox() as cwd:
        manager = SessionManager(cwd=str(cwd), session_file=cwd / "s.jsonl")
        manager._write_header()
        session, _ = make_session(
            cwd, [[TextContent(text="回答一")], [TextContent(text="摘要内容")]],
            session_manager=manager,
        )
        await session.prompt("问题一")
        before = len(session.messages)

        result = await session.compact()
        check("手动压缩: 返回结果", result is not None)
        check("手动压缩: 上下文变短", len(session.messages) < before + 2,
              f"{before} → {len(session.messages)}")
        check("手动压缩: 第一条是摘要",
              isinstance(session.messages[0], CustomMessage), str(type(session.messages[0])))
        kinds = [e.type for e in manager.get_entries()]
        check("手动压缩: 写入 compaction 条目", "compaction" in kinds, str(kinds))
        check("手动压缩: 摘要不重复落盘",
              kinds.count("custom_message") == 0, str(kinds))


async def case_auto_compact() -> None:
    """上下文逼近窗口时，transform_context 钩子应自动触发压缩。"""
    with Sandbox() as cwd:
        session, provider = make_session(
            cwd,
            [
                [TextContent(text="快满了")],       # 第一轮，回来的 usage 很大
                [TextContent(text="这是摘要")],     # 压缩调用
                [TextContent(text="压缩后的回答")],  # 第二轮
            ],
            usage=Usage(input=950, output=10),      # 窗口 1000，阈值 0.85
        )
        await session.prompt("第一个问题")
        check("自动压缩: 第一轮正常", session.messages[-1].text() == "快满了")

        await session.prompt("第二个问题")
        texts = [m.text() for m in session.messages]
        check("自动压缩: 触发了压缩",
              any(isinstance(m, CustomMessage) for m in session.messages), str(texts))
        check("自动压缩: 老消息被裁掉", "第一个问题" not in " ".join(texts), str(texts))
        check("自动压缩: 第二轮仍然回答了", "压缩后的回答" in " ".join(texts), str(texts))


async def case_compaction_disabled() -> None:
    with Sandbox() as cwd:
        models, _ = fake_models([[TextContent(text="一")], [TextContent(text="二")]],
                                usage=Usage(input=950, output=10))
        runtime = ModelRuntime(models, cwd / "agent")
        services = create_agent_session_services(
            cwd=cwd, agent_dir=cwd / "agent", model_runtime=runtime
        )
        services.settings.set("compaction.enabled", False)
        session = AgentSession(services, SessionManager.in_memory(cwd), model=FAKE_MODEL, tools=[])
        await session.prompt("一")
        await session.prompt("二")
        check("自动压缩: 设置里关掉后不触发",
              not any(isinstance(m, CustomMessage) for m in session.messages),
              str([type(m).__name__ for m in session.messages]))


# --------------------------------------------------------------------------- #
# 6. 提示模板与队列
# --------------------------------------------------------------------------- #


async def case_prompt_template() -> None:
    with Sandbox() as cwd:
        prompts = cwd / ".pi" / "prompts"
        prompts.mkdir(parents=True)
        (prompts / "tidy.md").write_text(
            "---\nname: tidy\ndescription: 清理\n---\n请整理下面这个文件，不要改行为。", "utf-8"
        )
        session, provider = make_session(cwd, [[TextContent(text="好的")]])

        expanded = session.expand_prompt_template("/tidy src/main.py")
        check("模板: 展开正文", "不要改行为" in expanded, expanded[:40])
        check("模板: 带上剩余参数", "src/main.py" in expanded, expanded[-40:])
        check("模板: 未知命令原样返回",
              session.expand_prompt_template("/没这个 x") == "/没这个 x")
        check("模板: 普通文本不动", session.expand_prompt_template("普通问题") == "普通问题")

        await session.prompt("/tidy src/main.py")
        sent = provider.calls[0]["context"].messages[0].text()
        check("模板: 发给模型的是展开后的文本", "不要改行为" in sent, sent[:40])


async def case_steer_and_followup() -> None:
    with Sandbox() as cwd:
        session, _ = make_session(cwd, [[TextContent(text="第一轮")], [TextContent(text="第二轮")]])
        session.follow_up("还有一件事")
        await session.prompt("开始")
        texts = [m.text() for m in session.messages]
        check("队列: follow_up 在模型停下时注入", "还有一件事" in texts, str(texts))
        check("队列: 触发了第二轮", "第二轮" in texts, str(texts))


async def case_abort() -> None:
    with Sandbox() as cwd:
        started = asyncio.Event()

        async def slow(args, ctx: ToolContext):
            started.set()
            for _ in range(200):
                if ctx.cancelled:
                    return AgentToolResult.text("收到中断", is_error=True)
                await asyncio.sleep(0.01)
            return AgentToolResult.text("跑完了")

        session, _ = make_session(
            cwd,
            [[ToolCall(id="t1", name="slow", arguments={})], [TextContent(text="不该出现")]],
            tools=[],
            custom_tools=[define_tool("slow", "慢工具",
                                      {"type": "object", "properties": {}}, slow)],
        )
        task = asyncio.create_task(session.prompt("跑"))
        await asyncio.wait_for(started.wait(), timeout=2)
        session.abort()
        await asyncio.wait_for(task, timeout=5)

        check("中断: 工具收到取消", "收到中断" in session.messages[2].text(),
              session.messages[2].text())
        check("中断: 没有第二轮",
              all(m.text() != "不该出现" for m in session.messages))
        check("中断: 状态复位", not session.is_streaming)


async def case_subscribe_and_dispose() -> None:
    with Sandbox() as cwd:
        session, _ = make_session(cwd, [[TextContent(text="一")], [TextContent(text="二")]])
        seen: list[str] = []
        unsubscribe = session.subscribe(lambda e: seen.append(e.type))
        await session.prompt("一")
        count = len(seen)
        unsubscribe()
        await session.prompt("二")
        check("订阅: 退订后不再收到", len(seen) == count, f"{count} → {len(seen)}")

        session.subscribe(lambda e: (_ for _ in ()).throw(RuntimeError("监听器炸了")))
        session2, _ = make_session(cwd, [[TextContent(text="ok")]])
        session2.subscribe(lambda e: (_ for _ in ()).throw(RuntimeError("炸")))
        await session2.prompt("试试")
        check("订阅: 监听器异常不影响主流程", session2.messages[-1].text() == "ok")

        session.dispose()
        check("清理: dispose 后监听器清空", session._listeners == [])


def test_create_agent_session_factory() -> None:
    with Sandbox() as cwd:
        models, _ = fake_models([[TextContent(text="x")]])
        runtime = ModelRuntime(models, cwd / "agent")
        services = create_agent_session_services(
            cwd=cwd, agent_dir=cwd / "agent", model_runtime=runtime
        )
        session = create_agent_session(
            services=services,
            session_manager=SessionManager.in_memory(cwd),
            model="fake/fake-1",
            tools=["read"],
        )
        check("工厂: 字符串模型被解析", session.model and session.model.id == "fake-1",
              str(session.model))
        check("工厂: cwd 已解析为绝对路径", Path(session.cwd).is_absolute())
        check("工厂: 未知模型返回 None 而不是崩",
              create_agent_session(
                  services=services,
                  session_manager=SessionManager.in_memory(cwd),
                  model="不存在/模型",
              ).model is None)


# --------------------------------------------------------------------------- #
# 7. CLI
# --------------------------------------------------------------------------- #


def test_cli_parser() -> None:
    from pi_coding_agent.cli import build_parser

    parser = build_parser()
    args = parser.parse_args([])
    check("CLI: 默认 cwd", args.cwd == ".")
    check("CLI: 默认思考档位", args.think == "off")
    check("CLI: 默认不指定模式", args.mode is None)

    args = parser.parse_args(["-m", "anthropic/x", "-t", "high", "--tools", "read,bash",
                              "--no-session", "帮我", "看看"])
    check("CLI: 模型参数", args.model == "anthropic/x")
    check("CLI: 工具白名单", args.tools == "read,bash")
    check("CLI: 位置参数收集成消息", args.message == ["帮我", "看看"], str(args.message))
    check("CLI: no-session 开关", args.no_session is True)

    args = parser.parse_args(["--continue"])
    check("CLI: --continue 映射到 continue_recent", args.continue_recent is True)


def test_cli_printers() -> None:
    from pi_agent import ToolExecutionEndEvent, ToolExecutionStartEvent
    from pi_coding_agent.cli import json_printer, make_printer

    buffer = io.StringIO()
    real_stdout, sys.stdout = sys.stdout, buffer
    try:
        json_printer(ToolExecutionStartEvent(
            tool_call_id="1", tool_name="read", arguments={"path": "a.py"}
        ))
        json_printer(ToolExecutionEndEvent(
            tool_call_id="1", tool_name="read",
            result=AgentToolResult.text("内容"), is_error=False,
        ))
    finally:
        sys.stdout = real_stdout

    lines = [json.loads(l) for l in buffer.getvalue().strip().split("\n")]
    check("CLI: JSON 模式每行一个对象", len(lines) == 2, str(lines))
    check("CLI: JSON 含工具名与参数",
          lines[0]["tool"] == "read" and lines[0]["arguments"]["path"] == "a.py", str(lines[0]))
    check("CLI: JSON 含成功标记", lines[1]["isError"] is False, str(lines[1]))

    printer = make_printer(show_thinking=False)
    check("CLI: 文本渲染器可构造", callable(printer))


async def case_cli_commands() -> None:
    from pi_coding_agent.cli import handle_command

    with Sandbox() as cwd:
        manager = SessionManager(cwd=str(cwd), session_file=cwd / "s.jsonl")
        manager._write_header()
        session, _ = make_session(cwd, [[TextContent(text="x")]], session_manager=manager)

        buffer = io.StringIO()
        real_stdout, sys.stdout = sys.stdout, buffer
        try:
            keep = await handle_command(session, "/help")
            check("CLI: /help 不退出", keep is True)
            await handle_command(session, "/tools")
            await handle_command(session, "/usage")
            await handle_command(session, "/session")
            await handle_command(session, "/think medium")
            await handle_command(session, f"/model fake/{BIG_MODEL.id}")
            leave = await handle_command(session, "/exit")
        finally:
            sys.stdout = real_stdout

        out = buffer.getvalue()
        check("CLI: /help 有输出", "命令" in out or "/model" in out, out[:80])
        check("CLI: /session 打印文件路径", "s.jsonl" in out, out[-120:])
        check("CLI: /think 生效", session.thinking_level == "medium")
        check("CLI: /model 生效", session.model.id == BIG_MODEL.id, str(session.model))
        check("CLI: /exit 返回 False", leave is False)


# --------------------------------------------------------------------------- #
# pytest 兼容层
# --------------------------------------------------------------------------- #


def test_simple_turn() -> None:
    asyncio.run(case_simple_turn())


def test_tool_turn_with_real_tools() -> None:
    asyncio.run(case_tool_turn_with_real_tools())


def test_persistence() -> None:
    asyncio.run(case_persistence())


def test_persist_injected_messages() -> None:
    asyncio.run(case_persist_injected_messages())


def test_persist_dedup() -> None:
    asyncio.run(case_persist_dedup())


def test_resume() -> None:
    asyncio.run(case_resume())


def test_model_and_thinking_switch() -> None:
    asyncio.run(case_model_and_thinking_switch())


def test_manual_compact() -> None:
    asyncio.run(case_manual_compact())


def test_auto_compact() -> None:
    asyncio.run(case_auto_compact())


def test_compaction_disabled() -> None:
    asyncio.run(case_compaction_disabled())


def test_prompt_template() -> None:
    asyncio.run(case_prompt_template())


def test_steer_and_followup() -> None:
    asyncio.run(case_steer_and_followup())


def test_abort() -> None:
    asyncio.run(case_abort())


def test_subscribe_and_dispose() -> None:
    asyncio.run(case_subscribe_and_dispose())


def test_cli_commands() -> None:
    asyncio.run(case_cli_commands())


async def main() -> int:
    print("端到端")
    await case_simple_turn()
    await case_tool_turn_with_real_tools()
    print("持久化")
    await case_persistence()
    await case_persist_injected_messages()
    await case_persist_dedup()
    print("恢复与状态")
    await case_resume()
    await case_model_and_thinking_switch()
    print("工具与扩展")
    test_tool_selection()
    test_extension_tools_wired()
    test_diagnostics_surface()
    print("压缩接入")
    await case_manual_compact()
    await case_auto_compact()
    await case_compaction_disabled()
    print("模板与队列")
    await case_prompt_template()
    await case_steer_and_followup()
    await case_abort()
    await case_subscribe_and_dispose()
    test_create_agent_session_factory()
    print("CLI")
    test_cli_parser()
    test_cli_printers()
    await case_cli_commands()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} 处失败：")
        for f in FAILURES:
            print(f"  - {f}")
        print("\n对照 tests/RUNTIME_CHECKLIST.md 定位实现位置")
        return 1
    print("全部通过 —— 三层已经串起来了")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))