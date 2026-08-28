"""
会话条目类型。
每行一条JSON对象。条目通过 `id` / `parent_id` 构成树形结构，
依托该结构可以实现原地分支，无需复制文件。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Union

from pi_agent import AgentMessage, agent_message_from_dict, agent_message_to_dict
from pi_ai import Usage, usage_from_dict, usage_to_dict

SESSION_VERSION = 3


def new_entry_id() -> str:
    return secrets.token_hex(4)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SessionHeader:
    id: str
    cwd: str
    timestamp: str = field(default_factory=now_iso)
    version: int = SESSION_VERSION
    parent_session: str | None = None
    type: str = "session"


@dataclass
class EntryBase:
    id: str = field(default_factory=new_entry_id)
    parent_id: str | None = None
    timestamp: str = field(default_factory=now_iso)


@dataclass
class MessageEntry(EntryBase):
    message: AgentMessage | None = None
    type: str = "message"


@dataclass
class ModelChangeEntry(EntryBase):
    provider: str = ""
    model_id: str = ""
    type: str = "model_change"


@dataclass
class ThinkingLevelChangeEntry(EntryBase):
    thinking_level: str = "off"
    type: str = "thinking_level_change"


@dataclass
class CompactionEntry(EntryBase):
    summary: str = ""
    tokens_before: int = 0
    retained_tail: list[AgentMessage] = field(default_factory=list)
    first_kept_entry_id: str | None = None
    details: Any = None
    usage: Usage | None = None
    type: str = "compaction"


@dataclass
class BranchSummaryEntry(EntryBase):
    from_id: str = ""
    summary: str = ""
    details: Any = None
    usage: Usage | None = None
    type: str = "branch_summary"


@dataclass
class CustomEntry(EntryBase):
    """扩展状态,不会进入大模型上下文。"""

    custom_type: str = ""
    data: Any = None
    type: str = "custom"


@dataclass
class CustomMessageEntry(EntryBase):
    """由扩展注入的消息，该消息会纳入LLM上下文。"""

    custom_type: str = ""
    content: Any = ""
    display: bool = True
    details: Any = None
    type: str = "custom_message"


@dataclass
class LabelEntry(EntryBase):
    target_id: str = ""
    label: str | None = None
    type: str = "label"


@dataclass
class SessionInfoEntry(EntryBase):
    name: str = ""
    type: str = "session_info"


SessionEntry = Union[
    MessageEntry,
    ModelChangeEntry,
    ThinkingLevelChangeEntry,
    CompactionEntry,
    BranchSummaryEntry,
    CustomEntry,
    CustomMessageEntry,
    LabelEntry,
    SessionInfoEntry,
]


def header_to_dict(h: SessionHeader) -> dict[str, Any]:
    d = {
        "type": "session",
        "version": h.version,
        "id": h.id,
        "timestamp": h.timestamp,
        "cwd": h.cwd
    }

    if h.parent_session:
        d["parentSession"] = h.parent_session

    return d


def header_from_dict(d: dict[str, Any]) -> SessionHeader:
    return SessionHeader(
        id=d.get("id", ""),
        cwd=d.get("cwd", ""),
        timestamp=d.get("timestamp", now_iso()),
        version=d.get("version", 1),
        parent_session=d.get("parentSession")
    )


def entry_to_dict(e: SessionEntry) -> dict[str, Any]:
    base = {
        "type": e.type,
        "id": e.id,
        "parentId": e.parent_id,
        "timestamp": e.timestamp,
    }
    if isinstance(e, MessageEntry):
        base["message"] = agent_message_to_dict(e.message) if e.message else None
    elif isinstance(e, ModelChangeEntry):
        base.update({"provider": e.provider, "modelId": e.model_id})
    elif isinstance(e, ThinkingLevelChangeEntry):
        base["thinkingLevel"] = e.thinking_level
    elif isinstance(e, CompactionEntry):
        base.update({
            "summary": e.summary,
            "tokensBefore": e.tokens_before,
            "retainedTail": [agent_message_to_dict(m) for m in e.retained_tail]
        })
        if e.first_kept_entry_id:
            base["firstKeptEntryId"] = e.first_kept_entry_id
        if e.details is not None:
            base["details"] = e.details
        if e.usage is not None:
            base["usage"] = usage_to_dict(e.usage)
    elif isinstance(e, BranchSummaryEntry):
        base.update({"fromId": e.from_id, "summary": e.summary})
        if e.details is not None:
            base["details"] = e.details
        if e.usage is not None:
            base["usage"] = usage_to_dict(e.usage)
    elif isinstance(e, CustomEntry):
        base.update({"customType": e.custom_type, "data": e.data})
    elif isinstance(e, CustomMessageEntry):
        base.update({"customType": e.custom_type, "content": e.content, "display": e.display})
        if e.details is not None:
            base["details"] = e.details
    elif isinstance(e, LabelEntry):
        base.update({"targetId": e.target_id, "label": e.label})
    elif isinstance(e, SessionEntry):
        base["name"] = e.name

    return base


def entry_from_dict(d: dict[str, Any]) -> SessionEntry:
    common = {
        "id": d.get("id") or new_entry_id(),
        "parent_id": d.get("parentId"),
        "timestamp": d.get("timestamp") or now_iso(),
    }

    t = d.get("type")
    if t == "message":
        return MessageEntry(**common, message=agent_message_from_dict(d["message"]))
    if t == "model_change":
        return ModelChangeEntry(**common, provider=d.get("provider", ""), model_id=d.get("modelId", ""))
    if t == "thinking_level_change":
        return ThinkingLevelChangeEntry(**common, thinking_level=d.get("thinkingLevel", "off"))
    if t == "compaction":
        return CompactionEntry(
            **common,
            summary=d.get("summary", ""),
            tokens_before=d.get("tokensBefore", 0),
            retained_tail=[agent_message_from_dict(m) for m in d.get("retainedTail") or []],
            first_kept_entry_id=d.get("firstKeptEntryId"),
            details=d.get("details"),
            usage=usage_from_dict(d["usage"]) if d.get("usage") else None
        )
    if t == "branch_summary":
        return BranchSummaryEntry(
            **common,
            from_id=d.get("fromId", ""),
            summary=d.get("summary", ""),
            details=d.get("details"),
            usage=usage_from_dict(d["usage"]) if d.get("usage") else None
        )
    if t == "custom":
        return CustomEntry(**common, custom_type=d.get("customType", ""), data=d.get("data"))
    if t == "custom_message":
        return CustomMessageEntry(
            **common,
            custom_type=d.get("customType", ""),
            content=d.get("content", ""),
            display=d.get("display", True),
            details=d.get("details")
        )
    if t == "label":
        return LabelEntry(**common, target_id=d.get("targetId", ""), label=d.get("label"))
    if t == "session_info":
        return SessionInfoEntry(**common, name=d.get("name", ""))

    raise ValueError(f"unknown session entry type: {t!r}")
