"""
/reflect：回顾这次会话，提议把值得留下的经验写进长期记忆、AGENTS.md 或技能（逻辑见 pi_coding_agent/reflect.py）。

- 有界面（pi-tui）：逐条弹选择——保存 / 跳过，也可以直接输入改过的内容再保存（记忆、AGENTS.md）；
- 没有界面（网页、print 模式）：列出提议，``/reflect apply 1 3`` 或 ``/reflect apply all`` 保存。

回顾用当前模型（设置 ``reflect.model`` 可以换），请求前缀和正常对话一样、能命中缓存；回顾本身不进会话。
"""

from __future__ import annotations

from pathlib import Path

from pi_coding_agent import memory as store
from pi_coding_agent import reflect as R
from pi_coding_agent.session_events import ProgressEvent

NAME = "reflect"
DESCRIPTION = "/reflect：回顾这次会话，提议把经验写进记忆、AGENTS.md 或技能，确认后保存"

SAVE, SKIP = "保存", "跳过"


def _agent_dir(session) -> Path:
    from pi_coding_agent.session.manager import DEFAULT_AGENT_DIR

    return Path(getattr(session.resources, "agent_dir", None) or DEFAULT_AGENT_DIR)


def apply(session, proposal: R.Proposal, text: str | None = None) -> str:
    """保存一条提议，返回给用户看的一行结论"""
    content = (text or proposal.text).strip()
    try:
        if proposal.kind == "memory":
            if not session.settings.get("memory.enabled", True):
                return f"✗ 长期记忆已关闭，没记：{content}"
            limit = int(session.settings.get(
                "memory.userChars" if proposal.scope == "user" else "memory.projectChars",
                store.DEFAULT_LIMITS[proposal.scope]) or store.DEFAULT_LIMITS[proposal.scope])
            memory = store.load(_agent_dir(session), proposal.scope, session.cwd)
            saved, _ = store.add(memory, content, session.cwd, limit)
            return f"✓ 记进{store.SCOPE_NAMES[proposal.scope]}记忆：{saved}"
        if proposal.kind == "agents_md":
            path = R.append_agents_md(session.cwd, content)
            return f"✓ 加进了 {path.name}：{content.splitlines()[0]}"
        path = R.write_skill(session.cwd, proposal.name, proposal.description, proposal.text)
        return f"✓ 新建技能 {proposal.name}（{_short(path, session.cwd)}）"
    except store.MemoryEditError as exc:
        return f"✗ 没记上：{str(exc).splitlines()[0]}"
    except (OSError, FileExistsError) as exc:
        return f"✗ 没写成：{exc}"


def _short(path: Path, cwd) -> str:
    try:
        return str(path.relative_to(cwd))
    except ValueError:
        return str(path)


def _question(proposal: R.Proposal) -> dict:
    lines = [proposal.title()]
    if proposal.kind == "skill":
        body = [line for line in proposal.text.splitlines() if line.strip()]
        lines += [f"  {line}" for line in body[:6]] + (["  …"] if len(body) > 6 else [])
    if proposal.why:
        lines.append(f"理由：{proposal.why}")
    if proposal.evidence:
        lines.append(f"依据：{proposal.evidence}")
    if proposal.kind != "skill":
        lines.append("（选「其他」可以输入改过的内容再保存）")
    kind = {"memory": "记忆", "agents_md": "AGENTS.md", "skill": "技能"}[proposal.kind]
    return {"question": "\n".join(lines), "header": f"回顾 · {kind}",
            "options": [{"label": SAVE, "description": "下次会话起生效", "recommended": True},
                        {"label": SKIP, "description": ""}]}


def _usage(result: R.Reflection) -> str:
    usage = result.usage
    if usage is None or not getattr(usage, "input", None) and not getattr(usage, "output", None):
        return ""
    read = (usage.input or 0) + (getattr(usage, "cache_read", 0) or 0)
    return f"（回顾用了 ↑{read} ↓{usage.output or 0} tokens）"


def activate(pi) -> None:

    async def confirm(session, proposals: list[R.Proposal]) -> list[str]:
        """逐条问用户（一次最多 4 条）；返回每条的结论。Esc 跳过剩下的全部"""
        lines: list[str] = []
        total = len(proposals)
        for start in range(0, total, 4):
            batch = proposals[start:start + 4]
            answers = await session.ask_user([_question(p) for p in batch])
            if answers is None:
                lines.append(f"跳过了剩下的 {total - start} 条")
                break
            for proposal, answer in zip(batch, answers):
                choice = str(answer.get("answer") or "")
                if answer.get("custom") and choice.strip():
                    if proposal.kind == "skill":
                        lines.append(f"- 跳过：{proposal.title()}（技能不能直接改写，想要的话自己建）")
                        continue
                    lines.append(apply(session, proposal, choice))     # 用户改过的内容
                elif choice == SAVE:
                    lines.append(apply(session, proposal))
                else:
                    lines.append(f"- 跳过：{proposal.title()}")
        return lines

    async def command(rest: str) -> str:
        session = pi.session
        if session is None:
            return "没有会话"
        words = rest.split()
        if words and words[0] == "apply":
            return apply_pending(session, words[1:])
        if not any(getattr(m, "role", "") == "user" for m in session.messages):
            return "这次会话还没有内容可回顾。"

        model = session.agent.state.model
        spec = str(session.settings.get("reflect.model") or "").strip()
        if spec:
            model = session.model_runtime.resolve(spec) or model
        session.notify(ProgressEvent(text="正在回顾这次会话…"))
        context = R.build_context(session.agent.state.system_prompt, session.agent.state.messages,
                                  session._llm_tools())
        message = await R.ask_model(session.model_runtime.stream_fn, model, context,
                                    reasoning=session.agent.state.thinking_level or "off")
        result = R.parse(message)
        usage = _usage(result)
        if result.error:
            head = result.raw.strip()[:300]
            return f"回顾没成功：{result.error}" + (f"\n模型的回答开头：{head}" if head else "")
        head = [f"回顾：{result.summary}" if result.summary else "回顾完了。"]
        if not result.proposals:
            return "\n".join(head + [f"没有发现值得长期保留的经验。{usage}"])
        if session.user_input is not None:
            lines = await confirm(session, result.proposals)
            return "\n".join(head + lines + ([usage] if usage else []))
        session.__dict__["_reflect_pending"] = result.proposals
        listing = [f"{i}. {p.title()}" + (f"\n   理由：{p.why}" if p.why else "")
                   + (f"\n   依据：{p.evidence}" if p.evidence else "")
                   for i, p in enumerate(result.proposals, start=1)]
        return "\n".join(head + ["提议：", *listing,
                                 f"用 /reflect apply 1 3（或 all）保存；不保存就不用管{usage}"])

    def apply_pending(session, picks: list[str]) -> str:
        pending: list[R.Proposal] = session.__dict__.get("_reflect_pending") or []
        if not pending:
            return "没有待保存的提议（先运行 /reflect）"
        if not picks:
            return "用法：/reflect apply 1 3（序号见 /reflect 的提议）或 /reflect apply all"
        if picks == ["all"]:
            chosen = list(range(1, len(pending) + 1))
        else:
            chosen = []
            for word in picks:
                for part in word.split(","):
                    if not part.strip().isdigit() or not 1 <= int(part) <= len(pending):
                        return f"没有第 {part} 条（共 {len(pending)} 条）"
                    chosen.append(int(part))
        lines = [apply(session, pending[i - 1]) for i in dict.fromkeys(chosen)]
        session.__dict__["_reflect_pending"] = [p for i, p in enumerate(pending, start=1) if i not in chosen]
        return "\n".join(lines)

    pi.register_command("reflect", "回顾这次会话，把值得留下的经验写进记忆 / AGENTS.md / 技能", command,
                        usage="[apply <序号…>|all]")
