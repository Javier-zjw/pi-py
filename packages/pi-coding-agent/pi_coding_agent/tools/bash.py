"""
Bash工具。
输出随产生实时流式推送；任务中止时杀死整个进程树；上下文仅保留尾部内容，完整输出落地至临时文件。
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import sys
import tempfile
from pathlib import Path
from typing import Any

from pi_agent import AgentTool, AgentToolResult, ToolContext
from pi_ai import TextContent

from ..text import stream_decoder
from .truncate import DEFAULT_MAX_BYTES, DEFAULT_MAX_LINES, truncate_head

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07")
DEFAULT_TIMEOUT = 120.0

def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)

def kill_process_tree(process: asyncio.subprocess.Process) -> None:
    """终止Shell进程及其创建的所有子进程"""
    if process.returncode is not None:
        return

    try:
        if sys.platform == "win32":
            import subprocess

            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True)
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


async def run_bash(
        command: str,
        cwd: str | Path,
        ctx: ToolContext | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if sys.platform != "win32":
        kwargs["preexec_fn"] = os.setsid

    process = await asyncio.create_subprocess_shell(
        command,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        **kwargs
    )

    chunks: list[str] = []
    total = 0

    async def pump() -> None:
        nonlocal total
        assert process.stdout is not None
        # 固定字节数读取会把多字节字符切在块边界上，必须用跨块保留状态的
        # 增量解码器；逐块 bytes.decode() 会让中文每 4KB 拦一个字
        decoder = stream_decoder()
        while True:
            data = await process.stdout.read(4096)
            if not data:
                tail = decoder.decode(b"", final=True)
                if tail:
                    chunks.append(strip_ansi(tail))
                break

            text = strip_ansi(decoder.decode(data))
            if not text:
                continue

            chunks.append(text)
            total += len(text)
            if ctx is not None:
                ctx.update(AgentToolResult(content=[TextContent(text="".join(chunks)[-4000:])]))

    async def watch_cancel() -> None:
        if ctx is None:
            return
        while process.returncode is None:
            if ctx.cancelled:
                kill_process_tree(process)
                return
            await asyncio.sleep(0.05)

    cancelled = False
    time_out = False
    watcher = asyncio.create_task(watch_cancel())

    try:
        await asyncio.wait_for(pump(), timeout=timeout)
        await process.wait()
    except asyncio.TimeoutError:
        time_out = True
        kill_process_tree(process)
    except asyncio.CancelledError:
        cancelled = True
        kill_process_tree(process)
        raise
    finally:
        watcher.cancel()
        if ctx is not None and ctx.cancelled:
            cancelled = True

    output = "".join(chunks)
    full_path: str | None = None
    if len(output.encode("utf-8")) > max_bytes:
        fd, full_path = tempfile.mkstemp(prefix="pi-bash-", suffix=".log")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(output)

    body, truncated = truncate_head(output, DEFAULT_MAX_LINES, max_bytes)
    if truncated and full_path:
        body += f"\n[full output: {full_path}]"


    return {
        "output": body,
        "exit_code": process.returncode,
        "cancelled": cancelled,
        "time_out": time_out,
        "truncated": truncated,
        "full_output_path": full_path
    }

def create_bash_tool(cwd: str | Path = ".", timeout: float = DEFAULT_TIMEOUT) -> AgentTool:
    async def execute(args: dict[str, Any], ctx: ToolContext) -> AgentToolResult:
        command = args["command"]
        result = await run_bash(command, cwd, ctx, timeout=float(args.get("timeout") or timeout))
        text = result["output"] or "(no output)"
        if result["time_out"]:
            text += f"\n[timed out after {timeout}s and was killed]"
        if result["cancelled"]:
            text += "\n[aborted by user]"
        if result["exit_code"] not in (0, None):
            text += f"\n[exit code {result['exit_code']}]"
        return AgentToolResult(
            content=[TextContent(text=text)],
            is_error=bool(result["exit_code"]) or result["time_out"],
            details={"command": command, **result}
        )

    return AgentTool(
        name="bash",
        label="Bash",
        description=(
            "Run a shell command in the working directory. Output is truncated to the last "
            "part; long output is written to a temp file whose path is reported."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "number", "description": "Seconds before the command is killed"},
            },
            "required": ["command"],
        },
        execute=execute
    )