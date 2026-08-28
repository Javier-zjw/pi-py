"""System prompt assembly: base prompt + environment + AGENTS.md + skills."""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .text import read_text_lenient, sanitize

BASE_SYSTEM_PROMPT = """
You are a coding agent operating inside a user's project directory.

Working style:
- Investigate before you edit. Read the files you are about to change.
- Prefer small, targeted edits over rewriting whole files.
- Match the surrounding code's conventions instead of importing your own.
- Run the project's own tests or build commands to verify your work when they exist.
- If a request is ambiguous in a way that changes the outcome, ask before guessing.

Tool use:
- Use `read` before `edit`; `edit` requires text that matches the file exactly.
- Batch independent tool calls in one turn instead of going one at a time.
- Never run destructive commands (rm -rf, force pushes, history rewrites) unless asked.

Answering:
- Be concise. Report what you changed and why, not a narration of every step.
- Do not claim something works if you have not verified it.
"""

@dataclass
class ContextFile:
    path: str
    content: str

def find_context_files(cwd: str | Path, agent_dir: str | Path | None = None) -> list[ContextFile]:
    """从当前工作目录向上逐级查找 AGENTS.md，同时加载全局 AGENTS.md"""
    files: list[ContextFile] = []
    if agent_dir:
        global_file = Path(agent_dir).expanduser() / "AGENTS.md"
        content = read_text_lenient(global_file) if global_file.exists() else None
        if content is not None:
            files.append(ContextFile(sanitize(str(global_file)), content))

    current = Path(cwd).expanduser().resolve()
    chain: list[Path] = []
    while True:
        candidate = current / "AGENTS.md"
        if candidate.exists():
            chain.append(candidate)
        if (current / ".git").exists() or current.parent == current:
            break
        current = current.parent

    for candidate in reversed(chain):
        content = read_text_lenient(candidate)
        if content is not None:
            files.append(ContextFile(sanitize(str(candidate)), content))

    return files

def environment_block(cwd: str | Path) -> str:
    return (
        "<environment>\n"
        f"cwd: {Path(cwd).expanduser().resolve()}\n"
        f"platform: {platform.system().lower()} ({platform.machine()})\n"
        f"shell: {os.environ.get('SHELL', 'sh')}\n"
        f"date: {date.today().isoformat()}\n"
        "</environment>"
    )

def build_system_prompt(
        cwd: str | Path,
        base: str = BASE_SYSTEM_PROMPT,
        context_files: list[ContextFile] | None = None,
        skills_block: str | None = None
) -> str:
    parts = [base, environment_block(cwd)]
    for f in context_files or []:
        parts.append(f"<context_file path=\"{f.path}\">\n{f.content}\n</context_file>")
    if skills_block:
        parts.append(skills_block)
    return "\n\n".join(parts)

