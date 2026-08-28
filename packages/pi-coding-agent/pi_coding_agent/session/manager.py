"""
会话管理器：持久化对话记录。
会话本质是只追加写入的JSONL文件，内部条目构成树形结构。当前位置为「叶子节点」；
分支操作仅移动叶子指针并追加新子节点，不同备选路径保存在同一个文件内。
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, List

from pi_agent import AgentMessage, CustomMessage
from pi_ai import AssistantMessage, ThinkingLevel, Usage

from ..text import has_surrogates, sanitize

from .entries import (
    BranchSummaryEntry,
    CompactionEntry,
    CustomEntry,
    CustomMessageEntry,
    LabelEntry,
    MessageEntry,
    ModelChangeEntry,
    SessionEntry,
    SessionHeader,
    SessionInfoEntry,
    ThinkingLevelChangeEntry,
    entry_from_dict,
    entry_to_dict,
    header_from_dict,
    header_to_dict,
    new_entry_id,
)

DEFAULT_AGENT_DIR = Path.home() / ".pi" / "agent"


def sessions_root(agent_dir: str | Path | None = None) -> Path:
    return Path(agent_dir or DEFAULT_AGENT_DIR).expanduser() / "sessions"


def project_dir_name(cwd: str | Path) -> str:
    resolved = str(Path(cwd).expanduser().resolve())
    return "--" + resolved.replace("/", "-").replace("\\", "-").replace(":", "") + "--"


class SessionManager:
    def __init__(
            self,
            cwd: str | Path = ".",
            session_file: Path | None = None,
            agent_dir: str | Path | None = None,
            header: SessionHeader | None = None
    ) -> None:
        self.cwd = str(Path(cwd).expanduser().resolve())
        self.agent_dir = Path(agent_dir or DEFAULT_AGENT_DIR).expanduser()
        self._file = session_file
        self._entries: list[SessionEntry] = []
        self._by_id: dict[str, SessionEntry] = {}
        self._leaf_id: str | None = None
        self._header = header or SessionHeader(id=str(uuid.uuid4()), cwd=self.cwd)

    @classmethod
    def create(cls, cwd: str | Path = ".", agent_dir: str | Path | None = None) -> "SessionManager":
        sm = cls(cwd, agent_dir=agent_dir)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        directory = sessions_root(sm.agent_dir) / project_dir_name(sm.cwd)
        directory.mkdir(parents=True, exist_ok=True)
        sm._file = directory / f"{stamp}_{sm._header.id[:8]}.jsonl"
        sm._write_header()
        return sm

    @classmethod
    def open(cls, path: str | Path, agent_dir: str | Path | None = None) -> "SessionManager":
        file = Path(path).expanduser()
        sm = cls(cwd=".", session_file=file, agent_dir=agent_dir)
        sm._load()
        return sm

    @classmethod
    def in_memory(cls, cwd: str | Path = ".") -> "SessionManager":
        return cls(cwd=cwd, session_file=None)

    @classmethod
    def continue_recent(
            cls, cwd: str | Path = ".", agent_dir: str | Path | None = None
    ) -> "SessionManager":
        sessions = cls.list(cwd, agent_dir)
        if sessions:
            return cls.open(sessions[0], agent_dir)
        return cls.create(cwd, agent_dir)

    @staticmethod
    def list(cwd: str | Path = ".", agent_dir: str | Path | None = None) -> list[Path]:
        directory = sessions_root(agent_dir) / project_dir_name(cwd)
        if not directory.exists():
            return []
        return sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)

    @staticmethod
    def list_all(agent_dir: str | Path | None = None) -> List[Path]:
        root = sessions_root(agent_dir)
        if not root.exists():
            return []
        return sorted(root.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)

    def is_persisted(self) -> bool:
        return self._file is not None

    def get_session_file(self) -> Path | None:
        return self._file

    def get_session_id(self) -> str:
        return self._header.id

    def get_header(self) -> SessionHeader:
        return self._header

    def get_cwd(self) -> str:
        return self.cwd

    def _write_header(self) -> None:
        if self._file is None:
            return
        self._file.parent.mkdir(parents=True, exist_ok=True)
        with self._file.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(header_to_dict(self._header), ensure_ascii=False) + "\n")

    def _append_line(self, entry: SessionEntry) -> None:
        if self._file is None:
            return
        line = json.dumps(entry_to_dict(entry), ensure_ascii=False)
        if has_surrogates(line):
            # 脏字符串流到这里才发现就太晚了——它可能是二十分钟前某次 find
            # 拿到的文件名。开发期直接炸掉定位源头，生产环境修好继续写。
            if __debug__ and os.environ.get("PI_STRICT_TEXT"):
                raise ValueError(f"脏数据流进 session：entry.type={entry.type} id={entry.id}")
            line = sanitize(line)

        with self._file.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def _load(self) -> None:
        assert self._file is not None
        if not self._file.exists():
            self._write_header()
            return
        with self._file.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh):
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                if data.get("type") == "session":
                    self._header = header_from_dict(data)
                    self.cwd = self._header.cwd or self.cwd
                    continue
                entry = entry_from_dict(data)
                if entry.parent_id is None and lineno > 1 and self._entries:
                    entry.parent_id = self._entries[-1].id
                self._entries.append(entry)
                self._by_id[entry.id] = entry
                self._leaf_id = entry.id

    def _append(self, entry: SessionEntry) -> str:
        entry.parent_id = self._leaf_id
        self._entries.append(entry)
        self._by_id[entry.id] = entry
        self._leaf_id = entry.id
        self._append_line(entry)
        return entry.id

    def append_message(self, message: AgentMessage) -> str:
        return self._append(MessageEntry(id=new_entry_id(), message=message))

    def append_model_change(self, provider: str, model_id: str) -> str:
        return self._append(ModelChangeEntry(id=new_entry_id(), provider=provider, model_id=model_id))

    def append_thinking_level_change(self, level: ThinkingLevel) -> str:
        return self._append(ThinkingLevelChangeEntry(id=new_entry_id(), thinking_level=level))

    def append_compaction(
            self,
            summary: str,
            tokens_before: int,
            retained_tail: Iterable[AgentMessage] = (),
            details: Any = None,
            usage: Usage | None = None
    ) -> str:
        return self._append(
            CompactionEntry(
                id=new_entry_id(),
                summary=summary,
                tokens_before=tokens_before,
                retained_tail=list(retained_tail),
                details=details,
                usage=usage
            )
        )

    def append_branch_summary(self, from_id: str, summary: str, details: Any = None) -> str:
        return self._append(
            BranchSummaryEntry(id=new_entry_id(), from_id=from_id, summary=summary, details=details)
        )

    def append_custom_entry(self, custom_type: str, data: Any = None) -> str:
        return self._append(CustomEntry(id=new_entry_id(), custom_type=custom_type, data=data))

    def append_custom_message_entry(
            self, custom_type: str, content: Any, display: bool = True, details: Any = None
    ) -> str:
        return self._append(
            CustomMessageEntry(
                id=new_entry_id(),
                custom_type=custom_type,
                content=content,
                display=display,
                details=details
            )
        )

    def append_label_change(self, target_id: str, label: str | None) -> str:
        return self._append(LabelEntry(id=new_entry_id(), target_id=target_id, label=label))

    def append_session_info(self, name: str) -> str:
        return self._append(SessionInfoEntry(id=new_entry_id(), name=name))

    def get_entries(self) -> List[SessionEntry]:
        return list(self._entries)

    def get_entry(self, entry_id: str) -> SessionEntry | None:
        return self._by_id.get(entry_id)

    def get_leaf_id(self) -> str | None:
        return self._leaf_id

    def get_leaf_entry(self) -> SessionEntry | None:
        return self._by_id.get(self._leaf_id) if self._leaf_id else None

    def get_children(self, parent_id: str | None) -> List[SessionEntry]:
        return [e for e in self._entries if e.parent_id == parent_id]

    def get_branch(self, from_id: str | None = None) -> List[SessionEntry]:
        """从根节点向下至 ``from_id`` 的路径（默认使用当前叶子节点）"""
        current = from_id or self._leaf_id
        path: list[SessionEntry] = []
        seen: set[str] = set()
        while current and current in self._by_id and current not in seen:
            seen.add(current)
            entry = self._by_id[current]
            path.append(entry)
            current = entry.parent_id
        return list(reversed(path))

    def get_tree(self) -> dict[str | None, List[SessionEntry]]:
        tree: dict[str | None, list[SessionEntry]] = {}
        for e in self._entries:
            tree.setdefault(e.parent_id, []).append(e)
        return tree

    def get_label(self, entry_id: str) -> str | None:
        label = None
        for e in self._entries:
            if isinstance(e, LabelEntry) and e.target_id == entry_id:
                label = e.label
        return label

    def get_session_name(self) -> str | None:
        name = None
        for e in self._entries:
            if isinstance(e, SessionInfoEntry):
                name = e.name
        return name

    def branch(self, entry_id: str) -> None:
        """将叶子指针移动至更早的条目；后续追加操作将形成新分支"""
        if entry_id not in self._by_id:
            raise KeyError(entry_id)
        self._leaf_id = entry_id

    def branch_with_summary(self, entry_id: str, summary: str, details: Any = None) -> str:
        from_id = self._leaf_id or entry_id
        self.branch(entry_id)
        return self.append_branch_summary(from_id, summary, details)

    def reset_leaf(self) -> None:
        self._leaf_id = None

    def create_branched_session(self, leaf_id: str, agent_dir: str | Path | None = None) -> "SessionManager":
        """提取以 leaf_id 终止的节点链路，生成一份全新独立会话文件"""
        target = SessionManager.create(self.cwd, agent_dir or self.agent_dir)
        target._header.parent_session = str(self._file) if self._file else None
        target._write_header()
        for entry in self.get_branch(leaf_id):
            clone = entry_from_dict(entry_to_dict(entry))
            clone.id = new_entry_id()
            target._append(clone)
        return target

    def build_context_entries(self) -> List[SessionEntry]:
        """当前活跃分支条目，已执行压缩处理"""
        path = self.get_branch()
        last_compaction = None
        for i, e in enumerate(path):
            if isinstance(e, CompactionEntry):
                last_compaction = i

        if last_compaction is None:
            return path

        compaction = path[last_compaction]
        assert isinstance(compaction, CompactionEntry)
        tail = path[last_compaction + 1:]
        if compaction.retained_tail:
            return [compaction, *tail]
        if compaction.first_kept_entry_id:
            start = next(
                (i for i, e in enumerate(path) if e.id == compaction.first_kept_entry_id), 0
            )
            return [compaction, *path[start: last_compaction], *tail]

        return [compaction, *tail]

    def build_session_context(self) -> dict[str, Any]:
        """Messages + model/thinking settings for the LLM"""
        model_ref: tuple[str, str] | None = None
        thinking_level: ThinkingLevel = "off"
        for e in self.get_branch():
            if isinstance(e, ModelChangeEntry):
                model_ref = (e.provider, e.model_id)
            elif isinstance(e, ThinkingLevelChangeEntry):
                thinking_level = e.thinking_level  # type: ignore[assignment]

        messages: list[AgentMessage] = []
        for e in self.build_context_entries():
            if isinstance(e, MessageEntry) and e.message is not None:
                messages.append(e.message)
            elif isinstance(e, CompactionEntry):
                messages.append(
                    CustomMessage(
                        custom_type="compaction_summary",
                        content=(
                                "Summary of the earlier conversation, which has been compacted:\n\n"
                                + e.summary
                        ),
                        display=True
                    )
                )
                messages.extend(e.retained_tail)
            elif isinstance(e, BranchSummaryEntry):
                messages.append(
                    CustomMessage(
                        custom_type="branch_summary",
                        content="Summary of an abandoned branch:\n\n" + e.summary
                    )
                )
            elif isinstance(e, CustomMessageEntry):
                messages.append(
                    CustomMessage(
                        custom_type=e.custom_type,
                        content=e.content,
                        display=e.display,
                        details=e.details
                    )
                )

        return {"messages": messages, "thinking_level": thinking_level, "model": model_ref}

    def total_usage(self) -> Usage:
        total = Usage()
        for e in self._entries:
            if isinstance(e, MessageEntry) and isinstance(e.message, AssistantMessage):
                total += e.message.usage
            elif isinstance(e, (CompactionEntry, BranchSummaryEntry)) and e.usage:
                total += e.usage

        return total

# if __name__ == '__main__':
#     sm = SessionManager.in_memory(".")
#     a = sm.append_message(UserMessage(content="一"))
#     b = sm.append_message(AssistantMessage(content=[TextContent(text="二")]))
#     c = sm.append_message(UserMessage(content="三"))
#     sm.branch(b)  # 回到第二条
#     d = sm.append_message(UserMessage(content="另一条路"))
#     print([e.id for e in sm.get_branch()])  # [a, b, d]
#     print(len(sm.get_entries()))
