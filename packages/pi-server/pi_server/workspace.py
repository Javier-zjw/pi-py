"""会话历史读取与文件浏览。

两件事都不需要活跃会话：浏览历史只是解析 jsonl，文件预览只是读磁盘。
所以它们独立于 registry，也不 import fastapi——纯函数，可单测。

安全边界：文件访问只允许在"已知根目录"内。这不是通用文件浏览器，越界
一律拒绝，避免网页端变成任意文件读取的入口。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pi_coding_agent import SessionManager
from pi_coding_agent.session.entries import entry_from_dict

MAX_PREVIEW_BYTES = 512_000
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".next", ".mypy_cache", ".pytest_cache", ".idea", ".DS_Store",
}
TEXT_SUFFIXES = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".vue", ".json", ".md", ".txt", ".yml",
    ".yaml", ".toml", ".ini", ".cfg", ".sh", ".html", ".css", ".scss", ".sql",
    ".go", ".rs", ".java", ".c", ".h", ".cpp", ".rb", ".php", ".xml", ".env",
}


class AccessDenied(PermissionError):
    pass


# --------------------------------------------------------------------------- #
# 文件浏览
# --------------------------------------------------------------------------- #


def resolve_within(root: str | Path, relative: str) -> Path:
    """把相对路径解析到 root 内，越界抛异常。

    必须 resolve 之后再比较，否则 ../../etc/passwd 这类会漏过去。
    """
    base = Path(root).expanduser().resolve()
    target = (base / (relative or ".")).resolve()
    if target != base and base not in target.parents:
        raise AccessDenied(f"越界访问：{relative}")
    return target


def list_directory(root: str | Path, relative: str = ".") -> dict[str, Any]:
    target = resolve_within(root, relative)
    if not target.exists():
        raise FileNotFoundError(relative)
    if not target.is_dir():
        raise NotADirectoryError(relative)

    base = Path(root).expanduser().resolve()
    entries = []
    for child in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if child.name in SKIP_DIRS or child.name.startswith("."):
            continue
        try:
            stat = child.stat()
        except OSError:
            continue
        entries.append({
            "name": child.name,
            "path": str(child.relative_to(base)),
            "dir": child.is_dir(),
            "size": stat.st_size if child.is_file() else 0,
            "mtime": stat.st_mtime,
        })
    return {
        "path": "." if target == base else str(target.relative_to(base)),
        "parent": None if target == base else str(target.parent.relative_to(base)),
        "entries": entries,
    }


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico"}
PDF_SUFFIXES = {".pdf"}
OFFICE_SUFFIXES = {".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt"}

MIME_BY_SUFFIX = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".svg": "image/svg+xml", ".ico": "image/x-icon", ".pdf": "application/pdf",
}


def classify(path: Path) -> str:
    """决定前端用哪种方式呈现：文本、图片、PDF、Office、还是纯二进制。"""
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in PDF_SUFFIXES:
        return "pdf"
    if suffix in OFFICE_SUFFIXES:
        return "office"
    if suffix in TEXT_SUFFIXES:
        return "text"
    return "unknown"


def guess_mime(path: Path) -> str:
    import mimetypes

    return MIME_BY_SUFFIX.get(path.suffix.lower()) or (
        mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    )


def quick_locations() -> list[dict[str, Any]]:
    """目录选择器的常用入口，省得从根目录一层层点。"""
    home = Path.home()
    candidates = [
        ("主目录", home),
        ("桌面", home / "Desktop"),
        ("下载", home / "Downloads"),
        ("文档", home / "Documents"),
        ("项目", home / "Projects"),
        ("代码", home / "Code"),
        ("根目录", Path("/")),
    ]
    return [
        {"name": name, "path": str(p)}
        for name, p in candidates
        if p.is_dir()
    ]


def read_file(root: str | Path, relative: str) -> dict[str, Any]:
    target = resolve_within(root, relative)
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(relative)

    size = target.stat().st_size
    kind = classify(target)
    base = {
        "path": relative,
        "name": target.name,
        "size": size,
        "kind": kind,
        "mime": guess_mime(target),
        "language": target.suffix.lstrip(".").lower(),
        "content": "",
    }

    # 图片和 PDF 交给浏览器渲染，走 /api/files/raw，不在这里塞 base64
    if kind in ("image", "pdf"):
        return base
    if kind == "office":
        # 不做转换：docx/xlsx 要靠额外依赖才能转 HTML，先老实说明白
        return {**base, "note": "Office 文档暂不支持在线预览，可下载后查看"}

    if kind == "unknown" and size > 64_000:
        return {**base, "kind": "binary"}
    try:
        content = target.read_bytes()[:MAX_PREVIEW_BYTES].decode("utf-8")
    except UnicodeDecodeError:
        return {**base, "kind": "binary"}
    return {
        **base,
        "kind": "text",
        "truncated": size > MAX_PREVIEW_BYTES,
        "content": content,
    }


def raw_file(root: str | Path, relative: str) -> tuple[Path, str]:
    """给 /api/files/raw 用：返回真实路径和 MIME，由 FastAPI 直接吐字节。"""
    target = resolve_within(root, relative)
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(relative)
    return target, guess_mime(target)


# --------------------------------------------------------------------------- #
# 会话历史
# --------------------------------------------------------------------------- #


@dataclass
class HistoryTurn:
    """一轮：用户消息 + 助手回复 + 中间的思考和工具调用。

    前端按轮渲染，和实时对话共用同一套组件，所以这里要输出同样的形状。
    """

    user: str = ""
    thinking: str = ""
    text: str = ""
    tools: list[dict] = None
    usage: dict = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "user": self.user,
            "thinking": self.thinking,
            "text": self.text,
            "tools": self.tools or [],
            "usage": self.usage or {},
            "phase": "done",
            "folded": True,
        }


def read_session_file(path: str | Path) -> dict[str, Any]:
    """解析一个 .jsonl，返回可直接渲染的轮次列表。"""
    file = Path(path).expanduser()
    if not file.exists():
        raise FileNotFoundError(str(path))

    header: dict[str, Any] = {}
    entries = []
    for line in file.read_text("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("type") == "session":
            header = data
            continue
        try:
            entries.append(entry_from_dict(data))
        except (ValueError, KeyError):
            continue

    turns: list[HistoryTurn] = []
    current: HistoryTurn | None = None
    pending_tools: dict[str, dict] = {}

    for entry in entries:
        if entry.type == "compaction":
            turns.append(HistoryTurn(user="", text=f"（上下文已压缩）{entry.summary}"))
            current = None
            continue
        if entry.type != "message" or entry.message is None:
            continue

        message = entry.message
        role = getattr(message, "role", "")

        if role == "user":
            current = HistoryTurn(user=message.text(), tools=[], usage={})
            turns.append(current)
        elif role == "assistant":
            if current is None:
                current = HistoryTurn(tools=[], usage={})
                turns.append(current)
            thinking = "".join(
                getattr(c, "thinking", "") for c in message.content
                if getattr(c, "type", "") == "thinking"
            )
            if thinking:
                current.thinking = (current.thinking or "") + thinking
            text = message.text()
            if text.strip():
                current.text = (current.text + "\n\n" + text).strip() if current.text else text
            for call in message.tool_calls():
                record = {"id": call.id, "name": call.name, "arguments": call.arguments,
                          "state": "run", "preview": ""}
                pending_tools[call.id] = record
                current.tools.append(record)
            usage = getattr(message, "usage", None)
            if usage and usage.input:
                current.usage = {
                    "input": usage.input, "output": usage.output,
                    "cacheRead": usage.cache_read, "cost": round(usage.cost.total, 6),
                }
        elif role == "toolResult":
            record = pending_tools.pop(getattr(message, "tool_call_id", ""), None)
            if record is not None:
                record["state"] = "bad" if message.is_error else "ok"
                record["preview"] = (message.text() or "").split("\n")[0][:200]
                details = getattr(message, "details", None)
                if isinstance(details, dict) and details.get("patch"):
                    record["patch"] = details["patch"]

    return {
        "file": str(file),
        "id": header.get("id", ""),
        "cwd": header.get("cwd", ""),
        "timestamp": header.get("timestamp", ""),
        "title": next((t.user for t in turns if t.user), "") [:60],
        "turns": [t.to_dict() for t in turns],
    }


# --------------------------------------------------------------------------- #
# 工作目录与历史可见性
# --------------------------------------------------------------------------- #


def _hidden_file(agent_dir: str | Path | None) -> Path:
    from pi_coding_agent.session.manager import DEFAULT_AGENT_DIR

    return Path(agent_dir or DEFAULT_AGENT_DIR).expanduser() / "web-hidden.json"


def load_hidden(agent_dir: str | Path | None = None) -> set[str]:
    """网页端隐藏掉的会话文件。

    只影响列表显示，磁盘上的 .jsonl 一个都不删——用户随时能从终端 --resume
    找回来，也不会丢掉排查问题需要的记录。
    """
    path = _hidden_file(agent_dir)
    try:
        return set(json.loads(path.read_text("utf-8")))
    except (OSError, ValueError):
        return set()


def hide_session(file: str, agent_dir: str | Path | None = None) -> set[str]:
    hidden = load_hidden(agent_dir)
    hidden.add(str(file))
    path = _hidden_file(agent_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(hidden), ensure_ascii=False, indent=2), "utf-8")
    return hidden


def unhide_all(agent_dir: str | Path | None = None) -> None:
    path = _hidden_file(agent_dir)
    if path.exists():
        path.unlink()


def list_workspaces(agent_dir: str | Path | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """曾经用过的工作目录。

    不从目录名反推路径（编码是有损的：路径里本来就有 - 的话分不清），
    而是读每个会话文件头里的 cwd 字段——那是准确的原始路径。
    """
    from pi_coding_agent.session.manager import sessions_root

    root = sessions_root(agent_dir)
    if not root.exists():
        return []

    found: dict[str, dict[str, Any]] = {}
    for project_dir in root.iterdir():
        if not project_dir.is_dir():
            continue
        files = sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            continue
        cwd = ""
        try:
            with files[0].open("r", encoding="utf-8") as fh:
                head = json.loads(fh.readline() or "{}")
                cwd = head.get("cwd", "")
        except (OSError, ValueError):
            continue
        if not cwd:
            continue
        found[cwd] = {
            "cwd": cwd,
            "name": Path(cwd).name or cwd,
            "sessions": len(files),
            "mtime": files[0].stat().st_mtime,
            "exists": Path(cwd).is_dir(),
        }
    return sorted(found.values(), key=lambda w: w["mtime"], reverse=True)[:limit]


def list_subdirectories(path: str | Path) -> dict[str, Any]:
    """给工作目录选择器用：只列子目录，不受 root 限制。

    这里刻意不做越界防护——用户是在挑自己的项目目录，本来就要能到处走。
    真正的边界在 list_directory/read_file 那两个函数上，它们绑定会话的 cwd。
    """
    target = Path(path or "~").expanduser()
    if not target.is_dir():
        target = Path.home()
    target = target.resolve()
    entries = []
    try:
        for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            if child.is_dir() and not child.name.startswith("."):
                entries.append({"name": child.name, "path": str(child)})
    except PermissionError:
        pass
    return {
        "path": str(target),
        "parent": str(target.parent) if target.parent != target else None,
        "entries": entries[:200],
    }


def list_session_files(cwd: str | Path, agent_dir: str | Path | None = None,
                       limit: int = 50) -> list[dict[str, Any]]:
    """当前工作目录下的历史会话，附带标题和轮数。"""
    hidden = load_hidden(agent_dir)
    out = []
    for path in SessionManager.list(cwd, agent_dir):
        if str(path) in hidden:
            continue
        if len(out) >= limit:
            break
        title, turns = "", 0
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    message = data.get("message") or {}
                    if data.get("type") == "message" and message.get("role") == "user":
                        turns += 1
                        if not title:
                            content = message.get("content")
                            text = content if isinstance(content, str) else "".join(
                                b.get("text", "") for b in content or []
                            )
                            title = text.replace("\n", " ")[:60]
        except OSError:
            continue
        out.append({
            "file": str(path),
            "name": path.stem,
            "title": title or path.stem,
            "turns": turns,
            "mtime": path.stat().st_mtime,
        })
    return out
