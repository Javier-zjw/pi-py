"""
AgentSession — 组合聚合层。
三层能力在此汇合：pi‑ai 提供模型流式能力，pi‑agent 提供Agent主循环，
本类补充编码Agent所需的全部能力：持久化、上下文压缩、资源加载、模型切换。
"""

from __future__ import annotations

import asyncio
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

from pi_ai import ImageContent, Model, ThinkingLevel, Tool, Usage

from .compaction import (
    CompactionBudget,
    CompactionCancelled,
    CompactionEndEvent,
    CompactionFailed,
    CompactionOutcome,
    CompactionProgress,
    CompactionProgressEvent,
    CompactionRequest,
    CompactionStartEvent,
    context_tokens,
    estimate_text_tokens,
    run_compaction,
)
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

        self._listeners: list[Callable[[Any], None]] = []
        self._persisted: set[int] = set()
        # id(message) → 会话条目 id，压缩时用来写 firstKeptEntryId
        self._entry_ids: dict[int, str] = {
            id(m): entry_id
            for m, entry_id in zip(state.messages, restored.get("entry_ids") or [])
            if entry_id
        }
        latest = self.session.latest_compaction()
        # 上一次压缩的结构化状态，下一次压缩在它的基础上增量合并
        self._compaction_state: dict[str, Any] | None = (
            latest.details.get("state") if latest and isinstance(latest.details, dict) else None
        )
        self._compacting = False
        # 压缩期间进来的 prompt 要等它做完，否则压缩结果会覆盖掉这一轮的消息
        self._compaction_idle = asyncio.Event()
        self._compaction_idle.set()
        self._in_loop = False
        self._compaction_cancel: asyncio.Event | None = None
        self._compaction_attempts = 0
        self._compaction_blocked = False
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
        self._emit(event)

    def _emit(self, event: Any) -> None:
        """agent 事件和会话自己的事件（如压缩）走同一条订阅通道，按 event.type 区分"""
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
            entry_id = self.session.append_custom_message_entry(
                message.custom_type, message.content, message.display, message.details
            )
        else:
            entry_id = self.session.append_message(message)
        self._entry_ids[id(message)] = entry_id

    def subscribe(self, listener: Callable[[Any], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    async def _transform_context(self, messages: list[AgentMessage]) -> list[AgentMessage]:
        """每次调用 LLM 之前由 Agent 主循环触发：长工具链在一轮内长满时在这里压缩"""
        self._in_loop = True
        try:
            if await self._maybe_compact():
                messages = list(self.agent.state.messages)
        finally:
            self._in_loop = False
        return await self.resources.get_extension_api().run_context_hook(messages)

    # -- 压缩 ---------------------------------------------------------- #

    def compaction_budget(self) -> CompactionBudget | None:
        model = self.agent.state.model
        if model is None:
            return None
        return CompactionBudget.from_settings(model, self.settings.get)

    def _llm_tools(self) -> list[Tool]:
        return [Tool(name=t.name, description=t.description, parameters=t.parameters)
                for t in self.agent.state.tools]

    def context_tokens(self) -> int:
        """下一次请求的上下文大小（以服务商实报 usage 为锚）"""
        return context_tokens(self.agent.state.messages, self.agent.state.system_prompt,
                              self._llm_tools())

    async def _maybe_compact(self, extra_text: str = "") -> bool:
        """超过触发线就同步压缩。一次 prompt 内最多尝试两次，失败或取消后本轮不再尝试"""
        if self._compacting or self._compaction_blocked:
            return False
        if not self.settings.get("compaction.enabled", True) or self._compaction_attempts >= 2:
            return False
        budget = self.compaction_budget()
        if budget is None or not budget.context_window:
            return False
        if not budget.should_compact(self.context_tokens() + estimate_text_tokens(extra_text)):
            return False
        self._compaction_attempts += 1
        return await self.compact(reason="threshold") is not None

    async def compact(
            self, custom_instructions: str | None = None, reason: str = "manual"
    ) -> CompactionOutcome | None:
        """同步压缩，全程发事件。任何失败都保持原历史不变并返回 None，不会打断这一轮"""
        model = self.agent.state.model
        budget = self.compaction_budget()
        if model is None or budget is None or self._compacting:
            return None

        tokens_before = self.context_tokens()
        if self.agent.is_streaming and not self._in_loop:
            # 主循环正在改消息列表，此时替换会丢消息；只能在循环的安全点（transform_context）压缩
            self._emit(CompactionEndEvent(reason=reason, tokens_before=tokens_before,
                                          error="agent 正在运行，请等这一轮结束后再压缩"))
            return None

        thinking = self.settings.get("compaction.thinking", "off")
        request = CompactionRequest(
            messages=list(self.agent.state.messages),
            model=model,
            stream_fn=self.model_runtime.stream_fn,
            budget=budget,
            system_prompt=self.agent.state.system_prompt,
            tools=self._llm_tools(),
            previous_state=self._compaction_state,
            cwd=self.cwd,
            reason=reason,
            custom_instructions=custom_instructions,
            reasoning=self.agent.state.thinking_level if thinking == "inherit" else thinking,
        )

        def on_progress(p: CompactionProgress) -> None:
            self._emit(CompactionProgressEvent(phase=p.phase, chunk=p.chunk, chunks=p.chunks,
                                               delta=p.delta))

        self._compacting = True
        self._compaction_idle.clear()
        self._compaction_cancel = asyncio.Event()
        self._emit(CompactionStartEvent(reason=reason, tokens_before=tokens_before,
                                        context_window=budget.context_window))
        end = CompactionEndEvent(reason=reason, tokens_before=tokens_before)
        outcome: CompactionOutcome | None = None
        try:
            outcome = await run_compaction(request, on_progress, self._compaction_cancel)
        except CompactionCancelled:
            end.cancelled = True
        except CompactionFailed as exc:
            end.error = str(exc)
        except Exception as exc:  # 压缩是附加能力，自身的 bug 不能让这一轮跑不起来
            end.error = f"{type(exc).__name__}: {exc}"
        finally:
            # 之后到替换消息之间没有 await，此处放行等待中的 prompt 是安全的；
            # 放在 finally 里，任务被取消（CancelledError）时也不会让后续 prompt 永远等下去
            self._compacting = False
            self._compaction_cancel = None
            self._compaction_idle.set()

        if outcome is not None and outcome.tokens_after >= outcome.tokens_before:
            # 没变小就不替换：原始历史信息更全
            end.error = f"压缩后没有变小（{outcome.tokens_before} → {outcome.tokens_after}），保持原样"
            outcome = None

        if outcome is None:
            end.skipped = not (end.cancelled or end.error)
            self._compaction_blocked = True
            self._emit(end)
            return None

        self._apply_compaction(outcome)
        end.tokens_after = outcome.tokens_after
        end.summary = outcome.summary
        end.state = outcome.state
        end.degraded = outcome.degraded
        end.restored = list(outcome.restored)
        self._emit(end)
        return outcome

    def _apply_compaction(self, outcome: CompactionOutcome) -> None:
        """落盘 + 替换内存消息，两步紧挨着做，中间没有 await"""
        retained = outcome.retained
        first_kept = self._entry_ids.get(id(retained[0])) if retained else None
        ledger = outcome.state.get("fileLedger") or {}
        details = {
            "version": 1,
            "state": outcome.state,
            # 与 TS 版 pi 同名的字段，保证互读
            "readFiles": list(ledger.get("read") or []),
            "modifiedFiles": [m["path"] for m in ledger.get("modified") or []],
            "reason": outcome.state.get("meta", {}).get("reason"),
            "tokensAfter": outcome.tokens_after,
            "degraded": outcome.degraded,
            "restored": outcome.restored,
            "trimmed": outcome.trimmed,
            "chunks": outcome.chunks,
        }
        self.session.append_compaction(
            summary=outcome.summary,
            tokens_before=outcome.tokens_before,
            retained_tail=() if first_kept else retained,
            details=details,
            usage=outcome.usage,
            first_kept_entry_id=first_kept,
        )
        state_message = outcome.state_message()
        self._persisted.add(id(state_message))
        self.agent.state.messages = [state_message, *retained]
        self._compaction_state = outcome.state
        live = {id(m) for m in self.agent.state.messages}
        self._entry_ids = {k: v for k, v in self._entry_ids.items() if k in live}

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
        await self._compaction_idle.wait()
        await self._apply_mode()
        errors = await self.resources.get_extension_api().run_before_start(self)
        for note in errors:
            self.services.diagnostics.append(f"before_agent_start 失败：{note}")
        # 先压缩、后执行：新请求本身不参与压缩，压缩完成后才追加
        self._compaction_attempts = 0
        self._compaction_blocked = False
        await self._maybe_compact(extra_text=text)
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
        if self._compaction_cancel is not None:
            self._compaction_cancel.set()
        self.agent.abort()

    @property
    def is_compacting(self) -> bool:
        return self._compacting


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