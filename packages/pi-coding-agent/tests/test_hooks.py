"""hooks 测试（hooks.py + 内置扩展 hooks）：不联网；钩子是真的子进程（用 Python 写的小脚本，不依赖 jq）。

    python tests/test_hooks.py
    pytest tests/test_hooks.py

覆盖：matcher 规则、配置合并去重 / disableAllHooks / 不支持的标出来、参数换成 Claude Code 的样子；
PreToolUse（退出码 2 拦下、deny / allow / ask 和权限的配合、deny 规则优先、updatedInput、
additionalContext）、PostToolUse（反馈给模型、只在成功时跑、continue:false 停下）、UserPromptSubmit
（拦下不发、输出作为补充上下文）、Stop（让模型继续、stop_hook_active、次数上限）、SessionStart
（进系统提示、只跑一次）、出错 / 超时只提醒、/hooks、/hooks import。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import textwrap
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
from pi_coding_agent import hooks as H
from pi_coding_agent.mentions import user_words

FAILURES: list[str] = []
PY = f'"{sys.executable}"'


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
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    return "".join(getattr(c, "text", "") or "" for c in content)


@contextlib.contextmanager
def home(path: Path):
    old = os.environ.get("HOME")
    os.environ["HOME"] = str(path)
    try:
        yield path
    finally:
        if old is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old


def script(root: Path, name: str, body: str) -> str:
    """写一个钩子脚本：stdin 的 JSON 存进 log/<name>.jsonl，然后跑 body（可用变量 data）"""
    path = root / "hooks" / f"{name}.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(f"""\
        import json, sys, os
        data = json.load(sys.stdin)
        os.makedirs({str(root / 'log')!r}, exist_ok=True)
        with open(os.path.join({str(root / 'log')!r}, {name + '.jsonl'!r}), "a") as f:
            f.write(json.dumps(data) + "\\n")
        """) + textwrap.dedent(body))
    return f'{PY} "{path}"'


def calls(root: Path, name: str) -> list[dict]:
    path = root / "log" / f"{name}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def hook(event: str, command: str, matcher: str = "", **extra) -> dict:
    return {event: [{"matcher": matcher, "hooks": [{"type": "command", "command": command, **extra}]}]}


def settings(root: Path, hooks: dict, where: str = "project", **more) -> None:
    path = root / "agent" / "settings.json" if where == "global" else root / ".pi" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(path.read_text()) if path.exists() else {}
    for event, groups in hooks.items():
        data.setdefault("hooks", {}).setdefault(event, []).extend(groups)
    data.update(more)
    path.write_text(json.dumps(data))


def make(root: Path, script_: list, tools=("read", "bash", "write", "edit")):
    models, provider = fake_models(script_)
    services = create_agent_session_services(cwd=root, agent_dir=root / "agent",
                                             model_runtime=ModelRuntime(models, root / "agent"))
    session = AgentSession(services, SessionManager.in_memory(root), model=FAKE_MODEL, tools=list(tools))
    notices: list = []
    session.subscribe(lambda e: notices.append(e) if e.type == "notice" else None)
    return session, provider, notices


def results(session) -> dict:
    return {m.tool_call_id: m for m in session.messages if getattr(m, "role", "") == "toolResult"}


def test_units() -> None:
    names = ["Edit", "edit"]
    check("matcher: 空 / * 匹配全部", H.matches("", names) and H.matches("*", names))
    check("matcher: 精确名字和 | , 列表", H.matches("Edit|Write", names) and H.matches("Write, Edit", names)
          and not H.matches("Write", names) and not H.matches("Edi", names))
    check("matcher: 其它字符当正则、不锚定", H.matches("mcp__github__.*", ["mcp__github__search"])
          and H.matches("Edit.*", ["NotebookEdit"]) and not H.matches("^Edit$", ["NotebookEdit"])
          and not H.matches("(", names))
    data = H.tool_input("edit", {"path": "src/a.py", "old_text": "x", "new_text": "y"}, "/work")
    check("输入: 加上 Claude Code 的字段（file_path 绝对路径、old_string）", data["file_path"] == "/work/src/a.py"
          and data["old_string"] == "x" and data["new_string"] == "y" and data["path"] == "src/a.py")
    back = H.pi_arguments("edit", {"file_path": "/work/b.py", "new_string": "z"}, {"path": "a.py",
                                                                                 "old_text": "x"})
    check("updatedInput: 换回 pi 的字段", back == {"path": "/work/b.py", "old_text": "x", "new_text": "z"},
          str(back))
    check("工具名: 换成 Claude Code 的", H.claude_name("bash") == "Bash" and H.claude_name("find") == "Glob"
          and H.claude_name("mcp__x__y") == "mcp__x__y")

    with Sandbox() as root:
        (root / "g.json").write_text(json.dumps({"hooks": {
            **hook("PreToolUse", "echo a", "Bash"), "Notification": [{"hooks": [{"command": "x"}]}]}}))
        (root / "p.json").write_text(json.dumps({"hooks": {
            **hook("PreToolUse", "echo a", "Bash"),
            "PostToolUse": [{"hooks": [{"type": "http", "url": "https://x"}, {"command": "echo b"}]}]}}))
        handlers, _ = H.load_handlers([(root / "g.json", "全局"), (root / "p.json", "项目")])
        events = [(h.event, h.command, h.supported) for h in handlers]
        check("配置: 两份合并、重复的只留一个、不支持的标出来", events == [
            ("PreToolUse", "echo a", True), ("Notification", "x", False),
            ("PostToolUse", "", False), ("PostToolUse", "echo b", True)], str(events))
        (root / "p.json").write_text(json.dumps({"disableAllHooks": True}))
        handlers, problems = H.load_handlers([(root / "g.json", "全局"), (root / "p.json", "项目")])
        check("配置: disableAllHooks 全部关掉", not handlers and any("disableAllHooks" in p for p in problems))


async def case_pre_tool() -> None:
    with Sandbox() as root:
        # 命令都是无害的 echo：钩子没生效时也不会真的删东西、推代码
        settings(root, {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"command": script(root, "guard", """
                    cmd = data["tool_input"].get("command", "")
                    if "DANGER" in cmd:
                        print("别删库", file=sys.stderr); sys.exit(2)
                    if "BUILD" in cmd:
                        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                            "permissionDecision": "allow", "additionalContext": "构建跑在 docker 里"}}))
                    if "PUSH" in cmd:
                        print(json.dumps({"hookSpecificOutput": {"permissionDecision": "allow"}}))
                    """)}]},
                {"matcher": "Write", "hooks": [{"command": script(root, "rewrite", """
                    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                        "updatedInput": {"content": "改过的内容\\n"}}}))
                    """)}]},
                {"matcher": "Read", "hooks": [{"command": script(root, "ask", """
                    print(json.dumps({"hookSpecificOutput": {"permissionDecision": "ask"}}))
                    """)}]},
            ]}, permissions={"deny": ["bash(echo PUSH*)"]})
        session, _, notices = make(root, [
            [ToolCall(id="b1", name="bash", arguments={"command": "echo DANGER > marker.txt"}),
             ToolCall(id="b2", name="bash", arguments={"command": "echo BUILD > built.txt"}),
             ToolCall(id="b3", name="bash", arguments={"command": "echo PUSH > pushed.txt"}),
             ToolCall(id="w1", name="write", arguments={"path": "out.txt", "content": "原来的"}),
             ToolCall(id="r1", name="read", arguments={"path": "out.txt"})],
            [TextContent(text="好")],
        ])
        asked: list = []

        async def handler(request):
            asked.append(request["tool"])
            return {"decision": "allow"}

        session.permission_handler = handler
        await session.prompt("干活")
        got = results(session)
        check("退出码 2: 拦下，原因给模型", got["b1"].is_error and "别删库" in text_of(got["b1"])
              and "PreToolUse hook" in text_of(got["b1"]), text_of(got["b1"]))
        check("拦下: 给用户一行说明", any("钩子拦下了 bash" in n.text for n in notices), str(notices))
        logged = calls(root, "guard")
        check("输入: tool_name 是 Claude Code 的名字、带 session / cwd / 事件名",
              logged and logged[0]["tool_name"] == "Bash" and logged[0]["hook_event_name"] == "PreToolUse"
              and logged[0]["cwd"] == str(root)
              and any(c["tool_input"]["command"] == "echo DANGER > marker.txt" for c in logged)
              and logged[0]["permission_mode"] == "default", str(logged[:1]))
        check("matcher: 只在匹配的工具上跑", len(logged) == 3, str(len(logged)))
        check("拦下的真没执行", not (root / "marker.txt").exists())
        check("allow: 不弹确认（写文件的命令本来要问）", "bash" not in asked and not got["b2"].is_error
              and (root / "built.txt").exists(), str(asked))
        check("additionalContext: 跟在工具结果后面", "构建跑在 docker 里" in text_of(got["b2"]),
              text_of(got["b2"]))
        check("deny 规则优先于钩子的 allow", got["b3"].is_error and "permission settings" in text_of(got["b3"])
              and not (root / "pushed.txt").exists(),
              text_of(got["b3"]))
        check("updatedInput: 改了写进去的内容（file_path 是绝对路径）",
              (root / "out.txt").read_text() == "改过的内容\n"
              and calls(root, "rewrite")[0]["tool_input"]["file_path"] == str(root / "out.txt"))
        check("ask: 只读工具也弹确认", "read" in asked and "write" in asked, str(asked))


async def case_post_tool() -> None:
    with Sandbox() as root:
        (root / "a.py").write_text("x=1\n")
        settings(root, {**hook("PostToolUse", script(root, "fmt", """
            if data["tool_name"] == "Edit":
                print("a.py: E225 missing whitespace", file=sys.stderr); sys.exit(2)
            print(json.dumps({"decision": "block", "reason": "写完要跑 make lint",
                              "hookSpecificOutput": {"additionalContext": "lint 配置在 setup.cfg"}}))
            """), "Edit|Write")}, where="global")
        session, provider, notices = make(root, [
            [ToolCall(id="e1", name="edit", arguments={"path": "a.py", "old_text": "x=1", "new_text": "x =1"}),
             ToolCall(id="w1", name="write", arguments={"path": "b.txt", "content": "hi"}),
             ToolCall(id="e2", name="edit", arguments={"path": "a.py", "old_text": "不存在", "new_text": "y"})],
            [TextContent(text="好")],
        ])
        await session.prompt("改")
        got = results(session)
        check("退出码 2: stderr 附在结果后面给模型（工具已经执行）", "[PostToolUse hook feedback]" in text_of(got["e1"])
              and "E225" in text_of(got["e1"]) and not got["e1"].is_error, text_of(got["e1"]))
        check("decision=block + additionalContext: 原因和补充都附上", "写完要跑 make lint" in text_of(got["w1"])
              and "lint 配置在 setup.cfg" in text_of(got["w1"]), text_of(got["w1"]))
        logged = calls(root, "fmt")
        check("只在工具成功时跑（失败的 edit 不触发）", len(logged) == 2, str(len(logged)))
        by_tool = {c["tool_name"]: c for c in logged}
        check("tool_response: 带 filePath 和内容", by_tool["Edit"]["tool_response"]["filePath"] == str(root / "a.py")
              and by_tool["Write"]["tool_response"]["type"] == "create", str(logged))
        check("全局配置也生效", bool(logged))

    with Sandbox() as root:
        settings(root, hook("PostToolUse", script(root, "halt", """
            print(json.dumps({"continue": False, "stopReason": "构建坏了，先停"}))
            """), "Bash"))
        session, provider, notices = make(root, [
            [ToolCall(id="b1", name="bash", arguments={"command": "echo hi"})],
            [TextContent(text="不该到这")],
        ])
        await session.prompt("跑")
        check("continue:false: 这一轮停下、给用户说明", len(provider.calls) == 1
              and any("构建坏了，先停" in n.text for n in notices), str(len(provider.calls)))


async def case_prompt() -> None:
    with Sandbox() as root:
        settings(root, hook("UserPromptSubmit", script(root, "ups", """
            if "密码" in data["prompt"]:
                print("不要把密码发给模型", file=sys.stderr); sys.exit(2)
            print("当前分支：main")
            """)))
        session, provider, notices = make(root, [[TextContent(text="好")]])
        out = await session.prompt("我的密码是 123")
        check("拦下: 不发给模型、给用户说明", out == [] and not provider.calls
              and any("不要把密码发给模型" in n.text for n in notices), str(notices))
        await session.prompt("看看 git 状态")
        sent = provider.calls[0]["context"].messages[-1]
        check("输出: 作为补充上下文跟在原话后面", user_words(sent) == "看看 git 状态"
              and "<user-prompt-submit-hook>" in text_of(sent) and "当前分支：main" in text_of(sent),
              text_of(sent))
        check("输入: prompt 是原话", calls(root, "ups")[-1]["prompt"] == "看看 git 状态")


async def case_stop() -> None:
    with Sandbox() as root:
        settings(root, hook("Stop", script(root, "stop", """
            if not data["stop_hook_active"]:
                print("测试还没跑", file=sys.stderr); sys.exit(2)
            """)))
        session, provider, notices = make(root, [[TextContent(text="改完了")], [TextContent(text="测试也跑了")]])
        await session.prompt("改代码")
        logged = calls(root, "stop")
        check("退出码 2: 模型接着做（原因作为消息插进去）", len(provider.calls) == 2
              and "测试还没跑" in text_of(provider.calls[1]["context"].messages[-1]), str(len(provider.calls)))
        check("stop_hook_active: 第一次 false、继续过之后 true；带最后一条回复",
              [c["stop_hook_active"] for c in logged] == [False, True]
              and logged[0]["last_assistant_message"] == "改完了", str(logged))

    with Sandbox() as root:
        settings(root, hook("Stop", script(root, "nag", """
            print(json.dumps({"decision": "block", "reason": "再检查一遍"}))
            """)))
        session, provider, notices = make(root, [[TextContent(text=f"第{i}次")] for i in range(10)])
        await session.prompt("做")
        check("上限: 一次运行最多续 5 次", len(provider.calls) == 6
              and any("不再继续" in n.text for n in notices), str(len(provider.calls)))


async def case_session_start() -> None:
    with Sandbox() as root:
        settings(root, hook("SessionStart", script(root, "start", """
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                              "additionalContext": "今天值班的是小王"}}))
            """)))
        session, provider, _ = make(root, [[TextContent(text="一")], [TextContent(text="二")]])
        await session.prompt("第一问")
        await session.prompt("第二问")
        logged = calls(root, "start")
        prompts = [c["context"].system_prompt for c in provider.calls]
        check("进这个会话的系统提示、每轮都在且不变", all("今天值班的是小王" in p for p in prompts)
              and prompts[0] == prompts[1])
        check("只跑一次，source=startup", len(logged) == 1 and logged[0]["source"] == "startup", str(logged))


async def case_failures() -> None:
    with Sandbox() as root:
        settings(root, {
            **hook("PreToolUse", script(root, "broken", """
                print("hook crashed", file=sys.stderr); sys.exit(1)
                """), "Bash"),
            "PostToolUse": [{"matcher": "Bash", "hooks": [{"command": script(root, "slow", """
                import time; time.sleep(5)
                """), "timeout": 0.5}]}]})
        session, _, notices = make(root, [
            [ToolCall(id="b1", name="bash", arguments={"command": "echo ok"})], [TextContent(text="好")]])
        await session.prompt("跑")
        got = results(session)["b1"]
        texts = [n.text for n in notices]
        check("出错（退出码 1）: 不拦，给用户一行", not got.is_error and "ok" in text_of(got)
              and any("出错：hook crashed" in t for t in texts), str(texts))
        check("超时: 不拦，说明超时", any("超时" in t for t in texts), str(texts))


async def case_commands() -> None:
    with Sandbox() as root, home(root / "home") as fake_home:
        settings(root, hook("PreToolUse", "echo pi-own", "Bash"))
        claude = fake_home / ".claude" / "settings.json"
        claude.parent.mkdir(parents=True)
        claude.write_text(json.dumps({"model": "x", "hooks": {
            **hook("PostToolUse", "prettier --write", "Edit|Write"),
            "Notification": [{"hooks": [{"type": "command", "command": "say hi"}]}]}}))
        local = root / ".claude" / "settings.local.json"
        local.parent.mkdir(parents=True)
        local.write_text(json.dumps({"hooks": hook("Stop", "make test")}))
        session, _, _ = make(root, [])
        before = await session.run_command("hooks", "")
        check("/hooks: 列出配置的钩子", "PreToolUse" in before and "echo pi-own" in before
              and "匹配 Bash" in before and "prettier" not in before, before)
        out = await session.run_command("hooks", "import")
        global_settings = json.loads((root / "agent" / "settings.json").read_text())
        project_settings = json.loads((root / ".pi" / "settings.json").read_text())
        check("/hooks import: ~/.claude 的进全局、项目的进项目，别的键不动", "PostToolUse" in global_settings["hooks"]
              and "model" not in global_settings and "Stop" in project_settings["hooks"]
              and "PreToolUse" in project_settings["hooks"], out)
        again = await session.run_command("hooks", "import")
        check("/hooks import: 再导一次不重复", "没有新的钩子" in again, again)
        after = await session.run_command("hooks", "")
        check("/hooks: 不支持的事件标出来", "prettier --write" in after and "make test" in after
              and "Notification（pi 不支持这个事件" in after, after)


def test_pre_tool() -> None:
    asyncio.run(case_pre_tool())


def test_post_tool() -> None:
    asyncio.run(case_post_tool())


def test_prompt() -> None:
    asyncio.run(case_prompt())


def test_stop() -> None:
    asyncio.run(case_stop())


def test_session_start() -> None:
    asyncio.run(case_session_start())


def test_failures() -> None:
    asyncio.run(case_failures())


def test_commands() -> None:
    asyncio.run(case_commands())


async def main() -> int:
    print("规则");           test_units()
    print("PreToolUse");     await case_pre_tool()
    print("PostToolUse");    await case_post_tool()
    print("UserPromptSubmit"); await case_prompt()
    print("Stop");           await case_stop()
    print("SessionStart");   await case_session_start()
    print("出错 / 超时");    await case_failures()
    print("/hooks");         await case_commands()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} 处失败：")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("全部通过 —— hooks 可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
