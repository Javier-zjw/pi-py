"""/reflect：回顾这次会话，提议把值得留下的经验写进记忆、AGENTS.md 或技能（参照 Harness² 的经验库）。

怎么问模型（同压缩）：用会话当前的系统提示 + 工具 + 全部消息，末尾追加一条 ``<reflect>`` 指令，
``tool_choice="none"``——前缀和正常请求一样，能命中前缀缓存；模型看到的是这次会话的原貌，不用另拼一份。
回顾请求不进会话：不写条目、不改消息。

要模型拿出证据（Harness² 的要求）：每条提议都要指出会话里哪一刻说明了它；只跟这次任务有关的、看代码就
知道的、已经记过的、没验证的猜测、密钥都不要；太泛（"要仔细"）没用，太细（"那天费用涨到 250"）不会再
用上。没有值得留的就一条都不提——这是正常结果。

三种提议：

- ``memory``：一条事实，scope = user（用户本人的偏好）/ project（这个项目的事实），写进长期记忆；
- ``agents_md``：项目 AGENTS.md 里加一条规则——会提交、全队共享，所以只提每个智能体都该遵守的；
- ``skill``：有明确触发场景的多步流程，新建 ``.pi/skills/<name>/SKILL.md``（同名已存在就不覆盖）。

都要用户确认后才写；写进去的东西下次会话生效（记忆、AGENTS.md、技能都在会话开始时读）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pi_agent import default_convert_to_llm
from pi_ai import (
    AssistantMessage,
    Context,
    DoneEvent,
    ErrorEvent,
    SimpleStreamOptions,
    Tool,
    UserMessage,
)

KINDS = ("memory", "agents_md", "skill")
MAX_PROPOSALS = 5
SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9\-]{0,63}$")
AGENTS_HEADING = "## 经验（/reflect）"

REFLECT_PROMPT = """<reflect>
Pause the task. The user ran /reflect: look back over this whole session and decide what is worth \
keeping for FUTURE sessions. Do not continue the task and do not call tools.

Look for (and cite the specific moment as evidence):
- The user corrected you, rejected an approach, or stated a preference.
- You hit the same error or dead end repeatedly, or spent many steps before finding the right \
command, file or convention.
- You discovered a non-obvious fact about this project (how to build / test / run it, a gotcha, \
where something lives) that took effort and will matter again.
- You worked out a multi-step procedure that will recur.

Propose at most {limit} items. Fewer is better; an empty list is a valid answer.
- "memory": one short, self-contained fact. scope "user" = the user's own preference that applies \
to every project; scope "project" = a fact about this project.
- "agents_md": a short rule or fact for this project's AGENTS.md. It is committed and shared with \
the team, so only propose what every agent working on this project should follow.
- "skill": a reusable procedure with a recognisable trigger: name (kebab-case), description (when to \
use it), body (the steps, in Markdown). Only for procedures, never for single facts.

Rules:
- Every proposal needs evidence: the moment in this session that shows it (the user message, the \
failing tool call, the step that finally worked).
- Do not propose: facts that only matter for this task, anything obvious from reading the code, \
anything already in AGENTS.md or in your memory (both are in your system prompt), unverified guesses, \
or secrets.
- Too generic is useless ("be careful"); too specific never applies again. Aim for something a \
future session can apply and check.
- Write summary, why, evidence and the proposal text in the user's language.

Reply with ONLY one JSON object (no prose, no code fence):
{{"summary": "<what happened in this session, 1-2 sentences>", "proposals": [
  {{"kind": "memory", "scope": "project", "text": "...", "why": "...", "evidence": "..."}},
  {{"kind": "agents_md", "text": "...", "why": "...", "evidence": "..."}},
  {{"kind": "skill", "name": "...", "description": "...", "body": "...", "why": "...", "evidence": "..."}}
]}}
</reflect>"""


@dataclass
class Proposal:
    kind: str
    text: str = ""              # memory / agents_md 的内容；skill 的正文
    scope: str = "project"      # memory
    name: str = ""              # skill
    description: str = ""       # skill
    why: str = ""
    evidence: str = ""

    def title(self) -> str:
        if self.kind == "memory":
            return f"记忆（{'全局' if self.scope == 'user' else '项目'}）：{self.text}"
        if self.kind == "agents_md":
            return f"AGENTS.md：{self.text}"
        return f"技能 {self.name}：{self.description}"


@dataclass
class Reflection:
    summary: str = ""
    proposals: list[Proposal] = field(default_factory=list)
    error: str = ""
    raw: str = ""
    usage: Any = None


def build_context(system_prompt: str, messages: list, tools: list[Tool]) -> Context:
    """会话当前的样子 + 末尾一条回顾指令（前缀和正常请求一样）"""
    llm_messages = default_convert_to_llm(list(messages))
    llm_messages.append(UserMessage(content=REFLECT_PROMPT.format(limit=MAX_PROPOSALS)))
    return Context(system_prompt=system_prompt, messages=llm_messages, tools=tools)


async def ask_model(stream_fn, model, context: Context, reasoning: str = "off",
                    max_tokens: int = 4000) -> AssistantMessage:
    options = SimpleStreamOptions(reasoning=reasoning, max_tokens=max_tokens,
                                  tool_choice="none" if context.tools else None)
    final: AssistantMessage | None = None
    async for event in stream_fn(model, context, options):
        if isinstance(event, (DoneEvent, ErrorEvent)):
            final = event.message
    return final or AssistantMessage(stop_reason="error", error_message="stream ended without a result")


def _json_object(text: str) -> dict | None:
    """回复里的那个 JSON 对象（容忍前后多余的字和代码围栏）"""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _clean(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def parse(message: AssistantMessage) -> Reflection:
    text = "".join(getattr(c, "text", "") or "" for c in message.content
                   if getattr(c, "type", "") == "text")
    result = Reflection(raw=text, usage=getattr(message, "usage", None))
    if message.stop_reason == "error":
        result.error = message.error_message or "模型调用出错"
        return result
    data = _json_object(text)
    if data is None:
        result.error = "模型没有按格式回答（不是 JSON）"
        return result
    result.summary = _clean(data.get("summary"), 500)
    for item in data.get("proposals") or []:
        if not isinstance(item, dict) or item.get("kind") not in KINDS:
            continue
        proposal = Proposal(kind=item["kind"], text=_clean(item.get("text") or item.get("body"), 4000),
                            scope="user" if item.get("scope") == "user" else "project",
                            name=str(item.get("name") or "").strip().lower(),
                            description=" ".join(str(item.get("description") or "").split())[:300],
                            why=_clean(item.get("why"), 400), evidence=_clean(item.get("evidence"), 600))
        if not proposal.text:
            continue
        if proposal.kind == "memory":
            proposal.text = " ".join(proposal.text.split())[:500]
        if proposal.kind == "skill" and not (SKILL_NAME.match(proposal.name) and proposal.description):
            continue
        result.proposals.append(proposal)
        if len(result.proposals) >= MAX_PROPOSALS:
            break
    return result


# -- 写进去 ------------------------------------------------------------------ #

def append_agents_md(cwd: str | Path, text: str) -> Path:
    """加到项目 AGENTS.md 的「经验（/reflect）」一节末尾（没有这节就在文件末尾新开）"""
    path = Path(cwd) / "AGENTS.md"
    current = path.read_text("utf-8") if path.is_file() else ""
    entry = text.strip()
    if not entry.startswith(("-", "*", "#")):
        entry = f"- {entry}"
    if AGENTS_HEADING not in current:
        current = current.rstrip("\n") + ("\n\n" if current.strip() else "") + AGENTS_HEADING + "\n"
    current = current.rstrip("\n") + "\n" + entry + "\n"
    path.write_text(current, "utf-8")
    return path


def write_skill(cwd: str | Path, name: str, description: str, body: str) -> Path:
    path = Path(cwd) / ".pi" / "skills" / name / "SKILL.md"
    if path.exists():
        raise FileExistsError(f"技能 {name} 已经有了（{path}），没有覆盖")
    path.parent.mkdir(parents=True, exist_ok=True)
    description = description.replace("\n", " ").strip()
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body.strip()}\n", "utf-8")
    return path
