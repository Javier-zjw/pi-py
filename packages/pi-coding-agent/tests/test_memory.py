"""长期记忆测试（memory.py + 内置扩展 memory）：不联网、不装东西。

    python tests/test_memory.py
    pytest tests/test_memory.py

覆盖：增删改、写满拒绝、去重、去密钥、一条太长、找不到 / 不唯一、手写的行原样保留、按序号删；
拼进系统提示的一段（空的不加、手改得太长截断）；接进会话：记下的下次会话才带上（这次系统提示不变）、
不用确认、计划模式能记、子智能体看得到记忆但不能写、关掉开关、片段出错进诊断；/memory、/remember。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))                     # packages/pi-coding-agent
sys.path.insert(0, str(HERE.parents[2] / "pi-agent"))
sys.path.insert(0, str(HERE.parents[2] / "pi-ai"))
sys.path.insert(0, str(HERE.parent))

from fakes import FAKE_MODEL, Sandbox, fake_models
from pi_ai import TextContent, ToolCall
from pi_coding_agent import (
    AgentSession,
    ModelRuntime,
    SessionManager,
    create_agent_session_services,
)
from pi_coding_agent import memory as store
from test_subagents import is_child, make_session, task_call, tool_results

FAILURES: list[str] = []


def under_pytest() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILURES.append(f"{name}: {detail}")
    print(f"  FAIL {name} {detail}")
    if under_pytest():
        raise AssertionError(f"{name}: {detail}")


def text_of(message) -> str:
    return "".join(getattr(c, "text", "") or "" for c in message.content)


def raises(fn, *args) -> str:
    try:
        fn(*args)
    except store.MemoryEditError as exc:
        return str(exc)
    return ""


def test_store() -> None:
    with Sandbox() as root:
        agent, cwd = root / "agent", root / "proj"
        cwd.mkdir()
        user = store.load(agent, "user", cwd)
        check("位置: 全局在 memory/MEMORY.md，项目按目录名分开", user.path == agent / "memory" / "MEMORY.md"
              and store.load(agent, "project", cwd).path.parent.name.startswith("--"))
        text, redacted = store.add(user, "  - 回答用中文\n 简洁一点 ", cwd, 100)
        check("添加: 并成一行、去掉列表符号、带日期", text == "回答用中文 简洁一点" and not redacted
              and user.lines[-1].startswith("- [") and user.lines[-1].endswith("] 回答用中文 简洁一点"),
              str(user.lines))
        on_disk = user.path.read_text()
        check("添加: 第一次写入时加上说明头", on_disk.startswith("# pi 记忆 · 全局") and "<!--" in on_disk)
        check("添加: 重复的拒绝", "already remembered" in raises(store.add, user, "回答用中文 简洁一点", cwd, 100))
        check("添加: 一条太长拒绝", "at most" in raises(store.add, user, "x" * 600, cwd, 10_000))
        full = raises(store.add, user, "y" * 90, cwd, 100)
        check("上限: 写满了拒绝，并要求先合并、列出现有条目", "memory is full" in full
              and "replace" in full and "回答用中文" in full, full)

        project = store.load(agent, "project", cwd)
        _, redacted = store.add(project, "部署用 token=ghp_abcdefghijklmnopqrstuvwx1234 推送", cwd, 1000)
        check("去密钥: 换成 [REDACTED]", redacted and "ghp_" not in project.path.read_text()
              and "[REDACTED]" in project.entries[0], project.entries[0])
        _, redacted = store.add(project, "OPENAI key 是 sk-proj-abcdefghijklmnop12345", cwd, 1000)
        check("去密钥: sk- 开头的也换", redacted and "sk-proj" not in project.entries[1])
        store.add(project, "测试用 uv run pytest -x", cwd, 1000)
        old, _, _ = store.replace(project, "uv run pytest", "测试用 uv run pytest -x -q（要先 uv sync）",
                                    cwd, 1000)
        check("修改: 按片段找到那条", old == "测试用 uv run pytest -x" and "uv sync" in project.entries[-1],
              str(project.entries))
        check("修改: 不唯一时要更长的片段", "use a longer" in raises(store.replace, project, "[REDACTED]",
                                                                 "x", cwd, 1000))
        check("修改: 找不到时列出现有条目", "Current entries" in raises(store.remove, project, "不存在", cwd))

        project.lines.insert(0, "## 我手写的标题")
        project.lines.append("我手写的一段说明")
        store.save(project, cwd)
        store.remove(project, "uv sync", cwd)
        reloaded = store.load(agent, "project", cwd)
        check("删除: 手写的行原样保留", "## 我手写的标题" in reloaded.lines
              and "我手写的一段说明" in reloaded.lines and len(reloaded.entries) == 2, str(reloaded.lines))
        removed = store.remove_at(reloaded, 1, cwd)
        check("按序号删", "[REDACTED]" in removed and len(store.load(agent, "project", cwd).entries) == 1)
        check("按序号删: 没有这条", "没有第 9 条" in raises(store.remove_at, reloaded, 9, cwd))

        block = store.prompt_block([store.load(agent, s, cwd) for s in store.SCOPES],
                                   store.DEFAULT_LIMITS, home=str(root))
        check("提示: 两份都在、标明不是用户指令", block is not None and block.startswith("<memory>")
              and "About the user" in block and "About this project" in block
              and "not instructions from the user" in block and "回答用中文" in block, block or "")
        check("提示: 路径里的家目录写成 ~", "~/agent/memory/MEMORY.md" in (block or ""), block or "")
        empty = Sandbox().__enter__()
        check("提示: 都没有条目时不加", store.prompt_block(
            [store.load(empty / "a", s, empty) for s in store.SCOPES], store.DEFAULT_LIMITS) is None)
        long = store.load(agent, "user", cwd)
        long.lines += [f"- 手写的第 {i} 条，" + "很长" * 40 for i in range(60)]
        block = store.prompt_block([long], {"user": 2000})
        check("提示: 手改得太长只放前面一部分", block is not None and "more entries in" in block
              and len(block) < 4000, str(len(block or "")))


def make(root: Path, script: list, settings: dict | None = None, tools=("read",)):
    if settings:
        (root / ".pi").mkdir(exist_ok=True)
        (root / ".pi" / "settings.json").write_text(json.dumps(settings))
    models, provider = fake_models(script)
    services = create_agent_session_services(cwd=root, agent_dir=root / "agent",
                                             model_runtime=ModelRuntime(models, root / "agent"))
    return AgentSession(services, SessionManager.in_memory(root), model=FAKE_MODEL, tools=list(tools)), provider


def results(session) -> dict:
    return {m.tool_call_id: m for m in session.messages if getattr(m, "role", "") == "toolResult"}


async def case_session() -> None:
    with Sandbox() as root:
        session, provider = make(root, [
            [ToolCall(id="m1", name="memory", arguments={"action": "add", "text": "测试用 make test"}),
             ToolCall(id="m2", name="memory", arguments={"action": "add", "scope": "user",
                                                         "text": "用户喜欢简短的回答"})],
            [TextContent(text="记下了")],
            [TextContent(text="第二问")],
        ])
        asked: list = []

        async def handler(request):
            asked.append(request)
            return {"decision": "deny"}

        session.permission_handler = handler
        first_prompt = session.agent.state.system_prompt
        check("会话: 没有记忆时系统提示不带 <memory>", "<memory>" not in first_prompt)
        await session.prompt("记住测试命令")
        got = results(session)
        check("工具: 记进项目和全局、不用确认", not got["m1"].is_error and not got["m2"].is_error
              and not asked and "Saved to project memory" in text_of(got["m1"]), text_of(got["m1"]))
        check("工具: details 给界面", got["m1"].details["action"] == "add"
              and got["m1"].details["text"] == "测试用 make test" and got["m2"].details["scope"] == "user")
        await session.prompt("再问一句")
        check("快照: 这次会话的系统提示不变（不破坏前缀缓存）",
              provider.calls[-1]["context"].system_prompt == provider.calls[0]["context"].system_prompt
              and "make test" not in provider.calls[-1]["context"].system_prompt)

        later, provider2 = make(root, [[TextContent(text="好")]])
        await later.prompt("你好")
        prompt = provider2.calls[0]["context"].system_prompt
        check("下次会话: 系统提示带上两份记忆", "<memory>" in prompt and "make test" in prompt
              and "用户喜欢简短的回答" in prompt, prompt[-600:])
        check("下次会话: 记忆在基线里（换模式重建后还在）", "make test" in later._base_system_prompt)

        planner, plan_provider = make(root, [[TextContent(text="先调研")]])
        await planner.set_mode("plan")
        await planner.prompt("规划一下")
        plan_tools = {t.name for t in plan_provider.calls[0]["context"].tools}
        check("计划模式: 也能记（发给模型的工具里有 memory）", "memory" in plan_tools
              and "submit_plan" in plan_tools and "write" not in plan_tools, str(plan_tools))

        listing = await later.run_command("memory", "")
        check("/memory: 列出两份、带序号和位置", "全局（" in listing and "项目（" in listing
              and "1. [" in listing and "2. [" in listing and "make test" in listing, listing)
        out = await later.run_command("remember", "--user 提交信息用中文")
        check("/remember --user: 记进全局", "已记住（全局）：提交信息用中文" in out
              and "提交信息用中文" in store.load(root / "agent", "user", root).path.read_text(), out)
        out = await later.run_command("remember", "")
        check("/remember: 空的给用法", "用法" in out)
        listing = await later.run_command("memory", "")
        number = next(line.split(".")[0].strip() for line in listing.splitlines() if "make test" in line)
        out = await later.run_command("memory", f"forget {number}")
        check("/memory forget: 按序号删", "已删除（项目）" in out and "make test" in out
              and not store.load(root / "agent", "project", root).entries, out)
        check("/memory forget: 序号不对", "没有第 99 条" in await later.run_command("memory", "forget 99"))

    with Sandbox() as root:
        store.add(store.load(root / "agent", "project", root), "旧的记忆", root, 1000)
        session, _ = make(root, [[ToolCall(id="m1", name="memory", arguments={"action": "add",
                                                                             "text": "x"})],
                                 [TextContent(text="好")]], settings={"memory": {"enabled": False}})
        await session.prompt("记")
        check("关掉: 不读也不写", "旧的记忆" not in session._base_system_prompt
              and results(session)["m1"].is_error and "turned off" in text_of(results(session)["m1"]))
        check("关掉: 命令也说明", "已关闭" in await session.run_command("memory", ""))

    with Sandbox() as root:
        session, _ = make(root, [])
        api = session.resources.get_extension_api()
        api.system_prompt_section(lambda s: (_ for _ in ()).throw(RuntimeError("坏片段")))
        api.system_prompt_section(lambda s: "<extra>好片段</extra>")
        again = AgentSession(session.services, SessionManager.in_memory(root), model=FAKE_MODEL, tools=[])
        check("片段 API: 出错的跳过、进诊断，别的照常", "<extra>好片段</extra>" in again._base_system_prompt
              and any("坏片段" in d for d in session.services.diagnostics))


async def case_subagent() -> None:
    with Sandbox() as cwd:
        store.add(store.load(cwd / "agent", "project", cwd), "数据库迁移在 db/migrations", cwd, 1000)

        async def respond(context):
            if not is_child(context):
                if not tool_results(context):
                    return [task_call("g1", "查迁移", "找迁移文件", agent="general")], None
                return [TextContent(text="好")], None
            return [TextContent(text="报告")], None

        session, router = make_session(cwd, respond)
        await session.prompt("派一个")
        child = next(c for c in router.calls if is_child(c))
        check("子智能体: 系统提示里有记忆", "db/migrations" in child.system_prompt)
        check("子智能体: 不能写记忆（主对话的事）", "memory" not in {t.name for t in child.tools})


def test_session() -> None:
    asyncio.run(case_session())


def test_subagent() -> None:
    asyncio.run(case_subagent())


async def main() -> int:
    print("存储");         test_store()
    print("接进会话");     await case_session()
    print("子智能体");     await case_subagent()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} 处失败：")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("全部通过 —— 长期记忆可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
