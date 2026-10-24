"""把 AgentSession 和 SessionRenderer 接起来的最小应用。

这是整个项目唯一一个"什么都认识"的地方，所以它必须薄——业务逻辑一行都
不该出现在这里，只有装配和输入循环。
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

from pi_coding_agent import SessionManager, create_agent_session, create_agent_session_services
from pi_coding_agent.text import sanitize
from pi_tui import (
    LineReader,
    PickerItem,
    PickResult,
    SlashCommand,
    Theme,
    display_width,
    missing_dependency_hint,
    pad,
    pick,
    truncate,
    wrap,
)

from .commands import BUILTIN_COMMANDS, AppCommand
from .renderer import SessionRenderer

# 像命令名的才报"未知命令"；/usr/bin 这种路径照常发给模型
COMMAND_NAME = re.compile(r"[A-Za-z][\w-]*")

SHORTCUTS = [
    ("ctrl+o", "展开上一轮的思考和工具调用"),
    ("Alt+Enter", "换行"),
    ("↑ ↓", "翻历史；菜单打开时选择命令"),
    ("Tab", "补全命令名，接着输参数"),
    ("ctrl+c", "清空输入；空行时退出；运行中中断"),
]


class App:
    """输入循环 + 斜杠命令分派。命令表见 commands.py；/clear、/resume 会换掉 self.session"""

    def __init__(self, session: Any, renderer: SessionRenderer, *,
                 agent_dir: str | Path | None = None) -> None:
        self.session = session
        self.renderer = renderer
        self.agent_dir = agent_dir
        self.commands: list[AppCommand] = list(BUILTIN_COMMANDS)
        self.turns = 0
        self.reader: LineReader | None = None
        self._unsubscribe: Any = None
        self._proposed: dict | None = None     # 这一轮里模型提交、等用户确认的计划
        self.attach(session)

    @property
    def theme(self) -> Theme:
        return self.renderer.theme

    # -- 输出 ---------------------------------------------------------- #

    def out(self, lines: list[str]) -> None:
        self.renderer.print_lines(lines)

    def note(self, text: str) -> None:
        self.out([self.theme.muted(f"  {text}")])

    def error(self, text: str) -> None:
        self.out([self.theme.error(f"  ✗ {text}")])

    async def pick(self, title: str, items: list[PickerItem], **kwargs: Any) -> PickResult | None:
        return await pick(title, items, theme=self.theme, width=self.renderer.width, **kwargs)

    # -- 命令表 -------------------------------------------------------- #

    def _extension_commands(self) -> dict[str, Any]:
        return self.session.resources.get_extension_api().commands

    def find(self, name: str) -> AppCommand | None:
        return next((c for c in self.commands if name == c.name or name in c.aliases), None)

    def slash_commands(self) -> list[SlashCommand]:
        """补全菜单的内容：内置命令 > 扩展命令 > 提示模板，同名时前面的优先。每次现取，
        扩展和模板在运行中可能变化"""
        out = [SlashCommand(c.name, c.description, c.aliases) for c in self.commands]
        taken = {c.name for c in out} | {a for c in self.commands for a in c.aliases}
        for command in self._extension_commands().values():
            if command.name not in taken:
                out.append(SlashCommand(command.name, command.description))
                taken.add(command.name)
        for template in self.session.resources.get_prompts():
            if template.name not in taken:
                out.append(SlashCommand(template.name, template.description or "提示模板"))
                taken.add(template.name)
        return out

    def print_help(self) -> None:
        t = self.theme
        rows: list[tuple[str, str]] = []
        for c in self.commands:
            alias = t.muted(f"  别名 {' '.join('/' + a for a in c.aliases)}") if c.aliases else ""
            rows.append((f"/{c.name} {c.usage}".rstrip(), c.description + alias))
        builtin = {c.name for c in self.commands}
        extension = [c for c in self._extension_commands().values() if c.name not in builtin]
        templates = self.session.resources.get_prompts()
        width = min(28, max(display_width(name) for name, _ in rows) + 2)

        def table(pairs: list[tuple[str, str]]) -> list[str]:
            return [f"  {t.accent(pad(truncate(name, width), width))}{desc}" for name, desc in pairs]

        lines = table(rows)
        if extension:
            lines += ["", t.muted("  扩展命令")]
            # 扩展的 usage 习惯写全（"/plan [需求]"），只写了参数的补上命令名
            lines += table([(c.usage if c.usage.startswith("/") else f"/{c.name} {c.usage}".rstrip(),
                             c.description) for c in extension])
        if templates:
            lines += ["", t.muted("  提示模板")]
            lines += table([(f"/{p.name}", p.description) for p in templates])
        lines += ["", t.muted("  快捷键")]
        lines += [f"  {t.accent(pad(key, width))}{desc}" for key, desc in SHORTCUTS]
        self.out(["", *lines, ""])

    # -- 会话 ---------------------------------------------------------- #

    def attach(self, session: Any) -> None:
        """渲染器改订阅这个会话。换会话（/clear、/resume）时旧会话随之释放"""
        if self._unsubscribe is not None:
            self._unsubscribe()
        if session is not self.session:
            self.session.dispose()
        self.session = session
        session.user_input = self.ask_user     # 计划模式的 ask_user 工具经它弹选择器
        session.permission_handler = self.confirm_tool     # 改文件、跑命令前经它确认
        render = session.subscribe(self.renderer.on_event)
        own = session.subscribe(self._on_session_event)
        self._unsubscribe = lambda: (render(), own())
        self.renderer.mode = session.mode
        from pi_coding_agent.builtin_extensions.permissions import MODES, current_mode
        mode = current_mode(session)
        self.renderer.permission = MODES[mode] if mode != "ask" else ""
        # 恢复的会话里有没做完的任务清单：接着显示
        from pi_coding_agent.builtin_extensions.todo import latest_todo
        record = latest_todo(session)
        self.renderer.plan = record["plan"] if record else None
        self.renderer.status.model = session.model.key if session.model else "none"
        self.renderer.status.thinking = session.thinking_level
        self.renderer.status.hint = "/help"

    def greet(self) -> None:
        session = self.session
        self.renderer.header(session.cwd, self.renderer.status.model, session.thinking_level)
        self.continue_hint()

    def continue_hint(self) -> None:
        session = self.session
        if session.can_continue:
            note = (f"（{len(session.interrupted_tool_calls)} 个工具调用没有返回结果）"
                    if session.interrupted_tool_calls else "")
            self.out([self.theme.warn(f"  上次的任务在一轮中途中断了{note}，输入 /continue 接着执行")])

    def switch_model(self, model: Any, level: str) -> None:
        session = self.session
        if model is not None and (session.model is None or model.key != session.model.key):
            session.set_model(model)
        if level != session.thinking_level:
            session.set_thinking_level(level)
        self.renderer.status.model = session.model.key if session.model else "none"
        self.renderer.status.thinking = session.thinking_level
        t = self.theme
        think = t.highlight(level, bold=True) if level != "off" else t.muted("off")
        model_key = t.accent(self.renderer.status.model, bold=True)
        self.out([f"  {t.success('✓')} 模型 {model_key}  {t.muted('·')}  思考 {think}"])

    def _replace_session(self, manager: SessionManager, **kwargs: Any) -> None:
        old = self.session
        session = create_agent_session(services=old.services, session_manager=manager, **kwargs)
        self.attach(session)
        self.renderer.reset()
        self.turns = 0

    async def new_session(self) -> None:
        """/clear：新会话，沿用当前模型和思考档位"""
        old = self.session
        manager = (SessionManager.create(old.cwd, self.agent_dir) if old.session.is_persisted()
                   else SessionManager.in_memory(old.cwd))
        self._replace_session(manager, model=old.model, thinking_level=old.thinking_level)
        self.out([""])
        self.greet()

    async def open_session(self, path: str | Path) -> None:
        """/resume：打开历史会话，模型和思考档位从会话记录里恢复"""
        try:
            manager = SessionManager.open(path, self.agent_dir)
        except Exception as exc:
            self.error(f"打不开会话：{type(exc).__name__}: {exc}")
            return
        self._replace_session(manager)
        self.out(self.renderer.recap(self.session.messages))
        self.note(f"已恢复会话 {Path(path).name} · {len(self.session.messages)} 条消息 · "
                  f"{self.renderer.status.model}")
        self.continue_hint()

    # -- 运行 ---------------------------------------------------------- #

    # -- 计划 / 执行模式 ------------------------------------------------- #

    def _on_session_event(self, event: Any) -> None:
        if getattr(event, "type", "") == "plan_proposed":
            self._proposed = event.plan

    def cycle_mode(self) -> None:
        """shift+tab：执行 ⇄ 计划（同 Claude Code）。在输入框里按也行：只改状态，下一轮生效"""
        modes = [m.id for m in self.session.available_modes()]
        order = [m for m in ("chat", "plan") if m in modes] + [m for m in modes if m not in ("chat", "plan")]
        if len(order) < 2:
            return
        current = self.session.mode if self.session.mode in order else order[0]
        self.session.switch_mode(order[(order.index(current) + 1) % len(order)])

    async def review_plan(self, plan: dict) -> None:
        """模型提交了计划：问用户执行 / 继续完善 / 先不执行（同 Codex 的 "Implement this plan?"）"""
        steps = len(plan.get("steps") or [])
        items = [
            PickerItem("执行计划", f"切到执行模式，按 {steps} 项任务逐项完成", value="run"),
            PickerItem("继续完善", "留在计划模式，直接输入修改意见", value="revise"),
            PickerItem("先不执行", "计划已保存，之后可以用 /build 执行", value="later"),
        ]
        result = await self.pick("执行这个计划？", items, header="需要你确认")
        choice = result.item.value if result else "later"
        if choice == "run":
            await self.execute_plan()
        elif choice == "revise":
            self.note("留在计划模式：直接输入修改意见，模型会重新提交完整的计划")
        else:
            self.note("计划已保存，随时用 /build 执行")

    async def ask_user(self, questions: list[dict]) -> list[dict] | None:
        """模型在一轮中途提问（ask_user 工具）：逐题弹选择器，最后一项是自己输入。
        Esc 跳过全部（模型按推荐的选项继续）。答案由渲染器在工具结束时写进 scrollback"""
        self.renderer.pause()
        try:
            answers = []
            for number, question in enumerate(questions, 1):
                options = question["options"]
                items = [PickerItem(o["label"] + ("（推荐）" if o.get("recommended") else ""),
                                    o.get("description", ""), value=o["label"]) for o in options]
                items.append(PickerItem("其他", "自己输入回答", value=None))
                start = next((i for i, o in enumerate(options) if o.get("recommended")), 0)
                header = "需要你选择"
                if len(questions) > 1:
                    header += f" · {number}/{len(questions)}"
                if question.get("header"):
                    header += f" · {question['header']}"
                result = await self.pick(question["question"], items, index=start, header=header)
                if result is None:
                    return None
                answer, custom = result.item.value, False
                if answer is None:
                    answer, custom = await self._read_answer(header, question["question"]), True
                    if not answer:
                        return None
                answers.append({"question": question["question"], "answer": answer, "custom": custom})
            return answers
        finally:
            self.renderer.resume()

    async def confirm_tool(self, request: dict) -> dict:
        """改文件、跑命令前确认（权限扩展发来的请求）：允许 / 本会话不再询问 / 拒绝 / 拒绝并说明。
        Esc 拒绝并停止这一轮。结论留一行在 scrollback 里"""
        t = self.theme
        detail = []
        for line in request.get("detail") or []:
            if line.startswith("- "):
                detail.append(t.error(line))
            elif line.startswith("+ "):
                detail.append(t.success(line))
            else:
                detail.append(t.muted(line) if request.get("kind") != "bash" else t.bold(line))
        items = [PickerItem("允许", "只这一次", value="allow")]
        if request.get("remember"):
            items.append(PickerItem("允许，并且" + request["remember"]["label"], value="always"))
        items += [PickerItem("拒绝", "告诉模型别这么做", value="deny"),
                  PickerItem("拒绝并说明", "输入你希望它怎么做", value="explain")]
        header = f"需要你确认 · {request.get('title') or '执行操作'}"
        self.renderer.pause()
        try:
            result = await self.pick(request.get("question") or "允许吗？", items, header=header, detail=detail)
            choice = result.item.value if result else "abort"
            feedback = ""
            if choice == "explain":
                feedback = await self._read_answer(header, "告诉模型你希望它怎么做")
                choice = "deny"
        finally:
            self.renderer.resume()
        summary = truncate(" ".join(str(request.get("summary") or "").split()), max(self.renderer.width - 20, 20))
        if choice == "always":
            self.out([f"  {t.success('✓')} {t.muted(request['remember']['label'])}"])
        elif choice == "deny":
            note = f"：{feedback}" if feedback else ""
            text = f"拒绝了 {request.get('tool', '')} {summary}{note}"
            self.out([f"  {t.error('✗')} {t.muted(text)}"])
        elif choice == "abort":
            self.out([f"  {t.error('✗')} {t.muted(f'拒绝并停止：{summary}')}"])
        return {"decision": choice, "feedback": feedback}

    async def _read_answer(self, header: str, question: str) -> str:
        """选了「其他」：问题留在输入行上面（选择器已经擦掉了），输完连同问题一起擦掉"""
        t = self.theme
        bar = t.accent("│", bold=True)
        prompt = "\n".join([
            "",
            t.accent("╭─ ", bold=True) + t.highlight(header, bold=True),
            *[f"{bar} {t.bold(line)}" for line in wrap(question, max(self.renderer.width - 3, 10))],
            t.accent("╰─ ", bold=True) + t.highlight("你的回答", bold=True)
            + t.muted("（Enter 确认，留空取消）") + t.accent(" ❯ ", bold=True),
        ])
        reader = LineReader(erase_when_done=True)
        try:
            raw = await reader.read(prompt)
        except (EOFError, KeyboardInterrupt):
            return ""
        return sanitize(raw).strip()

    async def execute_plan(self, extra: str = "") -> None:
        """批准最近的计划：切到执行模式、任务变成清单、开始执行。只回显一行标题，不回显整段任务说明"""
        from pi_coding_agent.builtin_extensions.plan_mode import approve_plan, latest_plan

        plan = latest_plan(self.session)
        try:
            task = approve_plan(self.session, extra)
        except LookupError as exc:
            self.error(str(exc))
            return
        t = self.theme
        self.out([f"  {t.success('✓')} 执行计划 {t.bold((plan or {}).get('title') or '')}"])
        await self.send(task)

    async def run_agent(self, run: Awaitable[Any]) -> None:
        self.turns += 1
        try:
            await run
        except KeyboardInterrupt:
            self.session.abort()
        except Exception as exc:
            self.error(f"{type(exc).__name__}: {exc}")

    async def send(self, text: str) -> None:
        # 输入行已经显示过（或 user_echo 回显过）这句话，渲染器不要再打一遍
        self.renderer.state.own_prompt = text
        self._proposed = None
        await self.run_agent(self.session.prompt(text))
        if self._proposed is not None:
            plan, self._proposed = self._proposed, None
            await self.review_plan(plan)

    async def dispatch(self, line: str) -> bool:
        """执行一条 / 开头的输入。返回 False 表示退出"""
        name, _, rest = line[1:].partition(" ")
        rest = rest.strip()
        if not name:
            self.print_help()
            return True
        command = self.find(name)
        if command is not None:
            try:
                return await command.handler(self, rest) is not False
            except Exception as exc:
                self.error(f"/{name} 失败：{type(exc).__name__}: {exc}")
                return True
        if name in self._extension_commands():
            try:
                output = await self.session.run_command(name, rest)
            except Exception as exc:
                self.error(f"/{name} 失败：{type(exc).__name__}: {exc}")
                return True
            if output:
                self.out([f"  {text}" for text in str(output).splitlines()])
            return True
        if any(p.name == name for p in self.session.resources.get_prompts()):
            await self.send(line)       # 提示模板：AgentSession.prompt 负责展开
            return True
        if COMMAND_NAME.fullmatch(name):
            self.error(f"未知命令 /{name}，输入 /help 查看可用命令")
            return True
        await self.send(line)
        return True

    async def run(self) -> int:
        self.greet()
        self.reader = LineReader(
            history_file=Path.home() / ".pi" / "agent" / "history",
            key_handlers={"c-o": self.renderer.expand_last_turn},
            key_actions={"s-tab": self.cycle_mode},
            erase_when_done=True,
            commands=self.slash_commands,
            footer=self.renderer.input_footer,
        )
        hint = missing_dependency_hint(self.theme)
        if hint:
            print(hint)
        while True:
            try:
                prompt = self.renderer.input_prompt() if self.reader.rich else "\033[1m❯ \033[0m"
                raw = await self.reader.read(prompt)
            except (EOFError, KeyboardInterrupt):
                print()
                break
            line = sanitize(raw).strip()
            if not line:
                continue
            if self.reader.rich:
                # 输入框提交后被擦掉了，用紧凑的样式把这句话留在 scrollback 里
                self.renderer.user_echo(line)
            if line.startswith("/"):
                if not await self.dispatch(line):
                    break
                continue
            await self.send(line)

        model = self.session.model
        self.renderer.summary(
            model.key if model else "none",
            self.turns,
            self.session.usage().input,
            model.context_window if model else 0,
        )
        return 0


async def run(session: Any, renderer: SessionRenderer, agent_dir: str | Path | None = None) -> int:
    return await App(session, renderer, agent_dir=agent_dir).run()


async def main_async(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pi-tui", description="pi 的终端界面")
    parser.add_argument("-C", "--cwd", default=".")
    parser.add_argument("-m", "--model")
    parser.add_argument("-t", "--think", default="off")
    parser.add_argument("--no-thinking", action="store_true", help="不显示思考内容")
    parser.add_argument("--no-diff", action="store_true", help="不显示 diff")
    parser.add_argument("--no-session", action="store_true")
    parser.add_argument("--continue", dest="continue_recent", action="store_true",
                        help="打开当前目录最近一次的会话")
    parser.add_argument("--resume", help="打开指定的会话文件（.jsonl）")
    parser.add_argument("--agent-dir")
    args = parser.parse_args(argv)

    cwd = str(Path(args.cwd).expanduser().resolve())
    services = create_agent_session_services(cwd=cwd, agent_dir=args.agent_dir)
    if args.no_session:
        manager = SessionManager.in_memory(cwd)
    elif args.resume:
        manager = SessionManager.open(args.resume, args.agent_dir)
    elif args.continue_recent:
        manager = SessionManager.continue_recent(cwd, args.agent_dir)
    else:
        manager = SessionManager.create(cwd, args.agent_dir)
    session = create_agent_session(
        services=services, session_manager=manager, model=args.model, thinking_level=args.think
    )
    if session.model is None:
        print("没有可用模型：设置 ANTHROPIC_API_KEY 或写 ~/.pi/agent/auth.json", file=sys.stderr)
        return 1

    renderer = SessionRenderer(
        theme=Theme(), show_thinking=not args.no_thinking, show_diff=not args.no_diff
    )
    app = App(session, renderer, agent_dir=args.agent_dir)
    try:
        result = await app.run()
    finally:
        renderer.stop()
        app.session.dispose()
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(main_async(argv))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())