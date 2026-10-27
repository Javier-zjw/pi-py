"""/reflect 测试（reflect.py + 内置扩展 reflect）：不联网，假模型按剧本回答。

    python tests/test_reflect.py
    pytest tests/test_reflect.py

覆盖：解析（JSON、代码围栏、坏格式、不认识的种类、技能名不合法、最多 5 条、模型出错）；
请求（前缀和正常对话一样、tool_choice=none、回顾不进会话）；没有界面时列出提议、/reflect apply
保存（记忆、AGENTS.md 新开一节再往下追加、技能、同名技能不覆盖）；有界面时逐条确认（保存 / 跳过 /
输入改过的内容 / Esc 全部跳过）；空会话、模型出错、记忆关闭。
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
from pi_ai import AssistantMessage, TextContent, ToolCall
from pi_coding_agent import (
    AgentSession,
    ModelRuntime,
    SessionManager,
    create_agent_session_services,
)
from pi_coding_agent import memory as store
from pi_coding_agent import reflect as R

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


def reply(data) -> AssistantMessage:
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return AssistantMessage(content=[TextContent(text=text)], stop_reason="stop")


PROPOSALS = {"summary": "修了登录 bug，中途测试命令试错了三次", "proposals": [
    {"kind": "memory", "scope": "project", "text": "测试要用 uv run pytest -x，直接 pytest 找不到包",
     "why": "试错了三次", "evidence": "第 2-4 次 bash 调用"},
    {"kind": "memory", "scope": "user", "text": "用户希望提交信息用中文", "why": "用户纠正过",
     "evidence": "用户说「提交信息用中文」"},
    {"kind": "agents_md", "text": "改数据库模型后要跑 make migrate", "why": "忘了迁移导致测试失败",
     "evidence": "test_login 报 no such column"},
    {"kind": "skill", "name": "release", "description": "发版本时用：改版本号、打 tag、推送",
     "body": "1. 改 pyproject.toml 的版本号\n2. git tag vX.Y.Z\n3. git push --tags",
     "why": "这次手工做了一遍", "evidence": "最后几步"},
]}


def test_parse() -> None:
    result = R.parse(reply(PROPOSALS))
    check("解析: 摘要和四条提议", result.summary.startswith("修了登录") and [p.kind for p in result.proposals]
          == ["memory", "memory", "agents_md", "skill"] and result.proposals[1].scope == "user",
          str(result))
    fenced = R.parse(reply("好的，回顾如下：\n```json\n" + json.dumps(PROPOSALS) + "\n```"))
    check("解析: 容忍代码围栏和前面的话", len(fenced.proposals) == 4 and not fenced.error)
    bad = R.parse(reply("我觉得这次挺顺利的"))
    check("解析: 不是 JSON 时报错并留下原文", bad.error and "不是 JSON" in bad.error and "挺顺利" in bad.raw)
    filtered = R.parse(reply({"summary": "x", "proposals": [
        {"kind": "hook", "text": "a"}, {"kind": "memory", "text": ""},
        {"kind": "skill", "name": "Bad Name", "description": "d", "body": "b"},
        {"kind": "skill", "name": "ok-name", "description": "", "body": "b"},
        *[{"kind": "memory", "text": f"事实 {i}"} for i in range(8)]]}))
    check("解析: 不认识的种类、空内容、技能名 / 描述不合格的丢掉，最多 5 条",
          [p.text for p in filtered.proposals] == [f"事实 {i}" for i in range(5)], str(filtered.proposals))
    error = R.parse(AssistantMessage(stop_reason="error", error_message="HTTP 429"))
    check("解析: 模型出错", error.error == "HTTP 429")


def make(root: Path, script: list):
    models, provider = fake_models(script)
    services = create_agent_session_services(cwd=root, agent_dir=root / "agent",
                                             model_runtime=ModelRuntime(models, root / "agent"))
    session = AgentSession(services, SessionManager.create(root, root / "agent"), model=FAKE_MODEL,
                           tools=["read", "bash"])
    return session, provider


def script_with(conversation: list, *reflections) -> list:
    return conversation + [[TextContent(text=r if isinstance(r, str) else json.dumps(r, ensure_ascii=False))]
                           for r in reflections]


CONVERSATION = [[ToolCall(id="b1", name="bash", arguments={"command": "echo hi"})],
                [TextContent(text="修好了")]]


async def case_no_ui() -> None:
    with Sandbox() as root:
        session, provider = make(root, script_with(CONVERSATION, PROPOSALS))
        out = await session.run_command("reflect", "")
        check("空会话: 没东西可回顾", "还没有内容" in out and not provider.calls, out)
        await session.prompt("修一下登录")
        before = len(session.messages)
        entries = len(session.session.get_branch())
        out = await session.run_command("reflect", "")
        request = provider.calls[-1]
        normal = provider.calls[-2]["context"]
        check("请求: 系统提示、工具和正常对话一样（命中前缀缓存）",
              request["context"].system_prompt == normal.system_prompt
              and [t.name for t in request["context"].tools] == [t.name for t in normal.tools])
        sent = request["context"].messages
        check("请求: 前面的消息原样带上，末尾一条回顾指令", len(sent) == len(normal.messages) + 2
              and "<reflect>" in sent[-1].content and sent[-2].role == "assistant", str(len(sent)))
        check("请求: 不让调工具", request["options"].tool_choice == "none")
        check("不进会话: 消息和会话条目都没变", len(session.messages) == before
              and len(session.session.get_branch()) == entries)
        check("没有界面: 列出提议和用法", "回顾：修了登录 bug" in out and "1. 记忆（项目）：测试要用 uv run" in out
              and "4. 技能 release" in out and "/reflect apply" in out and "理由：试错了三次" in out, out)

        out = await session.run_command("reflect", "apply 1 3,4")
        memory = store.load(root / "agent", "project", root)
        agents = (root / "AGENTS.md").read_text()
        skill = root / ".pi" / "skills" / "release" / "SKILL.md"
        check("apply: 记忆写进去了", any("uv run pytest -x" in e for e in memory.entries), out)
        check("apply: AGENTS.md 新开一节", agents.startswith("## 经验（/reflect）\n- 改数据库模型后要跑 make migrate"),
              agents)
        check("apply: 新建技能（带 front matter）", skill.read_text().startswith(
            "---\nname: release\ndescription: 发版本时用") and "git tag" in skill.read_text())
        check("apply: 每条一行结论", out.count("✓") == 3, out)
        out = await session.run_command("reflect", "apply 9")
        check("apply: 保存过的从待保存里拿掉（只剩 1 条）", "没有第 9 条（共 1 条）" in out, out)
        out = await session.run_command("reflect", "apply 1")
        check("apply: 剩下那条是全局记忆", "✓ 记进全局记忆：用户希望提交信息用中文" in out, out)
        check("apply: 都保存完了", "没有待保存" in await session.run_command("reflect", "apply all"))


async def case_appends() -> None:
    with Sandbox() as root:
        (root / "AGENTS.md").write_text("# 项目说明\n\n别动 vendor 目录。\n")
        R.append_agents_md(root, "第一条")
        R.append_agents_md(root, "- 第二条")
        text = (root / "AGENTS.md").read_text()
        check("AGENTS.md: 原文不动、同一节往下追加", text == "# 项目说明\n\n别动 vendor 目录。\n\n## 经验（/reflect）\n"
              "- 第一条\n- 第二条\n", repr(text))
        R.write_skill(root, "release", "d", "body")
        try:
            R.write_skill(root, "release", "d2", "body2")
            overwritten = True
        except FileExistsError:
            overwritten = False
        check("技能: 同名不覆盖", not overwritten and "body2" not in
              (root / ".pi" / "skills" / "release" / "SKILL.md").read_text())


async def case_ui() -> None:
    with Sandbox() as root:
        session, _ = make(root, script_with(CONVERSATION, PROPOSALS, PROPOSALS))
        asked: list = []
        answers = [[{"answer": "保存"}, {"answer": "跳过"},
                    {"answer": "迁移命令是 make db-migrate", "custom": True},
                    {"answer": "自己写的技能", "custom": True}]]

        async def user_input(questions):
            asked.append(questions)
            return answers.pop(0) if answers else None

        session.user_input = user_input
        await session.prompt("修一下登录")
        out = await session.run_command("reflect", "")
        questions = asked[0]
        check("有界面: 逐条问（标题、理由、依据、保存 / 跳过）", len(questions) == 4
              and questions[0]["header"] == "回顾 · 记忆" and "输入改过的内容" in questions[0]["question"] and "依据：第 2-4 次 bash 调用" in questions[0]["question"]
              and [o["label"] for o in questions[0]["options"]] == ["保存", "跳过"]
              and "git tag" in questions[3]["question"], json.dumps(questions, ensure_ascii=False)[:300])
        check("保存 / 跳过", any("uv run pytest" in e for e in store.load(root / "agent", "project", root).entries)
              and not store.load(root / "agent", "user", root).entries and "跳过：记忆（全局）" in out, out)
        agents = root / "AGENTS.md"
        check("输入改过的内容: 按改过的保存", agents.exists()
              and "- 迁移命令是 make db-migrate" in agents.read_text())
        check("技能不能直接改写: 跳过并说明", not (root / ".pi" / "skills" / "release").exists()
              and "技能不能直接改写" in out, out)
        out = await session.run_command("reflect", "")
        check("Esc: 剩下的全部跳过", "跳过了剩下的 4 条" in out, out)


async def case_failures() -> None:
    with Sandbox() as root:
        session, _ = make(root, script_with(CONVERSATION, "我觉得挺好的",
                                            {"summary": "很顺利", "proposals": []}))
        await session.prompt("修一下")
        out = await session.run_command("reflect", "")
        check("模型没按格式: 说明并给出开头", "回顾没成功" in out and "我觉得挺好的" in out, out)
        out = await session.run_command("reflect", "")
        check("没有提议: 正常结果", "回顾：很顺利" in out and "没有发现值得长期保留的经验" in out, out)

    with Sandbox() as root:
        (root / ".pi").mkdir()
        (root / ".pi" / "settings.json").write_text(json.dumps({"memory": {"enabled": False}}))
        session, _ = make(root, script_with(CONVERSATION, PROPOSALS))
        await session.prompt("修一下")
        await session.run_command("reflect", "")
        out = await session.run_command("reflect", "apply 1")
        check("记忆关闭: 不写并说明", "长期记忆已关闭" in out
              and not store.load(root / "agent", "project", root).entries, out)


def test_no_ui() -> None:
    asyncio.run(case_no_ui())


def test_appends() -> None:
    asyncio.run(case_appends())


def test_ui() -> None:
    asyncio.run(case_ui())


def test_failures() -> None:
    asyncio.run(case_failures())


async def main() -> int:
    print("解析");         test_parse()
    print("没有界面");     await case_no_ui()
    print("写文件");       await case_appends()
    print("有界面");       await case_ui()
    print("出错");         await case_failures()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} 处失败：")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("全部通过 —— /reflect 可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
