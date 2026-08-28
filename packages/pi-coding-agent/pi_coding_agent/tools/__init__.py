"""Built-in coding tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from pi_agent import AgentTool, AgentToolResult, ToolContext

from .bash import create_bash_tool, kill_process_tree, run_bash
from .edit import create_edit_tool, fuzzy_find, generate_unified_patch
from .files import create_ls_tool, create_write_tool, create_read_tool, resolve_path
from .search import create_find_tool, create_grep_tool
from .truncate import DEFAULT_MAX_LINES, DEFAULT_MAX_BYTES, truncate_head, truncate_tail

ALL_TOOL_NAMES = ("read", "bash", "edit", "write", "grep", "find", "ls")
DEFAULT_TOOL_NAMES = ("read", "bash", "edit", "write")
READ_ONLY_TOOL_NAMES = ("read", "grep", "find", "ls")

_FACTORIES: dict[str, Callable[[str | Path], AgentTool]] = {
    "read": create_read_tool,
    "write": create_write_tool,
    "edit": create_edit_tool,
    "bash": create_bash_tool,
    "ls": create_ls_tool,
    "grep": create_grep_tool,
    "find": create_find_tool,
}

def create_tools(names: Iterable[str], cwd: str | Path = ".") -> list[AgentTool]:
    tools: list[AgentTool] = []
    for name in names:
        factory = _FACTORIES.get(name)
        if factory:
            tools.append(factory(cwd))
    return tools

def create_coding_tools(cwd: str | Path = ".") -> list[AgentTool]:
    return create_tools(DEFAULT_TOOL_NAMES, cwd)

def create_all_tools(cwd: str | Path = ".") -> list[AgentTool]:
    return create_tools(ALL_TOOL_NAMES, cwd)

def create_read_only_tools(cwd: str | Path = ".") -> list[AgentTool]:
    return create_tools(READ_ONLY_TOOL_NAMES, cwd)

def define_tool(
        name: str,
        description: str,
        parameters: dict[str, Any],
        execute: Callable[[dict[str, Any], ToolContext], Awaitable[AgentToolResult]],
        label: str | None = None,
) -> AgentTool:
    """面向SDK使用者与扩展插件的辅助工具"""
    return AgentTool(
        name=name,
        description=description,
        parameters=parameters,
        execute=execute,
        label=label
    )

__all__ = [
    "ALL_TOOL_NAMES",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_LINES",
    "DEFAULT_TOOL_NAMES",
    "READ_ONLY_TOOL_NAMES",
    "create_all_tools",
    "create_bash_tool",
    "create_coding_tools",
    "create_edit_tool",
    "create_find_tool",
    "create_grep_tool",
    "create_ls_tool",
    "create_read_only_tools",
    "create_read_tool",
    "create_tools",
    "create_write_tool",
    "define_tool",
    "fuzzy_find",
    "generate_unified_patch",
    "kill_process_tree",
    "resolve_path",
    "run_bash",
    "truncate_head",
    "truncate_tail",
]