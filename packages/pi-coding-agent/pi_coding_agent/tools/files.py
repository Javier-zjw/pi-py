"""read / write / ls."""

from __future__ import annotations

import asyncio
import base64
import os
from os import mkdir
from pathlib import Path
from typing import Any

from pi_agent import AgentTool, AgentToolResult, ToolContext
from pi_ai import ImageContent, TextContent

from ..text import sanitize
from .truncate import DEFAULT_MAX_LINES, truncate_tail

# 通过文件头部魔数（Magic Number）识别图片 MIME 类型
_IMAGE_MAGIC: list[tuple[bytes, str]] = [
(b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),
]

# 每条绝对路径对应独立锁，避免两个并发工具调用出现写入操作交错
_file_locks: dict[str, asyncio.Lock] = {}

def _lock_for(path: Path) -> asyncio.Lock:
    key = str(path.resolve())

    if key not in _file_locks:
        _file_locks[key] = asyncio.Lock()

    return _file_locks[key]

def resolve_path(cwd: str | Path, raw: str) -> Path:
    p = Path(raw).expanduser()
    return p if p.is_absolute() else Path(cwd) / p

def sniff_mime(head: bytes) -> str | None:
    for magic, mime in _IMAGE_MAGIC:
        if head.startswith(magic):
            return mime

    return None

def create_read_tool(cwd: str | Path = ".") -> AgentTool:
    async def execute(args: dict[str, Any], ctx: ToolContext) -> AgentToolResult:
        path = resolve_path(cwd, args["path"])

        if not path.exists():
            return AgentToolResult.text(f"File not found: {path}", is_error=True)
        if path.is_dir():
            return AgentToolResult.text(f"Not a file: {path}", is_error=True)

        head = path.read_bytes()[:16]
        mime = sniff_mime(head)
        if mime:
            data = base64.b64encode(path.read_bytes()).decode("ascii")
            return AgentToolResult(
                content=[ImageContent(data, mime_type=mime)],
                details={"path": str(path), "mimeType": mime}
            )

        try:
            # 故意用严格模式：读不出来就明确报错，比把脏数据塞进上下文强。
            # 不要改成 errors="ignore"（静默丢数据）或 "surrogateescape"（会在
            # 序列化时炸，且离案发现场很远）
            text = path.read_text("utf-8")
        except UnicodeDecodeError:
            return AgentToolResult.text(
                f"Not valid UTF-8, cannot read as text: {path}", is_error=True
            )

        lines = text.split("\n")
        offset = int(args.get("offset") or 1)
        limit = int(args.get("limit") or DEFAULT_MAX_LINES)
        selected = lines[offset - 1: offset - 1 + limit]
        numbered = "\n".join(f"{offset + i:6d}\t{line}" for i, line in enumerate(selected))
        end = offset + len(selected)
        body, _ = truncate_tail(numbered, hint=f"use offset={end} to continue")
        if end <= len(lines):
            body += f"\n\n[file has {len(lines)} lines; use offset={end} to continue]"

        return AgentToolResult(
            content=[TextContent(text=body)],
            details={"path": str(path), "lines": len(lines), "offset": offset}
        )

    return AgentTool(
        name="read",
        label="Read",
        description=(
            "Read a file from disk. Returns line-numbered text, or the image itself for "
            "image files. Use offset/limit to page through large files."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path, absolute or relative to cwd"},
                "offset": {"type": "integer", "description": "1-based first line to read"},
                "limit": {"type": "integer", "description": "Maximum number of lines"},
            },
            "required": ["path"],
        },
        execute=execute
    )

def create_write_tool(cwd: str | Path = ".") -> AgentTool:

    async def execute(args: dict[str, Any], ctx: ToolContext) -> AgentToolResult:
        path = resolve_path(cwd, args["path"])
        content = args.get("content", "")
        async with _lock_for(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            existed = path.exists()
            path.write_text(content, "utf-8")

        verb = "Updated" if existed else "Created"
        return AgentToolResult(
            content=[TextContent(text=f"{verb} {path} ({len(content.splitlines())})")],
            details={"path": str(path), "created": not existed}
        )

    return AgentTool(
        name="write",
        label="Write",
        description="Write a file, creating parent directories. Overwrites existing content.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
        execute=execute
    )

def create_ls_tool(cwd: str | Path = ".") -> AgentTool:
    async def execute(args: dict[str, Any], ctx: ToolContext) -> AgentToolResult:
        path = resolve_path(cwd, args.get("path") or ".")
        if not path.exists():
            return AgentToolResult.text(f"Not found: {path}", is_error=True)
        recursive = bool(args.get("recursive"))
        entries: list[str] = []
        if recursive:
            for root, dirs, files in os.walk(path):
                dirs[:] = [d for d in dirs if not d.startswith(".") and d != "node_modules"]
                rel = Path(root).relative_to(path)
                for f in sorted(files):
                    entries.append(sanitize(str(rel / f) if str(rel) != "." else f))

        else:
            for child in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name)):
                entries.append(sanitize(child.name) + ("/" if child.is_dir() else ""))
        body, _ = truncate_tail("\n".join(entries) or "(empty)")
        return AgentToolResult(
            content=[TextContent(text=body)],
            details={"path": str(path), "count": len(entries)}
        )

    return AgentTool(
        name="ls",
        label="List",
        description="List directory contents.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "recursive": {"type": "boolean", "default": False},
            },
        },
        execute=execute
    )