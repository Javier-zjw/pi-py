"""hooks：在固定时机跑用户配置的 shell 命令（格式同 Claude Code 的 ``hooks``，别人写好的钩子直接能用）。

配置写在 pi 的 settings.json（全局 ``~/.pi/agent/settings.json``、项目 ``<cwd>/.pi/settings.json``，两边的
条目合并，不是覆盖）::

    "hooks": {
      "PostToolUse": [
        {"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "ruff format \\"$FILE\\""}]}
      ]
    }

Claude Code 自己的配置（``~/.claude/settings.json``、``.claude/settings.json``、``settings.local.json``）
**不自动生效**——那等于运行别人仓库里写的命令；``/hooks import`` 显式复制过来。

支持的事件：PreToolUse、PostToolUse、UserPromptSubmit、Stop、SessionStart（其余事件和 command 以外的
类型在 ``/hooks`` 里标出来、不执行）。和 Claude Code 一致的部分：

- 命令从 stdin 收 JSON（``session_id`` / ``transcript_path`` / ``cwd`` / ``hook_event_name`` /
  ``permission_mode`` + 事件字段）；工具名、参数换成 Claude Code 的样子（``bash`` → ``Bash``，
  ``path`` → ``file_path`` 绝对路径，``old_text`` → ``old_string``……），脚本按 Claude Code 写的照样读得到；
- 退出码 0 成功（stdout 是 JSON 就按字段处理，否则在 UserPromptSubmit / SessionStart 里当补充上下文）、
  2 拦下（stderr 是原因）、其它是不拦的出错（stderr 第一行给用户看）；
- matcher：空 / ``*`` 匹配全部；只含字母数字和 ``_ - , |`` 时按名字精确匹配（``Edit|Write``）；
  否则当正则、不锚定（``mcp__github__.*``）；
- 环境变量 ``CLAUDE_PROJECT_DIR`` / ``PI_PROJECT_DIR`` = 工作目录；同一个命令在几个文件里重复只跑一次；
  匹配的命令并行跑。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .text import sanitize

SUPPORTED_EVENTS = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop", "SessionStart")
TOOL_EVENTS = ("PreToolUse", "PostToolUse")
DEFAULT_TIMEOUT = {"UserPromptSubmit": 30.0}
FALLBACK_TIMEOUT = 600.0
MAX_TEXT = 10_000           # 钩子输出给模型的文字上限（同 Claude Code）

# pi 的工具名 → Claude Code 的（钩子脚本按 Claude Code 写：tool_name == "Bash"、matcher "Edit|Write"）
CLAUDE_NAMES = {"bash": "Bash", "read": "Read", "write": "Write", "edit": "Edit", "grep": "Grep",
                "find": "Glob", "ls": "LS", "web_fetch": "WebFetch", "web_search": "WebSearch",
                "task": "Task", "update_plan": "TodoWrite"}
PI_NAMES = {**{v: k for k, v in CLAUDE_NAMES.items()}, "MultiEdit": "edit"}
PERMISSION_MODES = {"ask": "default", "acceptEdits": "acceptEdits", "auto": "bypassPermissions"}
_EXACT = re.compile(r"^[A-Za-z0-9_\- ,|]+$")


@dataclass
class Handler:
    event: str
    matcher: str = ""
    type: str = "command"
    command: str = ""
    args: list[str] | None = None
    timeout: float | None = None
    condition: str = ""                 # "if": 权限规则写法，如 Bash(git *)
    background: bool = False            # "async": 后台跑，不等结果
    source: str = ""

    @property
    def supported(self) -> bool:
        return self.event in SUPPORTED_EVENTS and self.type == "command" and bool(self.command)


@dataclass
class Outcome:
    code: int | None                    # None：没跑成（超时、起不来）
    stdout: str = ""
    stderr: str = ""
    data: dict | None = None            # stdout 是一个 JSON 对象时
    error: str = ""

    @property
    def blocked(self) -> bool:
        return self.code == 2


@dataclass
class Summary:
    """几个钩子的结果合起来"""
    block: bool = False
    reasons: list[str] = field(default_factory=list)       # 拦下 / 要求继续的原因（给模型）
    context: list[str] = field(default_factory=list)       # 补充上下文（给模型）
    notices: list[tuple[str, str]] = field(default_factory=list)    # (level, 给用户看的话)
    stop: bool = False                                      # continue: false
    stop_reason: str = ""
    decision: str = ""                                      # PreToolUse：allow / ask / deny
    updated_input: dict | None = None
    updated_output: Any = None


# -- 配置 ------------------------------------------------------------------- #

def read_settings(path: Path) -> tuple[dict | None, str]:
    """(内容, 错误)：文件不存在 → ({}, "")"""
    if not path.is_file():
        return {}, ""
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"{path} 读不了：{exc}"
    return (data if isinstance(data, dict) else None), ("" if isinstance(data, dict) else f"{path} 不是 JSON 对象")


def parse_hooks(hooks: Any, source: str) -> tuple[list[Handler], list[str]]:
    handlers, problems = [], []
    if hooks in (None, {}):
        return handlers, problems
    if not isinstance(hooks, dict):
        return handlers, [f"{source}：hooks 应该是 {{事件: [...]}}"]
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            problems.append(f"{source}：{event} 应该是列表")
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            matcher = str(group.get("matcher") or "")
            for item in group.get("hooks") or []:
                if not isinstance(item, dict):
                    continue
                args = item.get("args")
                timeout = item.get("timeout")
                handlers.append(Handler(
                    event=str(event), matcher=matcher, type=str(item.get("type") or "command"),
                    command=str(item.get("command") or ""),
                    args=[str(a) for a in args] if isinstance(args, list) else None,
                    timeout=float(timeout) if isinstance(timeout, (int, float)) and timeout > 0 else None,
                    condition=str(item.get("if") or ""), background=bool(item.get("async")),
                    source=source))
    return handlers, problems


def load_handlers(sources: list[tuple[Path | None, str]], fallback: Any = None
                  ) -> tuple[list[Handler], list[str]]:
    """按顺序读几份 settings 的 hooks，合并、去重（同一个命令在几个文件里只跑一次）"""
    handlers: list[Handler] = []
    problems: list[str] = []
    disabled = False
    read_any = False
    for path, label in sources:
        if path is None:
            continue
        data, error = read_settings(path)
        if error:
            problems.append(error)
            continue
        read_any = True
        if data.get("disableAllHooks") is True:
            disabled = True
        found, issues = parse_hooks(data.get("hooks"), label)
        handlers += found
        problems += issues
    if not read_any and fallback:
        found, issues = parse_hooks(fallback, "settings")
        handlers += found
        problems += issues
    if disabled:
        return [], problems + ["disableAllHooks = true：所有钩子都不执行"]
    seen, unique = set(), []
    for handler in handlers:
        key = (handler.event, handler.matcher, handler.type, handler.command,
               tuple(handler.args or ()), handler.condition)
        if key not in seen:
            seen.add(key)
            unique.append(handler)
    return unique, problems


def matches(matcher: str, names: list[str]) -> bool:
    if matcher in ("", "*"):
        return True
    if _EXACT.match(matcher):
        wanted = {part.strip() for part in re.split(r"[|,]", matcher) if part.strip()}
        return any(name in wanted for name in names)
    try:
        pattern = re.compile(matcher)
    except re.error:
        return False
    return any(pattern.search(name) for name in names)


def condition_matches(condition: str, tool: str, args: dict, cwd: str) -> bool:
    """``if``：权限规则写法（``Bash(git *)``），工具名用 Claude Code 的或 pi 的都行"""
    if not condition:
        return True
    from .builtin_extensions.permissions import rule_matches

    match = re.fullmatch(r"\s*([\w.*-]+)(.*)", condition, re.DOTALL)
    if not match:
        return False
    name = PI_NAMES.get(match.group(1), match.group(1))
    return rule_matches(name + match.group(2), tool, args, cwd)


# -- 输入：换成 Claude Code 的样子 -------------------------------------------- #

def claude_name(tool: str) -> str:
    return CLAUDE_NAMES.get(tool, tool)


def tool_input(tool: str, args: dict, cwd: str) -> dict:
    """pi 的参数 + Claude Code 的同义字段（脚本读 .tool_input.file_path / old_string 照样有）"""
    data = dict(args or {})
    path = data.get("path")
    if tool in ("read", "write", "edit") and path:
        full = Path(str(path)).expanduser()
        data["file_path"] = str(full if full.is_absolute() else Path(cwd) / full)
    if tool == "edit":
        data.setdefault("old_string", data.get("old_text", ""))
        data.setdefault("new_string", data.get("new_text", ""))
    if tool == "task" and "agent" in data:
        data.setdefault("subagent_type", data["agent"])
    return data


def pi_arguments(tool: str, updated: dict, original: dict) -> dict:
    """钩子改过的参数（updatedInput，多半用 Claude Code 的字段名）→ pi 的参数，盖在原参数上"""
    renames = {"file_path": "path"} if tool in ("read", "write", "edit") else {}
    if tool == "edit":
        renames.update(old_string="old_text", new_string="new_text")
    if tool == "task":
        renames["subagent_type"] = "agent"
    out = dict(original or {})
    for key, value in (updated or {}).items():
        out[renames.get(key, key)] = value
    return out


def tool_response(tool: str, result: Any, args: dict, cwd: str) -> dict:
    text = "".join(getattr(c, "text", "") or "" for c in getattr(result, "content", []) or [])
    details = result.details if isinstance(getattr(result, "details", None), dict) else {}
    response: dict[str, Any] = {"content": text, "is_error": bool(getattr(result, "is_error", False))}
    if tool == "bash":
        response.update(stdout=text, stderr="", interrupted=False)
    if tool in ("read", "write", "edit"):
        response["filePath"] = tool_input(tool, args, cwd).get("file_path", "")
        response["success"] = not response["is_error"]
    if tool == "write":
        response["type"] = "create" if details.get("created") else "update"
    try:
        json.dumps(details)
        response["details"] = details
    except (TypeError, ValueError):
        pass
    return response


# -- 跑命令 ------------------------------------------------------------------- #

def _env(cwd: str) -> dict[str, str]:
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(cwd)
    env["PI_PROJECT_DIR"] = str(cwd)
    return env


async def _spawn(handler: Handler, cwd: str):
    pipes = {"stdin": asyncio.subprocess.PIPE, "stdout": asyncio.subprocess.PIPE,
             "stderr": asyncio.subprocess.PIPE, "cwd": cwd, "env": _env(cwd)}
    if handler.args is not None:            # exec 形式：不经过 shell
        return await asyncio.create_subprocess_exec(handler.command, *handler.args, **pipes)
    if os.name == "nt":
        from .tools.bash import windows_bash

        bash = windows_bash()
        if bash:                            # 钩子脚本多半按 sh 写的：有 Git Bash 就用它
            return await asyncio.create_subprocess_exec(bash, "-c", handler.command, **pipes)
    return await asyncio.create_subprocess_shell(handler.command, **pipes)


async def run(handler: Handler, payload: dict, cwd: str) -> Outcome:
    timeout = handler.timeout or DEFAULT_TIMEOUT.get(handler.event, FALLBACK_TIMEOUT)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        process = await _spawn(handler, cwd)
    except OSError as exc:
        return Outcome(None, error=f"起不来：{exc}")
    if handler.background:
        async def finish() -> None:
            try:
                await process.communicate(data)
            except Exception:  # noqa: BLE001, S110 —— 后台钩子的结果不看
                pass
        asyncio.get_running_loop().create_task(finish())
        return Outcome(0)
    try:
        out, err = await asyncio.wait_for(process.communicate(data), timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return Outcome(None, error=f"超时（{timeout:g} 秒）")
    stdout = sanitize(out.decode("utf-8", "replace")).strip()
    stderr = sanitize(err.decode("utf-8", "replace")).strip()
    parsed = None
    if stdout.startswith("{") and process.returncode == 0:
        try:
            value = json.loads(stdout)
            parsed = value if isinstance(value, dict) else None
        except ValueError:
            parsed = None
    return Outcome(process.returncode, stdout, stderr, parsed)


def _clip(text: str) -> str:
    text = str(text or "").strip()
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + f"\n… ({len(text) - MAX_TEXT} more chars)"


def summarize(event: str, outcomes: list[tuple[Handler, Outcome]]) -> Summary:
    """按 Claude Code 的规则把几个钩子的结果合起来"""
    summary = Summary()
    rank = {"deny": 3, "ask": 2, "allow": 1}
    for handler, outcome in outcomes:
        name = _short(handler.command)
        if outcome.code is None:
            summary.notices.append(("warn", f"{event} 钩子 {name} {outcome.error}，没有生效"))
            continue
        if outcome.blocked:
            reason = _clip(outcome.stderr) or "（没给原因）"
            if event in ("SessionStart",):
                summary.notices.append(("warn", f"{event} 钩子 {name}：{first_line(reason)}"))
            else:
                summary.block = True
                summary.reasons.append(reason)
                summary.decision = "deny" if event == "PreToolUse" else summary.decision
            continue
        if outcome.code != 0:
            detail = first_line(outcome.stderr) or f"退出码 {outcome.code}"
            summary.notices.append(("warn", f"{event} 钩子 {name} 出错：{detail}"))
            continue
        data = outcome.data
        if data is None:
            # 纯文本 stdout：UserPromptSubmit / SessionStart 里是补充上下文，别的事件不用
            if outcome.stdout and event in ("UserPromptSubmit", "SessionStart"):
                summary.context.append(_clip(outcome.stdout))
            continue
        if data.get("continue") is False:
            summary.stop = True
            summary.stop_reason = str(data.get("stopReason") or "")
        if data.get("systemMessage"):
            summary.notices.append(("info", _clip(data["systemMessage"])))
        specific = data.get("hookSpecificOutput") if isinstance(data.get("hookSpecificOutput"), dict) else {}
        if specific.get("additionalContext"):
            summary.context.append(_clip(specific["additionalContext"]))
        reason = str(specific.get("permissionDecisionReason") or data.get("reason") or "")
        if event == "PreToolUse":
            decision = str(specific.get("permissionDecision") or "")
            legacy = {"approve": "allow", "block": "deny"}.get(str(data.get("decision") or ""), "")
            decision = decision or legacy
            if decision in rank and rank[decision] > rank.get(summary.decision, 0):
                summary.decision = decision
            if decision == "deny":
                summary.block = True
                summary.reasons.append(_clip(reason) or "（没给原因）")
            if isinstance(specific.get("updatedInput"), dict):
                summary.updated_input = specific["updatedInput"]
        elif data.get("decision") == "block":
            summary.block = True
            summary.reasons.append(_clip(reason) or "（没给原因）")
        if event == "PostToolUse" and "updatedToolOutput" in specific:
            summary.updated_output = specific["updatedToolOutput"]
    return summary


def first_line(text: str, limit: int = 160) -> str:
    for line in str(text or "").splitlines():
        if line.strip():
            line = line.strip()
            return line if len(line) <= limit else line[:limit] + "…"
    return ""


def _short(command: str) -> str:
    text = " ".join(str(command).split())
    return text if len(text) <= 40 else text[:39] + "…"


# -- /hooks import：从 Claude Code 的配置复制过来 -------------------------------- #

def merge_into(target: dict, hooks: dict) -> int:
    """把 hooks 里的条目并进 target["hooks"]（已有的同一条不重复加）；返回加了几条"""
    current = target.setdefault("hooks", {})
    added = 0
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        existing = current.setdefault(event, [])
        have = {json.dumps(g, sort_keys=True, ensure_ascii=False) for g in existing}
        for group in groups:
            key = json.dumps(group, sort_keys=True, ensure_ascii=False)
            if isinstance(group, dict) and key not in have:
                existing.append(group)
                have.add(key)
                added += sum(1 for _ in group.get("hooks") or [])
    return added
