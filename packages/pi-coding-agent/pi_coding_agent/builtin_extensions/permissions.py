"""
权限确认：改文件、跑命令之前先问用户（同 Claude Code 的权限模式、Codex 的 approval）。

没有它的时候模型想跑什么命令就跑什么——`rm -rf`、`git push --force` 都不会有人拦。
这里挂在工具执行前的钩子上（ExtensionAPI.before_tool_call），每次调用按下面的顺序判断：

1. 只读工具（read / grep / find / ls、计划和提问工具）直接放行；
2. settings 里的 deny 规则命中 → 拦下；allow 规则命中 → 放行；
3. 模式：``auto`` 全部放行；``acceptEdits`` 工作目录里的文件修改放行；``ask``（默认）都要问；
4. bash 的只读命令（ls、cat、git status……，不带重定向、命令替换）放行；
5. 本会话里用户选过"不再询问"的同类调用放行；
6. 其余交给界面问用户（session.permission_handler）：允许 / 本会话不再询问 / 拒绝 / 拒绝并说明，
   Esc 拒绝并停止这一轮。没有界面（print 模式、网页）时按 ``permissions.nonInteractive``。

设置（settings.json）：

    "permissions": {
        "mode": "ask",                       # ask / acceptEdits / auto
        "allow": ["bash(git status*)", "bash(pytest*)", "edit(docs/*)"],
        "deny": ["bash(rm -rf*)", "bash(git push*)"],
        "nonInteractive": "allow"            # 没有界面可问时：allow / deny
    }

规则写法：``工具名`` 或 ``工具名(模式)``。bash 的模式匹配整条命令，edit / write 的模式匹配
相对工作目录的路径，都是 shell 通配（fnmatch）。
"""

from __future__ import annotations

import json
import os
import re
import shlex
from fnmatch import fnmatch

from pi_agent import BeforeToolCallResult

NAME = "permissions"
DESCRIPTION = "权限确认：改文件、跑命令前先问用户；支持模式切换和 allow / deny 规则"

MODES = {
    "ask": "每次确认",
    "acceptEdits": "自动接受文件修改",
    "auto": "全部自动",
}
SAFE_TOOLS = {"read", "grep", "find", "ls", "update_plan", "ask_user", "submit_plan",
              "web_search", "web_fetch",     # web_fetch 自己拒绝本机 / 内网地址
              "task",       # 派子智能体本身不用确认：它的每次工具调用另外经过检查
              "memory"}     # 只写 ~/.pi/agent/memory/ 下的记忆文件
EDIT_TOOLS = {"edit", "write"}

# 只读命令：单独出现（或在管道、&& 里每一段都是）时不用问
READ_ONLY_COMMANDS = {
    "ls", "pwd", "cat", "head", "tail", "wc", "echo", "which", "whoami", "date", "grep", "rg",
    "tree", "file", "stat", "du", "df", "uname", "diff", "sort", "uniq", "cut", "basename",
    "dirname", "realpath", "cd", "true", "nl", "less", "more", "type",
}
READ_ONLY_GIT = {"status", "diff", "log", "show", "branch", "remote", "rev-parse", "ls-files",
                 "blame", "describe", "shortlog", "tag"}
FIND_WRITES = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"}
# 重定向、命令替换、后台执行：看不清会干什么，一律问
UNSAFE_SHELL = re.compile(r">|<\(|\$\(|`|(?<![&|])&(?![&>])")
SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\||\n")

# 第二个词是子命令的工具："不再询问"记到子命令这一级（git commit、npm test），不放开整个 git
SUBCOMMAND_TOOLS = {"git", "npm", "pnpm", "yarn", "bun", "npx", "cargo", "go", "docker", "kubectl",
                    "pip", "pip3", "uv", "poetry", "make", "gh", "brew", "dotnet", "mvn", "gradle"}

DETAIL_LINES = 12

def session_state(session) -> dict:
    """每个会话自己的：模式覆盖（/permissions 切的）和"不再询问"的记录。
    挂在会话对象上而不是模块全局：扩展加载器按文件路径导入本模块，界面再 import 包路径时
    是另一份模块对象，全局变量不共享"""
    state = getattr(session, "_permission_state", None)
    if state is None:
        state = {"mode": None, "always": set()}
        session._permission_state = state
    return state


def current_mode(session) -> str:
    override = session_state(session)["mode"]
    mode = override or session.settings.get("permissions.mode", "ask")
    return mode if mode in MODES else "ask"


def set_mode(session, mode: str) -> str:
    if mode not in MODES:
        raise ValueError(f"未知的权限模式 {mode}，可选：{', '.join(MODES)}")
    session_state(session)["mode"] = mode
    return mode


def activate(pi) -> None:

    async def check(call, session):
        decision, detail = decide(session, call.name, call.arguments or {})
        # hooks.json 的 PreToolUse 钩子给的意见（hooks 扩展先于这里跑）：allow 免确认、ask 强制确认；
        # deny 规则照样优先（同 Claude Code："deny and ask rules are still evaluated"）
        hint = hook_hints(session).pop(getattr(call, "id", ""), None)
        if hint == "allow" and decision == "ask":
            return None
        if hint == "ask" and decision == "allow":
            decision = "ask"
        if decision == "allow":
            return None
        if decision == "deny":
            return BeforeToolCallResult(block=True, reason=(
                f"Blocked by the user's permission settings ({detail}). Do not retry this action; "
                "find another way or ask the user."))
        if session.permission_handler is None:
            if session.settings.get("permissions.nonInteractive", "allow") == "deny":
                return BeforeToolCallResult(block=True, reason=(
                    "This action needs the user's approval, but there is no interactive UI to ask. "
                    "It was not run."))
            return None
        request = build_request(session, call.name, call.arguments or {})
        try:
            answer = await session.permission_handler(request) or {}
        except Exception as exc:    # noqa: BLE001 —— 问不成就当没同意：宁可不执行，也不能出错就放行
            return BeforeToolCallResult(block=True, reason=(
                f"Could not ask the user for permission ({type(exc).__name__}); the action was not run."))
        choice = answer.get("decision", "deny")
        if choice in ("allow", "always"):
            if choice == "always" and request.get("remember"):
                session_state(session)["always"].update(request["remember"]["keys"])
            return None
        feedback = (answer.get("feedback") or "").strip()
        reason = f"The user denied permission for this {call.name} call."
        if feedback:
            reason += f" The user said: {feedback}"
        else:
            reason += " Do not retry it in another way; continue with what you can do, or ask the user."
        if choice == "abort":
            session.abort()
            reason += " The user stopped this turn."
        return BeforeToolCallResult(block=True, reason=reason)

    pi.before_tool_call(check)

    async def cmd_permissions(rest: str) -> str:
        session = pi.session
        if session is None:
            return "还没有活跃会话"
        rest = rest.strip()
        if rest:
            try:
                set_mode(session, rest)
            except ValueError as exc:
                return str(exc)
        return describe(session)

    pi.register_command("permissions", "查看或切换权限模式（ask / acceptEdits / auto）", cmd_permissions,
                        usage="/permissions [ask|acceptEdits|auto]")


def hook_hints(session) -> dict[str, str]:
    """PreToolUse 钩子对某次调用的意见：{tool_call_id: allow | ask}，挂在会话对象上（同 _permission_state）"""
    hints = getattr(session, "_hook_permission", None)
    if hints is None:
        hints = {}
        session._hook_permission = hints
    return hints


def describe(session) -> str:
    mode = current_mode(session)
    lines = [f"权限模式：{MODES[mode]}（{mode}）"]
    for key in ("allow", "deny"):
        rules = session.settings.get(f"permissions.{key}") or []
        if rules:
            lines.append(f"{key}：" + "、".join(rules))
    always = sorted(session_state(session)["always"])
    if always:
        lines.append("本会话不再询问：" + "、".join(always))
    return "\n".join(lines)


# -- 判断 -------------------------------------------------------------- #

def decide(session, name: str, args: dict) -> tuple[str, str]:
    """返回 (allow / deny / ask, 说明)"""
    if name in SAFE_TOOLS:
        return "allow", "只读工具"
    settings = session.settings
    for rule in settings.get("permissions.deny") or []:
        if rule_matches(rule, name, args, session.cwd):
            return "deny", f"deny 规则 {rule}"
    for rule in settings.get("permissions.allow") or []:
        if rule_matches(rule, name, args, session.cwd):
            return "allow", f"allow 规则 {rule}"
    mode = current_mode(session)
    if mode == "auto":
        return "allow", "全部自动"
    if name == "bash":
        command = str(args.get("command") or "")
        if is_read_only(command):
            return "allow", "只读命令"
        prefixes = command_prefixes(command)
        if prefixes and all(f"bash:{p}" in session_state(session)["always"] for p in prefixes):
            return "allow", "本会话已允许"
        return "ask", ""
    if name in EDIT_TOOLS:
        inside = inside_cwd(session.cwd, str(args.get("path") or ""))
        if inside and (mode == "acceptEdits" or "edit:*" in session_state(session)["always"]):
            return "allow", "自动接受文件修改"
        return "ask", ""
    if f"tool:{name}" in session_state(session)["always"]:
        return "allow", "本会话已允许"
    return "ask", ""


def rule_matches(rule: str, name: str, args: dict, cwd: str) -> bool:
    m = re.fullmatch(r"\s*([\w.*-]+)\s*(?:\((.*)\))?\s*", rule)
    if not m:
        return False
    target = m.group(1)
    # 工具名可以带通配（mcp__github__*）；mcp__服务器 不带工具名时指这个服务器的全部工具（同 Claude Code）
    if not (target == name or ("*" in target and fnmatch(name, target))
            or (target.startswith("mcp__") and target.count("__") == 1
                and name.startswith(target + "__"))):
        return False
    pattern = m.group(2)
    if pattern is None:
        return True
    if name == "bash":
        return fnmatch(str(args.get("command") or "").strip(), pattern.strip())
    if name in EDIT_TOOLS:
        return fnmatch(relative_path(cwd, str(args.get("path") or "")), pattern.strip())
    return False


def segments(command: str) -> list[list[str]] | None:
    """按 && || ; | 拆成几段，每段分词。拆不开（引号不配对）返回 None"""
    parts = []
    for raw in SEGMENT_SPLIT.split(command):
        if not raw.strip():
            continue
        try:
            words = shlex.split(raw)
        except ValueError:
            return None
        if words:
            parts.append(words)
    return parts


def is_read_only(command: str) -> bool:
    if not command.strip() or UNSAFE_SHELL.search(command):
        return False
    parts = segments(command)
    if not parts:
        return False
    for words in parts:
        head = os.path.basename(words[0])
        if "=" in words[0]:
            return False        # FOO=bar cmd：改环境变量再跑，不算只读
        if head == "git":
            if len(words) < 2 or words[1] not in READ_ONLY_GIT:
                return False
            if words[1] in ("branch", "tag", "remote") and len(words) > 2 and not words[2].startswith("-"):
                return False    # git branch 新名字 / git remote add：会改东西
            continue
        if head == "find":
            if FIND_WRITES & set(words):
                return False
            continue
        if head not in READ_ONLY_COMMANDS:
            return False
    return True


def command_prefixes(command: str) -> list[str]:
    """"不再询问"按什么记：每段取命令名，git / npm 这类再带上子命令，如 git commit、npm test、pytest。
    拆不开、带重定向或命令替换的命令不提供"不再询问"（返回空）"""
    if UNSAFE_SHELL.search(command):
        return []
    parts = segments(command)
    if not parts:
        return []
    prefixes = []
    for words in parts:
        head = os.path.basename(words[0])
        if "=" in words[0]:
            return []
        if head in SUBCOMMAND_TOOLS and len(words) > 1 and re.fullmatch(r"[a-z][a-z0-9:-]*", words[1]):
            head += f" {words[1]}"
        if head not in prefixes:
            prefixes.append(head)
    return prefixes


def relative_path(cwd: str, path: str) -> str:
    full = os.path.normpath(os.path.join(cwd, os.path.expanduser(path)))
    try:
        return os.path.relpath(full, cwd)
    except ValueError:      # Windows 不同盘符
        return full


def inside_cwd(cwd: str, path: str) -> bool:
    rel = relative_path(cwd, path)
    return bool(path) and not rel.startswith("..") and not os.path.isabs(rel)


# -- 给界面的请求 ------------------------------------------------------ #

def build_request(session, name: str, args: dict) -> dict:
    """界面显示用：标题、一句话摘要、几行细节（命令 / diff 预览），以及"不再询问"的范围"""
    cwd = session.cwd
    if name == "bash":
        command = str(args.get("command") or "")
        prefixes = command_prefixes(command)
        remember = None
        if prefixes:
            label = "、".join(f"{p} …" for p in prefixes)
            remember = {"label": f"本会话不再询问 {label}", "keys": [f"bash:{p}" for p in prefixes]}
        return {"tool": name, "kind": "bash", "title": "执行命令", "question": "允许执行这条命令吗？",
                "summary": command, "detail": _clip(command.splitlines()), "remember": remember}
    if name in EDIT_TOOLS:
        path = relative_path(cwd, str(args.get("path") or ""))
        remember = ({"label": "本会话自动接受工作目录里的文件修改", "keys": ["edit:*"]}
                    if inside_cwd(cwd, str(args.get("path") or "")) else None)
        if name == "edit":
            detail = [f"- {line}" for line in str(args.get("old_text") or "").splitlines()]
            detail += [f"+ {line}" for line in str(args.get("new_text") or "").splitlines()]
            question = f"允许修改 {path} 吗？"
        else:
            content = str(args.get("content") or "")
            exists = os.path.exists(os.path.join(cwd, path))
            lines = content.splitlines()
            detail = [f"{'覆盖' if exists else '新建'}，{len(lines)} 行"] + [f"  {line}" for line in lines]
            question = f"允许{'覆盖' if exists else '创建'} {path} 吗？"
        return {"tool": name, "kind": name, "title": "修改文件", "question": question, "summary": path,
                "detail": _clip(detail), "remember": remember}
    text = json.dumps(args, ensure_ascii=False)
    if name.startswith("mcp__") and name.count("__") >= 2:
        _, server, tool = name.split("__", 2)
        pretty = json.dumps(args, ensure_ascii=False, indent=2).splitlines() if args else ["（没有参数）"]
        return {"tool": name, "kind": "mcp", "title": f"调用 MCP 工具 · {server}",
                "question": f"允许调用 {server} 的 {tool} 吗？", "summary": f"{server} · {tool}",
                "detail": _clip(pretty),
                "remember": {"label": f"本会话不再询问 {server} 的 {tool}", "keys": [f"tool:{name}"]}}
    return {"tool": name, "kind": "tool", "title": f"调用工具 {name}", "question": f"允许调用 {name} 吗？",
            "summary": text, "detail": _clip([text[:400]]),
            "remember": {"label": f"本会话不再询问 {name}", "keys": [f"tool:{name}"]}}


def _clip(lines: list[str]) -> list[str]:
    if len(lines) <= DETAIL_LINES:
        return lines
    return lines[:DETAIL_LINES - 1] + [f"… 还有 {len(lines) - DETAIL_LINES + 1} 行"]
