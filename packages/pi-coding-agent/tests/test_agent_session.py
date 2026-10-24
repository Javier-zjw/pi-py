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

from fakes import BIG_MODEL, FAKE_MODEL, Sandbox, fake_models, scripted_stream  # noqa: E402

from pi_agent import AgentToolResult, CustomMessage, ToolContext  # noqa: E402
from pi_ai import (  # noqa: E402
    AssistantMessage,
    DoneEvent,
    ErrorEvent,
    Model,
    ModelCost,
    StartEvent,
    TextContent,
    TextDeltaEvent,
    ThinkingContent,
    ToolCall,
    Usage,
    UserMessage,
)

from pi_coding_agent import ModelRuntime, SessionManager, define_tool  # noqa: E402
from pi_coding_agent.agent_session import (  # noqa: E402
    AgentSession,
    create_agent_session,
    create_agent_session_services,
)

VERBOSE = "-v" in sys.argv
def under_pytest() -> bool:
    # 必须在调用时判断：import（收集）阶段 pytest 还没设置这个环境变量
    return "PYTEST_CURRENT_TEST" in os.environ
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILURES.append(f"{name}: {detail}")
    print(f"  FAIL {name} {detail}")
    if under_pytest():
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
        # 内置扩展 todo 注册的 update_plan 总会加入工具基线
        check("单轮: 没配工具时只有扩展注册的 update_plan",
              [t.name for t in ctx.tools] == ["update_plan"], str(ctx.tools))


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
              [t.name for t in provider.calls[0]["context"].tools] == ["read", "update_plan"],
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
        check("工具: 默认集合（含扩展注册的 update_plan）",
              names == {"read", "bash", "edit", "write", "update_plan"}, str(names))

        limited = AgentSession(services, SessionManager.in_memory(cwd), model=FAKE_MODEL,
                               tools=["read", "grep"])
        check("工具: 显式指定",
              {t.name for t in limited.agent.state.tools} == {"read", "grep", "update_plan"},
              str([t.name for t in limited.agent.state.tools]))

        excluded = AgentSession(services, SessionManager.in_memory(cwd), model=FAKE_MODEL,
                                exclude_tools=["bash", "write"])
        check("工具: 排除生效",
              {t.name for t in excluded.agent.state.tools} == {"read", "edit", "update_plan"},
              str([t.name for t in excluded.agent.state.tools]))

        custom = define_tool("mine", "自定义", {"type": "object", "properties": {}},
                             lambda a, c: _ok())
        with_custom = AgentSession(services, SessionManager.in_memory(cwd), model=FAKE_MODEL,
                                   tools=["read"], custom_tools=[custom])
        check("工具: 自定义工具加入",
              {t.name for t in with_custom.agent.state.tools} == {"read", "update_plan", "mine"},
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


async def case_plan_mode_tools() -> None:
    """submit_plan 只属于计划模式：聊天模式看不到，切回聊天后也要消失。"""
    with Sandbox() as cwd:
        models, _ = fake_models([[TextContent(text="x")]])
        runtime = ModelRuntime(models, cwd / "agent")
        services = create_agent_session_services(
            cwd=cwd, agent_dir=cwd / "agent", model_runtime=runtime
        )
        session = AgentSession(services, SessionManager.in_memory(cwd), model=FAKE_MODEL)

        def names() -> set[str]:
            return {t.name for t in session.agent.state.tools}

        await session._apply_mode()
        check("计划模式: 聊天模式无 submit_plan", "submit_plan" not in names(), str(names()))

        await session.set_mode("plan")
        await session._apply_mode()
        check("计划模式: 全部只读工具 + ask_user + submit_plan（没有写文件、bash、update_plan）",
              names() == {"read", "grep", "find", "ls", "ask_user", "submit_plan"}, str(names()))

        await session.set_mode("chat")
        await session._apply_mode()
        check("计划模式: 切回聊天后恢复基线",
              names() == {"read", "bash", "edit", "write", "update_plan"}, str(names()))


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


SUMMARY_JSON = """```json
{"task": {"goal": "回答一系列问题", "status": "等待下一个问题"},
 "constraints": [{"id": "c1", "text": "回答要简短"}],
 "progress": {"done": [{"id": "p1", "text": "回答了前几个问题"}]},
 "nextSteps": ["继续回答"]}
```"""

MID_MODEL = Model(id="fake-mid", provider="fake", api="anthropic-messages", name="Fake Mid",
                  cost=ModelCost(), context_window=16000, max_tokens=256)


def compaction_session(cwd: Path, script: list[list], **kw) -> tuple[AgentSession, object]:
    """可用预算 15000、触发线 12750、保留尾部 300 tokens 的会话。"""
    session, provider = make_session(cwd, script, model=MID_MODEL, **kw)
    session.settings.set("compaction.reserveTokens", 1000)
    session.settings.set("compaction.keepRecentTokens", 300)
    return session, provider


def record(session: AgentSession) -> list:
    events: list = []
    session.subscribe(events.append)
    return events


def use_estimates(session: AgentSession) -> None:
    """假 provider 报的 usage 只有 15 tokens，会被当成锚；清掉后改用估算。"""
    for m in session.messages:
        if isinstance(m, AssistantMessage):
            m.usage = Usage()


def file_manager(cwd: Path) -> SessionManager:
    manager = SessionManager(cwd=str(cwd), session_file=cwd / "s.jsonl")
    manager._write_header()
    return manager


async def case_manual_compact() -> None:
    with Sandbox() as cwd:
        manager = file_manager(cwd)
        long_answer = "。详细说明" * 300
        session, provider = compaction_session(
            cwd, [[TextContent(text="回答一" + long_answer)], [TextContent(text="回答二" + long_answer)],
                  [TextContent(text="回答三")], [TextContent(text=SUMMARY_JSON)]],
            session_manager=manager,
        )
        for i in ("一", "二", "三"):
            await session.prompt(f"问题{i}：" + "x" * 400)
        use_estimates(session)
        events = record(session)

        result = await session.compact("重点保留约束")
        check("手动压缩: 返回结果", result is not None)
        first = session.messages[0]
        check("手动压缩: 第一条是结构化状态",
              isinstance(first, CustomMessage) and first.text().startswith("<compacted_state"),
              str(first)[:80])
        check("手动压缩: 状态里有模型字段", "回答要简短" in first.text())
        check("手动压缩: 状态里有用户原话", "问题一" in first.text())
        check("手动压缩: 保留了最近的原文", session.messages[-1].text() == "回答三")

        types = [e.type for e in events]
        check("手动压缩: 事件 start → progress → end",
              types[0] == "compaction_start" and types[-1] == "compaction_end"
              and "compaction_progress" in types, str(types))
        end = events[-1]
        check("手动压缩: 结束事件带结果", end.ok and end.tokens_after < end.tokens_before
              and end.state["seq"] == 1, f"{end.tokens_before} → {end.tokens_after}")
        check("手动压缩: 额外要求进了指令",
              "重点保留约束" in provider.calls[-1]["context"].messages[-1].text())
        check("手动压缩: 禁止工具调用", provider.calls[-1]["options"].tool_choice is None
              or provider.calls[-1]["options"].tool_choice == "none")

        entries = manager.get_entries()
        compaction = [e for e in entries if e.type == "compaction"]
        check("手动压缩: 写入一条 compaction", len(compaction) == 1)
        c = compaction[0]
        check("手动压缩: 写 firstKeptEntryId 而不是整份尾部",
              c.first_kept_entry_id is not None and c.retained_tail == [], str(c.first_kept_entry_id))
        check("手动压缩: details 存结构化状态", c.details["state"]["llm"]["task"]["goal"] == "回答一系列问题")
        check("手动压缩: details 带上游同名字段", "readFiles" in c.details and "modifiedFiles" in c.details)
        check("手动压缩: 摘要不重复落盘", [e.type for e in entries].count("custom_message") == 0)

        # 恢复：上下文一致、状态可继续增量、旧 usage 不当锚
        reopened = AgentSession(session.services, SessionManager.open(cwd / "s.jsonl"),
                                model=MID_MODEL, tools=[])
        check("恢复: 消息条数一致", len(reopened.messages) == len(session.messages),
              f"{len(reopened.messages)} vs {len(session.messages)}")
        check("恢复: 状态消息内容一致", reopened.messages[0].text() == first.text())
        check("恢复: 尾部原文一致",
              [m.text() for m in reopened.messages[1:]] == [m.text() for m in session.messages[1:]])
        check("恢复: 结构化状态可用于下次增量", (reopened._compaction_state or {}).get("seq") == 1)
        check("恢复: 保留尾部的旧 usage 不当锚",
              reopened.messages[0].details.get("tailLength") == len(reopened.messages) - 1)


def read_script(n: int) -> list[list]:
    return [[ToolCall(id=f"r{i}", name="read", arguments={"path": f"f{i}.txt"})] for i in range(n)] \
        + [[TextContent(text="都读完了")]]


def write_files(cwd: Path, n: int) -> None:
    for i in range(n):
        (cwd / f"f{i}.txt").write_text("\n".join(f"第{i}个文件" + "内容" * 30 for _ in range(40)),
                                       "utf-8")


async def case_prune_in_tool_loop() -> None:
    """长工具链把上下文撑到 60%：先清理旧工具输出（不调模型），不必走 L2 摘要。"""
    with Sandbox() as cwd:
        write_files(cwd, 6)
        manager = file_manager(cwd)
        session, provider = compaction_session(cwd, read_script(6), tools=["read"],
                                               session_manager=manager, usage=Usage())
        session.settings.set("compaction.prune.protectTokens", 3000)
        events = record(session)
        await session.prompt("把 f0 到 f5 都读一遍")
        types = [e.type for e in events]
        check("清理: 触发了 L1", "context_pruned" in types, str(types))
        check("清理: 没有走 L2 摘要", "compaction_start" not in types, str(types))
        check("清理: 没有额外的模型调用", len(provider.calls) == 7, str(len(provider.calls)))
        pruned = next(e for e in events if e.type == "context_pruned")
        check("清理: 事件带前后大小", pruned.count >= 1 and pruned.tokens_after < pruned.tokens_before,
              f"{pruned.count}: {pruned.tokens_before} → {pruned.tokens_after}")
        last_call = provider.calls[-1]["context"].messages
        texts = [m.text() for m in last_call if getattr(m, "role", "") == "toolResult"]
        check("清理: 最旧的输出换成占位", texts[0].startswith("[已清理的 read 输出") and "f0.txt" in texts[0],
              texts[0][:60])
        check("清理: 最近的输出保留原文", "第5个文件" in texts[-1])
        check("清理: 这一轮正常结束", session.messages[-1].text() == "都读完了")

        entries = manager.get_entries()
        prunes = [e for e in entries if e.type == "custom" and e.custom_type == "context_prune"]
        check("清理: 写入 context_prune 条目", len(prunes) >= 1 and "r0" in prunes[0].data["toolCallIds"],
              str([e.data for e in prunes]))
        check("清理: 条目数和事件数一致", len(prunes) == types.count("context_pruned"))
        disk = [e.message.text() for e in entries
                if e.type == "message" and getattr(e.message, "role", "") == "toolResult"]
        check("清理: 会话文件保留原文", "第0个文件" in disk[0])

        reopened = AgentSession(session.services, SessionManager.open(cwd / "s.jsonl"),
                                model=MID_MODEL, tools=["read"])
        check("清理: 恢复后内容一致", [m.text() for m in reopened.messages]
              == [m.text() for m in session.messages])
        check("清理: 恢复后旧 usage 仍不当锚", reopened._anchor_floor > 0, str(reopened._anchor_floor))


async def case_prune_ignores_stale_usage() -> None:
    with Sandbox() as cwd:
        write_files(cwd, 6)
        session, _ = compaction_session(cwd, read_script(6), tools=["read"], usage=Usage())
        session.settings.set("compaction.prune.protectTokens", 3000)
        await session.prompt("读文件")
        floor = session._anchor_floor
        check("清理: 记下了计量下限", 0 < floor <= len(session.messages), str(floor))
        for m in session.messages[:floor]:
            if isinstance(m, AssistantMessage):
                m.usage = Usage(input=14_000)   # 清理前报的大数
        session.messages.append(AssistantMessage(content=[TextContent(text="新回复")],
                                                 usage=Usage(input=5_000), stop_reason="stop"))
        check("清理: 清理之后的新 usage 才当锚", session.context_tokens() == 5_000,
              str(session.context_tokens()))
        session.messages.pop()
        check("清理: 清理之前的 usage 不当锚", session.context_tokens() < 14_000,
              str(session.context_tokens()))


async def case_prune_skips_small_batches() -> None:
    """一次清理省不到 minBatchRatio 就不清：清理会让前缀缓存整段失效，省一点不值得"""
    with Sandbox() as cwd:
        write_files(cwd, 6)
        session, provider = compaction_session(cwd, read_script(6), tools=["read"], usage=Usage())
        session.settings.set("compaction.prune.protectTokens", 3000)
        session.settings.set("compaction.prune.minBatchRatio", 0.95)
        events = record(session)
        await session.prompt("把 f0 到 f5 都读一遍")
        check("清理: 省得太少时不清理", "context_pruned" not in [e.type for e in events],
              str([e.type for e in events]))
        # 不清理就一直涨到 L2 摘要线；在那之前每次请求的前缀都原样不动，缓存可以一直命中
        contexts = [[m.text() for m in call["context"].messages] for call in provider.calls[1:]
                    if call["context"].messages]
        before = [c for c in contexts if not c[0].startswith("<compacted_state")]
        check("清理: 不清理时前面的消息原样不动（前缀缓存可以一直命中）",
              len(before) >= 3 and all(c[:len(before[0])] == before[0] for c in before),
              str(len(before)))
        check("清理: 涨到摘要线后照常走 L2", "compaction_end" in [e.type for e in events])
        budget = session.compaction_budget()
        check("清理: 门槛按可用窗口算", budget.prune_min_batch == int(budget.usable * 0.95),
              str(budget.prune_min_batch))
        check("清理: 超长兜底（显式目标）不受门槛限制",
              session.prune(budget, target_tokens=0, protect_tokens=1) is True)


async def case_tool_budget() -> None:
    """一次请求工具调用太多：到 softLimit 次插一条提醒让模型收尾，之后每 every 次再提醒"""
    with Sandbox() as cwd:
        write_files(cwd, 6)
        session, provider = compaction_session(cwd, read_script(6), tools=["read"], usage=Usage())
        session.settings.set("compaction.enabled", False)
        session.settings.set("toolBudget.softLimit", 3)
        session.settings.set("toolBudget.every", 2)
        await session.prompt("把 f0 到 f5 都读一遍")
        reminders = [m for m in session.messages
                     if getattr(m, "role", "") == "custom" and m.custom_type == "tool_budget"]
        check("预算: 第 3、5 次工具之后各提醒一次", [m.details["count"] for m in reminders] == [3, 5],
              str([m.details for m in reminders]))
        index = next(i for i, call in enumerate(provider.calls)
                     if any("已经调用了 3 次工具" in m.text() for m in call["context"].messages))
        check("预算: 提醒在第 3 次工具之后、下一次调模型时送到", index == 3, str(index))
        sent = [m for m in provider.calls[index]["context"].messages if "已经调用了 3 次工具" in m.text()]
        check("预算: 模型看到的是一条用户消息", sent and sent[0].role == "user")
        check("预算: 不打断，这一轮照常跑完", session.messages[-1].text() == "都读完了")

        session.settings.set("toolBudget.enabled", False)
        provider.fn = scripted_stream(read_script(6))
        before = len(session.messages)
        await session.prompt("再读一遍")
        check("预算: 关掉后不提醒", not any(getattr(m, "role", "") == "custom"
                                          for m in session.messages[before:]))


def plan_call(call_id: str, *steps: tuple[str, str], explanation: str = "") -> ToolCall:
    args: dict = {"plan": [{"step": step, "status": status} for step, status in steps]}
    if explanation:
        args["explanation"] = explanation
    return ToolCall(id=call_id, name="update_plan", arguments=args)


async def case_todo_plan() -> None:
    """update_plan：存进会话记录、拒绝两个 in_progress、压缩后补回没做完的计划、恢复会话后还在"""
    from pi_coding_agent.builtin_extensions.todo import latest_todo

    with Sandbox() as cwd:
        write_files(cwd, 1)
        script = [
            [plan_call("p1", ("读文件", "in_progress"), ("总结", "pending"))],
            [ToolCall(id="r0", name="read", arguments={"path": "f0.txt"})],
            [plan_call("p2", ("读文件", "in_progress"), ("总结", "in_progress"))],
            [plan_call("p3", ("读文件", "completed"), ("总结", "in_progress"))],
            [TextContent(text="总结完了")],
        ]
        manager = file_manager(cwd)
        session, _ = make_session(cwd, script, tools=["read"], session_manager=manager)
        await session.prompt("读一下 f0 再总结")
        results = {m.tool_call_id: m for m in session.messages if getattr(m, "role", "") == "toolResult"}
        check("计划: 正常更新返回进度", "1/2" not in results["p1"].text()
              and "0/2" in results["p1"].text() and "读文件" in results["p1"].text(), results["p1"].text())
        check("计划: 两个 in_progress 被拒绝", results["p2"].is_error, results["p2"].text())
        check("计划: 拒绝后模型改正，记下最新状态", "1/2" in results["p3"].text(), results["p3"].text())
        entries = [e for e in manager.get_entries() if e.type == "custom" and e.custom_type == "todo"]
        check("计划: 只有合法的更新写进会话记录", len(entries) == 2, str([e.data for e in entries]))

        api = session.resources.get_extension_api()
        sections, errors = await api.run_compaction_context(session)
        todo = [s for s in sections if "当前任务计划" in s]
        check("计划: 压缩后补回没做完的计划", len(todo) == 1 and "- [x] 读文件" in todo[0]
              and "- [>] 总结" in todo[0] and not errors, str(sections))

        reopened = AgentSession(session.services, SessionManager.open(cwd / "s.jsonl"),
                                model=FAKE_MODEL, tools=["read"])
        record = latest_todo(reopened)
        check("计划: 恢复会话后还在", record is not None
              and [s["status"] for s in record["plan"]] == ["completed", "in_progress"], str(record))

        session.session.append_custom_entry("todo", {"plan": [{"step": "读文件", "status": "completed"},
                                                               {"step": "总结", "status": "completed"}]})
        sections, _ = await api.run_compaction_context(session)
        check("计划: 全部完成后不再补回", not any("当前任务计划" in s for s in sections), str(sections))


class ScriptedSSE:
    """真 OpenAIProvider 用的假 HTTP：每次请求回放一段 SSE，并记下请求体"""

    def __init__(self, replies: list[list[dict]]) -> None:
        self.replies = list(replies)
        self.payloads: list[dict] = []

    async def stream_sse(self, url, headers, payload, timeout=600.0):
        from pi_ai import SSEEvent
        self.payloads.append(payload)
        for data in self.replies.pop(0):
            yield SSEEvent(None, data)


async def case_reasoning_passback_end_to_end() -> None:
    """DeepSeek 思考模式：第一步流出来的思考，要原样出现在第二步请求里（三层串起来验证）"""
    from pi_ai import InMemoryCredentialStore, Models, OpenAIProvider

    def chunk(delta, finish=None):
        return {"choices": [{"delta": delta, "finish_reason": finish}]}

    transport = ScriptedSSE([
        [chunk({"reasoning_content": "先读入口文件 main.py，"}), chunk({"reasoning_content": "再决定看哪里"}),
         chunk({"tool_calls": [{"index": 0, "id": "c1", "function": {
             "name": "read", "arguments": '{"path": "main.py"}'}}]}),
         chunk({}, "tool_calls")],
        [chunk({"reasoning_content": "够了"}), chunk({"content": "入口只打印 hi"}), chunk({}, "stop")],
    ])
    model = Model(id="deepseek-flash", provider="deepseek-api", api="openai-completions",
                  reasoning=True, base_url="https://api.deepseek.com")
    store = InMemoryCredentialStore()
    store.set("deepseek-api", "k")
    models = Models(models=[model], credentials=store)
    models.set_provider(OpenAIProvider(provider_id="deepseek-api", base_url=model.base_url,
                                       transport=transport))
    with Sandbox() as cwd:
        (cwd / "main.py").write_text("print('hi')\n", "utf-8")
        services = create_agent_session_services(cwd=cwd, agent_dir=cwd / "agent",
                                                 model_runtime=ModelRuntime(models, cwd / "agent"))
        session = AgentSession(services, SessionManager.in_memory(cwd), model=model,
                               thinking_level="low", tools=["read"])
        await session.prompt("入口文件做了什么")
        check("回传: 跑完两步", session.messages[-1].text() == "入口只打印 hi", session.messages[-1].text())
        second = transport.payloads[1]["messages"]
        calls = [m for m in second if m.get("role") == "assistant" and m.get("tool_calls")]
        check("回传: 第二步请求里带着第一步的思考",
              calls and calls[0].get("reasoning_content") == "先读入口文件 main.py，再决定看哪里",
              json.dumps(calls, ensure_ascii=False)[:300])


async def case_goal_reanchor() -> None:
    """每 reanchorEvery 次工具调用把用户的请求和当前计划放回上下文末尾；update_plan 不计数"""
    with Sandbox() as cwd:
        write_files(cwd, 7)
        script = [[plan_call("p1", ("读文件", "in_progress"), ("总结", "pending"))]] + read_script(7)
        session, provider = compaction_session(cwd, script, tools=["read"], usage=Usage())
        session.settings.set("compaction.enabled", False)
        session.settings.set("toolBudget.reanchorEvery", 3)
        session.settings.set("toolBudget.softLimit", 100)
        await session.prompt("把 f0 到 f6 读一遍，然后总结它们的共同点")
        reminders = [m for m in session.messages
                     if getattr(m, "role", "") == "custom" and m.custom_type == "tool_budget"]
        check("重锚: 第 3、6 次工具之后各一次（update_plan 不算）",
              [m.details["count"] for m in reminders] == [3, 6], str([m.details for m in reminders]))
        check("重锚: 界面上不单独显示", all(m.details.get("quiet") for m in reminders))
        text = reminders[0].text()
        check("重锚: 带上用户的请求", "「把 f0 到 f6 读一遍，然后总结它们的共同点」" in text, text)
        check("重锚: 带上当前计划", "- [>] 读文件" in text and "- [ ] 总结" in text, text)
        check("重锚: 要求自查能不能回答", "够不够回答" in text, text)
        sent = [m for m in provider.calls[-1]["context"].messages if "对照一下方向" in m.text()]
        check("重锚: 以用户消息送到模型", len(sent) == 2 and all(m.role == "user" for m in sent))
        check("重锚: 不打断，这一轮照常跑完", session.messages[-1].text() == "都读完了")

    with Sandbox() as cwd:
        write_files(cwd, 4)
        session, _ = compaction_session(cwd, read_script(4), tools=["read"], usage=Usage())
        session.settings.set("compaction.enabled", False)
        session.settings.set("toolBudget.reanchorEvery", 0)
        session.settings.set("toolBudget.softLimit", 4)
        await session.prompt("读一下 f0 到 f3")
        wrap = [m for m in session.messages if getattr(m, "role", "") == "custom"]
        check("收尾: 到上限时提醒并带上请求", len(wrap) == 1 and "停止继续探索" in wrap[0].text()
              and "「读一下 f0 到 f3」" in wrap[0].text() and not wrap[0].details.get("quiet"),
              str([m.text()[:80] for m in wrap]))
        check("收尾: 没有计划时提示可以列计划", "update_plan" in wrap[0].text(), wrap[0].text())


def submit_call(call_id: str, *steps: str, title: str = "加超时配置") -> ToolCall:
    return ToolCall(id=call_id, name="submit_plan", arguments={
        "title": title, "summary": "## 思路\n把超时做成可配置项。", "steps": list(steps),
        "files": ["cfg.py"], "questions": []})


async def case_plan_execute_flow() -> None:
    """计划模式：只读调研 → 提交即停 → 批准后切执行模式、任务变进度单 → 逐项打勾；resume 后模式还在"""
    from pi_coding_agent.builtin_extensions.plan_mode import approve_plan, latest_plan

    with Sandbox() as cwd:
        (cwd / "cfg.py").write_text("TIMEOUT = 30\n", "utf-8")
        script = [
            [ToolCall(id="r1", name="read", arguments={"path": "cfg.py"})],
            [submit_call("s1", "读 cfg.py 确认现状", "把 TIMEOUT 改成读环境变量", "跑测试")],
            [TextContent(text="不该出现：提交计划后应该停下")],
        ]
        manager = file_manager(cwd)
        session, provider = make_session(cwd, script, session_manager=manager,
                                         tools=["read", "write", "edit", "bash"])
        events = record(session)
        session.switch_mode("plan")
        await session.prompt("给配置加一个可调的超时")
        types = [e.type for e in events]
        check("计划: 切模式发 mode_changed", types[0] == "mode_changed" and events[0].mode == "plan")
        tools = {t.name for t in provider.calls[0]["context"].tools}
        check("计划: 计划模式只给只读工具、ask_user 和 submit_plan",
              tools == {"read", "grep", "find", "ls", "ask_user", "submit_plan"}, str(tools))
        check("计划: 系统提示带计划模式说明", "<plan_mode>" in provider.calls[0]["context"].system_prompt)
        check("计划: 提交即停（不再调模型）", len(provider.calls) == 2
              and types[-1] == "agent_end" and events[-1].reason == "terminated", str(len(provider.calls)))
        proposed = [e for e in events if e.type == "plan_proposed"]
        check("计划: 发 plan_proposed，带任务", len(proposed) == 1
              and proposed[0].plan["steps"][1] == "把 TIMEOUT 改成读环境变量", str(proposed))
        check("计划: 存进会话记录（proposed）", latest_plan(session)["status"] == "proposed")
        check("计划: 还在计划模式", session.mode == "plan")

        reopened = AgentSession(session.services, SessionManager.open(cwd / "s.jsonl"), model=FAKE_MODEL)
        check("计划: resume 后模式还在", reopened.mode == "plan", reopened.mode)
        session.resources.get_extension_api().session = session   # reopened 抢走了绑定，换回来

        events.clear()
        task = approve_plan(session, "注意兼容旧配置")
        check("批准: 切到执行模式", session.mode == "chat" and events[0].type == "mode_changed")
        todo = [e for e in events if e.type == "todo_updated"]
        check("批准: 任务变成进度单、第一项进行中", len(todo) == 1
              and [s["status"] for s in todo[0].plan] == ["in_progress", "pending", "pending"], str(todo))
        check("批准: 记录为 approved", latest_plan(session)["status"] == "approved")
        check("批准: 任务说明带清单和用户补充", "2. 把 TIMEOUT 改成读环境变量" in task
              and "注意兼容旧配置" in task and "update_plan" in task, task)

        provider.fn = scripted_stream([
            [plan_call("u1", ("读 cfg.py 确认现状", "completed"), ("把 TIMEOUT 改成读环境变量", "in_progress"),
                       ("跑测试", "pending"))],
            [plan_call("u2", ("读 cfg.py 确认现状", "completed"), ("把 TIMEOUT 改成读环境变量", "completed"),
                       ("跑测试", "completed"))],
            [TextContent(text="都做完了")],
        ])
        await session.prompt(task)
        tools = {t.name for t in provider.calls[-1]["context"].tools}
        check("执行: 执行模式工具齐全", {"write", "edit", "bash", "update_plan"} <= tools, str(tools))
        updates = [e for e in events if e.type == "todo_updated"]
        check("执行: 每次 update_plan 都通知界面", [sum(s["status"] == "completed" for s in e.plan)
                                                  for e in updates] == [0, 1, 3], str(updates))
        check("执行: 跑完", session.messages[-1].text() == "都做完了")

    with Sandbox() as cwd:
        session, _ = make_session(cwd, [[TextContent(text="x")]])
        try:
            approve_plan(session)
            check("批准: 没有计划时报错", False)
        except LookupError as exc:
            check("批准: 没有计划时报错", "还没有可执行的计划" in str(exc))


def ask_call(call_id: str) -> ToolCall:
    return ToolCall(id=call_id, name="ask_user", arguments={"questions": [
        {"question": "list 金额也改成两位小数吗？", "header": "金额格式",
         "options": [{"label": "统一改", "description": "list 也显示 10.00", "recommended": True},
                     {"label": "保持原样"}, {"label": "  "}]},
        {"question": "   ", "options": [{"label": "空问题会被丢掉"}]},
        {"question": "budget 金额要校验吗？", "options": [{"label": "同样校验"}, {"label": "不校验"}]},
    ]})


async def case_plan_ask_user() -> None:
    """计划模式提问：界面弹选项、答案作为工具结果回来，本轮接着规划；没有界面 / 用户跳过时告诉模型怎么办"""
    script = [
        [TextContent(text="先确认两个问题。"), ask_call("a1")],
        [submit_call("s1", "改 report", "改 storage", "补测试")],
    ]
    with Sandbox() as cwd:
        session, provider = make_session(cwd, script)
        asked: list = []

        async def answer(questions):
            asked.append(questions)
            return [{"question": questions[0]["question"], "answer": "统一改", "custom": False},
                    {"question": questions[1]["question"], "answer": "金额必须大于 0", "custom": True}]

        session.user_input = answer
        session.switch_mode("plan")
        await session.prompt("规划一下")
        check("提问: 问题交给界面，空问题和空选项被丢掉", len(asked) == 1 and len(asked[0]) == 2
              and [o["label"] for o in asked[0][0]["options"]] == ["统一改", "保持原样"], str(asked))
        check("提问: 推荐项和主题带给界面", asked[0][0]["options"][0]["recommended"]
              and asked[0][0]["header"] == "金额格式" and asked[0][1]["options"][0]["label"] == "同样校验",
              str(asked))
        result = next(m for m in session.messages if getattr(m, "role", "") == "toolResult")
        text = result.content[0].text
        check("提问: 答案作为工具结果给模型", "统一改" in text and "金额必须大于 0 (typed by the user)" in text
              and not result.is_error, text)
        check("提问: 答完同一轮接着规划、提交计划", len(provider.calls) == 2
              and session.mode == "plan", str(len(provider.calls)))

    with Sandbox() as cwd:
        session, provider = make_session(cwd, [[ask_call("a1")], [TextContent(text="问题如下……")]])
        session.switch_mode("plan")
        await session.prompt("规划一下")
        result = next(m for m in session.messages if getattr(m, "role", "") == "toolResult")
        check("提问: 没有界面时报错，让模型在回复里问", result.is_error
              and "Ask them in your reply" in result.content[0].text, result.content[0].text)

    with Sandbox() as cwd:
        session, provider = make_session(cwd, [[ask_call("a1")], [TextContent(text="按默认")]])

        async def skip(questions):
            return None

        session.user_input = skip
        session.switch_mode("plan")
        await session.prompt("规划一下")
        result = next(m for m in session.messages if getattr(m, "role", "") == "toolResult")
        check("提问: 用户跳过时让模型按推荐继续", "skipped" in result.content[0].text
              and result.details == {"questions": result.details["questions"], "answers": []},
              result.content[0].text)


async def case_prune_disabled() -> None:
    with Sandbox() as cwd:
        write_files(cwd, 6)
        session, _ = compaction_session(cwd, read_script(6), tools=["read"], usage=Usage())
        session.settings.set("compaction.prune.enabled", False)
        events = record(session)
        await session.prompt("读文件")
        check("清理: 关闭后不触发", "context_pruned" not in [e.type for e in events])


async def case_rehydrate_after_compaction() -> None:
    """压缩后补回最近改过的文件和当前计划；恢复会话后一样。"""
    with Sandbox() as cwd:
        (cwd / "cfg.py").write_text("TIMEOUT = 30\n", "utf-8")
        manager = file_manager(cwd)
        long_answer = "。详细说明" * 300
        script = [
            [ToolCall(id="e1", name="edit", arguments={
                "path": "cfg.py", "old_text": "TIMEOUT = 30", "new_text": "TIMEOUT = 60"})],
            [TextContent(text="改好了" + long_answer)],
            [TextContent(text="回答二" + long_answer)],
            [TextContent(text="回答三")],
            [TextContent(text=SUMMARY_JSON)],
        ]
        session, _ = compaction_session(cwd, script, tools=["edit"], session_manager=manager)
        await session.prompt("把超时改成 60")
        await session.prompt("问题二")
        await session.prompt("问题三")
        use_estimates(session)
        session.resources.get_extension_api().state["plan"] = {
            "summary": "把配置迁到环境变量", "steps": ["读 cfg.py", "改成 os.environ"]}
        events = record(session)

        result = await session.compact()
        check("补回: 压缩成功", result is not None, str(events[-1:]))
        content = session.messages[0].text()
        check("补回: 状态消息带文件当前内容", "TIMEOUT = 60" in content, content[-300:])
        check("补回: 计划模式补回当前计划", "## 当前计划" in content and "迁到环境变量" in content)
        check("补回: 结束事件里的摘要不含补回内容", "TIMEOUT" not in events[-1].summary)
        entry = [e for e in manager.get_entries() if e.type == "compaction"][-1]
        check("补回: 落盘", entry.details.get("rehydratedFiles") == ["cfg.py"],
              str(entry.details.get("rehydratedFiles")))

        reopened = AgentSession(session.services, SessionManager.open(cwd / "s.jsonl"),
                                model=MID_MODEL, tools=[])
        check("补回: 恢复后状态消息一致", reopened.messages[0].text() == content)


async def case_rehydrate_hook_failure() -> None:
    with Sandbox() as cwd:
        long_answer = "。详细说明" * 300
        session, _ = compaction_session(
            cwd, [[TextContent(text="回答一" + long_answer)], [TextContent(text="回答二" + long_answer)],
                  [TextContent(text="回答三")], [TextContent(text=SUMMARY_JSON)]])
        for q in ("一", "二", "三"):
            await session.prompt(f"问题{q}")
        use_estimates(session)

        async def broken(_session):
            raise RuntimeError("扩展炸了")

        session.resources.get_extension_api().compaction_context(broken)
        result = await session.compact()
        check("补回: 扩展钩子出错不影响压缩", result is not None)
        check("补回: 钩子错误进诊断",
              any("扩展炸了" in d for d in session.services.diagnostics), str(session.services.diagnostics))


def overflow_model(limit: int, error: str = "HTTP 400: prompt is too long: 9000 tokens > 8000 maximum"):
    """上下文估算超过 limit 就报 error；收到压缩指令就回摘要；否则正常回答"""
    from pi_coding_agent.compaction import estimate_tokens

    calls: list[str] = []

    def stream_fn(model, context, options=None):
        last = context.messages[-1].text() if context.messages else ""
        if "<compaction_request>" in last:
            kind, blocks = "summary", [TextContent(text=SUMMARY_JSON)]
        elif estimate_tokens(context.messages) > limit:
            kind, blocks = "overflow", None
        else:
            kind, blocks = "answer", [TextContent(text="好了")]
        calls.append(kind)

        async def gen():
            message = AssistantMessage(api=model.api, provider=model.provider, model=model.id)
            yield StartEvent(partial=message)
            if blocks is None:
                message.stop_reason = "error"
                message.error_message = error
                yield ErrorEvent(error=error, message=message)
                return
            message.content.extend(blocks)
            message.stop_reason = "stop"
            yield DoneEvent(reason="stop", message=message)

        return gen()

    return stream_fn, calls


async def overflow_session(cwd: Path, limit: int, **kw):
    """预先灌好一段长历史；把主动压缩的阈值调高，只让超长兜底起作用"""
    error = kw.pop("error", None)
    long_answer = "。详细说明" * 300
    session, _ = compaction_session(
        cwd, [[TextContent(text=f"回答{i}" + long_answer)] for i in range(3)], usage=Usage(), **kw)
    for i in range(3):
        await session.prompt(f"问题{i}")
    session.settings.set("compaction.threshold", 0.99)
    session.settings.set("compaction.prune.triggerRatio", 0.99)
    fake, calls = overflow_model(limit, **({"error": error} if error else {}))
    session.agent.stream_fn = fake
    session.model_runtime.models.stream_simple = fake
    return session, calls


async def case_overflow_recovery() -> None:
    """服务商报上下文超长：压缩后重试本轮，用户看到的是正常回答"""
    with Sandbox() as cwd:
        manager = file_manager(cwd)
        session, calls = await overflow_session(cwd, limit=2500, session_manager=manager)
        events = record(session)
        await session.prompt("继续")
        types = [e.type for e in events]
        starts = [e for e in events if e.type == "compaction_start"]
        check("超长兜底: 触发了一次压缩", len(starts) == 1 and starts[0].reason == "overflow",
              str([(e.type, getattr(e, "reason", "")) for e in events if "compaction" in e.type]))
        check("超长兜底: 顺序是 超长 → 摘要 → 重试成功", calls == ["overflow", "summary", "answer"], str(calls))
        check("超长兜底: 这一轮正常结束", events[-1].type == "agent_end" and events[-1].reason == "stop",
              str(types[-3:]))
        check("超长兜底: 用户看到正常回答", session.messages[-1].text() == "好了")
        check("超长兜底: 失败的回复不在上下文里",
              all(getattr(m, "stop_reason", "") != "error" for m in session.messages))
        disk = [e.message for e in manager.get_entries() if e.type == "message"]
        check("超长兜底: 失败的回复不落盘", all(getattr(m, "stop_reason", "") != "error" for m in disk))
        check("超长兜底: 新请求没被压缩掉", any(m.text() == "继续" for m in session.messages))


async def case_overflow_unrecoverable() -> None:
    """压缩后仍然超长：只救一次，然后照常报错（错误信息可读）"""
    with Sandbox() as cwd:
        session, calls = await overflow_session(cwd, limit=10)
        events = record(session)
        await session.prompt("继续")
        starts = [e for e in events if e.type == "compaction_start"]
        check("超长兜底: 只尝试一次", len(starts) == 1, str(len(starts)))
        check("超长兜底: 救不回来时照常报错", events[-1].reason == "error", events[-1].reason)
        check("超长兜底: 错误信息可读", "prompt is too long" in (session.agent.state.error_message or ""))


async def case_overflow_other_errors() -> None:
    with Sandbox() as cwd:
        session, calls = await overflow_session(cwd, limit=10, error="HTTP 429: rate limit exceeded")
        events = record(session)
        await session.prompt("继续")
        check("超长兜底: 非超长错误不压缩", not any(e.type == "compaction_start" for e in events)
              and calls == ["overflow"], str(calls))

    with Sandbox() as cwd:
        session, calls = await overflow_session(cwd, limit=2500)
        session.settings.set("compaction.enabled", False)
        events = record(session)
        await session.prompt("继续")
        check("超长兜底: 关闭压缩后不处理", events[-1].reason == "error" and calls == ["overflow"],
              str(calls))


def dangling_calls(messages) -> set[str]:
    calls = {c.id for m in messages if getattr(m, "role", "") == "assistant" for c in m.tool_calls()}
    return calls - {m.tool_call_id for m in messages if getattr(m, "role", "") == "toolResult"}


async def case_resume_after_crash_mid_tool() -> None:
    """工具执行中进程退出：文件里只有调用、没有结果。恢复后补上中断结果并能继续对话"""
    with Sandbox() as cwd:
        manager = file_manager(cwd)
        session, _ = make_session(
            cwd, [[ToolCall(id="c1", name="bash", arguments={"command": "sleep 5"})]],
            tools=["bash"], session_manager=manager)
        task = asyncio.create_task(session.prompt("跑个长命令"))
        await asyncio.sleep(0.3)
        task.cancel()                                   # 相当于进程被杀
        try:
            await task
        except asyncio.CancelledError:
            pass
        on_disk = [e.message.role for e in SessionManager.open(cwd / "s.jsonl").get_entries()
                   if e.type == "message"]
        check("中断恢复: 前提——文件里只有调用", on_disk == ["user", "assistant"], str(on_disk))

        reopened, provider = make_session(cwd, [[TextContent(text="我先检查一下命令是否跑完")]],
                                          tools=["bash"], session_manager=SessionManager.open(cwd / "s.jsonl"))
        check("中断恢复: 记下补全了哪些调用", reopened.interrupted_tool_calls == ["c1"])
        repaired = [e.message for e in SessionManager.open(cwd / "s.jsonl").get_entries()
                    if e.type == "message"]
        check("中断恢复: 中断结果写回了文件",
              repaired[-1].role == "toolResult" and repaired[-1].details == {"interrupted": True},
              str([m.role for m in repaired]))
        await reopened.prompt("继续")
        sent = provider.calls[0]["context"].messages
        check("中断恢复: 发出的请求里调用都有结果", dangling_calls(sent) == set(), str(dangling_calls(sent)))
        check("中断恢复: 对话能继续", reopened.messages[-1].text() == "我先检查一下命令是否跑完")

        again = AgentSession(reopened.services, SessionManager.open(cwd / "s.jsonl"), model=FAKE_MODEL)
        check("中断恢复: 再次打开不会重复补", again.interrupted_tool_calls == [])


async def case_abort_mid_tool_call_stream() -> None:
    """模型刚发出工具调用就被取消：这条回复带着没执行的调用，下一次请求不能因此被拒"""
    with Sandbox() as cwd:
        session, provider = make_session(cwd, [[TextContent(text="好的")]], tools=["bash"])
        session.messages.extend([
            UserMessage(content="跑测试"),
            AssistantMessage(content=[ToolCall(id="x1", name="bash", arguments={"command": "pytest"})],
                             stop_reason="aborted"),
        ])
        await session.prompt("算了，先别跑")
        sent = provider.calls[0]["context"].messages
        check("取消后: 发出的请求里调用都有结果", dangling_calls(sent) == set(), str([m.role for m in sent]))


async def case_continue_after_crash() -> None:
    """工具执行中退出 → 恢复 → 不用再说话，直接从断点接着做完这一轮"""
    with Sandbox() as cwd:
        manager = file_manager(cwd)
        session, _ = make_session(
            cwd, [[ToolCall(id="c1", name="bash", arguments={"command": "sleep 5"})]],
            tools=["bash"], session_manager=manager)
        task = asyncio.create_task(session.prompt("部署一下"))
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        reopened, provider = make_session(cwd, [[TextContent(text="上次的命令被中断了，我重新检查后完成了部署")]],
                                          tools=["bash"], session_manager=SessionManager.open(cwd / "s.jsonl"))
        check("续跑: 恢复后识别出上次没做完", reopened.can_continue)
        events = record(reopened)
        await reopened.continue_()
        sent = provider.calls[0]["context"].messages
        check("续跑: 没有追加新的用户消息", [m.role for m in sent] == ["user", "assistant", "toolResult"],
              str([m.role for m in sent]))
        check("续跑: 模型看到了中断结果", "工具执行被中断" in sent[-1].text())
        check("续跑: 这一轮做完", events[-1].type == "agent_end" and events[-1].reason == "stop"
              and reopened.messages[-1].text().startswith("上次的命令被中断了"))
        check("续跑: 做完后不能再继续", not reopened.can_continue)
        try:
            await reopened.continue_()
            check("续跑: 没有可继续的任务时报错", False, "没有抛")
        except RuntimeError:
            check("续跑: 没有可继续的任务时报错", True)
        on_disk = [e.message.role for e in SessionManager.open(cwd / "s.jsonl").get_entries()
                   if e.type == "message"]
        check("续跑: 落盘完整", on_disk == ["user", "assistant", "toolResult", "assistant"], str(on_disk))


async def case_continue_unanswered_prompt_after_compaction() -> None:
    """压缩过的会话里，最后一条用户消息还没得到回复就退出了：恢复后续跑，状态和前缀都在"""
    with Sandbox() as cwd:
        manager = file_manager(cwd)
        long_answer = "。详细说明" * 300
        session, _ = compaction_session(
            cwd, [[TextContent(text="回答一" + long_answer)], [TextContent(text="回答二" + long_answer)],
                  [TextContent(text="回答三")], [TextContent(text=SUMMARY_JSON)]], session_manager=manager)
        for q in ("一", "二", "三"):
            await session.prompt(f"问题{q}")
        use_estimates(session)
        await session.compact()
        manager.append_message(UserMessage(content="问题四：还没来得及回答"))   # 发出后进程就退出了

        reopened, provider = make_session(cwd, [[TextContent(text="回答四")]], model=MID_MODEL,
                                          session_manager=SessionManager.open(cwd / "s.jsonl"))
        check("续跑: 压缩后的会话也能识别", reopened.can_continue)
        await reopened.continue_()
        sent = provider.calls[0]["context"].messages
        check("续跑: 请求以压缩状态开头", sent[0].text().startswith("<compacted_state"))
        check("续跑: 回答的是没答的那个问题", sent[-1].text() == "问题四：还没来得及回答"
              and reopened.messages[-1].text() == "回答四")


async def case_compaction_no_gain() -> None:
    """内容太少、压缩后反而更大时不替换历史。"""
    with Sandbox() as cwd:
        session, _ = compaction_session(
            cwd, [[TextContent(text="好")], [TextContent(text="行")], [TextContent(text=SUMMARY_JSON)]])
        await session.prompt("你好")
        await session.prompt("在吗")
        before = [m.text() for m in session.messages]
        events = record(session)
        result = await session.compact()
        check("无收益: 不替换历史", result is None and [m.text() for m in session.messages] == before)
        check("无收益: 说明原因", "没有变小" in (events[-1].error or ""), str(events[-1:]))


async def case_double_compaction_resume() -> None:
    """连续两次压缩，第二次的保留区间会跨过第一次的压缩条目；恢复后只能有一份状态。"""
    with Sandbox() as cwd:
        manager = file_manager(cwd)
        session, _ = compaction_session(
            cwd, [[TextContent(text=f"回答{i}" + "。展开说明" * 200)] for i in range(4)]
            + [[TextContent(text=SUMMARY_JSON)], [TextContent(text=SUMMARY_JSON)]],
            session_manager=manager,
        )
        for i in range(4):
            await session.prompt(f"问题{i}：" + "y" * 400)
        use_estimates(session)
        session.settings.set("compaction.keepRecentTokens", 2500)   # 第一次多留几轮
        first = await session.compact()
        check("二次压缩: 第一次保留了多条", first is not None and len(first.retained) >= 3,
              str(len(first.retained) if first else None))
        session.settings.set("compaction.keepRecentTokens", 300)    # 第二次切进上次的保留区
        second = await session.compact()
        check("二次压缩: 成功且 seq=2", second is not None and second.state["seq"] == 2)
        reopened = AgentSession(session.services, SessionManager.open(cwd / "s.jsonl"),
                                model=MID_MODEL, tools=[])
        states = [m for m in reopened.messages
                  if isinstance(m, CustomMessage) and m.custom_type == "compaction_summary"]
        check("二次压缩: 恢复后只有一份状态", len(states) == 1, str(len(states)))
        context_entries = SessionManager.open(cwd / "s.jsonl").build_context_entries()
        check("二次压缩: 上下文条目里只有最新的压缩",
              [e.type for e in context_entries].count("compaction") == 1,
              str([e.type for e in context_entries]))
        check("二次压缩: 恢复后内容一致",
              [m.text() for m in reopened.messages] == [m.text() for m in session.messages])


async def case_compact_before_prompt() -> None:
    """上下文到线时，新请求进来先压缩、后执行；新请求本身不进压缩。"""
    with Sandbox() as cwd:
        session, provider = compaction_session(
            cwd, [[TextContent(text="回答一")], [TextContent(text="回答二")],
                  [TextContent(text=SUMMARY_JSON)], [TextContent(text="压缩后的回答")]],
        )
        await session.prompt("第一个问题 " + "a" * 1200)
        await session.prompt("第二个问题 " + "b" * 1200)
        session.messages[-1].usage = Usage(input=13500, output=10)   # 逼近窗口
        events = record(session)

        await session.prompt("第三个问题")
        types = [e.type for e in events]
        check("先压后跑: 触发了压缩", "compaction_end" in types and events[types.index(
            "compaction_end")].ok, str(types))
        check("先压后跑: 压缩在新请求之前",
              types.index("compaction_end") < types.index("agent_start"), str(types))
        summarize_call = provider.calls[2]["context"].messages
        check("先压后跑: 新请求不进压缩",
              "第三个问题" not in " ".join(m.text() for m in summarize_call))
        answer_call = provider.calls[3]["context"].messages
        check("先压后跑: 正文请求以状态开头", answer_call[0].text().startswith("<compacted_state"))
        check("先压后跑: 正文请求带上新问题", answer_call[-1].text() == "第三个问题")
        check("先压后跑: 回答正常", session.messages[-1].text() == "压缩后的回答")


async def case_compact_mid_loop() -> None:
    """一轮内工具链把上下文撑满：在下一次调用模型之前压缩，这一轮继续跑完。"""
    with Sandbox() as cwd:
        (cwd / "big.txt").write_text("内容" * 50, "utf-8")
        session, provider = compaction_session(
            cwd,
            [[TextContent(text="先读文件"), ToolCall(id="t1", name="read", arguments={"path": "big.txt"})],
             [TextContent(text=SUMMARY_JSON)],
             [TextContent(text="读完了")]],
            tools=["read"], usage=Usage(input=13500, output=10),
        )
        events = record(session)
        await session.prompt("请读 big.txt " + "c" * 1200)
        types = [e.type for e in events]
        check("循环内压缩: 发生在工具执行之后",
              "compaction_start" in types
              and types.index("tool_execution_end") < types.index("compaction_start"), str(types))
        check("循环内压缩: 这一轮继续跑完", session.messages[-1].text() == "读完了")
        check("循环内压缩: 状态在最前", session.messages[0].text().startswith("<compacted_state"))
        check("循环内压缩: 工具结果被保留",
              any(getattr(m, "role", "") == "toolResult" for m in session.messages))
        check("循环内压缩: 一轮只压一次", types.count("compaction_start") == 1, str(types))


async def case_compaction_failure_keeps_history() -> None:
    with Sandbox() as cwd:
        manager = file_manager(cwd)
        session, _ = compaction_session(
            cwd, [[TextContent(text="回答一")], [TextContent(text="回答二")]],
            session_manager=manager,
        )
        await session.prompt("问题一 " + "d" * 1200)
        await session.prompt("问题二 " + "e" * 1200)
        before = [m.text() for m in session.messages]

        def failing(model, context, options=None):
            async def gen():
                message = AssistantMessage(stop_reason="error", error_message="429 rate limited")
                yield StartEvent(partial=message)
                yield ErrorEvent(error="429", message=message)
            return gen()

        session.model_runtime.models.stream_simple = failing
        events = record(session)
        result = await session.compact()
        check("失败: 返回 None", result is None)
        check("失败: 历史原样保留", [m.text() for m in session.messages] == before)
        check("失败: 不写 compaction", all(e.type != "compaction" for e in manager.get_entries()))
        end = events[-1]
        check("失败: 结束事件带错误", end.type == "compaction_end" and "429" in (end.error or ""),
              str(end))


async def case_compaction_cancel() -> None:
    with Sandbox() as cwd:
        session, _ = compaction_session(
            cwd, [[TextContent(text="回答一")], [TextContent(text="回答二")]])
        await session.prompt("问题一 " + "f" * 1200)
        await session.prompt("问题二 " + "g" * 1200)
        before = [m.text() for m in session.messages]

        def slow(model, context, options=None):
            async def gen():
                message = AssistantMessage()
                yield StartEvent(partial=message)
                for _ in range(50):
                    await asyncio.sleep(0.005)
                    yield TextDeltaEvent(content_index=0, delta="…", partial=message)
                message.content.append(TextContent(text=SUMMARY_JSON))
                message.stop_reason = "stop"
                yield DoneEvent(reason="stop", message=message)
            return gen()

        session.model_runtime.models.stream_simple = slow
        events: list = []

        def on_event(e) -> None:
            events.append(e)
            if e.type == "compaction_progress":
                session.abort()

        session.subscribe(on_event)
        result = await session.compact()
        check("取消: 返回 None", result is None)
        check("取消: 结束事件标记取消", events[-1].type == "compaction_end" and events[-1].cancelled)
        check("取消: 历史原样保留", [m.text() for m in session.messages] == before)
        check("取消: 压缩状态复位", not session.is_compacting)


async def case_prompt_waits_for_compaction() -> None:
    """手动压缩还没做完时来了新请求：先等压缩完成，再执行这一轮。"""
    with Sandbox() as cwd:
        long_answer = "。详细说明" * 300
        session, _ = compaction_session(
            cwd, [[TextContent(text="回答一" + long_answer)], [TextContent(text="回答二" + long_answer)],
                  [TextContent(text="新回答")]])
        await session.prompt("问题一")
        await session.prompt("问题二")
        use_estimates(session)
        real_stream = session.model_runtime.models.stream_simple

        def slow_summary(model, context, options=None):
            async def gen():
                message = AssistantMessage()
                yield StartEvent(partial=message)
                for _ in range(10):
                    await asyncio.sleep(0.01)
                    yield TextDeltaEvent(content_index=0, delta="…", partial=message)
                message.content.append(TextContent(text=SUMMARY_JSON))
                message.stop_reason = "stop"
                yield DoneEvent(reason="stop", message=message)
            return gen()

        session.model_runtime.models.stream_simple = slow_summary
        order: list[str] = []
        session.subscribe(lambda e: order.append(e.type)
                          if e.type in ("compaction_end", "agent_start") else None)
        compaction = asyncio.create_task(session.compact())
        await asyncio.sleep(0.02)
        session.model_runtime.models.stream_simple = real_stream
        await session.prompt("问题三")
        await compaction
        check("并发: 压缩结束后才开始新一轮", order == ["compaction_end", "agent_start"], str(order))
        check("并发: 新一轮的消息没被压缩覆盖",
              session.messages[0].text().startswith("<compacted_state")
              and session.messages[-1].text() == "新回答",
              str([m.text()[:20] for m in session.messages]))


async def case_compact_guard_while_streaming() -> None:
    with Sandbox() as cwd:
        session, _ = compaction_session(cwd, [[TextContent(text="回答")]])
        await session.prompt("问题 " + "h" * 1200)
        events = record(session)
        session.agent.state.is_streaming = True
        try:
            result = await session.compact()
        finally:
            session.agent.state.is_streaming = False
        check("运行中: 不允许从外部压缩", result is None and events[-1].error is not None,
              str(events[-1:]))


async def case_compaction_disabled() -> None:
    with Sandbox() as cwd:
        session, _ = compaction_session(
            cwd, [[TextContent(text="一")], [TextContent(text="二")]],
            usage=Usage(input=13500, output=10))
        session.settings.set("compaction.enabled", False)
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


def test_plan_mode_tools() -> None:
    asyncio.run(case_plan_mode_tools())


def test_manual_compact() -> None:
    asyncio.run(case_manual_compact())


def test_prune_in_tool_loop() -> None:
    asyncio.run(case_prune_in_tool_loop())


def test_prune_ignores_stale_usage() -> None:
    asyncio.run(case_prune_ignores_stale_usage())


def test_prune_skips_small_batches() -> None:
    asyncio.run(case_prune_skips_small_batches())


def test_tool_budget() -> None:
    asyncio.run(case_tool_budget())


def test_todo_plan() -> None:
    asyncio.run(case_todo_plan())


def test_plan_execute_flow() -> None:
    asyncio.run(case_plan_execute_flow())


def test_plan_ask_user() -> None:
    asyncio.run(case_plan_ask_user())


def test_goal_reanchor() -> None:
    asyncio.run(case_goal_reanchor())


def test_reasoning_passback_end_to_end() -> None:
    asyncio.run(case_reasoning_passback_end_to_end())


def test_prune_disabled() -> None:
    asyncio.run(case_prune_disabled())


def test_rehydrate_after_compaction() -> None:
    asyncio.run(case_rehydrate_after_compaction())


def test_rehydrate_hook_failure() -> None:
    asyncio.run(case_rehydrate_hook_failure())


def test_overflow_recovery() -> None:
    asyncio.run(case_overflow_recovery())


def test_overflow_unrecoverable() -> None:
    asyncio.run(case_overflow_unrecoverable())


def test_overflow_other_errors() -> None:
    asyncio.run(case_overflow_other_errors())


def test_resume_after_crash_mid_tool() -> None:
    asyncio.run(case_resume_after_crash_mid_tool())


def test_abort_mid_tool_call_stream() -> None:
    asyncio.run(case_abort_mid_tool_call_stream())


def test_continue_after_crash() -> None:
    asyncio.run(case_continue_after_crash())


def test_continue_unanswered_prompt_after_compaction() -> None:
    asyncio.run(case_continue_unanswered_prompt_after_compaction())


def test_compaction_no_gain() -> None:
    asyncio.run(case_compaction_no_gain())


def test_double_compaction_resume() -> None:
    asyncio.run(case_double_compaction_resume())


def test_compact_before_prompt() -> None:
    asyncio.run(case_compact_before_prompt())


def test_compact_mid_loop() -> None:
    asyncio.run(case_compact_mid_loop())


def test_compaction_failure_keeps_history() -> None:
    asyncio.run(case_compaction_failure_keeps_history())


def test_compaction_cancel() -> None:
    asyncio.run(case_compaction_cancel())


def test_prompt_waits_for_compaction() -> None:
    asyncio.run(case_prompt_waits_for_compaction())


def test_compact_guard_while_streaming() -> None:
    asyncio.run(case_compact_guard_while_streaming())


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
    await case_plan_mode_tools()
    test_diagnostics_surface()
    print("压缩接入")
    await case_manual_compact()
    await case_prune_in_tool_loop()
    await case_prune_ignores_stale_usage()
    await case_prune_skips_small_batches()
    await case_tool_budget()
    await case_todo_plan()
    await case_plan_execute_flow()
    await case_plan_ask_user()
    await case_goal_reanchor()
    await case_reasoning_passback_end_to_end()
    await case_prune_disabled()
    await case_rehydrate_after_compaction()
    await case_rehydrate_hook_failure()
    await case_overflow_recovery()
    await case_overflow_unrecoverable()
    await case_overflow_other_errors()
    await case_resume_after_crash_mid_tool()
    await case_abort_mid_tool_call_stream()
    await case_continue_after_crash()
    await case_continue_unanswered_prompt_after_compaction()
    await case_compaction_no_gain()
    await case_double_compaction_resume()
    await case_compact_before_prompt()
    await case_compact_mid_loop()
    await case_compaction_failure_keeps_history()
    await case_compaction_cancel()
    await case_prompt_waits_for_compaction()
    await case_compact_guard_while_streaming()
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