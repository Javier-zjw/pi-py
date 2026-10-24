"""
AgentSession — 组合聚合层。
三层能力在此汇合：pi‑ai 提供模型流式能力，pi‑agent 提供Agent主循环，
本类补充编码Agent所需的全部能力：持久化、上下文压缩、资源加载、模型切换。
"""

from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Sequence

from pi_agent import (
    Agent,
    AgentEvent,
    AgentMessage,
    AgentState,
    AgentTool,
    CustomMessage,
    MessageEndEvent,
    TurnEndEvent,
    find_missing_tool_results,
    interrupted_tool_result,
)
from pi_ai import (
    AssistantMessage,
    ImageContent,
    Model,
    ThinkingLevel,
    Tool,
    ToolResultMessage,
    Usage,
    UserMessage,
    is_context_overflow,
    is_retryable,
    retry_after,
)

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
    ContextPrunedEvent,
    apply_prune,
    context_tokens,
    estimate_text_tokens,
    plan_prune,
    run_compaction,
)
from .compaction.prune import PRUNE_ENTRY_TYPE
from .model_runtime import ModelRuntime
from .resources import DefaultResourceLoader, ResourceLoader
from .session.entries import CustomEntry
from .session.manager import SessionManager
from .session_events import ModeChangedEvent, RetryEvent
from .settings import SettingsManager
from .tools import DEFAULT_TOOL_NAMES, create_tools

# 界面实现的提问回调：问题列表 → 每题的回答（用户跳过返回 None）
UserInputHandler = Callable[[list[dict[str, Any]]], Awaitable["list[dict[str, Any]] | None"]]
# 界面实现的权限确认回调：请求（工具、摘要、细节）→ {"decision": allow/always/deny/abort, "feedback"}
PermissionHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


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
        # 上次在工具执行中退出：补上"被中断"的结果并写回文件（发请求时 pi-agent 也会兜底补，
        # 这里落盘是为了历史里看得到这一步没做完）
        self.interrupted_tool_calls = self._repair_interrupted(restored)
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
            on_stream_error=self._on_stream_error,
            before_tool_call=self._before_tool_call,
            # 重试次数由 _retry_after_error 按设置控制；这里给够余量（+1 留给上下文超长的那次）
            max_error_retries=int(self.settings.get("retry.maxRetries", 3) or 0) + 1,
        )

        self._listeners: list[Callable[[Any], None]] = []
        self._persisted: set[int] = set()
        # id(message) → 会话条目 id，压缩时用来写 firstKeptEntryId
        self._entry_ids: dict[int, str] = {
            id(m): entry_id
            for m, entry_id in zip(state.messages, restored.get("entry_ids") or [])
            if entry_id
        }
        # L1 清理过的工具结果；之前那些回复的 usage 是清理前的，不能当计量锚
        self._pruned_ids: set[str] = set()
        self._anchor_floor = 0
        self._restore_prune(restored.get("entry_ids") or [])
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
        self._overflow_recovered = False
        self._retry_attempts = 0        # 连续几次临时失败了；模型成功回复一次就清零
        # 当前工作模式。核心不解释它的含义，具体行为由扩展的 Mode.apply 决定
        self.mode: str = self._restore_mode()
        self._base_system_prompt = state.system_prompt or ""
        self._base_tools = list(active_tools)
        # 界面提供的"向用户提问"入口（pi-tui 弹选择器）。None = 没有可交互的界面（print 模式、网页），
        # 扩展的提问工具据此退回到"在回复里写问题"
        self.user_input: UserInputHandler | None = None
        # 界面提供的"执行前确认"入口（pi-tui 弹选择器）。None = 没有可交互的界面，
        # 权限扩展按 permissions.nonInteractive 决定放行还是拒绝
        self.permission_handler: PermissionHandler | None = None
        self.agent.subscribe(self._on_agent_event)
        self.resources.get_extension_api().session = self


    def _repair_interrupted(self, restored: dict[str, Any]) -> list[str]:
        """只处理末尾那次：结果追加到分支末尾刚好紧跟在调用之后。更早的缺口由请求出口兜底"""
        messages: list[AgentMessage] = restored["messages"]
        missing = find_missing_tool_results(messages)
        if not missing or missing[-1][0] != len(messages):
            return []
        entry_ids = restored.setdefault("entry_ids", [])
        repaired: list[str] = []
        for call in missing[-1][1]:
            result = interrupted_tool_result(call)
            entry_ids.append(self.session.append_message(result))
            messages.append(result)
            repaired.append(call.id)
        return repaired

    def _restore_prune(self, entry_ids: list[str | None]) -> None:
        """恢复会话：重放 context_prune 条目，让发给模型的内容和中断前一致"""
        branch = self.session.get_branch()
        position = {e.id: i for i, e in enumerate(branch)}
        last_prune = -1
        for i, e in enumerate(branch):
            if isinstance(e, CustomEntry) and e.custom_type == PRUNE_ENTRY_TYPE:
                self._pruned_ids.update((e.data or {}).get("toolCallIds") or [])
                last_prune = i
        if not self._pruned_ids:
            return
        self._swap_messages(apply_prune(self.agent.state.messages, self._pruned_ids)[0])
        before = [i for i, entry_id in enumerate(entry_ids)
                  if entry_id and position.get(entry_id, -1) < last_prune]
        self._anchor_floor = before[-1] + 1 if before else 0

    def _swap_messages(self, messages: list[AgentMessage]) -> None:
        """换成新列表；被替换成副本的消息沿用原条目 id，且永远不会再落盘"""
        for old, new in zip(self.agent.state.messages, messages):
            if new is not old:
                if id(old) in self._entry_ids:
                    self._entry_ids[id(new)] = self._entry_ids[id(old)]
                self._persisted.add(id(new))
        self.agent.state.messages = messages

    def _on_agent_event(self, event: AgentEvent) -> None:
        if isinstance(event, MessageEndEvent) and event.message is not None:
            self._persist(event.message)
            if getattr(event.message, "role", "") == "assistant" and event.message.stop_reason != "error":
                self._retry_attempts = 0
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

    async def _on_stream_error(self, message: AssistantMessage) -> bool:
        """模型调用出错时主循环来问要不要重试本轮：临时失败（限流、过载、断网）等一会儿再试；
        上下文超长就压缩后再试。返回 False 照常报错"""
        if is_retryable(message):
            return await self._retry_after_error(message)
        return await self._recover_overflow(message)

    async def _retry_after_error(self, message: AssistantMessage) -> bool:
        """退避重试：等 baseDelay·2^(n-1) 秒（封顶 maxDelay），服务商给了 Retry-After 就按它等。
        连续失败超过 maxRetries 次就放弃。等待中用户中断（ctrl+c）立刻停下"""
        get = self.settings.get
        max_retries = int(get("retry.maxRetries", 3) or 0)
        if not get("retry.enabled", True) or self._retry_attempts >= max_retries:
            return False
        self._retry_attempts += 1
        base = float(get("retry.baseDelay", 2) or 0)
        cap = float(get("retry.maxDelay", 60) or 0)
        wanted = retry_after(message)
        delay = wanted if wanted is not None else base * 2 ** (self._retry_attempts - 1)
        delay = max(0.0, min(delay, cap))
        self._emit(RetryEvent(attempt=self._retry_attempts, max_attempts=max_retries, delay=delay,
                              error=message.error_message or ""))
        return not await self.agent.wait_cancelled(delay)

    async def _recover_overflow(self, message: AssistantMessage) -> bool:
        """服务商报上下文超长（说明估算偏小，或单个工具输出特别大）：
        激进清理 + 摘要压缩，然后让主循环重试本轮。每次 prompt 只救一次，再超就照常报错"""
        if self._overflow_recovered or not is_context_overflow(message):
            return False
        if self._compacting or not self.settings.get("compaction.enabled", True):
            return False
        budget = self.compaction_budget()
        if budget is None:
            return False
        self._overflow_recovered = True
        self._in_loop = True
        try:
            # L1 全清：受保护区间缩到保留尾部那么大，目标 0
            pruned = self.prune(budget, target_tokens=0, protect_tokens=budget.keep_recent)
            outcome = await self.compact(reason="overflow",
                                         keep_recent_tokens=max(1, budget.keep_recent // 2))
        finally:
            self._in_loop = False
        return outcome is not None or pruned

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
                              self._llm_tools(), min_anchor=self._anchor_floor)

    async def _maybe_compact(self, extra_text: str = "") -> bool:
        """先 L1 清理旧工具输出（不调模型），仍超触发线再做 L2 摘要。返回消息是否被替换。
        L2 一次 prompt 内最多尝试两次，失败或取消后本轮不再尝试"""
        if self._compacting or not self.settings.get("compaction.enabled", True):
            return False
        budget = self.compaction_budget()
        if budget is None or not budget.context_window:
            return False
        extra = estimate_text_tokens(extra_text)
        changed = False
        if budget.should_prune(self.context_tokens() + extra):
            changed = self.prune(budget, extra)
        if self._compaction_blocked or self._compaction_attempts >= 2:
            return changed
        if not budget.should_compact(self.context_tokens() + extra):
            return changed
        self._compaction_attempts += 1
        return await self.compact(reason="threshold") is not None or changed

    def prune(
            self,
            budget: CompactionBudget | None = None,
            extra_tokens: int = 0,
            target_tokens: int | None = None,
            protect_tokens: int | None = None,
    ) -> bool:
        """L1：把受保护区间之外的旧工具输出换成占位，清到目标线以下。同步、不调模型。
        只能在安全点调用（prompt 开始前 / transform_context 里），否则会和主循环抢消息列表"""
        budget = budget or self.compaction_budget()
        if budget is None:
            return False
        before = self.context_tokens()
        plan = plan_prune(
            self.agent.state.messages, before + extra_tokens,
            budget.prune_target if target_tokens is None else target_tokens,
            budget.protect if protect_tokens is None else protect_tokens,
            budget.prune_min_tokens,
        )
        # 平时一次至少要省下 prune_min_batch（见 CompactionBudget）；超长兜底（显式给了目标）不设下限
        min_saving = budget.prune_min_batch if target_tokens is None else 0
        if not plan.tool_call_ids or plan.tokens_saved < min_saving:
            return False
        messages, count = apply_prune(self.agent.state.messages, plan.tool_call_ids)
        self._swap_messages(messages)
        self._pruned_ids.update(plan.tool_call_ids)
        self.session.append_custom_entry(
            PRUNE_ENTRY_TYPE, {"toolCallIds": plan.tool_call_ids, "tokensSaved": plan.tokens_saved}
        )
        # 之前那些回复报的 usage 都是清理前的
        self._anchor_floor = len(messages)
        self._emit(ContextPrunedEvent(count=count, tokens_before=before,
                                      tokens_after=self.context_tokens()))
        return True

    async def compact(
            self,
            custom_instructions: str | None = None,
            reason: str = "manual",
            keep_recent_tokens: int | None = None,
    ) -> CompactionOutcome | None:
        """同步压缩，全程发事件。任何失败都保持原历史不变并返回 None，不会打断这一轮"""
        model = self.agent.state.model
        budget = self.compaction_budget()
        if model is None or budget is None or self._compacting:
            return None
        if keep_recent_tokens is not None:
            budget = dataclasses.replace(budget, keep_recent_tokens=keep_recent_tokens)

        tokens_before = self.context_tokens()
        if self.agent.is_streaming and not self._in_loop:
            # 主循环正在改消息列表，此时替换会丢消息；只能在循环的安全点（transform_context）压缩
            self._emit(CompactionEndEvent(reason=reason, tokens_before=tokens_before,
                                          error="agent 正在运行，请等这一轮结束后再压缩"))
            return None

        thinking = self.settings.get("compaction.thinking", "off")
        extra_context, hook_errors = await self.resources.get_extension_api().run_compaction_context(
            self
        )
        for note in hook_errors:
            self.services.diagnostics.append(f"compaction_context 失败：{note}")
        rehydrate = bool(self.settings.get("compaction.rehydrate.enabled", True))
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
            rehydrate_max_files=int(self.settings.get("compaction.rehydrate.maxFiles", 5))
            if rehydrate else 0,
            rehydrate_max_tokens=min(int(self.settings.get("compaction.rehydrate.maxTokens", 10_000)),
                                     int(budget.usable * 0.08)),
            extra_context=extra_context,
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
            # 补回的上下文和状态一起放进状态消息；恢复会话时原样拼回
            "rehydration": outcome.rehydration.text or None,
            "rehydratedFiles": outcome.rehydration.files,
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
        self._anchor_floor = 0          # 保留尾部的旧 usage 由状态消息的 tailLength 排除
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

    def switch_mode(self, mode: str) -> str:
        """切换工作模式（同步，界面的快捷键里也能直接调）。写进会话记录，恢复时能还原；
        发 mode_changed 事件，界面据此刷新。只在两轮之间生效：下一轮开始时 _apply_mode 重建工具和提示"""
        if mode == self.mode:
            return self.mode
        previous, self.mode = self.mode, mode
        self.session.append_custom_entry("mode_change", {"mode": mode})
        self._emit(ModeChangedEvent(mode=mode, previous=previous))
        return self.mode

    async def set_mode(self, mode: str) -> str:
        return self.switch_mode(mode)

    def _restore_mode(self) -> str:
        """恢复会话时还原上次的模式（以前总是回到 chat，计划模式里 resume 后就丢了）"""
        modes = self.resources.get_extension_api().modes
        for entry in reversed(self.session.get_branch()):
            if getattr(entry, "type", "") == "custom" and getattr(entry, "custom_type", "") == "mode_change":
                mode = (entry.data or {}).get("mode") if isinstance(entry.data, dict) else None
                return mode if mode in modes else "chat"
        return "chat"

    async def _before_tool_call(self, call: Any, state: Any) -> Any:
        return await self.resources.get_extension_api().run_tool_call_hooks(call, self)

    async def ask_user(self, questions: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        """让界面向用户提问。questions 每项 {question, header?, options: [{label, description?,
        recommended?}]}；返回每题的 {question, answer, custom}，用户跳过返回 None。
        没有界面时抛 RuntimeError（先看 user_input 是否为 None）"""
        if self.user_input is None:
            raise RuntimeError("no interactive UI to ask the user")
        return await self.user_input(questions)

    def notify(self, event: Any) -> None:
        """扩展发会话事件（如计划提交、任务清单更新）给界面"""
        self._emit(event)

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
        # 先压缩、后执行：新请求本身不参与压缩，压缩完成后才追加
        await self._prepare_run(extra_text=text)
        return await self.agent.prompt(text, images)

    async def _prepare_run(self, extra_text: str = "") -> None:
        """每次运行前（新请求或续跑）：模式、扩展钩子、按需压缩"""
        await self._apply_mode()
        errors = await self.resources.get_extension_api().run_before_start(self)
        for note in errors:
            self.services.diagnostics.append(f"before_agent_start 失败：{note}")
        self._compaction_attempts = 0
        self._compaction_blocked = False
        self._overflow_recovered = False
        self._retry_attempts = 0
        await self._maybe_compact(extra_text=extra_text)

    @property
    def can_continue(self) -> bool:
        """上次在一轮中途中断：最后一条是工具结果，或是还没得到回复的用户消息"""
        last = self.agent.state.messages[-1] if self.agent.state.messages else None
        return isinstance(last, (ToolResultMessage, UserMessage))

    async def continue_(self) -> list[AgentMessage]:
        """不追加新消息，从断点接着跑完上次没做完的那一轮。准备工作和 prompt() 相同（先压后跑）"""
        await self._compaction_idle.wait()
        if not self.can_continue:
            raise RuntimeError("没有可以继续的任务：上一轮已经完成")
        await self._prepare_run()
        return await self.agent.continue_()

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