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

from pi_agent import AgentEvent, AgentTool, AgentToolResult, ToolContext

@dataclass
class Command:
    name: str
    description: str
    handler: Callable[[str], Awaitable[str | None]]

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
        return tool

    def register_command(
            self, name: str, description: str, handler: Callable[[str], Awaitable[str | None]]
    ) -> None:
        self.commands[name] = Command(name=name, description=description, handler=handler)

    def on(self, event_type: str, handler: Callable[[AgentEvent], None]) -> None:
        """订阅Agent事件类型，如 agent_start、turn_end"""
        self.agent_event_handlers.setdefault(event_type, []).append(handler)

    def dispatch(self, event: AgentEvent) -> None:
        for handler in self.agent_event_handlers.get(event.type, []):
            try:
                handler(event)
            except Exception:
                pass

    async def send_message(self, text: str) -> None:
        """允许扩展驱动Agent执行流程（供命令使用）"""
        if self.session is None:
            raise RuntimeError("no session bound to this extension runtime")
        await self.session.prompt(text)

@dataclass
class LoadedExtension:
    name: str
    path: str | None
    error: str | None = None


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
        activate(api)
        return LoadedExtension(getattr(module, "NAME", path.stem), str(path))
    except Exception as exc:
        return LoadedExtension(path.stem, str(path), f"{type(exc).__name__}: {exc}")

def load_extensions(directories: list[Path], api: ExtensionAPI) -> list[LoadedExtension]:
    loaded: list[LoadedExtension] = []
    for directory in directories:
        if not directory.exists():
            continue
        for file in sorted(directory.glob("*.py")):
            if file.name.startswith("_"):
                continue
            loaded.append(load_extension_file(file, api))

    return loaded