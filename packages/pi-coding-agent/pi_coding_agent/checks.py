"""改完就检查：edit / write 成功后立刻检查那个文件，有问题附在工具结果后面（参照 Harness² / OpenCode）。

写入成功不代表文件能用。语法写坏、JSON 少个括号，模型往往要到很多步以后（跑测试、用户报错）
才发现，那时已经在坏文件上又做了好几步。工具结果里直接告诉它，下一步就能改——而且是在工具循环里
做的，模型跳不过去（写在系统提示里的"改完记得检查"可能被忽略）。

- 内置检查（纯 Python，毫秒级，不起进程）：``.py`` / ``.pyi`` 语法（``compile``，不执行），
  ``.json`` / ``.ipynb`` 解析，``.toml`` 解析（Python 3.11+ 才有 tomllib）。
  允许注释的 JSON（tsconfig、``.vscode/`` 下的、``*.jsonc``）跳过。
- 配置的命令（settings ``checks.commands``）：按文件名通配配 lint / 类型检查，``{file}`` 换成文件路径，
  退出码非 0 就把输出附上；默认没有。

只在发现问题时附文字（没问题不占上下文）。结果不标成错误：文件确实写进去了，标成错误模型会以为
没改成、重写一遍。
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import shlex
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pi_ai import TextContent

from .text import sanitize

MAX_BYTES = 2 * 1024 * 1024         # 更大的文件不做内置检查（生成的数据文件之类）
JSONC = ("tsconfig*.json", "jsconfig*.json", "*.jsonc", ".vscode/*", ".devcontainer/*",
         "*/.vscode/*", "*/.devcontainer/*")


@dataclass
class Problem:
    source: str         # python / json / toml / 配置的命令
    message: str        # 给模型看的（英文）


def _snippet(text: str, line: int | None) -> str:
    if not line:
        return ""
    lines = text.splitlines()
    if not 1 <= line <= len(lines):
        return ""
    return f"\n    {line} | {lines[line - 1].rstrip()[:200]}"


def builtin_check(path: Path, rel: str) -> Problem | None:
    """按扩展名做内置检查；认不出的类型、读不了的、太大的都不检查"""
    suffix = path.suffix.lower()
    if suffix not in (".py", ".pyi", ".json", ".ipynb", ".toml"):
        return None
    try:
        if path.stat().st_size > MAX_BYTES:
            return None
        text = path.read_text("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if suffix in (".py", ".pyi"):
        try:
            compile(text, rel, "exec", dont_inherit=True)
        except SyntaxError as exc:
            version = f"{sys.version_info.major}.{sys.version_info.minor}"
            return Problem("python", f"syntax error at line {exc.lineno}, column {exc.offset}: "
                                     f"{exc.msg} (checked with Python {version})"
                                     + _snippet(text, exc.lineno))
        except ValueError as exc:       # 源码里有 NUL 字节
            return Problem("python", f"cannot be compiled: {exc}")
        return None
    if suffix in (".json", ".ipynb"):
        if any(fnmatch.fnmatch(rel, pattern) for pattern in JSONC):
            return None
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            return Problem("json", f"invalid JSON at line {exc.lineno}, column {exc.colno}: "
                                   f"{exc.msg}" + _snippet(text, exc.lineno))
        return None
    try:
        import tomllib
    except ImportError:                 # Python 3.10
        return None
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return Problem("toml", f"invalid TOML: {exc}")
    return None


def matching_commands(commands: Any, rel: str) -> list[str]:
    """settings checks.commands：{"*.py": "ruff check -q {file}", "*.ts": ["eslint {file}", ...]}。
    通配对相对路径和文件名都试（"*.py" 能匹配 src/a.py）"""
    if not isinstance(commands, dict):
        return []
    name = rel.rsplit("/", 1)[-1]
    found: list[str] = []
    for pattern, value in commands.items():
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern):
            found += [value] if isinstance(value, str) else [v for v in value or [] if isinstance(v, str)]
    return found


def _quote(path: str) -> str:
    return f'"{path}"' if os.name == "nt" else shlex.quote(path)


def _clip(output: str, max_lines: int) -> str:
    lines = output.strip().splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join(lines[:max_lines] + [f"... ({len(lines) - max_lines} more lines)"])


async def run_command(template: str, rel: str, cwd: str, timeout: float,
                      max_lines: int = 30) -> Problem | None:
    """跑一条检查命令：退出码 0 不算问题；非 0、超时、起不来都把原因附上"""
    command = template.replace("{file}", _quote(rel))
    try:
        process = await asyncio.create_subprocess_shell(
            command, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL)
    except OSError as exc:
        return Problem(command, f"check command could not start: {exc}")
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return Problem(command, f"check command timed out after {timeout:g}s")
    if process.returncode == 0:
        return None
    text = sanitize(output.decode("utf-8", "replace"))
    return Problem(command, _clip(text, max_lines) or f"exited with code {process.returncode}")


def render(rel: str, problems: list[Problem]) -> str:
    lines = [(f"\n\n[post-edit check] The change to {rel} WAS written, but checking the file "
              "found problems. Fix them before moving on:")]
    for problem in problems:
        body = problem.message.replace("\n", "\n  ")
        lines.append(f"- {problem.source}: {body}")
    return "\n".join(lines)


async def check_after_edit(call: Any, result: Any, session: Any) -> Any:
    """after_tool_call 钩子：edit / write 成功后检查文件，有问题返回附了说明的新结果"""
    if getattr(call, "name", "") not in ("edit", "write") or result.is_error:
        return None
    get = session.settings.get
    if not get("checks.enabled", True):
        return None
    details = result.details if isinstance(result.details, dict) else {}
    raw = details.get("path") or (call.arguments or {}).get("path")
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(session.cwd) / path
    try:
        rel = os.path.relpath(path, session.cwd).replace(os.sep, "/")
    except ValueError:                  # Windows 上不同盘符
        rel = str(path)
    if rel.startswith(".."):
        rel = str(path)

    problems: list[Problem] = []
    problem = builtin_check(path, rel) if get("checks.builtin", True) else None
    if problem:
        problems.append(problem)
    timeout = float(get("checks.timeout", 30) or 30)
    max_lines = int(get("checks.maxLines", 30) or 30)
    commands = matching_commands(get("checks.commands", {}), rel)
    if commands:
        found = await asyncio.gather(*(run_command(c, rel, session.cwd, timeout, max_lines)
                                       for c in commands))
        problems += [p for p in found if p]
    if not problems:
        return None
    details = {**details, "check": {"ok": False, "file": rel,
                                    "problems": [{"source": p.source, "message": p.message}
                                                 for p in problems]}}
    return replace(result, content=[*result.content, TextContent(text=render(rel, problems))],
                   details=details)
