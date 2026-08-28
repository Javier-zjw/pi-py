"""
编辑工具：精准的搜索替换能力。
优先执行精确匹配。匹配失败时，会使用规范化比对重试；
该比对机制容忍模型常会产生的各类差异：末尾空白、智能引号、Unicode长横线等，
但最终写入文件的仍是原始真实字节。
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from pi_agent import AgentTool, AgentToolResult, ToolContext
from pi_ai import TextContent

from .files import _lock_for, resolve_path

_NORMALIZE = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2015": "-",
    "\u00a0": " ",
}

def normalize(text: str) -> str:
    for src, dst in _NORMALIZE.items():
        text = text.replace(src, dst)

    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n"))

def fuzzy_find(haystack: str, needle: str) -> tuple[int, int] | None:
    """返回 `haystack` 中与 `needle` 松散匹配的起止位置区间 (start, end)。"""
    h_lines = haystack.split("\n")
    n_lines = normalize(needle).split("\n")
    if not n_lines:
        return None

    norm_h = [normalize(line) for line in h_lines]
    window = len(n_lines)
    for i in range(0, max(len(h_lines) - window + 1, 0)):
        if norm_h[i : i + window] == n_lines:
            start = sum(len(line) + 1 for line in h_lines[:i])
            end = start + sum(len(line) + 1 for line in h_lines[i : i + window]) - 1
            return start, end

    return None

def generate_unified_patch(path: str, before: str, after: str) -> str:
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=3
    )
    return "".join(diff)

def create_edit_tool(cwd: str | Path = ".") -> AgentTool:
    async def execute(args: dict[str, Any], ctx: ToolContext) -> AgentToolResult:
        path = resolve_path(cwd, args["path"])
        old_text = args["old_text"]
        new_text = args.get("new_text", "")
        replace_all = bool(args.get("replace_all"))

        if not path.exists():
            return AgentToolResult.text(f"File not found: {path}", is_error=True)

        async with _lock_for(path):
            original = path.read_text("utf-8")
            content = original.replace("\r\n", "\n")

            count = content.count(old_text)
            if count == 0:
                span = fuzzy_find(content, old_text)
                if span is None:
                    return AgentToolResult.text(
                        f"old_text not found in {path}. Read the file again and copy the exact text.",
                        is_error=True
                    )
                start, end = span
                updated = content[:start] + new_text + content[end:]
                matched = "fuzzy"

            elif count > 1 and not replace_all:
                return AgentToolResult.text(
                    f"old_text appears {count} times in {path}. Add surrounding context to make "
                    "it unique, or pass replace_all=true.",
                    is_error=True
                )
            else:
                updated = content.replace(old_text, new_text) if replace_all else content.replace(old_text, new_text, 1)

                matched = "exact"

            if updated == content:
                return AgentToolResult.text("No change: old_text and new_text are identical.", is_error=True)

            path.write_text(updated, "utf-8")

        patch = generate_unified_patch(str(path), content, updated)
        added = sum(1 for line in patch.split("\n") if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in patch.split("\n") if line.startswith("-") and not line.startswith("---"))

        return AgentToolResult(
            content=[TextContent(text=f"Edited {path} (+{added} -{removed}, {matched} match)")],
            details={"path": str(path), "patch": patch, "match": matched}
        )

    return AgentTool(
        name="edit",
        label="Edit",
        description=(
            "Replace an exact block of text in a file. old_text must match the file content, "
            "including indentation. Include enough surrounding lines to make it unique."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string", "description": "Exact text to replace"},
                "new_text": {"type": "string", "description": "Replacement text"},
                "replace_all": {"type": "boolean", "default": False},
            },
            "required": ["path", "old_text", "new_text"],
        },
        execute=execute
    )