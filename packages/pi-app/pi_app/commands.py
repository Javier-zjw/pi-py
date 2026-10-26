"""斜杠命令表。和 Claude Code 对齐：

    /model     选择模型（↑↓）和思考档位（←→）     /clear     开始新会话（别名 /new /reset）
    /resume    从历史会话里选一个接着聊            /compact   压缩上下文
    /context   上下文用量和压缩阈值                /cost      token 与费用（别名 /usage）
    /status    模型、会话、扩展等状态（别名 /session）
    /help /exit                                    ctrl+o（/expand）展开上一轮

pi 自己的：/think 单独调思考档位、/continue 续跑中断的一轮、/tools 列出工具。
扩展注册的命令（如 /plan）和提示模板也出现在补全菜单里，由 App 分派。

每条命令是 ``async handler(app, rest)``；返回 False 表示退出。输出走 ``app.out``，
选择器走 pi_tui.pick。这里只做"命令 → 会话 API"的翻译，不放业务逻辑。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pi_ai import THINKING_LEVELS
from pi_coding_agent.session import CompactionEntry, list_sessions
from pi_tui import PickerItem, key_value

if TYPE_CHECKING:
    from .app import App

Handler = Callable[["App", str], Awaitable[bool | None]]

LEVEL_NOTES = {
    "off": "不思考，最快",
    "minimal": "极少量思考",
    "low": "少量思考",
    "medium": "适中",
    "high": "深入思考",
    "xhigh": "非常深入",
    "max": "最大思考预算",
}


@dataclass(frozen=True)
class AppCommand:
    name: str
    description: str
    handler: Handler
    aliases: tuple[str, ...] = ()
    usage: str = ""


def _k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def ago(ts: float, now: float | None = None) -> str:
    seconds = max(0, int((now or time.time()) - ts))
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前"
    if seconds < 86400 * 30:
        return f"{seconds // 86400} 天前"
    return time.strftime("%Y-%m-%d", time.localtime(ts))


# -- 模型与思考 ---------------------------------------------------------- #

def thinking_choices(model: Any) -> list[str]:
    """模型能用的思考档位。不支持思考的模型返回空列表。
    models.json 的 thinkingLevelMap 里显式写成 null 的档位视为不支持"""
    if model is None or not model.reasoning:
        return []
    levels = model.thinking_level_map or {}
    return [lvl for lvl in THINKING_LEVELS
            if lvl == "off" or lvl not in levels or levels[lvl] is not None]


def model_items(models: list[Any], current: Any, level: str) -> tuple[list[PickerItem], int]:
    """模型选择器的行。每个支持思考的模型默认停在当前档位（不支持该档位就 off），
    这样 ↑↓ 换模型不会顺手把思考档位丢掉。当前模型不在可用列表里（比如没配 key）也放在第一行"""
    if current is not None and all(m.key != current.key for m in models):
        models = [current, *models]
    items: list[PickerItem] = []
    index = 0
    for i, model in enumerate(models):
        is_current = current is not None and model.key == current.key
        choices = thinking_choices(model)
        name = model.name if model.name and model.name != model.id else ""
        description = " · ".join(b for b in (name, f"{_k(model.context_window)} 上下文") if b)
        items.append(PickerItem(
            model.key,
            description,
            choices=choices,
            choice=choices.index(level) if level in choices else 0,
            note="" if choices else "不支持思考",
            current=is_current,
            value=model,
        ))
        if is_current:
            index = i
    return items, index


async def cmd_model(app: App, rest: str) -> None:
    session = app.session
    if rest:
        model = session.model_runtime.resolve(rest)
        if model is None:
            app.error(f"未知模型：{rest}（输入 /model 打开选择器）")
            return
        level = session.thinking_level
        app.switch_model(model, level if level in thinking_choices(model) else "off")
        return
    models = session.model_runtime.available_models()
    if not models and session.model is None:
        app.note("没有可用模型：设置 API key 或写 ~/.pi/agent/auth.json")
        return
    items, index = model_items(models, session.model, session.thinking_level)
    result = await app.pick("选择模型", items, index=index, choice_label="思考")
    if result is None:
        app.note("已取消，模型未变")
        return
    app.switch_model(result.item.value, result.choice or "off")


async def cmd_think(app: App, rest: str) -> None:
    session = app.session
    choices = thinking_choices(session.model)
    key = session.model.key if session.model else "当前模型"
    if rest:
        if rest not in THINKING_LEVELS:
            app.error(f"未知档位：{rest}，可选 {' '.join(THINKING_LEVELS)}")
        elif rest != "off" and rest not in choices:
            app.error(f"{key} 不支持思考档位 {rest}")
        else:
            app.switch_model(session.model, rest)
        return
    if not choices:
        app.note(f"{key} 不支持思考；/model 可以换一个支持思考的模型")
        return
    items = [PickerItem(lvl, LEVEL_NOTES.get(lvl, ""), current=lvl == session.thinking_level, value=lvl)
             for lvl in choices]
    index = next((i for i, item in enumerate(items) if item.current), 0)
    result = await app.pick(f"思考档位 · {key}", items, index=index)
    if result is None:
        app.note("已取消，思考档位未变")
        return
    app.switch_model(session.model, result.item.value)


# -- 会话 ---------------------------------------------------------------- #

async def cmd_clear(app: App, rest: str) -> None:
    await app.new_session()


async def cmd_resume(app: App, rest: str) -> None:
    session = app.session
    current = session.session_file
    sessions = list_sessions(session.cwd, app.agent_dir, limit=30, include_empty=False)
    if rest:
        target = Path(rest).expanduser()
        if not target.exists():
            target = next((s.path for s in sessions if s.name.startswith(rest)), None)
        if target is None:
            app.error(f"找不到会话：{rest}")
            return
    else:
        if not sessions:
            app.note("当前目录还没有历史会话")
            return
        items = [PickerItem(s.title, f"{ago(s.mtime)} · {s.turns} 条消息",
                            current=current is not None and s.path == current, value=s.path)
                 for s in sessions]
        result = await app.pick("恢复会话", items)
        if result is None:
            app.note("已取消")
            return
        target = result.item.value
    if current is not None and Path(target).resolve() == Path(current).resolve():
        app.note("已经是当前会话")
        return
    await app.open_session(target)


async def cmd_plan(app: App, rest: str) -> None:
    app.session.switch_mode("plan")
    if rest:
        await app.send(rest)
    else:
        app.note("已进入计划模式：只读调研，先出带任务清单的计划。描述你的需求开始（shift+tab 切回）")


async def cmd_build(app: App, rest: str) -> None:
    await app.execute_plan(rest)


async def cmd_permissions(app: App, rest: str) -> None:
    """权限模式：每次确认 / 自动接受文件修改 / 全部自动。只对当前会话生效，长期设置写 settings.json"""
    from pi_coding_agent.builtin_extensions.permissions import (
        MODES,
        current_mode,
        describe,
        set_mode,
    )

    session = app.session
    if rest:
        try:
            set_mode(session, rest)
        except ValueError as exc:
            app.error(str(exc))
            return
    else:
        notes = {"ask": "改文件、跑命令前都问（只读命令除外）",
                 "acceptEdits": "工作目录里的文件修改直接做，命令仍然要问",
                 "auto": "全部直接执行，不再询问（deny 规则仍然生效）"}
        mode = current_mode(session)
        items = [PickerItem(label, notes[key], current=key == mode, value=key) for key, label in MODES.items()]
        result = await app.pick("权限模式", items, index=list(MODES).index(mode))
        if result is None:
            app.note("已取消，权限模式未变")
            return
        set_mode(session, result.item.value)
    app.renderer.permission = MODES[current_mode(session)] if current_mode(session) != "ask" else ""
    app.out([f"  {line}" for line in describe(session).splitlines()])


async def cmd_continue(app: App, rest: str) -> None:
    if not app.session.can_continue:
        app.note("没有可以继续的任务：上一轮已经完成")
        return
    await app.spawn(app.run_agent(app.session.continue_()))


async def cmd_compact(app: App, rest: str) -> None:
    # 进度和结果由渲染器根据 compaction_* 事件显示；输入框收起着，活动区直接画
    with app.renderer.direct():
        try:
            await app.session.compact(rest or None)
        except KeyboardInterrupt:
            app.session.abort()


# -- 信息 ---------------------------------------------------------------- #

def usage_bar(used: int, total: int, width: int = 24) -> str:
    if total <= 0:
        return ""
    filled = min(width, round(used / total * width))
    return "█" * filled + "░" * (width - filled)


async def cmd_context(app: App, rest: str) -> None:
    session, t = app.session, app.theme
    model = session.model
    window = model.context_window if model else 0
    used = session.context_tokens()
    pct = f"{used / window * 100:.1f}%" if window else "?"
    bar = t.info(usage_bar(used, window))
    rows = [("上下文", f"{t.info(_k(used), bold=True)} / {_k(window)}  {t.muted(pct)}  {bar}")]
    budget = session.compaction_budget()
    if budget is not None and session.settings.get("compaction.enabled", True):
        prune = f"≥ {_k(budget.prune_trigger)} 清理旧工具输出" if budget.prune_enabled else ""
        rows.append(("自动", " · ".join(b for b in (prune, f"≥ {_k(budget.trigger)} 摘要压缩") if b)))
    else:
        rows.append(("自动", "压缩已关闭（settings: compaction.enabled）"))
    compactions = sum(isinstance(e, CompactionEntry) for e in session.session.get_branch())
    rows.append(("消息", f"{len(session.messages)} 条 · 已压缩 {compactions} 次"))
    app.out(key_value(rows, t))


async def cmd_cost(app: App, rest: str) -> None:
    t = app.theme
    u = app.session.usage()
    rows = [
        ("输入", t.info(f"↑{_k(u.input)}", bold=True)),
        ("输出", t.success(f"↓{_k(u.output)}", bold=True)),
    ]
    if u.cache_read or u.cache_write:
        rows.append(("缓存", f"读 {_k(u.cache_read)} · 写 {_k(u.cache_write)}"))
    rows.append(("费用", t.warn(f"¥{u.cost.total:.4f}")))
    rows.append(("轮次", str(app.turns)))
    app.out(key_value(rows, t))


async def cmd_status(app: App, rest: str) -> None:
    session, t = app.session, app.theme
    extensions = session.resources.get_extensions()
    ext = ", ".join(
        e.name + (t.error(" ✗") if e.error else "") + ("" if getattr(e, "enabled", True) else t.muted(" (停用)"))
        for e in extensions
    )
    rows = [
        ("模型", session.model.key if session.model else "none"),
        ("思考", session.thinking_level),
        ("模式", session.mode),
        ("目录", session.cwd),
        ("会话", str(session.session_file or "(内存，不保存)")),
        ("工具", ", ".join(tool.name for tool in session.agent.state.tools)),
        ("扩展", ext or t.muted("无")),
    ]
    diagnostics = session.services.diagnostics + [f"{e.name}: {e.error}" for e in extensions if e.error]
    if diagnostics:
        rows.append(("诊断", t.warn(f"{len(diagnostics)} 条")))
    app.out(key_value(rows, t))
    for note in diagnostics:
        app.out([f"    {t.warn('!')} {t.muted(note)}"])


async def cmd_tools(app: App, rest: str) -> None:
    t = app.theme
    tools = app.session.agent.state.tools
    width = max((len(tool.name) for tool in tools), default=0)
    app.out([f"  {t.accent(tool.name.ljust(width))}  "
             f"{t.muted((tool.description.splitlines() or [''])[0][:70])}" for tool in tools])


async def cmd_expand(app: App, rest: str) -> None:
    await app.show_details()


async def cmd_help(app: App, rest: str) -> None:
    app.print_help()


async def cmd_exit(app: App, rest: str) -> bool:
    return False


BUILTIN_COMMANDS: list[AppCommand] = [
    AppCommand("help", "显示命令和快捷键", cmd_help),
    AppCommand("model", "选择模型和思考档位（↑↓ 模型，←→ 思考）", cmd_model, usage="[provider/model]"),
    AppCommand("think", "设置思考档位", cmd_think, usage="[档位]"),
    AppCommand("clear", "开始新会话（清空上下文）", cmd_clear, aliases=("new", "reset")),
    AppCommand("resume", "恢复历史会话", cmd_resume, usage="[会话文件]"),
    AppCommand("plan", "进入计划模式（只读调研、先出计划；shift+tab 切换）", cmd_plan, usage="[需求]"),
    AppCommand("build", "执行最近提交的计划", cmd_build, usage="[补充说明]"),
    AppCommand("continue", "接着执行上次中断的那一轮", cmd_continue),
    AppCommand("permissions", "权限模式：每次确认 / 自动接受文件修改 / 全部自动", cmd_permissions,
               usage="[ask|acceptEdits|auto]"),
    AppCommand("compact", "压缩上下文，可附加压缩重点", cmd_compact, usage="[说明]"),
    AppCommand("context", "上下文用量和压缩阈值", cmd_context),
    AppCommand("cost", "本次会话的 token 与费用", cmd_cost, aliases=("usage",)),
    AppCommand("status", "模型、会话、工具、扩展等状态", cmd_status, aliases=("session",)),
    AppCommand("tools", "列出当前启用的工具", cmd_tools),
    AppCommand("expand", "全屏查看上一轮的思考和工具调用（ctrl+o）", cmd_expand),
    AppCommand("exit", "退出", cmd_exit, aliases=("quit",)),
]

