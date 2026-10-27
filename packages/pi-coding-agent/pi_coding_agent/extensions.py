"""
扩展插件。

扩展是提供 ``activate(pi)`` 入口的Python模块。通过 ``pi`` 对象，插件可以注册工具与斜杠命令、
订阅Agent事件，并依托共享事件总线与其他扩展通信。

安全提示：和主框架权限规则一致，扩展拥有进程完整操作权限，安装前请审阅代码。
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from pi_agent import AgentEvent, AgentTool, AgentToolResult, BeforeToolCallResult, ToolContext


@dataclass
class Command:
    name: str
    description: str
    handler: Callable[[str], Awaitable[str | None]]
    usage: str = ""  # 参数说明，供 /help 和网页命令面板显示。


@dataclass
class Mode:
    """
    工作模式（聊天、计划……）。
    模式不是核心概念——核心只知道"有人想在每轮开始前改状态"。
    具体行为由扩展通过 apply 决定，所以加新模式不用动任何下层代码。
    """
    id: str
    label: str
    description: str = ""
    # 每轮开始前调用，入参是 AgentSession。可改系统提示、换工具集
    apply: Callable[[Any], Awaitable[None]] | None = None
    badge: str = ""


class EventBus:

    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable[[Any], None]]] = {}

    def on(self, event: str, handler: Callable[[Any], None]) -> Callable[[], None]:
        self._handlers.setdefault(event, []).append(handler)

        def off() -> None:
            if handler in self._handlers.get(event, []):
                self._handlers[event].remove(handler)

        return off

    def emit(self, event: str, data: Any = None) -> None:
        for handler in list(self._handlers.get(event, [])):
            try:
                handler(data)
            except Exception:
                pass


def create_event_bus() -> EventBus:
    return EventBus()


@dataclass
class ExtensionAPI:
    """传递给所有扩展的 `pi` 核心对象"""

    cwd: str
    events: EventBus
    tools: list[AgentTool] = field(default_factory=list)
    commands: dict[str, Command] = field(default_factory=dict)
    agent_event_handlers: dict[str, list[Callable[[AgentEvent], None]]] = field(default_factory=dict)
    session: Any = None
    state: dict[str, Any] = field(default_factory=dict)
    modes: dict[str, Mode] = field(default_factory=dict)
    before_start_hooks: list[Callable[[Any], Awaitable[None]]] = field(default_factory=list)
    context_hooks: list[Callable[[list], Awaitable[list]]] = field(default_factory=list)
    compaction_context_hooks: list[Callable[[Any], Awaitable[str | None]]] = field(
        default_factory=list
    )
    # 工具执行前的钩子：(调用, 会话) → BeforeToolCallResult | None。权限确认就挂在这里
    tool_call_hooks: list[Callable[[Any, Any], Awaitable[BeforeToolCallResult | None]]] = field(
        default_factory=list
    )
    tool_call_errors: list[str] = field(default_factory=list)
    # 工具执行后的钩子：(调用, 结果, 会话) → 新结果 | None。改完文件就检查、修剪吵闹的输出、
    # 反复出现的报错后面追加提示——都在这里，模型跳不过去（写在提示里的规则可能被忽略）
    after_tool_hooks: list[Callable[[Any, Any, Any], Awaitable[Any]]] = field(default_factory=list)
    # 会话开始时拼进系统提示的片段：handler(session) → 文字 | None（记忆等）
    prompt_sections: list[Callable[[Any], str | None]] = field(default_factory=list)
    owner: str = ""
    # 当前正在 activate 的扩展名，用于把注册项归属到扩展上
    ownership: dict[str, list[str]] = field(default_factory=dict)

    def _own(self, kind: str, name: str) -> None:
        self.ownership.setdefault(self.owner or "inline", []).append(f"{kind}:{name}")

    def register_tool(
            self,
            name: str,
            description: str,
            parameters: dict[str, Any],
            execute: Callable[[dict[str, Any], ToolContext], Awaitable[AgentToolResult]],
            label: str | None = None
    ) -> AgentTool:
        tool = AgentTool(
            name=name,
            description=description,
            parameters=parameters,
            execute=execute,
            label=label
        )
        self.tools = [t for t in self.tools if t.name != name] + [tool]
        self._own("tool", name)
        return tool

    def register_command(
            self,
            name: str,
            description: str,
            handler: Callable[[str], Awaitable[str | None]],
            usage: str = ""
    ) -> None:
        self.commands[name] = Command(
            name=name,
            description=description,
            handler=handler,
            usage=usage
        )
        self._own("command", name)

    def register_mode(
            self,
            id: str,
            label: str,
            description: str = "",
            apply: Callable[[Any], Awaitable[None]] | None = None,
            badge: str = ""
    ) -> None:
        """注册一种工作模式,界面会把它显示成可切换的选项"""
        self.modes[id] = Mode(
            id=id,
            label=label,
            description=description,
            apply=apply,
            badge=badge
        )
        self._own("mode", id)

    def before_agent_start(self, handler: Callable[[Any], Awaitable[None]]) -> None:
        """
        每轮 prompt 开始前调用，入参是 AgentSession。
        可以改系统提示、换工具集、注入消息——计划模式就是靠它落地的
        """
        self.before_start_hooks.append(handler)
        self._own("hook", "before_agent_start")

    def transform_context(self, handler: Callable[[list], Awaitable[list]]) -> None:
        """
        每次 LLM 调用前改上下文消息列表。
        agent 层的 transform_context 早就支持，这里只是把它开放给扩展
        """
        self.context_hooks.append(handler)
        self._own("hook", "transform_context")

    def compaction_context(self, handler: Callable[[Any], Awaitable[str | None]]) -> None:
        """
        上下文压缩之后要补回给模型的内容（Markdown 片段，返回 None 表示没有）。
        入参是 AgentSession。比如计划模式在这里提供当前计划——压缩后模型仍知道在执行哪份计划
        """
        self.compaction_context_hooks.append(handler)
        self._own("hook", "compaction_context")

    async def run_compaction_context(self, session: Any) -> tuple[list[str], list[str]]:
        """返回 (各扩展给出的片段, 错误)；单个失败不影响压缩"""
        sections: list[str] = []
        errors: list[str] = []
        for handler in list(self.compaction_context_hooks):
            try:
                section = await handler(session)
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
                continue
            if isinstance(section, str) and section.strip():
                sections.append(section)
        return sections, errors

    async def run_before_start(self, session: Any) -> list[str]:
        """依次跑钩子，单个失败不影响其它 -- 一个坏扩展不该让整轮跑不起来"""
        errors: list[str] = []
        for handler in list(self.before_start_hooks):
            try:
                await handler(session)
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")

        return errors

    def before_tool_call(
            self, handler: Callable[[Any, Any], Awaitable[BeforeToolCallResult | None]]
    ) -> None:
        """工具执行前调用 handler(call, session)：返回 block=True 拦下这次调用（reason 作为工具结果
        给模型看），返回 arguments 改写参数，返回 None 放行。多个钩子按注册顺序跑，第一个拦下的生效"""
        self.tool_call_hooks.append(handler)
        self._own("hook", "before_tool_call")

    async def run_tool_call_hooks(self, call: Any, session: Any) -> BeforeToolCallResult | None:
        arguments = None
        for handler in list(self.tool_call_hooks):
            try:
                result = await handler(call, session)
            except Exception as exc:  # noqa: BLE001
                # 坏钩子不该让所有工具都跑不了；记下来，放行（权限扩展自己兜住界面出错，出错即拒绝）
                self.tool_call_errors.append(f"{type(exc).__name__}: {exc}")
                continue
            if result is None:
                continue
            if result.block:
                return result
            if result.arguments is not None:
                arguments = result.arguments
                call.arguments = arguments
        return BeforeToolCallResult(arguments=arguments) if arguments is not None else None

    def after_tool_call(self, handler: Callable[[Any, Any, Any], Awaitable[Any]]) -> None:
        """工具执行完（被拦下的不算）调用 handler(call, result, session)：返回新的 AgentToolResult
        替换结果（给模型看的和界面显示的都换），返回 None 不改。多个钩子按注册顺序串起来，
        后一个拿到前一个改过的结果"""
        self.after_tool_hooks.append(handler)
        self._own("hook", "after_tool_call")

    async def run_after_tool_hooks(self, call: Any, result: Any, session: Any) -> Any | None:
        changed = None
        for handler in list(self.after_tool_hooks):
            try:
                new = await handler(call, changed or result, session)
            except Exception as exc:  # noqa: BLE001
                # 坏钩子不该吞掉工具结果：记下来，保留原结果
                self.tool_call_errors.append(f"{type(exc).__name__}: {exc}")
                continue
            if new is not None:
                changed = new
        return changed

    def system_prompt_section(self, handler: Callable[[Any], str | None]) -> None:
        """会话开始时调一次 handler(session)，返回的文字接在基础系统提示后面——这次会话每轮都在、
        子智能体也有，会话中途不再变（每轮都变的内容用 before_agent_start 改 state.system_prompt，
        但那样会让前缀缓存失效）"""
        self.prompt_sections.append(handler)
        self._own("hook", "system_prompt_section")

    def run_prompt_sections(self, session: Any) -> tuple[list[str], list[str]]:
        """返回 (片段, 错误)；出错的片段跳过"""
        parts, errors = [], []
        for handler in list(self.prompt_sections):
            try:
                text = handler(session)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")
                continue
            if text:
                parts.append(text)
        return parts, errors

    async def run_context_hook(self, messages: list) -> list:
        for handler in list(self.context_hooks):
            try:
                result = await handler(messages)
                if isinstance(result, list):
                    messages = result
            except Exception:
                pass

        return messages

    def on(self, event_type: str, handler: Callable[[AgentEvent], None]) -> None:
        """订阅一个 Agent 事件类型，例如 `agent_start` 或者 `turn_end`"""
        self.agent_event_handlers.setdefault(event_type, []).append(handler)

    def dispatch(self, event: AgentEvent) -> None:
        for handler in self.agent_event_handlers.get(event.type, []):
            try:
                handler(event)
            except Exception:
                pass

    async def send_message(self, text: str) -> None:
        """让扩展模块驱动Agent，供各类命令调用"""
        if self.session is None:
            raise RuntimeError("no session bound to this extension runtime")
        await self.session.prompt(text)


@dataclass
class LoadedExtension:
    name: str
    path: str | None
    error: str | None = None
    description: str = ""
    registered: list[str] = field(default_factory=list)
    enabled: bool = True


def load_extension_file(path: Path, api: ExtensionAPI) -> LoadedExtension:
    name = f"pi_ext_{path.stem}"

    try:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            return LoadedExtension(path.stem, str(path), "could not load module spec")

        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        activate = getattr(module, "activate", None)
        if activate is None:
            return LoadedExtension(path.stem, str(path), "no activate(pi) function")
        ext_name = getattr(module, "NAME", path.stem)
        api.owner = ext_name
        try:
            activate(api)
        finally:
            api.owner = ""
        return LoadedExtension(
            ext_name,
            str(path),
            description=getattr(module, "DESCRIPTION", ""),
            registered=list(api.ownership.get(ext_name, []))
        )
    except Exception as exc:
        return LoadedExtension(path.stem, str(path), f"{type(exc).__name__}: {exc}")


def load_extensions(
        directories: list[Path],
        api: ExtensionAPI,
        disabled: set[str] | None = None
) -> list[LoadedExtension]:
    """
    加载目录下的扩展
    disabled 里的扩展仍会被列出（界面要显示它存在），但不执行 activate——
    所以它注册的工具、命令、钩子一个都不会生效。
    """
    disabled = disabled or set()
    loaded: list[LoadedExtension] = []
    for directory in directories:
        if not directory.exists():
            continue
        for file in sorted(directory.glob("*.py")):
            if file.name.startswith("_"):
                continue
            if file.stem in disabled:
                loaded.append(LoadedExtension(
                    file.stem,
                    str(file),
                    enabled=False,
                    description="（已停用）"
                ))
                continue
            loaded.append(load_extension_file(file, api))

    return loaded
