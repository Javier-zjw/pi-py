"""历史会话的概要：标题、轮数、修改时间。终端的 /resume 和网页的会话列表共用。

只扫一遍 JSONL 取需要的字段，不构建整棵会话树——列表里可能有几十个文件。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .manager import SessionManager

TITLE_LIMIT = 60


@dataclass
class SessionSummary:
    path: Path
    title: str      # 会话名（session_info）优先，否则第一条用户消息
    turns: int      # 用户消息条数
    mtime: float

    @property
    def name(self) -> str:
        return self.path.stem


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def describe_session(path: str | Path) -> SessionSummary | None:
    """读不了的文件返回 None；坏行跳过"""
    path = Path(path)
    first_user, name, turns = "", "", 0
    try:
        mtime = path.stat().st_mtime
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue
                if data.get("type") == "session_info" and data.get("name"):
                    name = str(data["name"])
                message = data.get("message") or {}
                if data.get("type") == "message" and message.get("role") == "user":
                    turns += 1
                    if not first_user:
                        first_user = " ".join(_text(message.get("content")).split())
    except OSError:
        return None
    title = (name or first_user)[:TITLE_LIMIT] or path.stem
    return SessionSummary(path, title, turns, mtime)


def list_sessions(cwd: str | Path = ".", agent_dir: str | Path | None = None, *,
                  limit: int = 50, include_empty: bool = True,
                  exclude: set[str] | None = None) -> list[SessionSummary]:
    """当前目录的历史会话，新的在前。include_empty=False 跳过还没说过话的会话
    （每次启动都会建一个文件）；exclude 是要跳过的文件路径（字符串）"""
    out: list[SessionSummary] = []
    for path in SessionManager.list(cwd, agent_dir):
        if len(out) >= limit:
            break
        if exclude and str(path) in exclude:
            continue
        summary = describe_session(path)
        if summary is None or (not include_empty and summary.turns == 0):
            continue
        out.append(summary)
    return out
