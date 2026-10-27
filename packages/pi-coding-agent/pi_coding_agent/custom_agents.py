"""自定义子智能体：一个 Markdown 文件定义一个子智能体（同 Claude Code 的 ``.claude/agents/*.md``）。

    ---
    name: reviewer
    description: 审查改动是否有 bug、是否符合项目约定。改完一批代码后用。
    tools: read, grep, find, bash        # 不写 = 和 general 一样（主会话的工具，改动照常确认）
    model: deepseek/deepseek-chat        # 不写 / inherit = 设置 subagents.model 或当前模型
    maxTurns: 20                         # 不写 = 设置 subagents.maxTurns
    ---
    你是代码审查员。……（正文就是这个子智能体的角色说明）

从这些目录读，后面的覆盖前面的同名项：``~/.claude/agents`` → ``~/.pi/agent/agents`` →
``<cwd>/.claude/agents`` → ``<cwd>/.pi/agents``。Claude Code 的文件直接能用：工具名
``Read`` / ``Glob`` / ``WebFetch`` 这类换成 pi 的名字，认不出的忽略并提示；模型写 ``sonnet`` 这类
别名找不到时用当前模型。

主智能体怎么知道有哪些：task 工具的说明里列出每个自定义子智能体的 description，``agent`` 参数可以填
它的名字——和 Claude Code 一样靠 description 让模型挑，所以 description 要写清"什么时候用"。

文件里的正文只是角色说明：子智能体仍然带主会话的基础系统提示（AGENTS.md、记忆、技能）和子智能体规则，
工具调用照常经过主会话的权限检查。
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path

from .skills import parse_front_matter
from .text import read_text_lenient, sanitize

BUILTIN = ("explore", "general")
NAME_RULE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")
CORE_TOOLS = ("read", "grep", "find", "ls", "bash", "edit", "write")
READ_ONLY = ("read", "grep", "find", "ls", "bash", "web_search", "web_fetch")
# Claude Code / 别家的工具名 → pi 的；None = pi 没有对应的，忽略
ALIASES: dict[str, str | None] = {
    "glob": "find", "multiedit": "edit", "notebookedit": None, "notebookread": "read",
    "webfetch": "web_fetch", "websearch": "web_search", "todowrite": None, "todoread": None,
    "task": None, "agent": None, "exitplanmode": None, "bashoutput": None, "killshell": None,
    "list": "ls",
}


@dataclass
class AgentDef:
    name: str
    description: str
    prompt: str                         # 正文：角色说明
    tools: list[str] | None = None      # None = 继承（同 general）
    model: str = ""                     # "" = 默认
    max_turns: int | None = None
    path: str = ""
    source: str = ""                    # project / user / claude-project / claude-user
    notes: list[str] = field(default_factory=list)     # 加载时的提示（忽略了哪些工具等）


def agent_dirs(cwd: str | Path, agent_dir: str | Path) -> list[tuple[Path, str]]:
    home = Path.home()
    project = Path(cwd).expanduser()
    dirs = [
        (home / ".claude" / "agents", "claude-user"),
        (Path(agent_dir).expanduser() / "agents", "user"),
        (project / ".claude" / "agents", "claude-project"),
        (project / ".pi" / "agents", "project"),
    ]
    # 在家目录里启动时 ~/.claude/agents 和 <cwd>/.claude/agents 是同一个：只读一次（按后面的算来源）
    seen: dict[str, int] = {}
    for index, (directory, _) in enumerate(dirs):
        seen[str(directory.resolve())] = index
    return [entry for index, entry in enumerate(dirs) if seen[str(entry[0].resolve())] == index]


def signature(cwd: str | Path, agent_dir: str | Path) -> tuple:
    """目录里文件的 (路径, 修改时间)：没变就不用重新读、也不用改 task 工具的说明（前缀缓存）"""
    found = []
    for directory, _ in agent_dirs(cwd, agent_dir):
        if directory.is_dir():
            try:
                found += [(str(p), p.stat().st_mtime) for p in sorted(directory.glob("*.md"))]
            except OSError:
                continue
    return tuple(found)


def _tool_names(raw: str) -> tuple[list[str] | None, list[str]]:
    """frontmatter 的 tools → (pi 工具名列表 | None 表示继承, 提示)"""
    text = raw.strip().strip("[]")
    if not text or text in ("*", "all", "inherit"):
        return None, []
    names, notes = [], []
    for item in re.split(r"[,\s]+", text):
        item = item.strip().strip("'\"")
        if not item:
            continue
        lower = item.lower()
        if lower.startswith("mcp__"):
            names.append(item)          # MCP 工具名区分大小写，可以带通配 mcp__github__*
            continue
        mapped = ALIASES.get(lower, lower)
        if mapped is None:
            notes.append(f"工具 {item} 在 pi 里没有，忽略")
            continue
        names.append(mapped)
    return list(dict.fromkeys(names)), notes


def load_file(path: Path, source: str) -> tuple[AgentDef | None, str | None]:
    """读一个定义文件：(定义, None) 或 (None, 为什么跳过)"""
    text = read_text_lenient(path)
    if text is None:
        return None, "读不了"
    meta, body = parse_front_matter(text)
    name = (meta.get("name") or path.stem).strip().lower()
    if not NAME_RULE.match(name):
        return None, f"名字 {name!r} 不合法（小写字母、数字、- 和 _）"
    if name in BUILTIN:
        return None, f"{name} 是内置子智能体的名字，换一个"
    description = " ".join((meta.get("description") or "").split())
    if not description:
        return None, "缺 description（主智能体靠它决定什么时候用）"
    tools, notes = _tool_names(meta.get("tools", ""))
    model = (meta.get("model") or "").strip()
    max_turns = None
    raw_turns = (meta.get("maxTurns") or meta.get("max_turns") or "").strip()
    if raw_turns:
        if raw_turns.isdigit() and int(raw_turns) >= 2:
            max_turns = int(raw_turns)
        else:
            notes.append(f"maxTurns {raw_turns!r} 不是 ≥2 的整数，用默认值")
    return AgentDef(name=name, description=description, prompt=body.strip(), tools=tools,
                    model="" if model.lower() == "inherit" else model, max_turns=max_turns,
                    path=sanitize(str(path)), source=source, notes=notes), None


def load_agents(cwd: str | Path, agent_dir: str | Path) -> tuple[dict[str, AgentDef], list[str]]:
    """所有自定义子智能体（同名后面的覆盖前面的）和跳过的文件说明"""
    agents: dict[str, AgentDef] = {}
    skipped: list[str] = []
    for directory, source in agent_dirs(cwd, agent_dir):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            if path.name.lower() in ("readme.md",):
                continue
            agent, why = load_file(path, source)
            if agent is None:
                skipped.append(f"{sanitize(str(path))}：{why}")
            else:
                agents[agent.name] = agent
    return agents, skipped


def pick_tools(agent: AgentDef, parent_tools: list, cwd: str, never: set[str],
               read_only: bool = False) -> list:
    """按定义挑工具：内置文件工具现建（主会话没开 grep 也能给）、别的（web_*、MCP）从主会话当前的工具里挑；
    read_only（计划模式）只留只读的"""
    from .tools import create_tools

    if agent.tools is None:
        tools = [t for t in parent_tools if t.name not in never]
    else:
        wanted = [name for name in agent.tools if name not in never]
        tools = create_tools([name for name in CORE_TOOLS if name in wanted], cwd)
        have = {t.name for t in tools}
        for tool in parent_tools:
            if tool.name in have or tool.name in never:
                continue
            if any(fnmatch.fnmatchcase(tool.name, pattern) for pattern in wanted):
                tools.append(tool)
                have.add(tool.name)
    if read_only:
        tools = [t for t in tools if t.name in READ_ONLY]
    return tools


def tool_listing(agents: dict[str, AgentDef], limit: int = 300) -> str:
    """task 工具说明里列出自定义子智能体"""
    if not agents:
        return ""
    lines = [("\nCustom agents defined for this project/user (pass the name as `agent`; pick one "
              "when its description fits the task):")]
    for agent in agents.values():
        text = agent.description if len(agent.description) <= limit else agent.description[:limit] + "…"
        lines.append(f"- {agent.name}: {text}")
    return "\n".join(lines)
