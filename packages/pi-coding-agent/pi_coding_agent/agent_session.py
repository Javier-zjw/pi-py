"""
AgentSession — 组合聚合层。
三层能力在此汇合：pi‑ai 提供模型流式能力，pi‑agent 提供Agent主循环，
本类补充编码Agent所需的全部能力：持久化、上下文压缩、资源加载、模型切换。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from pi_agent import (
    Agent,
    AgentEvent,
    AgentMessage,
    AgentState,
    AgentTool,
    CustomMessage,
    MessageEndEvent,
    TurnEndEvent
)

from pi_ai import ImageContent, Model, ThinkingLevel, Usage

from .compaction import CompactionResult, compact, should_compact
from .model_runtime import ModelRuntime
from .resources import DefaultResourceLoader, ResourceLoader
from .session.manager import SessionManager
from .settings import SettingsManager
from .tools import DEFAULT_TOOL_NAMES, create_tools

@dataclass
class AgentSessionServices:
    """全部与工作目录(cwd)绑定，仅做一次解析"""
    cwd: str
    model_runtime: ModelRuntime
    settings: SettingsManager
    resources: ResourceLoader
    diagnostics: list[str] = field(default_factory=list)

def create_agent_session_services(
        cwd: str | Path = ".",
        agent_dir: str | Path | None = None,
        model_runtime: ModelRuntime | None = None,
        settings: SettingsManager | None = None,
        resources: ResourceLoader | None = None
) -> AgentSessionServices:
    cwd = str(Path(cwd).expanduser().resolve())
    runtime = model_runtime or ModelRuntime.create(agent_dir=agent_dir)
    settings_manager = settings or SettingsManager.create(cwd, agent_dir)
    loader = resources or DefaultResourceLoader(cwd=cwd, agent_dir=agent_dir)
    loader.reload()
    diagnostics = [
        f"extension '{e.name}' failed: {e.error}" for e in loader.get_extensions() if e.error
    ]

    return AgentSessionServices(
        cwd=cwd,
        model_runtime=runtime,
        settings=settings_manager,
        resources=loader,
        diagnostics=diagnostics
    )

class AgentSession:
    def __init__(
            self,
            services: AgentSessionServices,
            session_manager: SessionManager,
            model: Model | None = None,
            thinking_level: ThinkingLevel = "off",
            tools: Sequence[str] | None = None,
            custom_tools: Iterable[AgentTool] = (),
            exclude_tools: Sequence[str] = ()
    ) -> None:
        self.services = services
        self.session = session_manager
        self.settings = services.settings
        self.resources = services.resources
        self.model_runtime = services.model_runtime
        self.cwd = services.cwd

        tool_names = list(tools if tools is not None else (self.settings.get("tools") or DEFAULT_TOOL_NAMES))
        all_tools = create_tools(tool_names, self.cwd)
        all_tools += list(self.resources.get_extension_api().tools)
        all_tools += list(custom_tools)
        excluded = set(exclude_tools) | set(self.settings.get("excludeTools") or [])
        active_tools = [t for t in all_tools if t.name not in excluded]

        restored = self.session.build_session_context()
        if model is None and restored["model"]:
            model = self.model_runtime.get_model(*restored["model"])
        if model is None:
            model = self.model_runtime.default_model(self.settings.get("model"))
        if thinking_level == "off" and restored["thinking_level"] != "off":
            thinking_level = restored["thinking_level"]

        state = AgentState(
            system_prompt=self.resources.get_system_prompt(),
            model=model,
            thinking_level=thinking_level,
            tools=active_tools,
            messages=list(restored["messages"])
        )

        self.agent = Agent(
            stream_fn=self.model_runtime.stream_fn,
            initial_state=state,
            tool_execution=self.settings.get("toolExecution", "parallel"),
            transform_context=self._transform_context,
        )

        self._listeners: list[Callable[[AgentEvent], None]] = []
        self._persisted: set[int] = set()
        self._compacting = False
        # 当前工作模式。核心不解释它的含义，具体行为由扩展的 Mode.apply 决定
        self.mode: str = "chat"
        self._base_system_prompt = state.system_prompt or ""
        self._base_tools = list(active_tools)
        self.agent.subscribe(self._on_agent_event)
        self.resources.get_extension_api().session = self


    def _on_agent_event(self, event: AgentEvent) -> None:
        if isinstance(event, MessageEndEvent) and event.message is not None:
            self._persist(event.message)
        elif isinstance(event, TurnEndEvent):
            for result in event.tool_results:
                self._persist(result)

        self.resources.get_extension_api().dispatch(event)
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                pass

    def _persist(self, message: AgentMessage) -> None:
        if id(message) in self._persisted:
            return
        self._persisted.add(id(message))
        if isinstance(message, CustomMessage):
            self.session.append_custom_message_entry(
                message.custom_type, message.content, message.display, message.details
            )
        else:
            self.session.append_message(message)

    def subscribe(self, listener: Callable[[AgentEvent], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    async def _transform_context(self, messages: list[AgentMessage]) -> list[AgentMessage]:
        """自动上下文压缩钩子，由Agent主循环在每次调用LLM之前触发"""
        messages = await self.resources.get_extension_api().run_context_hook(messages)
        if self._compacting or not self.settings.get("compaction.enabled", True):
            return messages
        model = self.agent.state.model
        if model is None:
            return messages

        threshold = float(self.settings.get("compaction.threshold", 0.85))
        if not should_compact(messages, model, threshold):
            return messages
        await self.compact()
        return list(self.agent.state.messages)

    async def compact(self, custom_instructions: str | None = None) -> CompactionResult | None:
        model = self.agent.state.model
        if model is None:
            return None
        self._compacting = True
        try:
            result = await compact(
                list(self.agent.state.messages),
                model,
                self.model_runtime.models.complete_simple,
                keep_last_turns=int(self.settings.get("compaction.keepLastTurns", 4)),
                custom_instructions=custom_instructions
            )
        finally:
            self._compacting = False

        self.session.append_compaction(
            summary=result.summary,
            tokens_before=result.tokens_before,
            retained_tail=result.retained_tail,
            details=result.details,
            usage=result.usage,
        )
        summary_message = CustomMessage(
            custom_type="compaction_summary",
            content="Summary of the earlier conversation, which has been compacted:\n\n" + result.summary
        )
        self._persisted.add(id(summary_message))
        self.agent.state.messages = [summary_message, *result.retained_tail]
        return result

    def expand_prompt_template(self, text: str) -> str:
        """``/deploy 附加参数`` —— 模板正文与传入参数结合使用。"""
        if not text.startswith("/"):
            return text

        name, _, rest = text[1:].partition(" ")
        for template in self.resources.get_prompts():
            if template.name == name:
                return f"{template.content}\n\n{rest}".strip()

        return text

    async def set_mode(self, mode: str) -> str:
        """切换工作模式。写进会话记录，恢复时能还原"""
        self.mode = mode
        self.session.append_custom_entry("mode_change", {"mode": mode})
        return self.mode

    def available_modes(self) -> list[Any]:
        api = self.resources.get_extension_api()
        return list(api.modes.values())

    async def _apply_mode(self) -> None:
        """
        每轮开始前把模式效果落到 state 上
        每次都从基线重建，而不是在上一轮的结果上叠加——否则来回切几次模式
        系统提示会越滚越长，工具集也会残留。
        """
        self.agent.state.system_prompt = self._base_system_prompt
        self.agent.set_tools(list(self._base_tools))
        mode = self.resources.get_extension_api().modes.get(self.mode)
        if mode is not None and mode.apply is not None:
            await mode.apply(self)

    async def prompt(
            self,
            text: str,
            images: Iterable[ImageContent] | None = None,
            expand_prompt_templates: bool = True
    ) -> list[AgentMessage]:
        if expand_prompt_templates:
            text = self.expand_prompt_template(text)
        await self._apply_mode()
        errors = await self.resources.get_extension_api().run_before_start(self)
        for note in errors:
            self.services.diagnostics.append(f"before_agent_start 失败：{note}")
        return await self.agent.prompt(text, images)

    async def run_command(self, name: str, rest: str = "") -> str | None:
        """执行扩展注册的斜杠命令,终端和网页共用这一条路径"""
        command = self.resources.get_extension_api().commands.get(name)
        if command is None:
            raise KeyError(name)
        return await command.handler(rest)

    def set_base_tools(self, tools: Iterable[AgentTool]) -> None:
        """更新工具基线,模式会在这个基线上做增减"""
        self._base_tools = list(tools)
        self.agent.set_tools(list(self._base_tools))

    def steer(self, text: str) -> None:
        self.agent.steer(text)

    def follow_up(self, text: str) -> None:
        self.agent.follow_up(text)

    def abort(self) -> None:
        self.agent.abort()


    def set_model(self, model: Model) -> None:
        self.agent.set_model(model)
        self.session.append_model_change(model.provider, model.id)

    def set_thinking_level(self, level: ThinkingLevel) -> None:
        self.agent.set_thinking_level(level)
        self.session.append_thinking_level_change(level)

    @property
    def model(self) -> Model | None:
        return self.agent.state.model

    @property
    def thinking_level(self) -> ThinkingLevel:
        return self.agent.state.thinking_level

    @property
    def messages(self) -> list[AgentMessage]:
        return self.agent.state.messages

    @property
    def is_streaming(self) -> bool:
        return self.agent.is_streaming

    @property
    def session_file(self) -> Path | None:
        return self.session.get_session_file()

    @property
    def session_id(self) -> str:
        return self.session.get_session_id()

    def usage(self) -> Usage:
        return self.agent.state.usage()

    def dispose(self) -> None:
        self._listeners.clear()


def create_agent_session(
        cwd: str | Path = ".",
        agent_dir: str | Path | None = None,
        model: Model | str | None = None,
        thinking_level: ThinkingLevel = "off",
        tools: Sequence[str] | None = None,
        custom_tools: Iterable[AgentTool] = (),
        exclude_tools: Sequence[str] = (),
        session_manager: SessionManager | None = None,
        services: AgentSessionServices | None = None,
        **service_kwargs: Any
) -> AgentSession:
    """供SDK使用者调用的一站式入口"""
    services = services or create_agent_session_services(cwd=cwd, agent_dir=agent_dir, **service_kwargs)
    session_manager = session_manager or SessionManager.create(services.cwd, agent_dir)

    if isinstance(model, str):
        model = services.model_runtime.resolve(model)

    return AgentSession(
        services=services,
        session_manager=session_manager,
        model=model,
        thinking_level=thinking_level,
        tools=tools,
        custom_tools=custom_tools,
        exclude_tools=exclude_tools
    )