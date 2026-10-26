"""MCP 服务器配置：读 pi 自己的配置，以及从别的客户端导入。

格式和 Claude Desktop / Cursor / Claude Code 的 ``mcpServers`` 一样，别处的配置可以原样贴进来::

    {"mcpServers": {
        "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                   "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"}},
        "docs":   {"type": "http", "url": "https://example.com/mcp",
                   "headers": {"Authorization": "Bearer ${DOCS_TOKEN}"}},
        "old":    {"type": "sse", "url": "https://example.com/sse"},
        "off":    {"command": "uvx", "args": ["some-server"], "disabled": true}
    }}

pi 读两处（项目级覆盖全局同名项）：``~/.pi/agent/mcp.json``、``<cwd>/.pi/mcp.json``。
别的客户端的配置（含仓库里的 ``.mcp.json``）不会自动启用——那等于运行别人写的命令；
``/mcp import`` 由用户明确导入，导入后写进上面两处之一。

字符串里的 ``${VAR}`` / ``${VAR:-默认值}`` 用环境变量替换（同 Claude Code），密钥不用写死在文件里。
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_NAME = "mcp.json"
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


@dataclass
class ServerConfig:
    name: str
    transport: str                  # stdio / http / sse / auto（有 url 但没写 type：先试 http 再退回 sse）
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float | None = None    # 单次工具调用的超时（秒），不写用 settings 的 mcp.toolTimeout
    disabled: bool = False
    source: str = ""                # 来自哪个文件
    missing_vars: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if self.transport == "stdio":
            return " ".join([self.command, *self.args])
        return self.url


def expand(value: Any, env: dict[str, str], missing: list[str]) -> Any:
    """递归替换 ${VAR} / ${VAR:-默认}；没设又没默认值的变量记进 missing、替换成空串"""
    if isinstance(value, str):
        def replace(match: re.Match) -> str:
            name, default = match.group(1), match.group(2)
            if name in env:
                return env[name]
            if default is not None:
                return default
            missing.append(name)
            return ""
        return _VAR.sub(replace, value)
    if isinstance(value, list):
        return [expand(v, env, missing) for v in value]
    if isinstance(value, dict):
        return {k: expand(v, env, missing) for k, v in value.items()}
    return value


def parse_server(name: str, raw: dict, source: str = "", env: dict[str, str] | None = None) -> ServerConfig:
    """一项配置 → ServerConfig。认得 Claude Desktop / Cursor / Claude Code / VS Code 的写法"""
    env = dict(os.environ) if env is None else env
    missing: list[str] = []
    data = expand(raw, env, missing)
    kind = str(data.get("type") or data.get("transport") or "").lower()
    kind = {"streamable-http": "http", "streamablehttp": "http", "streamable_http": "http",
            "local": "stdio"}.get(kind, kind)
    url = str(data.get("url") or data.get("serverUrl") or "")
    if not kind:
        kind = "auto" if url else "stdio"
    command = data.get("command") or ""
    args = data.get("args") or []
    if isinstance(command, list):            # 有的客户端把命令和参数写在一个数组里
        command, args = (command[0] if command else ""), [*command[1:], *args]
    timeout = data.get("timeout")
    return ServerConfig(
        name=name, transport=kind, command=str(command), args=[str(a) for a in args],
        env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
        cwd=str(data.get("cwd") or ""), url=url,
        headers={str(k): str(v) for k, v in (data.get("headers") or {}).items()},
        timeout=float(timeout) if isinstance(timeout, (int, float)) and timeout > 0 else None,
        disabled=bool(data.get("disabled")) or data.get("enabled") is False,
        source=source, missing_vars=sorted(set(missing)),
    )


def servers_in(data: Any) -> dict[str, dict]:
    """从一份配置里取服务器表：mcpServers（大多数客户端）或 servers（VS Code）"""
    if not isinstance(data, dict):
        return {}
    table = data.get("mcpServers")
    if not isinstance(table, dict):
        table = data.get("servers")
    if not isinstance(table, dict):
        return {}
    return {str(k): v for k, v in table.items() if isinstance(v, dict)}


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None


def config_paths(cwd: str | Path, agent_dir: str | Path) -> list[Path]:
    """pi 自己的配置，后面的覆盖前面的同名项"""
    return [Path(agent_dir).expanduser() / CONFIG_NAME, Path(cwd) / ".pi" / CONFIG_NAME]


def load_servers(cwd: str | Path, agent_dir: str | Path,
                 env: dict[str, str] | None = None) -> tuple[list[ServerConfig], list[str]]:
    """返回 (服务器列表, 问题)。文件坏了不让 pi 起不来，记成问题"""
    merged: dict[str, ServerConfig] = {}
    problems: list[str] = []
    for path in config_paths(cwd, agent_dir):
        if not path.exists():
            continue
        data = read_json(path)
        if data is None:
            problems.append(f"{path} 不是合法的 JSON，已跳过")
            continue
        for name, raw in servers_in(data).items():
            merged[name] = parse_server(name, raw, str(path), env)
    return list(merged.values()), problems


# -- 从别的客户端导入 ---------------------------------------------------- #

def _app_support() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def import_sources(cwd: str | Path) -> list[tuple[str, Path, str]]:
    """(客户端名, 文件, 作用范围 global / project)。按常见程度排"""
    cwd = Path(cwd)
    home = Path.home()
    return [
        ("Claude Code（项目 .mcp.json）", cwd / ".mcp.json", "project"),
        ("Cursor（项目）", cwd / ".cursor" / "mcp.json", "project"),
        ("VS Code（项目）", cwd / ".vscode" / "mcp.json", "project"),
        ("Claude Desktop", _app_support() / "Claude" / "claude_desktop_config.json", "global"),
        ("Claude Code", home / ".claude.json", "global"),
        ("Cursor", home / ".cursor" / "mcp.json", "global"),
        ("Windsurf", home / ".codeium" / "windsurf" / "mcp_config.json", "global"),
        ("Codex", home / ".codex" / "config.toml", "global"),
    ]


def read_source(path: Path, cwd: str | Path) -> dict[str, dict]:
    """读一个客户端的配置，返回原样的服务器表（不展开变量：导入后照样从环境变量取）"""
    if not path.exists():
        return {}
    if path.suffix == ".toml":
        try:
            import tomllib  # Python 3.11+
        except ImportError:
            return {}
        try:
            data = tomllib.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        table = data.get("mcp_servers") or {}
        return {str(k): v for k, v in table.items() if isinstance(v, dict)}
    data = read_json(path)
    servers = servers_in(data)
    if path.name == ".claude.json" and isinstance(data, dict):
        # Claude Code 的 ~/.claude.json：顶层是用户级，projects[目录] 里是这个项目私有的
        project = (data.get("projects") or {}).get(str(Path(cwd).resolve())) or {}
        servers.update(servers_in(project))
    # VS Code 的 ${input:xxx} 要在它的界面里输入，这里拿不到：带它的项跳过
    return {k: v for k, v in servers.items() if "${input:" not in json.dumps(v)}


def import_servers(cwd: str | Path, agent_dir: str | Path) -> tuple[list[str], list[str]]:
    """把别的客户端里的服务器写进 pi 的配置（同名的不覆盖）。返回 (导入了的说明, 跳过的说明)"""
    targets = {"global": Path(agent_dir).expanduser() / CONFIG_NAME,
               "project": Path(cwd) / ".pi" / CONFIG_NAME}
    existing = {scope: servers_in(read_json(p)) if p.exists() else {} for scope, p in targets.items()}
    known = set(existing["global"]) | set(existing["project"])
    added: dict[str, dict[str, dict]] = {"global": {}, "project": {}}
    done, skipped = [], []
    for client, path, scope in import_sources(cwd):
        for name, raw in read_source(path, cwd).items():
            if name in known:
                skipped.append(f"{name}（{client}：pi 里已有同名的）")
                continue
            known.add(name)
            added[scope][name] = {k: v for k, v in raw.items() if k not in ("autoApprove", "alwaysAllow")}
            done.append(f"{name}（来自 {client}）")
    for scope, servers in added.items():
        if not servers:
            continue
        path = targets[scope]
        data = read_json(path) if path.exists() else None
        data = data if isinstance(data, dict) else {}
        table = data.setdefault("mcpServers", {})
        table.update(servers)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return done, skipped


def importable(cwd: str | Path, agent_dir: str | Path) -> list[str]:
    """别的客户端里有、pi 里还没有的服务器名（给 /mcp 提示用）"""
    known = {s.name for s in load_servers(cwd, agent_dir)[0]}
    names: list[str] = []
    for _, path, _ in import_sources(cwd):
        names += [n for n in read_source(path, cwd) if n not in known and n not in names]
    return names


# -- /mcp add、/mcp remove ----------------------------------------------- #

def target_path(cwd: str | Path, agent_dir: str | Path, scope: str) -> Path:
    return Path(cwd) / ".pi" / CONFIG_NAME if scope == "project" else Path(agent_dir).expanduser() / CONFIG_NAME


def _load_for_write(path: Path) -> dict:
    """要写的配置文件：不存在当空的；坏了不覆盖（里面可能有用户手写的东西）"""
    if not path.exists():
        return {}
    data = read_json(path)
    if not isinstance(data, dict):
        raise ValueError(f"{path} 不是合法的 JSON，先手动修好再添加（不覆盖它，免得丢掉你写的内容）")  # noqa: TRY004 —— 调用方统一按 ValueError 报给用户
    return data


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")


def write_server(path: Path, name: str, raw: dict) -> None:
    data = _load_for_write(path)
    table = data.get("mcpServers")
    if not isinstance(table, dict):
        table = data["mcpServers"] = {}
    table[name] = raw
    _save(path, data)


def remove_server(cwd: str | Path, agent_dir: str | Path, name: str) -> list[Path]:
    """从 pi 的两个配置里删掉这一项，返回改了哪些文件"""
    changed = []
    for path in config_paths(cwd, agent_dir):
        if not path.exists():
            continue
        data = _load_for_write(path)
        table = data.get("mcpServers")
        if isinstance(table, dict) and name in table:
            del table[name]
            _save(path, data)
            changed.append(path)
    return changed
