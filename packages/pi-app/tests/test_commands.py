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
        self.pick_headers: list[str] = []
        self.pick_details: list = []
        self.typed: list[str] = []      # ask_user 选「其他」时依次输入的回答
        self.typed_for: list[tuple[str, str]] = []

    async def pick(self, title, items, **kwargs):
        self.pick_calls.append((title, items))
        self.pick_headers.append(kwargs.get("header", ""))
        self.pick_details.append(kwargs.get("detail"))
        choice = self.picks.pop(0)
        if choice is None:
            return None
        index, value = choice
        item = items[index]
        if value is not None:
            item.choice = item.choices.index(value)
        return PickResult(index, item, item.choices[item.choice] if item.choices else None)

    async def _read_answer(self, header: str, question: str) -> str:
        self.typed_for.append((header, question))
        return self.typed.pop(0)


def make_app(cwd: Path, script: list[list] | None = None, persist: bool = False, tools=("read",)):
    models, _ = fake_models(script or [], models=[PLAIN, THINKER])
    agent_dir = cwd / "agent"
    services = create_agent_session_services(cwd=cwd, agent_dir=agent_dir,
                                             model_runtime=ModelRuntime(models, agent_dir))
    manager = SessionManager.create(cwd, agent_dir) if persist else SessionManager.in_memory(cwd)
    session = AgentSession(services, manager, model=PLAIN, tools=list(tools))
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
        "steps": [{"title": step, "files": ["cfg.py"], "done_when": f"{step}完成"}
                  for step in ["读 cfg.py", "改成读环境变量", "跑测试"]], "files": ["cfg.py"]})


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
        check("计划: 计划单独成块显示，任务是一张表", "╭ 计划 加超时配置" in text
              and "│ 2 │ 改成读环境变量" in text and "改成读环境变量完成" in text, text)
        check("执行: 选「执行计划」后切到执行模式", app.session.mode == "chat")
        check("执行: 只回显一行标题，不回显整段任务说明", "✓ 执行计划 加超时配置" in text
              and "执行要求" not in text, text)
        check("执行: 跑完", "● 都改好了，测试通过。" in text, text)
        last = text[text.rindex("✻ "):]
        check("执行: 用时行下面是打完勾的清单", "✓ 任务 3/3" in last and "✓ 1. 读 cfg.py" in last
              and "✓ 3. 跑测试" in last, last)

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


def ask() -> ToolCall:
    return ToolCall(id="a1", name="ask_user", arguments={"questions": [
        {"question": "list 金额也改成两位小数吗？", "header": "金额格式",
         "options": [{"label": "统一改", "description": "list 也显示 10.00", "recommended": True},
                     {"label": "保持原样"}]},
        {"question": "budget 金额要校验吗？", "options": [{"label": "同样校验"}, {"label": "不校验"}]},
    ]})


async def case_plan_ask_user() -> None:
    """计划模式提问：逐题弹选择器（推荐项标出、最后一项自己输入），答案留在 scrollback，接着出计划"""
    with Sandbox() as cwd:
        app, out = make_app(cwd, [[TextContent(text="先确认两个问题。"), ask()], [submit()]])
        app.picks = [(0, None), (2, None), (2, None)]     # 统一改 / 其他（自己输入）/ 先不执行
        app.typed = ["金额必须大于 0"]
        start = len(out.getvalue())
        await app.dispatch("/plan 规划一下")
        text = tail(out, start)
        title, items = app.pick_calls[0]
        check("提问: 选择器标题是问题，上边框写明需要选择、进度和主题",
              title == "list 金额也改成两位小数吗？"
              and app.pick_headers[0] == "需要你选择 · 1/2 · 金额格式"
              and app.pick_headers[1] == "需要你选择 · 2/2", str(app.pick_headers))
        check("提问: 自己输入时问题还在输入行上面", app.typed_for == [("需要你选择 · 2/2", "budget 金额要校验吗？")],
              str(app.typed_for))
        check("提问: 「执行这个计划？」写明需要确认", app.pick_headers[-1] == "需要你确认", str(app.pick_headers))
        check("提问: 推荐项标出，最后一项自己输入", [i.label for i in items] == ["统一改（推荐）", "保持原样", "其他"]
              and items[0].description == "list 也显示 10.00", str([i.label for i in items]))
        check("提问: 答案留在 scrollback", "? 金额格式 → 统一改" in text
              and "? budget 金额要校验吗？ → 金额必须大于 0" in text, text)
        check("提问: 答完接着出计划、问要不要执行", "╭ 计划 加超时配置" in text
              and app.pick_calls[-1][0] == "执行这个计划？", text)
        check("提问: ask_user 不算工具调用", "次工具" not in text, text)
        check("提问: 答完界面回调还挂在会话上", app.session.user_input == app.ask_user)

    with Sandbox() as cwd:
        app, out = make_app(cwd, [[ask()], [TextContent(text="按推荐的来：……")]])
        app.picks = [None]
        start = len(out.getvalue())
        await app.dispatch("/plan 规划一下")
        text = tail(out, start)
        check("提问: Esc 跳过全部", "跳过了提问" in text and len(app.pick_calls) == 1, text)


# -- 权限确认 ---------------------------------------------------------------- #

def sh(call_id: str, command: str) -> ToolCall:
    return ToolCall(id=call_id, name="bash", arguments={"command": command})


async def case_permissions_ui() -> None:
    """改文件、跑命令前弹「需要你确认」面板：命令写在面板里；拒绝 / 说明 / 不再询问各留一行结论"""
    with Sandbox() as cwd:
        (cwd / "keep.txt").write_text("x", "utf-8")
        app, out = make_app(cwd, [[sh("b1", "rm -f keep.txt")], [sh("b2", "touch t.txt")],
                                  [sh("b3", "touch u.txt")], [TextContent(text="完成")]], tools=("read", "bash"))
        app.picks = [(2, None), (1, None)]          # 拒绝；允许并且不再询问 touch
        start = len(out.getvalue())
        await app.send("整理")
        text = tail(out, start)
        title, items = app.pick_calls[0]
        check("确认面板: 上边框写明需要确认、问题、命令", app.pick_headers[0] == "需要你确认 · 执行命令"
              and title == "允许执行这条命令吗？" and "rm -f keep.txt" in "".join(app.pick_details[0]),
              str((app.pick_headers[0], title, app.pick_details[0])))
        check("确认面板: 选项", [i.label for i in items] == ["允许", "允许，并且本会话不再询问 rm …", "拒绝", "拒绝并说明"],
              str([i.label for i in items]))
        check("拒绝: 不执行，留一行结论", (cwd / "keep.txt").exists() and "✗ 拒绝了 bash rm -f keep.txt" in text, text)
        check("不再询问: 留一行，同类命令不再弹", "✓ 本会话不再询问 touch …" in text and len(app.pick_calls) == 2
              and (cwd / "t.txt").exists() and (cwd / "u.txt").exists(), text)

    with Sandbox() as cwd:
        app, out = make_app(cwd, [[sh("b1", "rm -rf build")], [TextContent(text="好")]], tools=("read", "bash"))
        app.picks = [(3, None)]
        app.typed = ["先用 git clean -n 看看"]
        start = len(out.getvalue())
        await app.send("清理")
        result = next(m for m in app.session.messages if getattr(m, "role", "") == "toolResult")
        check("拒绝并说明: 用户的话给模型", "先用 git clean -n 看看" in result.content[0].text
              and "拒绝了 bash rm -rf build：先用 git clean -n 看看" in tail(out, start), tail(out, start))

    with Sandbox() as cwd:
        app, out = make_app(cwd, [], tools=("read", "bash"))
        app.picks = [(1, None)]
        await app.dispatch("/permissions")
        title, items = app.pick_calls[-1]
        check("/permissions: 选择器列出三种模式", title == "权限模式"
              and [i.label for i in items] == ["每次确认", "自动接受文件修改", "全部自动"] and items[0].current)
        check("/permissions: 输入框下显示非默认模式", "⚠ 自动接受文件修改" in app.renderer.input_footer())
        await app.dispatch("/permissions ask")
        check("/permissions: 切回默认后不显示", "⚠" not in app.renderer.input_footer())


# -- 运行中输入、中断 -------------------------------------------------------- #

def slow_model(script: list, delay: float = 0.15):
    """每次调用先停一会儿再回复：给"运行中"留出时间"""
    from pi_ai import AssistantMessage, DoneEvent, StartEvent

    def stream_fn(model, context, options=None):
        step = script.pop(0) if script else [TextContent(text="好了")]
        seen = [m.text() for m in context.messages if getattr(m, "role", "") == "user"]

        async def gen():
            message = AssistantMessage(api=model.api, provider=model.provider, model=model.id)
            yield StartEvent(partial=message)
            await asyncio.sleep(delay)
            message.content.extend(step(seen) if callable(step) else step)
            message.stop_reason = "toolUse" if any(isinstance(b, ToolCall) for b in message.content) else "stop"
            yield DoneEvent(reason=message.stop_reason, message=message)

        return gen()

    return stream_fn


async def case_live_input() -> None:
    """一轮在后台跑：运行中回车是插话（模型做完这一步就看到），命令要等；Esc 中断；没送到的插话放回输入框"""
    with Sandbox() as cwd:
        (cwd / "a.txt").write_text("内容", "utf-8")
        app, out = make_app(cwd, [])
        app.background = True
        app.session.agent.stream_fn = slow_model([
            [ToolCall(id="r1", name="read", arguments={"path": "a.txt"})],
            lambda seen: [TextContent(text="看到了：" + " / ".join(seen))],
        ])
        await app.submit("读一下")
        check("后台运行: 提交后马上返回，一轮还在跑", app.busy)
        await app.submit("顺便看看格式")
        check("插话: 显示在活动区等着送到", app.renderer.queued == ["顺便看看格式"]
              and "插话（这一步做完就送到）：顺便看看格式" in "\n".join(app.renderer._live_lines()))
        start = len(out.getvalue())
        await app.submit("/context")
        check("运行中命令: 不执行，提示等一等", "运行中不能执行命令" in tail(out, start)
              and "上下文" not in tail(out, start), tail(out, start))
        await app.task
        text = out.getvalue()
        check("插话: 做完这一步就送到，模型看到了", app.session.messages[-1].text() == "看到了：读一下 / 顺便看看格式",
              app.session.messages[-1].text())
        check("插话: 送到后回显、活动区不再显示", "❯ 顺便看看格式" in text and app.renderer.queued == [], text)
        check("插话: 输入框下提示 Esc 中断只在运行中出现", "Esc" not in app.renderer.input_footer())

    with Sandbox() as cwd:
        app, out = make_app(cwd, [])
        app.background = True
        app.session.agent.stream_fn = slow_model([[TextContent(text="不该出现")]], delay=30)
        check("中断: 没在跑时 Esc 不做事", not app.interrupt())
        await app.submit("想很久")
        await asyncio.sleep(0.05)
        check("中断: 运行中输入框下提示 Esc", "Esc" in app.renderer.input_footer())
        await app.submit("补一句")
        check("ctrl+c: 有字时不中断（照旧清空这一行）", not app._on_ctrl_c("还有字") and app.busy)
        started = asyncio.get_running_loop().time()
        check("中断: Esc 中断正在跑的一轮", app.interrupt())
        await asyncio.wait_for(app.task, 2)
        check("中断: 模型还没出字也立刻停", asyncio.get_running_loop().time() - started < 1)
        text = out.getvalue()
        check("中断: 留一行「已中断」", "⏹ 已中断" in text and "不该出现" not in text, text)
        check("中断: 没送到的插话放回输入框", app._draft == "补一句" and "已放回输入框" in text
              and app.renderer.queued == [] and not app.session.agent.steering_queue, app._draft)

    with Sandbox() as cwd:
        app, out = make_app(cwd, [])
        app.background = True
        app.session.agent.stream_fn = slow_model([[TextContent(text="不该出现")]], delay=30)
        await app.submit("再想很久")
        await asyncio.sleep(0.05)
        check("ctrl+c: 运行中空行是中断", app._on_ctrl_c(""))
        await asyncio.wait_for(app.task, 2)
        check("ctrl+c: 中断后空行照旧退出（不再拦）", not app._on_ctrl_c(""))

        async def nested() -> str:
            async with app.exclusive():  # noqa: SIM117 —— 测的就是嵌套（可重入）
                async with app.exclusive():
                    return "ok"
        check("屏幕交接: 同一次交互里可重入，不会自己卡死", await asyncio.wait_for(nested(), 1) == "ok")


async def case_details_viewer() -> None:
    """ctrl+o / /expand：没有过程时提示；没有 prompt_toolkit 时照旧打印；全屏期间后台输出先攒着"""
    import contextlib
    import io as _io

    from pi_app.app import ReaderHost

    with Sandbox() as cwd:
        app, out = make_app(cwd, [[ToolCall(id="r1", name="read", arguments={"path": "a.txt"})],
                                  [TextContent(text="好")]])
        start = len(out.getvalue())
        await app.dispatch("/expand")
        check("详情: 还没有过程时提示", "还没有可以查看的过程" in tail(out, start), tail(out, start))
        (cwd / "a.txt").write_text("内容", "utf-8")
        await app.send("读一下")
        start = len(out.getvalue())
        await app.dispatch("/expand")
        check("详情: 没有 prompt_toolkit 的输入框时照旧打印出来", "上一轮详情" in tail(out, start)
              and "read" in tail(out, start), tail(out, start))

    class FakeReader:
        def invalidate(self) -> None:
            pass

    host = ReaderHost(FakeReader())
    buffer = _io.StringIO()
    with contextlib.redirect_stdout(buffer):
        host.print_above("第一行")
        host.hold()
        host.print_above("全屏时写的")
        held = buffer.getvalue()
        host.release()
        host.print_above("之后")
    check("全屏期间: 输出先攒着，关掉后按顺序打印", held == "第一行\n"
          and buffer.getvalue() == "第一行\n全屏时写的\n之后\n", repr(buffer.getvalue()))


async def case_attention() -> None:
    """需要用户时响铃：只在交互模式；离上次输入不到 minSeconds 不响（人在看着）；两秒内不重复"""
    import time

    import pi_app.app as app_module

    rang: list[tuple[str, bool, bool]] = []
    real = app_module.notify
    app_module.notify = lambda message, bell=True, desktop=False: rang.append((message, bell, desktop))
    try:
        with Sandbox() as cwd:
            app, _ = make_app(cwd, [[TextContent(text="好")]])
            app.attention("这一轮做完了")
            check("响铃: 非交互模式（测试、管道）不响", rang == [])
            app.background = True
            app.attention("这一轮做完了")
            check("响铃: 刚输入过（人在看着）不响", rang == [])
            app._last_input = time.monotonic() - 60
            app.attention("这一轮做完了")
            check("响铃: 离开久了响", rang == [("pi：这一轮做完了", True, False)], str(rang))
            app.attention("计划写好了，等你确认")
            check("响铃: 两秒内不重复", len(rang) == 1)
            app._last_bell -= 5
            app.session.settings.set("notifications.desktop", True)
            app.attention("有问题要问你")
            check("响铃: desktop 设置时一起发系统通知", rang[-1] == ("pi：有问题要问你", True, True), str(rang))
            app._last_bell -= 5
            app.session.settings.set("notifications.bell", False)
            app.session.settings.set("notifications.desktop", False)
            app.attention("x")
            check("响铃: 都关掉时不响", len(rang) == 2)
            app.session.settings.set("notifications.bell", True)
            await app.submit("你好")
            app._last_bell -= 5
            app.attention("这一轮做完了")
            check("响铃: 提交输入会重置计时", len(rang) == 2)
            app._last_input -= 60
            app.task = asyncio.ensure_future(asyncio.sleep(10))
            app.interrupt()
            app.task.cancel()
            app._last_bell -= 5
            app.attention("这一轮做完了")
            check("响铃: 按 Esc 中断也算人在跟前", len(rang) == 2)
    finally:
        app_module.notify = real


async def case_mcp_warmup() -> None:
    """交互模式下打开会话就在后台连 MCP：连不上的提示一行；非交互模式（测试、管道）不预连接"""
    with Sandbox() as cwd:
        (cwd / ".pi").mkdir()
        (cwd / ".pi" / "mcp.json").write_text(
            '{"mcpServers": {"broken": {"command": "no-such-mcp-command-xyz"}}}')
        app, out = make_app(cwd)
        app._warm_mcp(app.session)
        await asyncio.sleep(0.2)
        check("MCP 预连接: 非交互模式不连", "broken" not in out.getvalue(), out.getvalue())
        app.background = True
        app._warm_mcp(app.session)
        for _ in range(50):
            await asyncio.sleep(0.05)
            if "broken" in out.getvalue():
                break
        check("MCP 预连接: 连不上的提示一行、指向 /mcp", "⚠ MCP：broken 连接失败（/mcp 查看原因）" in out.getvalue(),
              out.getvalue())


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


def test_plan_ask_user() -> None:
    asyncio.run(case_plan_ask_user())


def test_permissions_ui() -> None:
    asyncio.run(case_permissions_ui())


def test_live_input() -> None:
    asyncio.run(case_live_input())


def test_details_viewer() -> None:
    asyncio.run(case_details_viewer())


def test_attention() -> None:
    asyncio.run(case_attention())


def test_mcp_warmup() -> None:
    asyncio.run(case_mcp_warmup())


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
    await case_plan_ask_user()
    await case_permissions_ui()
    await case_live_input()
    await case_details_viewer()
    await case_attention()
    await case_mcp_warmup()
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
