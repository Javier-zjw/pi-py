"""
文件检索工具（grep/find）
环境存在 ripgrep、fd 命令时优先调用；缺失则降级为纯Python目录遍历实现，保证对外接口完全一致
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import shutil
from pathlib import Path
from typing import Any

from pi_agent import AgentTool, AgentToolResult, ToolContext
from pi_ai import TextContent

from ..text import sanitize
from .files import resolve_path
from .truncate import truncate_tail

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".mypy_cache"}
MAX_MATCHES = 200

async def _run(cmd: list[str], cwd: str | Path) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(cmd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    out, _ = await process.communicate()
    return process.returncode or 0, out.decode("utf-8", "replace")


def create_grep_tool(cwd: str | Path = ".") -> AgentTool:
    async def execute(args: dict[str, Any], ctx: ToolContext) -> AgentToolResult:
        pattern = args["pattern"]
        root = resolve_path(cwd, args.get("path") or ".")
        glob = args.get("glob")
        ignore_case = bool(args.get("ignore_case"))

        if shutil.which("rg"):
            cmd = ["rg", "--line-number", "--no-heading", "--color", "never", "-m", str(MAX_MATCHES)]
            if ignore_case:
                cmd.append("-i")
            if glob:
                cmd += ["--glob", glob]
            cmd += [pattern, str(root)]
            code, out = await _run(cmd, cwd)
            if code > 1:
                return AgentToolResult.text(f"ripgrep failed for pattern: {pattern}", is_error=True)
        else:
            try:
                regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
            except re.error as exc:
                return AgentToolResult.text(f"Invalid regex: {exc}", is_error=True)
            lines: list[str] = []
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
                for name in filenames:
                    if glob and not fnmatch.fnmatch(name, glob):
                        continue
                    file_path = Path(dirpath) / name
                    try:
                        with file_path.open("r", encoding="utf-8", errors="ignore") as fh:
                            for lineno, line in enumerate(fh, 1):
                                if regex.search(line):
                                    lines.append(
                                        sanitize(f"{file_path}:{lineno}:{line.rstrip()}")
                                    )
                                    if len(lines) >= MAX_MATCHES:
                                        break
                    except OSError:
                        continue
                    if len(lines) >= MAX_MATCHES:
                        break
                if len(lines) >= MAX_MATCHES:
                    break
            out = "\n".join(lines)

        if not out.strip():
            return AgentToolResult.text(f"No matches for: {pattern}")
        body, _ = truncate_tail(out, hint="narrow the pattern or pass a path")
        return AgentToolResult(
            content=[TextContent(text=body)],
            details={"pattern": pattern, "matches": len(out.strip().split("\n"))},
        )

    return AgentTool(
        name="grep",
        label="Grep",
        description="Search file contents by regular expression. Returns path:line:match.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "description": "Directory to search, defaults to cwd"},
                "glob": {"type": "string", "description": "Filter filenames, e.g. '*.py'"},
                "ignore_case": {"type": "boolean", "default": False},
            },
            "required": ["pattern"],
        },
        execute=execute,
    )


def create_find_tool(cwd: str | Path = ".") -> AgentTool:
    async def execute(args: dict[str, Any], ctx: ToolContext) -> AgentToolResult:
        pattern = args["pattern"]
        root = resolve_path(cwd, args.get("path") or ".")
        matches: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for name in filenames:
                full = Path(dirpath) / name
                rel = full.relative_to(root)
                if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(str(rel), pattern):
                    matches.append(sanitize(str(rel)))
                    if len(matches) >= MAX_MATCHES:
                        break
            if len(matches) >= MAX_MATCHES:
                break
        if not matches:
            return AgentToolResult.text(f"No files matching: {pattern}")
        body, _ = truncate_tail("\n".join(sorted(matches)))
        return AgentToolResult(
            content=[TextContent(text=body)], details={"pattern": pattern, "count": len(matches)}
        )

    return AgentTool(
        name="find",
        label="Find",
        description="Find files by glob pattern, e.g. '*.py' or 'src/**/test_*.py'.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["pattern"],
        },
        execute=execute,
    )
