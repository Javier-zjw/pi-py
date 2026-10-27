"""改完就检查测试（工具执行后的钩子 + 内置扩展 checks）：不联网、不装东西。

    python tests/test_checks.py
    pytest tests/test_checks.py

覆盖：内置检查（Python 语法、JSON、允许注释的 JSON 跳过、TOML、太大 / 非 UTF-8 不查）、
配置的命令（通配、{file} 带空格、退出码、超时）、扩展 API 的 after_tool_call（串联、出错保留原结果、
被拦下的调用不触发）、接进会话后模型收到的工具结果、关掉开关、子智能体改的文件也查。
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
from pi_agent import AgentToolResult, BeforeToolCallResult
from pi_ai import TextContent, ToolCall
from pi_coding_agent import (
    AgentSession,
    ModelRuntime,
    SessionManager,
    create_agent_session_services,
)
from pi_coding_agent.checks import builtin_check, matching_commands, run_command
from test_subagents import is_child, make_session, task_call, tool_results

FAILURES: list[str] = []
POSIX = os.name != "nt"


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


def results(session) -> dict:
    return {m.tool_call_id: m for m in session.messages if getattr(m, "role", "") == "toolResult"}


def test_builtin() -> None:
    with Sandbox() as root:
        def write(rel: str, text: str) -> Path:
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            return path

        problem = builtin_check(write("bad.py", "def f(:\n    pass\n"), "bad.py")
        check("Python: 语法错误给出行列和那一行", problem is not None and problem.source == "python"
              and "line 1" in problem.message and "1 | def f(:" in problem.message, str(problem))
        check("Python: 正确的不报", builtin_check(write("ok.py", "x = 1\n"), "ok.py") is None)
        check("Python: 只编译不执行", builtin_check(write("side.py", "open('ran', 'w')\n"), "side.py")
              is None and not (root / "ran").exists())
        problem = builtin_check(write("a.json", '{"a": 1,\n "b": }'), "a.json")
        check("JSON: 解析失败给出行列", problem is not None and "line 2" in problem.message, str(problem))
        check("JSON: 允许注释的（tsconfig、.vscode）跳过",
              builtin_check(write("tsconfig.json", "{// c\n}"), "tsconfig.json") is None
              and builtin_check(write(".vscode/settings.json", "{// c\n}"), ".vscode/settings.json")
              is None)
        try:
            import tomllib  # noqa: F401
            problem = builtin_check(write("p.toml", "[a\nb = 1\n"), "p.toml")
            check("TOML: 解析失败（Python 3.11+）", problem is not None and problem.source == "toml",
                  str(problem))
        except ImportError:
            print("  skip TOML（Python 3.10 没有 tomllib）")
        check("不认识的类型、非 UTF-8 不查", builtin_check(write("a.rs", "fn ("), "a.rs") is None
              and builtin_check(write("b.py", ""), "b.py") is None)
        (root / "latin.py").write_bytes(b"x = '\xe9\n")
        check("非 UTF-8 的 .py 不查（不误报）", builtin_check(root / "latin.py", "latin.py") is None)

    commands = {"*.py": "ruff check {file}", "src/*.ts": ["tsc", "eslint {file}"], "bad": 3}
    check("命令: 通配对文件名和相对路径都试", matching_commands(commands, "pkg/a.py") == ["ruff check {file}"]
          and matching_commands(commands, "src/x.ts") == ["tsc", "eslint {file}"]
          and matching_commands(commands, "README.md") == [] and matching_commands(None, "a.py") == [])


async def case_commands() -> None:
    if not POSIX:
        print("  skip 检查命令（用 POSIX shell 写的）")
        return
    with Sandbox() as root:
        (root / "my file.py").write_text("x = 1\n")
        ok = await run_command("test -f {file}", "my file.py", str(root), 5)
        check("命令: 退出码 0 不算问题、{file} 带空格也对", ok is None, str(ok))
        bad = await run_command("echo 'E501 line too long' {file}; exit 2", "my file.py", str(root), 5)
        check("命令: 非 0 把输出附上", bad is not None and "E501 line too long my file.py" in bad.message,
              str(bad))
        slow = await run_command("sleep 5", "my file.py", str(root), 0.3)
        check("命令: 超时说明", slow is not None and "timed out" in slow.message, str(slow))
        many = await run_command("seq 1 100; exit 1", "x", str(root), 5, max_lines=5)
        check("命令: 输出太长只留前几行", many is not None and many.message.count("\n") == 5
              and "95 more lines" in many.message, many.message if many else "")


def make(root: Path, script: list, tools=("read", "write", "edit"), settings: dict | None = None):
    if settings:
        (root / ".pi").mkdir(exist_ok=True)
        (root / ".pi" / "settings.json").write_text(json.dumps(settings))
    models, provider = fake_models(script)
    services = create_agent_session_services(cwd=root, agent_dir=root / "agent",
                                             model_runtime=ModelRuntime(models, root / "agent"))
    return AgentSession(services, SessionManager.in_memory(root), model=FAKE_MODEL, tools=list(tools)), provider


async def case_session() -> None:
    with Sandbox() as root:
        (root / "app.py").write_text("def main():\n    return 1\n")
        session, provider = make(root, [
            [ToolCall(id="w1", name="write", arguments={"path": "bad.py", "content": "def f(:\n"}),
             ToolCall(id="e1", name="edit", arguments={"path": "app.py", "old_text": "return 1",
                                                       "new_text": "return 2"})],
            [TextContent(text="改好了")],
        ])
        await session.prompt("改一下")
        got = results(session)
        bad, good = got["w1"], got["e1"]
        check("会话: 写坏的文件，工具结果后面附上检查结果", "[post-edit check]" in text_of(bad)
              and "bad.py WAS written" in text_of(bad) and "syntax error at line 1" in text_of(bad),
              text_of(bad))
        check("会话: 不标成错误（文件确实写进去了）", not bad.is_error and (root / "bad.py").exists())
        check("会话: details 带检查结果（界面用）", bad.details.get("check", {}).get("ok") is False
              and bad.details["check"]["file"] == "bad.py" and bad.details.get("created") is True,
              str(bad.details))
        check("会话: 没问题的不附任何文字（不占上下文）", "post-edit" not in text_of(good)
              and "check" not in (good.details or {}), text_of(good))
        sent = [m for m in provider.calls[1]["context"].messages if getattr(m, "role", "") == "toolResult"]
        check("会话: 模型下一步就看到了", any("[post-edit check]" in text_of(m) for m in sent))

    with Sandbox() as root:
        session, _ = make(root, [[ToolCall(id="w1", name="write", arguments={"path": "bad.json",
                                                                            "content": "{"})],
                                 [TextContent(text="好")]], settings={"checks": {"enabled": False}})
        await session.prompt("写")
        check("开关: checks.enabled=false 不查", "post-edit" not in text_of(results(session)["w1"]))

    if POSIX:
        with Sandbox() as root:
            session, _ = make(root, [[ToolCall(id="w1", name="write", arguments={"path": "src/a.py",
                                                                                "content": "x=1\n"})],
                                     [TextContent(text="好")]],
                              settings={"checks": {"commands": {"*.py": "echo lint: {file} E225; exit 1",
                                                                "*.md": "exit 1"}}})
            await session.prompt("写")
            text = text_of(results(session)["w1"])
            check("配置的命令: 匹配的才跑、失败输出给模型", "lint: src/a.py E225" in text
                  and text.count("\n- ") == 1, text)


async def case_hook_api() -> None:
    with Sandbox() as root:
        session, _ = make(root, [[ToolCall(id="r1", name="read", arguments={"path": "a.txt"}),
                                  ToolCall(id="r2", name="read", arguments={"path": "blocked.txt"})],
                                 [TextContent(text="好")]])
        (root / "a.txt").write_text("hello\n")
        api = session.resources.get_extension_api()
        seen: list[str] = []

        async def first(call, result, sess):
            seen.append(call.id)
            return AgentToolResult(content=[*result.content, TextContent(text="\n[first]")],
                                   details=result.details)

        async def broken(call, result, sess):
            raise RuntimeError("boom")

        async def second(call, result, sess):
            return AgentToolResult(content=[*result.content, TextContent(text="[second sees first:"
                                            f" {'[first]' in text_of(result)}]")], details=result.details)

        async def block(call, sess):
            return BeforeToolCallResult(block=True, reason="no") if call.id == "r2" else None

        api.after_tool_call(first)
        api.after_tool_call(broken)
        api.after_tool_call(second)
        api.before_tool_call(block)
        await session.prompt("读")
        text = text_of(results(session)["r1"])
        check("钩子: 按注册顺序串起来，后一个拿到前一个改过的结果",
              "hello" in text and "[first]" in text and "[second sees first: True]" in text, text)
        check("钩子: 出错的记下来、不吞结果", any("boom" in e for e in api.tool_call_errors))
        check("钩子: 被拦下的调用不触发", seen == ["r1"], str(seen))


async def case_subagent() -> None:
    with Sandbox() as cwd:
        async def respond(context):
            if not is_child(context):
                if not tool_results(context):
                    return [task_call("g1", "写脚本", "创建 tool.py", agent="general")], None
                return [TextContent(text="好")], None
            if not tool_results(context):
                return [ToolCall(id="w1", name="write", arguments={"path": "tool.py",
                                                                  "content": "def f(:\n"})], None
            seen = text_of(tool_results(context)[-1])
            return [TextContent(text=f"报告：{'看到检查结果' if 'post-edit check' in seen else '没看到'}")], None

        session, _ = make_session(cwd, respond)
        await session.prompt("派一个")
        report = text_of(results(session)["g1"])
        check("子智能体: 它改的文件也检查、它自己能看到", "看到检查结果" in report, report)


def test_commands() -> None:
    asyncio.run(case_commands())


def test_session() -> None:
    asyncio.run(case_session())


def test_hook_api() -> None:
    asyncio.run(case_hook_api())


def test_subagent() -> None:
    asyncio.run(case_subagent())


async def main() -> int:
    print("内置检查");     test_builtin()
    print("配置的命令");   await case_commands()
    print("接进会话");     await case_session()
    print("钩子 API");     await case_hook_api()
    print("子智能体");     await case_subagent()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} 处失败：")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("全部通过 —— 改完就检查可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
