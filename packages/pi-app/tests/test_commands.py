"""pi-tui 斜杠命令测试：命令表、补全候选、/model 选择器、/clear、/resume 等

    python tests/test_commands.py
    pytest tests/test_commands.py

用真的 AgentSession + 假模型；选择器换成预先设定好的结果，不弹交互界面。不联网、不需要 key。
"""

from __future__ import annotations

import asyncio
import dataclasses
import io
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve()
PACKAGES = HERE.parents[2]
for _p in (HERE.parents[1], PACKAGES / "pi-tui", PACKAGES / "pi-coding-agent",
           PACKAGES / "pi-coding-agent" / "tests", PACKAGES / "pi-agent", PACKAGES / "pi-ai"):
    sys.path.insert(0, str(_p))

from fakes import FAKE_MODEL, Sandbox, fake_models

from pi_ai import TextContent, ToolCall
from pi_app.app import App
from pi_app.commands import ago, model_items, thinking_choices, usage_bar
from pi_app.renderer import SessionRenderer
from pi_coding_agent import ModelRuntime, SessionManager
from pi_coding_agent.agent_session import AgentSession, create_agent_session_services
from pi_coding_agent.session import list_sessions
from pi_tui import PickResult, Theme, match_commands
from pi_tui.theme import ColorMode

FAILURES: list[str] = []

# base_url 让模型在没有 key 时也算"可用"
PLAIN = dataclasses.replace(FAKE_MODEL, base_url="http://fake")
THINKER = dataclasses.replace(FAKE_MODEL, id="thinker", name="Thinker", reasoning=True,
                              context_window=200_000, base_url="http://fake",
                              thinking_level_map={"max": None})


def under_pytest() -> bool:
    # 必须在调用时判断：import（收集）阶段 pytest 还没设置这个环境变量
    return "PYTEST_CURRENT_TEST" in os.environ


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILURES.append(f"{name}: {detail}")
    print(f"  FAIL {name} {detail}")
    if under_pytest():
        raise AssertionError(f"{name}: {detail}")


class ScriptedApp(App):
    """选择器不弹界面：依次返回 picks 里预设的 (行号, 次要选项)；None 表示按了 Esc"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.picks: list[tuple[int, str | None] | None] = []
        self.pick_calls: list[tuple[str, list]] = []

    async def pick(self, title, items, **kwargs):
        self.pick_calls.append((title, items))
        choice = self.picks.pop(0)
        if choice is None:
            return None
        index, value = choice
        item = items[index]
        if value is not None:
            item.choice = item.choices.index(value)
        return PickResult(index, item, item.choices[item.choice] if item.choices else None)


def make_app(cwd: Path, script: list[list] | None = None, persist: bool = False):
    models, _ = fake_models(script or [], models=[PLAIN, THINKER])
    agent_dir = cwd / "agent"
    services = create_agent_session_services(cwd=cwd, agent_dir=agent_dir,
                                             model_runtime=ModelRuntime(models, agent_dir))
    manager = SessionManager.create(cwd, agent_dir) if persist else SessionManager.in_memory(cwd)
    session = AgentSession(services, manager, model=PLAIN, tools=["read"])
    out = io.StringIO()
    renderer = SessionRenderer(theme=Theme(ColorMode.NONE), stream=out)
    return ScriptedApp(session, renderer, agent_dir=agent_dir), out


def tail(out: io.StringIO, start: int) -> str:
    return out.getvalue()[start:]


# -- 纯函数 ---------------------------------------------------------------- #

def test_model_items() -> None:
    check("思考档位: 不支持思考的模型没有档位", thinking_choices(PLAIN) == [])
    levels = thinking_choices(THINKER)
    check("思考档位: 从 off 开始，map 里写成 null 的去掉",
          levels[0] == "off" and "high" in levels and "max" not in levels, str(levels))

    items, index = model_items([PLAIN, THINKER], THINKER, "high")
    check("模型选择器: 光标停在当前模型", index == 1 and items[1].current and not items[0].current)
    check("模型选择器: 当前模型停在当前档位", items[1].choices[items[1].choice] == "high")
    check("模型选择器: 不支持思考的写明", items[0].choices == [] and items[0].note == "不支持思考")
    check("模型选择器: 说明带名字和窗口", "Thinker" in items[1].description
          and "200.0k 上下文" in items[1].description, items[1].description)
    check("模型选择器: 行的值是模型本身", items[1].value is THINKER)

    items, index = model_items([THINKER], PLAIN, "off")
    check("模型选择器: 当前模型不在可用列表里也放在第一行",
          [i.label for i in items] == [PLAIN.key, THINKER.key] and index == 0)
    items, _ = model_items([THINKER], PLAIN, "max")
    check("模型选择器: 档位不支持时停在 off", items[1].choices[items[1].choice] == "off")


def test_helpers() -> None:
    now = time.time()
    check("时间: 刚刚", ago(now - 5, now) == "刚刚")
    check("时间: 分钟", ago(now - 300, now) == "5 分钟前")
    check("时间: 天", ago(now - 86400 * 2, now) == "2 天前")
    check("进度条: 按比例填充", usage_bar(50, 100, 10) == "█████░░░░░")
    check("进度条: 超出不越界", usage_bar(500, 100, 4) == "████")


# -- 命令表与补全 ------------------------------------------------------------ #

async def case_command_table() -> None:
    with Sandbox() as cwd:
        prompts = cwd / ".pi" / "prompts"
        prompts.mkdir(parents=True)
        (prompts / "review.md").write_text("---\ndescription: 代码审查\n---\n审查这段代码", "utf-8")
        app, out = make_app(cwd)
        commands = app.slash_commands()
        names = [c.name for c in commands]
        for expected in ("model", "clear", "resume", "compact", "context", "cost", "status", "help"):
            check(f"命令表: 有 /{expected}", expected in names, str(names))
        check("命令表: 扩展命令也在（plan_mode）", "plan" in names and "build" in names, str(names))
        check("命令表: 提示模板也在", "review" in names, str(names))
        check("命令表: 没有重名", len(names) == len(set(names)))
        check("补全: /mo → /model", [c.name for c in match_commands("/mo", commands)][0] == "model")
        check("补全: 别名 /usage 找到 /cost",
              [c.name for c in match_commands("/usage", commands)] == ["cost"])
        check("补全: 别名 /new 找到 /clear", "clear" in [c.name for c in match_commands("/new", commands)])
        check("查找: 别名也能执行", app.find("quit") is not None and app.find("quit").name == "exit")

        start = len(out.getvalue())
        await app.dispatch("/help")
        text = tail(out, start)
        check("/help: 列出内置命令和别名", "/model" in text and "别名 /usage" in text, text)
        check("/help: 列出扩展命令", "扩展命令" in text and "/plan [需求]" in text, text)
        check("/help: 扩展 usage 不重复命令名", "/plan /plan" not in text)
        check("/help: 列出提示模板", "提示模板" in text and "/review" in text)
        check("/help: 列出快捷键", "ctrl+o" in text)

        start = len(out.getvalue())
        await app.dispatch("/")
        check("只输 / 显示帮助", "/model" in tail(out, start))


async def case_dispatch() -> None:
    with Sandbox() as cwd:
        app, out = make_app(cwd, script=[[TextContent(text="路径问题的回答")]])
        start = len(out.getvalue())
        check("/exit 返回 False", await app.dispatch("/exit") is False)
        check("/quit 也退出", await app.dispatch("/quit") is False)
        check("其他命令返回 True", await app.dispatch("/cost") is True)

        start = len(out.getvalue())
        await app.dispatch("/nosuch")
        check("未知命令: 提示而不是发给模型", "未知命令 /nosuch" in tail(out, start)
              and app.turns == 0, tail(out, start))

        await app.dispatch("/usr/bin/env 是什么")
        check("像路径的 / 开头照常发给模型", app.turns == 1
              and app.session.messages[0].content == "/usr/bin/env 是什么")

        start = len(out.getvalue())
        await app.dispatch("/show_plan")
        check("扩展命令: 经 run_command 执行并打印输出", "计划" in tail(out, start), tail(out, start))


# -- /model /think ------------------------------------------------------------ #

async def case_model_picker() -> None:
    with Sandbox() as cwd:
        app, out = make_app(cwd)
        app.picks = [(1, "high")]
        start = len(out.getvalue())
        await app.dispatch("/model")
        title, items = app.pick_calls[-1]
        check("/model: 打开选择器，列出可用模型", title == "选择模型"
              and [i.label for i in items] == [PLAIN.key, THINKER.key])
        check("/model: 选中后切换模型", app.session.model.key == THINKER.key)
        check("/model: ←→ 选的思考档位生效", app.session.thinking_level == "high")
        check("/model: 状态栏同步", app.renderer.status.model == THINKER.key
              and app.renderer.status.thinking == "high")
        text = tail(out, start)
        check("/model: 打印一行结论", "✓ 模型 fake/thinker" in text and "思考 high" in text, text)

        app.picks = [None]
        start = len(out.getvalue())
        await app.dispatch("/model")
        check("/model: Esc 取消不变", app.session.model.key == THINKER.key
              and "已取消" in tail(out, start))

        app.picks = [(0, None)]
        await app.dispatch("/model")
        check("/model: 换到不支持思考的模型时档位归 off",
              app.session.model.key == PLAIN.key and app.session.thinking_level == "off")

        await app.dispatch("/model fake/thinker")
        check("/model <spec>: 直接切换", app.session.model.key == THINKER.key)
        start = len(out.getvalue())
        await app.dispatch("/model nope/nope")
        check("/model <spec>: 未知模型报错", "未知模型" in tail(out, start)
              and app.session.model.key == THINKER.key)


async def case_think() -> None:
    with Sandbox() as cwd:
        app, out = make_app(cwd)
        start = len(out.getvalue())
        await app.dispatch("/think")
        check("/think: 不支持思考的模型给提示、不弹选择器",
              "不支持思考" in tail(out, start) and not app.pick_calls)
        await app.dispatch("/model fake/thinker")
        await app.dispatch("/think medium")
        check("/think <level>: 设置档位", app.session.thinking_level == "medium")
        start = len(out.getvalue())
        await app.dispatch("/think turbo")
        check("/think: 未知档位报错", "未知档位" in tail(out, start)
              and app.session.thinking_level == "medium")
        await app.dispatch("/think max")
        check("/think: 模型不支持的档位报错", app.session.thinking_level == "medium")
        app.picks = [(3, None)]
        await app.dispatch("/think")
        title, items = app.pick_calls[-1]
        check("/think: 选择器列出该模型的档位并标出当前",
              items[0].label == "off" and any(i.current and i.label == "medium" for i in items))
        check("/think: 选中生效", app.session.thinking_level == items[3].label)
        entries = [e for e in app.session.session.get_entries()
                   if getattr(e, "type", "") == "thinking_level_change"]
        check("/think: 写进会话记录", entries and entries[-1].thinking_level == items[3].label)


# -- /clear /resume ------------------------------------------------------------ #

async def case_clear() -> None:
    with Sandbox() as cwd:
        app, out = make_app(cwd, script=[[TextContent(text="第一轮")], [TextContent(text="第二轮")]],
                            persist=True)
        await app.dispatch("/model fake/thinker")
        await app.dispatch("/think low")
        await app.send("你好")
        old = app.session
        old_file = old.session_file
        check("准备: 有一轮记录", app.renderer.last_turn is not None and len(old.messages) == 2)
        await app.dispatch("/clear")
        check("/clear: 换了新会话", app.session is not old and app.session.session_file != old_file
              and app.session.messages == [])
        check("/clear: 保留模型和思考档位", app.session.model.key == THINKER.key
              and app.session.thinking_level == "low")
        check("/clear: 旧会话文件还在", old_file.exists())
        check("/clear: 渲染器清空上一轮", app.renderer.last_turn is None and app.turns == 0)
        await app.send("新问题")
        check("/clear: 渲染器订阅了新会话", "第二轮" in out.getvalue())
        check("/clear: 旧会话不再收消息", len(old.messages) == 2)
        check("/new 是 /clear 的别名", app.find("new").name == "clear")


async def case_resume() -> None:
    with Sandbox() as cwd:
        app, out = make_app(cwd, script=[[TextContent(text="关于 README 的回答\n第二行\n第三行\n第四行\n第五行")],
                                         [TextContent(text="新会话的回答")]], persist=True)
        await app.send("看一下 README")
        first = app.session.session_file
        await app.dispatch("/clear")
        await app.send("另一个话题")
        second = app.session.session_file
        await app.dispatch("/clear")       # 空会话：不该出现在 /resume 列表里

        listed = list_sessions(cwd, app.agent_dir, include_empty=False)
        check("列表: 跳过空会话、新的在前", [s.path for s in listed] == [second, first],
              str([s.path.name for s in listed]))
        check("列表: 标题取第一条用户消息", listed[1].title == "看一下 README" and listed[1].turns == 1)

        app.picks = [(1, None)]
        start = len(out.getvalue())
        await app.dispatch("/resume")
        title, items = app.pick_calls[-1]
        check("/resume: 选择器列出标题和时间", title == "恢复会话"
              and items[1].label == "看一下 README" and "1 条消息" in items[1].description,
              str([(i.label, i.description) for i in items]))
        check("/resume: 切到选中的会话", app.session.session_file == first
              and len(app.session.messages) == 2)
        text = tail(out, start)
        check("/resume: 回顾最近的对话", "最近的对话" in text and "❯ 看一下 README" in text
              and "● 关于 README 的回答" in text, text)
        check("/resume: 长回答只显示前几行", "第五行" not in text and "还有 2 行" in text, text)
        check("/resume: 一行结论", "已恢复会话" in text)

        app.picks = [(0, None)]
        await app.dispatch("/resume")     # 第一行是 second
        check("/resume: 再选一次切换", app.session.session_file == second)
        app.picks = [(0, None)]
        start = len(out.getvalue())
        await app.dispatch("/resume")
        check("/resume: 选当前会话时提示", "已经是当前会话" in tail(out, start))

        await app.dispatch(f"/resume {first}")
        check("/resume <文件>: 直接打开", app.session.session_file == first)


# -- 计划 / 执行模式 ------------------------------------------------------------ #

def submit(call_id: str = "s1") -> ToolCall:
    return ToolCall(id=call_id, name="submit_plan", arguments={
        "title": "加超时配置", "summary": "把超时做成可配置。",
        "steps": ["读 cfg.py", "改成读环境变量", "跑测试"], "files": ["cfg.py"]})


def tick(call_id: str, done: int) -> ToolCall:
    steps = ["读 cfg.py", "改成读环境变量", "跑测试"]
    plan = [{"step": step, "status": "completed" if i < done else ("in_progress" if i == done else "pending")}
            for i, step in enumerate(steps)]
    return ToolCall(id=call_id, name="update_plan", arguments={"plan": plan})


async def case_plan_mode() -> None:
    with Sandbox() as cwd:
        (cwd / "cfg.py").write_text("TIMEOUT = 30\n", "utf-8")
        script = [
            [ToolCall(id="r1", name="read", arguments={"path": "cfg.py"})],
            [submit()],
            [tick("u1", 1)], [tick("u2", 3)], [TextContent(text="都改好了，测试通过。")],
        ]
        app, out = make_app(cwd, script)
        check("shift+tab: 执行 → 计划", (app.cycle_mode(), app.session.mode)[1] == "plan")
        check("shift+tab: 输入框下显示计划模式", "⏸ 计划模式" in app.renderer.input_footer())
        check("shift+tab: 计划 → 执行", (app.cycle_mode(), app.session.mode)[1] == "chat")

        app.picks = [(0, None)]
        start = len(out.getvalue())
        await app.dispatch("/plan 给配置加一个可调的超时")
        text = tail(out, start)
        title, items = app.pick_calls[-1]
        check("计划: 提交后问要不要执行", title == "执行这个计划？"
              and [i.label for i in items] == ["执行计划", "继续完善", "先不执行"], str(items))
        check("计划: 计划单独成块显示", "╭ 计划 加超时配置" in text and "│ 2. 改成读环境变量" in text, text)
        check("执行: 选「执行计划」后切到执行模式", app.session.mode == "chat")
        check("执行: 只回显一行标题，不回显整段任务说明", "✓ 执行计划 加超时配置" in text
              and "执行要求" not in text, text)
        check("执行: 跑完", "● 都改好了，测试通过。" in text, text)
        last = text[text.rindex("✻ "):]
        check("执行: 用时行下面是打完勾的清单", "✓ 读 cfg.py" in last and "✓ 跑测试" in last, last)

    with Sandbox() as cwd:
        app, out = make_app(cwd, [[submit()]])
        app.picks = [(1, None)]
        start = len(out.getvalue())
        await app.dispatch("/plan 再想想")
        check("完善: 选「继续完善」留在计划模式", app.session.mode == "plan"
              and "直接输入修改意见" in tail(out, start), tail(out, start))
        app.session.switch_mode("chat")
        app.picks = []
        await app.dispatch("/build")
        check("/build: 执行最近提交的计划", app.session.mode == "chat"
              and "执行计划 加超时配置" in out.getvalue())

    with Sandbox() as cwd:
        app, out = make_app(cwd, [])
        start = len(out.getvalue())
        await app.dispatch("/build")
        check("/build: 没有计划时提示", "还没有可执行的计划" in tail(out, start), tail(out, start))


# -- 信息类 ------------------------------------------------------------------ #

async def case_info() -> None:
    with Sandbox() as cwd:
        app, out = make_app(cwd, script=[[TextContent(text="好")]])
        await app.dispatch("/model fake/thinker")
        await app.send("你好")
        start = len(out.getvalue())
        await app.dispatch("/context")
        text = tail(out, start)
        check("/context: 用量和窗口", "/ 200.0k" in text and "░" in text, text)
        check("/context: 压缩阈值", "摘要压缩" in text and "清理旧工具输出" in text, text)
        check("/context: 消息数", "2 条" in text, text)

        start = len(out.getvalue())
        await app.dispatch("/cost")
        text = tail(out, start)
        check("/cost: 输入输出和费用", "↑" in text and "↓" in text and "¥" in text, text)
        start = len(out.getvalue())
        await app.dispatch("/usage")
        check("/usage 是 /cost 的别名", "¥" in tail(out, start))

        start = len(out.getvalue())
        await app.dispatch("/status")
        text = tail(out, start)
        check("/status: 模型、目录、会话、工具、扩展", "fake/thinker" in text and str(cwd) in text
              and "(内存" in text and "read" in text and "plan_mode" in text, text)
        start = len(out.getvalue())
        await app.dispatch("/session")
        check("/session 是 /status 的别名", "fake/thinker" in tail(out, start))


def test_command_table() -> None:
    asyncio.run(case_command_table())


def test_dispatch() -> None:
    asyncio.run(case_dispatch())


def test_model_picker() -> None:
    asyncio.run(case_model_picker())


def test_think() -> None:
    asyncio.run(case_think())


def test_clear() -> None:
    asyncio.run(case_clear())


def test_resume() -> None:
    asyncio.run(case_resume())


def test_plan_mode() -> None:
    asyncio.run(case_plan_mode())


def test_info() -> None:
    asyncio.run(case_info())


async def main() -> int:
    print("纯函数")
    test_model_items()
    test_helpers()
    print("命令表与补全")
    await case_command_table()
    await case_dispatch()
    print("/model /think")
    await case_model_picker()
    await case_think()
    print("/clear /resume")
    await case_clear()
    await case_resume()
    print("计划 / 执行模式")
    await case_plan_mode()
    print("信息类")
    await case_info()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} 处失败：")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("全部通过 —— 斜杠命令可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
