"""会话注册表：进程内持有活跃的 AgentSession，并把事件扇出给订阅者。

不 import fastapi，纯 asyncio，可以单测。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pi_coding_agent import (
    SessionManager,
    create_agent_session,
    create_agent_session_services,
)

from .dto import EventTranslator

MAX_BUFFERED_FRAMES = 2000


@dataclass
class LiveSession:
    """一个会话 = 一个 AgentSession + 一份帧历史 + 若干订阅者队列。"""

    id: str
    session: Any
    translator: EventTranslator = field(default_factory=EventTranslator)
    frames: list[dict] = field(default_factory=list)
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    running: asyncio.Task | None = None
    title: str = ""

    def publish(self, frame: dict) -> None:
        # 留一份历史，页面刷新或断线重连时可以补齐
        self.frames.append(frame)
        if len(self.frames) > MAX_BUFFERED_FRAMES:
            del self.frames[: len(self.frames) - MAX_BUFFERED_FRAMES]
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                pass  # 慢客户端不该拖垮别人

    def on_agent_event(self, event: Any) -> None:
        for frame in self.translator.translate(event):
            self.publish(frame)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if queue in self.subscribers:
            self.subscribers.remove(queue)

    @property
    def busy(self) -> bool:
        return self.running is not None and not self.running.done()


class SessionRegistry:
    """所有 web 会话的容器。服务端唯一的状态。"""

    def __init__(self, cwd: str | Path = ".", agent_dir: str | Path | None = None) -> None:
        self.cwd = str(Path(cwd).expanduser().resolve())
        self.agent_dir = agent_dir
        # 每个工作目录一套 services（技能、扩展、设置都是目录相关的），
        # 缓存起来避免每次新建会话都重新扫一遍磁盘
        self._services_cache: dict[str, Any] = {}
        self.services = self.services_for(self.cwd)
        self._sessions: dict[str, LiveSession] = {}

    def services_for(self, cwd: str | Path | None = None):
        key = str(Path(cwd or self.cwd).expanduser().resolve())
        if key not in self._services_cache:
            self._services_cache[key] = create_agent_session_services(
                cwd=key, agent_dir=self.agent_dir
            )
        return self._services_cache[key]

    def set_cwd(self, cwd: str | Path) -> str:
        """切换默认工作目录。已存在的会话不受影响。"""
        target = Path(cwd).expanduser().resolve()
        if not target.is_dir():
            raise NotADirectoryError(str(cwd))
        self.cwd = str(target)
        self.services = self.services_for(self.cwd)
        return self.cwd

    # -- 元信息 -------------------------------------------------------- #

    def list_models(self) -> list[dict]:
        runtime = self.services.model_runtime
        available = {m.key for m in runtime.available_models()}
        return [
            {
                "key": m.key,
                "id": m.id,
                "provider": m.provider,
                "name": m.name or m.id,
                "reasoning": m.reasoning,
                "contextWindow": m.context_window,
                "available": m.key in available,
            }
            for m in runtime.list_models()
        ]

    def list_tools(self, cwd: str | None = None) -> list[dict]:
        from pi_coding_agent.tools import ALL_TOOL_NAMES, DEFAULT_TOOL_NAMES, create_tools

        services = self.services_for(cwd)
        tools = create_tools(ALL_TOOL_NAMES, cwd or self.cwd)
        extension_tools = list(services.resources.get_extension_api().tools)
        out = []
        for tool in tools + extension_tools:
            out.append(
                {
                    "name": tool.name,
                    "label": tool.label or tool.name,
                    "description": tool.description.split("\n")[0],
                    "default": tool.name in DEFAULT_TOOL_NAMES,
                    "source": "builtin" if tool in tools else "extension",
                }
            )
        return out

    def list_extensions(self, cwd: str | None = None) -> list[dict]:
        services = self.services_for(cwd)
        disabled = set(services.settings.get("disabledExtensions") or [])
        out = []
        for e in services.resources.get_extensions():
            key = Path(e.path).stem if e.path else e.name
            out.append({
                "key": key,
                "name": e.name,
                "description": e.description,
                "path": e.path,
                "error": e.error,
                "enabled": key not in disabled and e.enabled,
                "registered": e.registered,
                "builtin": bool(e.path and "builtin_extensions" in str(e.path)),
            })
        return out

    def toggle_extension(self, key: str, enabled: bool, cwd: str | None = None) -> list[dict]:
        """启用/停用扩展。写进设置并重新加载，活跃会话的工具集一起重建。"""
        services = self.services_for(cwd)
        disabled = set(services.settings.get("disabledExtensions") or [])
        disabled.discard(key) if enabled else disabled.add(key)
        services.settings.set("disabledExtensions", sorted(disabled))
        services.settings.flush()
        services.resources.set_disabled(disabled)

        # 已经跑起来的会话也要跟着变，否则要等新建会话才生效
        for live in self._sessions.values():
            if live.session.cwd == (cwd or self.cwd):
                self.update_tools(live, [t.name for t in live.session.agent.state.tools])
        return self.list_extensions(cwd)

    def list_modes(self, cwd: str | None = None) -> list[dict]:
        api = self.services_for(cwd).resources.get_extension_api()
        return [
            {"id": m.id, "label": m.label, "description": m.description, "badge": m.badge}
            for m in api.modes.values()
        ]

    def list_commands(self, cwd: str | None = None) -> list[dict]:
        services = self.services_for(cwd)
        api = services.resources.get_extension_api()
        out = [
            {"name": c.name, "description": c.description, "usage": c.usage, "source": "extension"}
            for c in api.commands.values()
        ]
        out += [
            {"name": p.name, "description": p.description, "usage": f"/{p.name} [参数]",
             "source": "prompt"}
            for p in services.resources.get_prompts()
        ]
        return sorted(out, key=lambda c: c["name"])

    def list_skills(self, cwd: str | None = None) -> list[dict]:
        return [
            {
                "name": s.name,
                "description": s.description,
                "source": s.source,
                "path": s.file_path,
            }
            for s in self.services_for(cwd).resources.get_skills()
        ]

    def list_recent(self, limit: int = 20) -> list[dict]:
        out = []
        for path in SessionManager.list(self.cwd, self.agent_dir)[:limit]:
            live = next(
                (s for s in self._sessions.values() if str(s.session.session_file) == str(path)),
                None,
            )
            out.append(
                {
                    "file": str(path),
                    "name": path.stem,
                    "mtime": path.stat().st_mtime,
                    "id": live.id if live else None,
                    "title": live.title if live else "",
                }
            )
        return out

    # -- 会话生命周期 -------------------------------------------------- #

    def create(
        self,
        model: str | None = None,
        thinking_level: str = "off",
        tools: list[str] | None = None,
        skills: list[str] | None = None,
        resume: str | None = None,
        persist: bool = True,
        cwd: str | None = None,
    ) -> LiveSession:
        workdir = str(Path(cwd).expanduser().resolve()) if cwd else self.cwd
        services = self.services_for(workdir)
        if resume:
            manager = SessionManager.open(resume, self.agent_dir)
        elif persist:
            manager = SessionManager.create(workdir, self.agent_dir)
        else:
            manager = SessionManager.in_memory(workdir)

        session = create_agent_session(
            services=services,
            session_manager=manager,
            model=model,
            thinking_level=thinking_level,
            tools=tools,
        )
        if skills is not None:
            self._restrict_skills(session, skills)

        live = LiveSession(id=uuid.uuid4().hex[:12], session=session)
        session.subscribe(live.on_agent_event)
        self._sessions[live.id] = live
        return live

    def update_tools(
        self,
        live: LiveSession,
        tools: list[str] | None = None,
        skills: list[str] | None = None,
    ) -> dict[str, Any]:
        """会话跑起来之后换工具/技能。

        工具集不是创建时定死的——用户中途勾了新工具，下一轮就该能用。
        重建之后立刻生效，因为循环每轮都从 state.tools 现取。
        """
        from pi_coding_agent.tools import DEFAULT_TOOL_NAMES, create_tools

        session = live.session
        services = self.services_for(session.cwd)
        names = list(tools) if tools is not None else list(DEFAULT_TOOL_NAMES)

        rebuilt = create_tools(names, session.cwd)
        # 扩展注册的工具也按勾选过滤，否则取消勾选了还留着
        rebuilt += [
            t for t in services.resources.get_extension_api().tools if t.name in names
        ]
        session.agent.set_tools(rebuilt)

        if skills is not None:
            self._restrict_skills(session, skills)
        return {
            "tools": [t.name for t in session.agent.state.tools],
            "skills": skills if skills is not None else None,
        }

    def _restrict_skills(self, session: Any, enabled: list[str]) -> None:
        """只把勾选的技能写进系统提示。

        技能本身是"描述进提示、正文按需读"的机制，所以过滤发生在提示装配处，
        不需要动 agent 层任何东西。
        """
        from pi_coding_agent.prompt import build_system_prompt
        from pi_coding_agent.skills import skills_block

        resources = session.resources
        chosen = [s for s in resources.get_skills() if s.name in enabled]
        session.agent.state.system_prompt = build_system_prompt(
            session.cwd,
            context_files=resources.get_context_files(),
            skills_block=skills_block(chosen),
        )

    def get(self, session_id: str) -> LiveSession | None:
        return self._sessions.get(session_id)

    def close(self, session_id: str) -> bool:
        live = self._sessions.pop(session_id, None)
        if live is None:
            return False
        if live.busy and live.running:
            live.running.cancel()
        live.session.dispose()
        return True

    async def prompt(self, live: LiveSession, text: str) -> None:
        """跑一轮。立刻返回，事件从 SSE 出去。"""
        if live.busy:
            raise RuntimeError("会话正忙")
        if not live.title:
            live.title = text.strip().replace("\n", " ")[:40]
        # 告诉翻译器这句是用户自己提交的，别再当插话回传
        live.translator.own_prompt = text
        live.publish({"type": "user", "text": text})

        async def run() -> None:
            try:
                await live.session.prompt(text)
            except asyncio.CancelledError:
                live.publish({"type": "aborted"})
                raise
            except Exception as exc:
                live.publish({"type": "error", "message": f"{type(exc).__name__}: {exc}"})

        live.running = asyncio.create_task(run())

    def abort(self, live: LiveSession) -> None:
        live.session.abort()

    def snapshot(self, live: LiveSession) -> dict:
        session = live.session
        model = session.model
        usage = session.usage()
        return {
            "id": live.id,
            "title": live.title,
            "cwd": session.cwd,
            "file": str(session.session_file) if session.session_file else None,
            "model": model.key if model else None,
            "thinking": session.thinking_level,
            "busy": live.busy,
            "tools": [t.name for t in session.agent.state.tools],
            "usage": {
                "input": usage.input,
                "output": usage.output,
                "cost": round(usage.cost.total, 6),
            },
            "contextWindow": model.context_window if model else 0,
        }
