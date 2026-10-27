"""
hooks（同 Claude Code 的 hooks，逻辑见 pi_coding_agent/hooks.py）：在固定时机跑用户配置的 shell 命令。

- PreToolUse → before_tool_call：退出码 2 / permissionDecision=deny 拦下（原因给模型）；allow 免确认、ask
  强制确认（权限扩展认，deny 规则照样优先）；updatedInput 改参数；additionalContext 跟在工具结果后面；
- PostToolUse → after_tool_call（只在工具成功时）：退出码 2 / decision=block 把原因附在结果后面给模型；
  updatedToolOutput 换掉结果；additionalContext 附上；
- UserPromptSubmit → before_prompt：拦下就不发给模型（原因给用户看）；纯文本输出 / additionalContext
  作为补充上下文跟在原话后面；
- Stop → before_stop：退出码 2 / decision=block 让模型接着做（原因作为消息插进去）；stop_hook_active
  告诉脚本这次是不是已经因为它继续过（防死循环），一次运行最多续 MAX_STOP_CONTINUES 次；
- SessionStart → 会话第一次运行前跑一次（source = startup / resume），输出放进这个会话的系统提示；
- continue: false 停止这一轮，systemMessage / 出错 / 超时给用户看一行。

``/hooks`` 列出配置的钩子；``/hooks import`` 把 Claude Code 的钩子（~/.claude/settings.json →
全局；.claude/settings.json、settings.local.json → 项目）复制进 pi 的 settings.json。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from pi_agent import AgentToolResult, BeforeToolCallResult
from pi_ai import TextContent

from pi_coding_agent import hooks as H
from pi_coding_agent.extensions import PromptHookResult
from pi_coding_agent.session_events import NoticeEvent

NAME = "hooks"
DESCRIPTION = "钩子：在工具执行前后、提交消息、模型停下、会话开始时跑配置的命令（格式同 Claude Code）"

MAX_STOP_CONTINUES = 5


def _sources(session) -> list[tuple[Path | None, str]]:
    settings = session.settings
    return [(getattr(settings, "global_path", None), "全局"),
            (getattr(settings, "project_path", None), "项目")]


def _handlers(session, event: str) -> list[H.Handler]:
    handlers, _ = H.load_handlers(_sources(session), session.settings.get("hooks"))
    return [h for h in handlers if h.event == event and h.supported]


def _payload(session, event: str, **extra) -> dict:
    try:
        transcript = str(session.session.get_session_file() or "")
    except Exception:  # noqa: BLE001
        transcript = ""
    mode = "plan" if getattr(session, "mode", "") == "plan" else H.PERMISSION_MODES.get(
        session.settings.get("permissions.mode", "ask"), "default")
    return {"session_id": Path(transcript).stem if transcript else "", "transcript_path": transcript,
            "cwd": str(session.cwd), "hook_event_name": event, "permission_mode": mode, **extra}


async def _run_all(session, event: str, handlers: list[H.Handler], payload: dict) -> H.Summary:
    outcomes = await asyncio.gather(*(H.run(h, payload, str(session.cwd)) for h in handlers))
    summary = H.summarize(event, list(zip(handlers, outcomes)))
    for level, text in summary.notices:
        session.notify(NoticeEvent(text=text, level=level, source="hook"))
    return summary


def _context_block(event: str, texts: list[str]) -> str:
    tag = {"UserPromptSubmit": "user-prompt-submit-hook", "SessionStart": "session-start-hook"}.get(
        event, "hook-context")
    return f"<{tag}>\n" + "\n\n".join(texts) + f"\n</{tag}>"


def activate(pi) -> None:
    from pi_coding_agent.builtin_extensions.permissions import hook_hints

    def tool_handlers(session, event: str, call) -> list[H.Handler]:
        names = [H.claude_name(call.name), call.name]
        args = call.arguments or {}
        return [h for h in _handlers(session, event) if H.matches(h.matcher, names)
                and H.condition_matches(h.condition, call.name, args, str(session.cwd))]

    async def pre_tool(call, session):
        handlers = tool_handlers(session, "PreToolUse", call)
        if not handlers:
            return None
        args = call.arguments or {}
        payload = _payload(session, "PreToolUse", tool_name=H.claude_name(call.name),
                           tool_input=H.tool_input(call.name, args, str(session.cwd)),
                           tool_use_id=call.id)
        summary = await _run_all(session, "PreToolUse", handlers, payload)
        if summary.context:
            session.__dict__.setdefault("_hook_context", {}).setdefault(call.id, []).extend(summary.context)
        if summary.stop:
            session.abort()
            reason = summary.stop_reason or "a hook stopped this turn"
            session.notify(NoticeEvent(text=f"钩子停止了这一轮：{reason}", level="warn", source="hook"))
            return BeforeToolCallResult(block=True, reason=f"Stopped by a PreToolUse hook: {reason}")
        if summary.block:
            reason = "\n".join(summary.reasons)
            session.notify(NoticeEvent(text=f"钩子拦下了 {call.name}：{H.first_line(reason)}",
                                       level="warn", source="hook"))
            return BeforeToolCallResult(block=True, reason=f"Blocked by a PreToolUse hook: {reason}")
        if summary.decision in ("allow", "ask"):
            hook_hints(session)[call.id] = summary.decision
        if summary.updated_input is not None:
            return BeforeToolCallResult(arguments=H.pi_arguments(call.name, summary.updated_input, args))
        return None

    async def post_tool(call, result, session):
        pending = session.__dict__.get("_hook_context", {}).pop(call.id, [])
        handlers = [] if result.is_error else tool_handlers(session, "PostToolUse", call)
        summary = H.Summary()
        if handlers:
            args = call.arguments or {}
            payload = _payload(session, "PostToolUse", tool_name=H.claude_name(call.name),
                               tool_input=H.tool_input(call.name, args, str(session.cwd)),
                               tool_response=H.tool_response(call.name, result, args, str(session.cwd)),
                               tool_use_id=call.id)
            summary = await _run_all(session, "PostToolUse", handlers, payload)
        notes = [*pending, *summary.context]
        if not (notes or summary.reasons or summary.stop or summary.updated_output is not None):
            return None
        content = list(result.content)
        if summary.updated_output is not None:
            output = summary.updated_output
            content = [TextContent(text=output if isinstance(output, str)
                                   else json.dumps(output, ensure_ascii=False))]
        extra = []
        if summary.reasons:
            extra.append("[PostToolUse hook feedback]\n" + "\n".join(summary.reasons))
        if notes:
            extra.append(_context_block("hook", notes))
        if extra:
            content.append(TextContent(text="\n\n" + "\n\n".join(extra)))
        if summary.stop:
            session.notify(NoticeEvent(text=f"钩子停止了这一轮：{summary.stop_reason or '（没给原因）'}",
                                       level="warn", source="hook"))
        details = result.details if isinstance(result.details, dict) else {}
        if summary.reasons:
            details = {**details, "hook": {"feedback": summary.reasons}}
        return AgentToolResult(content=content, details=details, is_error=result.is_error,
                               usage=result.usage, terminate=result.terminate or summary.stop)

    async def on_prompt(text: str, session):
        handlers = _handlers(session, "UserPromptSubmit")
        if not handlers:
            return None
        summary = await _run_all(session, "UserPromptSubmit", handlers,
                                 _payload(session, "UserPromptSubmit", prompt=text))
        if summary.block or summary.stop:
            reason = "\n".join(summary.reasons) or summary.stop_reason or "被钩子拦下"
            return PromptHookResult(block=True, reason=reason)
        context = [_context_block("UserPromptSubmit", summary.context)] if summary.context else []
        return PromptHookResult(context=context)

    async def on_stop(session):
        handlers = _handlers(session, "Stop")
        state = session.__dict__.setdefault("_hook_stop", {"count": 0})
        if not handlers:
            return None
        if state["count"] >= MAX_STOP_CONTINUES:
            session.notify(NoticeEvent(text=f"Stop 钩子已经让模型继续了 {MAX_STOP_CONTINUES} 次，这次不再继续",
                                       level="warn", source="hook"))
            return None
        last = ""
        for message in reversed(session.messages):
            if getattr(message, "role", "") == "assistant":
                last = "".join(getattr(c, "text", "") or "" for c in message.content
                               if getattr(c, "type", "") == "text")
                break
        summary = await _run_all(session, "Stop", handlers, _payload(
            session, "Stop", stop_hook_active=state["count"] > 0, last_assistant_message=last))
        if summary.stop or not summary.block:
            return None
        state["count"] += 1
        reason = "\n".join(summary.reasons)
        return f"[Stop hook] Do not stop yet: {reason}"

    async def before_start(session) -> None:
        session.__dict__["_hook_stop"] = {"count": 0}       # 每次运行重新计数
        if "_hook_session_start" not in session.__dict__:
            session.__dict__["_hook_session_start"] = ""
            handlers = _handlers(session, "SessionStart")
            if handlers:
                source = "resume" if session.messages else "startup"
                summary = await _run_all(session, "SessionStart", handlers,
                                         _payload(session, "SessionStart", source=source))
                if summary.context:
                    session.__dict__["_hook_session_start"] = _context_block("SessionStart", summary.context)
        block = session.__dict__.get("_hook_session_start")
        if block:   # 模式每轮把系统提示重置回基线，所以每轮都接上（内容不变，前缀缓存不受影响）
            session.agent.state.system_prompt = (session.agent.state.system_prompt or "") + "\n\n" + block

    pi.before_tool_call(pre_tool)
    pi.after_tool_call(post_tool)
    pi.before_prompt(on_prompt)
    pi.before_stop(on_stop)
    pi.before_agent_start(before_start)

    async def hooks_command(rest: str) -> str:
        session = pi.session
        if session is None:
            return "没有会话"
        if rest.strip() == "import":
            return import_from_claude(session)
        handlers, problems = H.load_handlers(_sources(session), session.settings.get("hooks"))
        lines = []
        if not handlers:
            lines.append("还没有配置钩子。")
        for event in [*H.SUPPORTED_EVENTS, *sorted({h.event for h in handlers} - set(H.SUPPORTED_EVENTS))]:
            group = [h for h in handlers if h.event == event]
            if not group:
                continue
            lines.append(f"{event}" + ("" if event in H.SUPPORTED_EVENTS else "（pi 不支持这个事件，不会执行）"))
            for h in group:
                what = h.command + (" " + " ".join(h.args) if h.args else "")
                flags = [f"匹配 {h.matcher}"] if h.matcher not in ("", "*") else []
                if h.condition:
                    flags.append(f"if {h.condition}")
                if h.background:
                    flags.append("后台")
                if h.type != "command":
                    flags.append(f"类型 {h.type}（pi 只支持 command，不会执行）")
                lines.append(f"  {what}" + (f"  · {' · '.join(flags)}" if flags else "") + f"  · {h.source}")
        lines += [f"⚠ {p}" for p in problems]
        lines += ["", ("配置写在 settings.json 的 \"hooks\"（格式同 Claude Code）；/hooks import 把 Claude Code "
                       "的钩子复制过来（~/.claude 的进全局，项目 .claude 的进项目）")]
        return "\n".join(lines)

    pi.register_command("hooks", "查看钩子；/hooks import 导入 Claude Code 的钩子", hooks_command,
                        usage="[import]")


def import_from_claude(session) -> str:
    home = Path.home()
    cwd = Path(session.cwd)
    plan = [(home / ".claude" / "settings.json", getattr(session.settings, "global_path", None), "全局"),
            (cwd / ".claude" / "settings.json", getattr(session.settings, "project_path", None), "项目"),
            (cwd / ".claude" / "settings.local.json", getattr(session.settings, "project_path", None), "项目")]
    lines, total = [], 0
    for source, target, label in plan:
        data, error = H.read_settings(source)
        if error:
            lines.append(f"⚠ {error}")
            continue
        hooks = (data or {}).get("hooks")
        if not isinstance(hooks, dict) or not hooks:
            continue
        if target is None:
            lines.append(f"⚠ 不知道{label}设置文件在哪，跳过 {source}")
            continue
        current, error = H.read_settings(Path(target))
        if current is None:
            lines.append(f"⚠ {error}，不覆盖它")
            continue
        added = H.merge_into(current, hooks)
        if added:
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            Path(target).write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", "utf-8")
        total += added
        lines.append(f"{source} → {target}（{label}）：新增 {added} 个" + ("" if added else "（都已经有了）"))
    if not lines:
        return "没找到 Claude Code 的钩子（~/.claude/settings.json、.claude/settings.json、settings.local.json）"
    lines.append(f"共导入 {total} 个，马上生效；/hooks 查看" if total else "没有新的钩子要导入")
    return "\n".join(lines)
