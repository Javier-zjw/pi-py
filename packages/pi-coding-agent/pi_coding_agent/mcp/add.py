"""``/mcp add`` 的解析：把从 MCP 网站 / 别的客户端复制来的东西变成 ``{名字: 配置}``。

认得的写法（都可以带 ``--project`` / ``--force``）::

    {"mcpServers": {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}}   网站给的整段
    "fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}                      只复制了其中一项
    {"command": "uvx", "args": ["mcp-server-fetch"]}                               只有配置，名字自动取
    {"servers": {...}}  /  {"name": "x", "command": ...}                           VS Code 的写法
    claude mcp add github -e GITHUB_TOKEN=xxx -- npx -y @modelcontextprotocol/server-github
    claude mcp add --transport http docs https://example.com/mcp -H "Authorization: Bearer x"
    claude mcp add-json fetch '{"command": "uvx", "args": ["mcp-server-fetch"]}'
    codex mcp add fetch -- uvx mcp-server-fetch
    npx -y @modelcontextprotocol/server-memory                                     直接写命令
    amap https://mcp.amap.com/sse                                                   名字 + 地址

没写名字时按包名 / 镜像名 / 网址取（@modelcontextprotocol/server-github → github，
mcp-server-fetch → fetch，@upstash/context7-mcp → context7）。
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .config import servers_in

LAUNCHERS = {"npx", "uvx", "uv", "docker", "podman", "node", "python", "python3", "bunx", "bun", "deno",
             "pipx", "pnpm", "yarn", "java", "go", "cargo", "dotnet"}
GENERIC = {"www", "mcp", "api", "server", "servers", "sse", "http", "https", "v1", "v2", "app"}
SERVER_KEYS = {"command", "url", "serverUrl", "type", "transport"}


class AddError(ValueError):
    pass


@dataclass
class AddRequest:
    servers: dict[str, dict] = field(default_factory=dict)
    scope: str = "global"           # global / project
    force: bool = False


def parse_add(text: str) -> AddRequest:
    request = AddRequest()
    text = text.strip()
    # 我们自己的开关只认开头的（JSON 里面可能也有 -- 开头的字符串）
    while True:
        match = re.match(r"(--project|--global|--force|-p|-g|-f)(\s+|$)", text)
        if not match:
            break
        flag = match.group(1)
        if flag in ("--project", "-p"):
            request.scope = "project"
        elif flag in ("--global", "-g"):
            request.scope = "global"
        else:
            request.force = True
        text = text[match.end():].strip()
    if not text:
        raise AddError("要添加什么？把网站上的配置 JSON、claude mcp add 命令，或者 npx / uvx 命令贴在 /mcp add 后面")
    if text[0] in "{\"'" and not text.startswith("'{"):
        request.servers = parse_json_text(text)
    else:
        parse_command_line(text, request)
    for name, raw in request.servers.items():
        if not (raw.get("command") or raw.get("url") or raw.get("serverUrl")):
            raise AddError(f"{name} 没有 command 也没有 url，不知道怎么启动")
    return request


# -- JSON ---------------------------------------------------------------- #

def _loads(text: str):
    text = text.strip().rstrip(",")
    for candidate in (text, "{" + text + "}"):
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    # 常见的手误：最后一项后面多了逗号（网站上的片段经常这样）
    cleaned = re.sub(r",(\s*[}\]])", r"\1", text)
    for candidate in (cleaned, "{" + cleaned + "}"):
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    raise AddError("贴的 JSON 解析不了：检查是不是只复制了一半（括号要成对）")


def parse_json_text(text: str, name: str = "") -> dict[str, dict]:
    data = _loads(text)
    if not isinstance(data, dict):
        raise AddError("贴的 JSON 不是一个对象 {...}")
    if isinstance(data.get("mcp"), dict):            # VS Code settings.json 里的 "mcp": {"servers": ...}
        data = data["mcp"]
    table = servers_in(data)
    if table:
        return {clean_name(k): v for k, v in table.items()}
    if SERVER_KEYS & set(data):                      # 单个服务器的配置
        raw = dict(data)
        server_name = name or str(raw.pop("name", "") or "") or derive_name(raw)
        return {clean_name(server_name): raw}
    if data and all(isinstance(v, dict) and SERVER_KEYS & set(v) for v in data.values()):
        return {clean_name(k): v for k, v in data.items()}   # 复制了 mcpServers 里面的几项
    raise AddError("没在 JSON 里找到服务器配置（要有 command 或 url）")


# -- 命令行 -------------------------------------------------------------- #

def parse_command_line(text: str, request: AddRequest) -> None:
    try:
        tokens = shlex.split(text.replace("\\\n", " "), posix=True)
    except ValueError as exc:
        raise AddError(f"命令解析不了：{exc}（引号要成对）") from exc
    if not tokens:
        raise AddError("命令是空的")
    if "@smithery/cli" in tokens and "install" in tokens:
        raise AddError("这是 Smithery 的安装器，不是服务器本身：在网站上选 JSON 配置或连接地址（URL）贴过来")
    head = tokens[0].lower()
    if head in ("claude", "codex") and len(tokens) > 2 and tokens[1] == "mcp":
        verb, rest = tokens[2], tokens[3:]
        if verb == "add-json":
            if len(rest) < 2:
                raise AddError("用法：claude mcp add-json 名字 '{...}'")
            _strip_scope(rest, request)
            request.servers = parse_json_text(rest[1], name=rest[0])
            return
        if verb != "add":
            raise AddError(f"只认 {head} mcp add / add-json")
        tokens = rest
    name, raw = parse_launch(tokens, request)
    request.servers = {clean_name(name or derive_name(raw)): raw}


def _strip_scope(tokens: list[str], request: AddRequest) -> None:
    for flag in ("-s", "--scope"):
        while flag in tokens:
            i = tokens.index(flag)
            value = tokens[i + 1] if i + 1 < len(tokens) else ""
            request.scope = "global" if value == "user" else "project"
            del tokens[i:i + 2]


def parse_launch(tokens: list[str], request: AddRequest) -> tuple[str, dict]:
    """claude mcp add 的选项 + 名字 + 命令 / 地址；也适用于直接写的 "名字 命令..." """
    env: dict[str, str] = {}
    headers: dict[str, str] = {}
    transport = ""
    positional: list[str] = []
    command: list[str] | None = None
    i = 0
    while i < len(tokens):
        token = tokens[i]
        value = tokens[i + 1] if i + 1 < len(tokens) else ""
        if token == "--":
            command = tokens[i + 1:]
            break
        # 选项只在命令开始之前认（docker run -e 里的 -e 是 docker 自己的）；地址后面还可以跟 -H
        options_allowed = command is None and (len(positional) < 2 or any(map(_is_url, positional)))
        if options_allowed:
            if token in ("-e", "--env"):
                key, sep, val = value.partition("=")
                if not sep:
                    raise AddError(f"{token} 后面要写 KEY=值")
                env[key] = val
                i += 2
                continue
            if token in ("-H", "--header"):
                key, sep, val = value.partition(":")
                if not sep:
                    raise AddError(f"{token} 后面要写 \"名字: 值\"")
                headers[key.strip()] = val.strip()
                i += 2
                continue
            if token in ("-t", "--transport"):
                transport = value.lower()
                i += 2
                continue
            if token in ("-s", "--scope"):
                request.scope = "global" if value == "user" else "project"
                i += 2
                continue
            match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", token)
            if match and positional == []:          # 开头的 KEY=值（像 shell 的 env 前缀）
                env[match.group(1)] = match.group(2)
                i += 1
                continue
        positional.append(token)
        i += 1
    name = ""
    if command is not None:                          # 名字 -- 命令 参数...
        name = positional[0] if positional else ""
    else:
        if positional and not _is_url(positional[0]) and len(positional) > 1 \
                and positional[0].lower() not in LAUNCHERS and "/" not in positional[0] \
                and "\\" not in positional[0]:
            name, positional = positional[0], positional[1:]
        command = positional
    if not command:
        raise AddError("缺少要运行的命令或地址")
    if _is_url(command[0]):
        raw: dict = {"url": command[0]}
        if transport in ("http", "sse"):
            raw["type"] = transport
        if headers:
            raw["headers"] = headers
    else:
        raw = {"command": command[0], "args": command[1:]}
        if env:
            raw["env"] = env
    return name, raw


def _is_url(text: str) -> bool:
    return text.startswith(("http://", "https://"))


# -- 名字 ---------------------------------------------------------------- #

def clean_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", name.strip()).strip("-").lower()
    return name or "server"


def _package_name(package: str) -> str:
    """@org/server-github@1.2 → github；mcp-server-fetch → fetch；context7-mcp → context7"""
    package = package.split("/")[-1] if "/" in package else package
    package = re.sub(r"@[\w.\-^~]*$", "", package) or package          # 去版本号
    package = re.sub(r"==.*$", "", package)
    for prefix in ("mcp-server-", "server-", "mcp-"):
        if package.startswith(prefix) and len(package) > len(prefix):
            package = package[len(prefix):]
    for suffix in ("-mcp-server", "-mcp", "-server", "_mcp"):
        if package.endswith(suffix) and len(package) > len(suffix):
            package = package[: -len(suffix)]
    return package


def derive_name(raw: dict) -> str:
    url = str(raw.get("url") or raw.get("serverUrl") or "")
    if url:
        parts = urlsplit(url)
        segments = [s for s in parts.path.split("/") if s and s.lower() not in GENERIC]
        if segments:
            return _package_name(segments[-1].lstrip("@"))
        labels = [l for l in (parts.hostname or "").split(".")[:-1] if l.lower() not in GENERIC]
        return labels[-1] if labels else "remote"
    command = str(raw.get("command") or "")
    args = [str(a) for a in raw.get("args") or []]
    base = re.split(r"[\\/]", command)[-1].lower().removesuffix(".exe").removesuffix(".cmd")
    if base == "docker" or base == "podman":
        images = [a for a in args if not a.startswith("-") and a not in ("run", "--rm") and ("/" in a or ":" in a)]
        if images:
            return _package_name(images[-1].split(":")[0])
    skip_next = {"--from", "--with", "-p", "--package", "-e", "--env", "--python"}
    if base in ("npx", "uvx", "bunx", "pnpm", "pipx", "uv", "yarn", "deno"):
        i = 0
        while i < len(args):
            arg = args[i]
            if arg in skip_next:
                i += 2
                continue
            if arg.startswith("-") or arg in ("dlx", "run", "tool", "exec", "x"):
                i += 1
                continue
            return _package_name(arg)
    if base.startswith("python") and "-m" in args:
        i = args.index("-m")
        if i + 1 < len(args):
            return _package_name(args[i + 1].replace("_", "-"))
    scripts = [a for a in args if re.search(r"\.(js|mjs|py|ts)$", a)]
    if scripts:
        stem = re.split(r"[\\/]", scripts[0])[-1].rsplit(".", 1)[0]
        return _package_name(stem if stem not in ("index", "main", "server") else
                             (re.split(r"[\\/]", scripts[0])[-2] if len(re.split(r"[\\/]", scripts[0])) > 1
                              else stem))
    return base or "server"
