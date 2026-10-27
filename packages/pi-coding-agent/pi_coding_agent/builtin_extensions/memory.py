"""
长期记忆（逻辑见 pi_coding_agent/memory.py）：模型用 memory 工具记下以后还用得上的事，下次会话开始时
读进系统提示。

- memory(action, scope, text, old_text)：add / replace / remove；scope = user（用户偏好，所有项目）
  或 project（这个项目的事实）。不用确认（只写 ~/.pi/agent/memory/ 下的文件），界面留一行「✎ 记住了」。
- /memory：列出两份记忆（带序号和文件位置）；/memory forget <序号> 删一条。
- /remember [--user] <内容>：用户直接让它记一条（默认记进项目，--user / -g 记进全局）。

设置（settings.json）：

    "memory": {"enabled": true, "userChars": 2000, "projectChars": 4000}

不要这个功能就把 memory 写进 disabledExtensions（或 memory.enabled = false：不读也不写）。
"""

from __future__ import annotations

from pathlib import Path

from pi_agent import AgentToolResult, ToolContext

from pi_coding_agent import memory as store

NAME = "memory"
DESCRIPTION = "长期记忆：记下以后还用得上的偏好和项目事实，下次会话自动带上"

TOOL_DESCRIPTION = """\
Save durable notes that future sessions will see: they are loaded into the system prompt when a \
session starts.
- scope="user": the user's own preferences that apply to every project (language, tone, workflow \
habits, things they told you never/always to do).
- scope="project" (default): facts about this project that took effort to find and will matter \
again (how to build/test/run, non-obvious conventions, gotchas, where things live).
Save when the user corrects you or states a preference, or when you learn such a fact. Do NOT save: \
details that only matter for the current task, anything obvious from the code or already in \
AGENTS.md, guesses you have not verified, or secrets.
One short, self-contained fact per entry. Keep memory tidy: when an entry is outdated, wrong or \
overlaps another, use action="replace" (old_text = a unique part of the existing entry) or \
action="remove". Notes saved now take effect next session; in this session you already know them."""


PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["add", "replace", "remove"]},
        "scope": {"type": "string", "enum": ["user", "project"],
                  "description": "user = the user's preferences (all projects); "
                                 "project = facts about this project (default)"},
        "text": {"type": "string", "description": "The entry to save (add / replace)"},
        "old_text": {"type": "string",
                     "description": "A unique part of the existing entry (replace / remove)"},
    },
    "required": ["action"],
}


def _agent_dir(session) -> Path:
    from pi_coding_agent.session.manager import DEFAULT_AGENT_DIR

    return Path(getattr(session.resources, "agent_dir", None) or DEFAULT_AGENT_DIR)


def _limits(session) -> dict[str, int]:
    get = session.settings.get
    return {"user": int(get("memory.userChars", 2000) or 2000),
            "project": int(get("memory.projectChars", 4000) or 4000)}


def _enabled(session) -> bool:
    return bool(session.settings.get("memory.enabled", True))


def _short(path: Path) -> str:
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home):] if text.startswith(home) else text


def activate(pi) -> None:

    def section(session) -> str | None:
        if not _enabled(session):
            return None
        memories = [store.load(_agent_dir(session), scope, session.cwd) for scope in store.SCOPES]
        return store.prompt_block(memories, _limits(session), home=str(Path.home()))

    pi.system_prompt_section(section)

    async def execute(args: dict, ctx: ToolContext) -> AgentToolResult:
        session = pi.session
        if session is None or not _enabled(session):
            return AgentToolResult.text("memory is turned off (settings memory.enabled)", is_error=True)
        action = str(args.get("action") or "add")
        scope = str(args.get("scope") or "project")
        if scope not in store.SCOPES:
            return AgentToolResult.text('scope must be "user" or "project"', is_error=True)
        limit = _limits(session)[scope]
        memory = store.load(_agent_dir(session), scope, session.cwd)
        details = {"action": action, "scope": scope, "path": str(memory.path)}
        try:
            if action == "add":
                text, redacted = store.add(memory, args.get("text", ""), session.cwd, limit)
                details.update(text=text, redacted=redacted)
                message = f"Saved to {scope} memory"
            elif action == "replace":
                old, text, redacted = store.replace(memory, args.get("old_text", ""),
                                                    args.get("text", ""), session.cwd, limit)
                details.update(old=old, text=text, redacted=redacted)
                message = f"Updated the {scope} memory entry"
            elif action == "remove":
                old = store.remove(memory, args.get("old_text", ""), session.cwd)
                details.update(old=old)
                message = f"Removed the {scope} memory entry"
            else:
                return AgentToolResult.text('action must be "add", "replace" or "remove"',
                                            is_error=True)
        except store.MemoryEditError as exc:
            return AgentToolResult.text(str(exc), is_error=True, details=details)
        details.update(used=memory.used, limit=limit)
        note = " Secrets were replaced with [REDACTED]." if details.get("redacted") else ""
        return AgentToolResult.text(f"{message} ({memory.used}/{limit} chars used).{note}",
                                    details=details)

    pi.register_tool("memory", TOOL_DESCRIPTION, PARAMETERS, execute, label="记忆")

    def listing(session) -> tuple[str, list[tuple[store.Memory, int]]]:
        """两份记忆连续编号：全局在前、项目在后；返回 (文字, 序号 → (哪份, 份内第几条))"""
        lines, index = [], []
        for scope in store.SCOPES:
            memory = store.load(_agent_dir(session), scope, session.cwd)
            limit = _limits(session)[scope]
            lines.append(f"{store.SCOPE_NAMES[scope]}（{_short(memory.path)} · "
                         f"{memory.used}/{limit} 字）")
            if not memory.entries:
                lines.append("  （还没有）")
            for position, entry in enumerate(memory.entries, start=1):
                index.append((memory, position))
                lines.append(f"  {len(index)}. {entry}")
            lines.append("")
        lines.append("/memory forget <序号> 删一条 · /remember [--user] <内容> 让它记一条 · "
                     "也可以直接改上面的文件；改动下次会话生效")
        return "\n".join(lines), index

    async def memory_command(rest: str) -> str:
        session = pi.session
        if session is None:
            return "没有会话"
        if not _enabled(session):
            return "长期记忆已关闭（settings memory.enabled = false）"
        words = rest.split()
        if not words or words[0] in ("list", "ls", "show"):
            return listing(session)[0]
        if words[0] in ("forget", "rm", "remove", "delete"):
            if len(words) < 2 or not words[1].isdigit():
                return "用法：/memory forget <序号>（序号见 /memory）"
            _, index = listing(session)
            number = int(words[1])
            if not 1 <= number <= len(index):
                return f"没有第 {number} 条（共 {len(index)} 条，见 /memory）"
            memory, position = index[number - 1]
            old = store.remove_at(memory, position, session.cwd)
            return f"已删除（{store.SCOPE_NAMES[memory.scope]}）：{old}"
        return "用法：/memory 列出 · /memory forget <序号> 删一条"

    async def remember_command(rest: str) -> str:
        session = pi.session
        if session is None:
            return "没有会话"
        if not _enabled(session):
            return "长期记忆已关闭（settings memory.enabled = false）"
        words = rest.split()
        scope = "project"
        if words and words[0] in ("--user", "-g", "--global"):
            scope, rest = "user", rest.split(None, 1)[1] if len(words) > 1 else ""
        if not rest.strip():
            return "用法：/remember [--user] <要记住的内容>（--user 记进全局，默认记进这个项目）"
        memory = store.load(_agent_dir(session), scope, session.cwd)
        try:
            text, redacted = store.add(memory, rest, session.cwd, _limits(session)[scope])
        except store.MemoryEditError as exc:
            return f"没记上：{exc}"
        note = "（密钥已替换成 [REDACTED]）" if redacted else ""
        return (f"已记住（{store.SCOPE_NAMES[scope]}）：{text}{note}\n"
                "下次会话起生效（这次对话里要用的话，直接跟它说一声）")

    pi.register_command("memory", "查看 / 删除长期记忆", memory_command, usage="[forget <序号>]")
    pi.register_command("remember", "让它记住一件事（下次会话起生效）", remember_command,
                        usage="[--user] <内容>")
