"""长期记忆：pi 自己记的笔记，跨会话保留（参照 Claude Code auto memory / Codex memories / Hermes）。

两份，都是人能读、能手改的 Markdown，不放进仓库（个人笔记，可能有隐私，也不弄脏 git）：

- 全局 ``~/.pi/agent/memory/MEMORY.md``：用户本人的偏好（回答语言、提交习惯……）；
- 项目 ``~/.pi/agent/memory/<项目目录名>/MEMORY.md``：这个项目查明的事实（测试怎么跑、哪里有坑）。
  目录名同会话目录（``session.manager.project_dir_name``）。

和 ``AGENTS.md`` 的分工：AGENTS.md 是人写的、可以提交给团队的指令；记忆是模型干活时顺手记的。

格式：以 ``- `` 开头的行是一条记忆（工具写入时带 ``[日期]``，方便判断是否过时）；别的行（标题、
用户手写的说明）原样保留——增删改只动条目那一行，不重写整个文件。

会话开始时整份读进系统提示（快照）：会话中途写入马上落盘，但这次的系统提示不变——每记一条就改
系统提示会让前缀缓存整段失效；刚记的内容本来就在当前对话里。下次会话带上。

每份有字数上限（settings ``memory.userChars`` / ``projectChars``）：写满了拒绝新增，让模型先合并、
删掉旧的——不设上限的话文件只会越来越长，常驻上下文跟着涨。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from .session.manager import project_dir_name
from .text import read_text_lenient, sanitize

SCOPES = ("user", "project")
SCOPE_NAMES = {"user": "全局", "project": "项目"}
ENTRY_MAX = 500                 # 一条最多多少字：记忆是一句事实，不是一段文档
DEFAULT_LIMITS = {"user": 2000, "project": 4000}

HEADER = {
    "user": ("# pi 记忆 · 全局\n\n<!-- 由 pi 维护：每条以 \"- \" 开头，可以手改或删除，下次会话生效。"
             "这里记用户本人的偏好，所有项目都会用到。 -->\n"),
    "project": ("# pi 记忆 · 项目 {cwd}\n\n<!-- 由 pi 维护：每条以 \"- \" 开头，可以手改或删除，"
                "下次会话生效。这里记这个项目查明的事实。 -->\n"),
}

# 疑似密钥：写进记忆前换成 [REDACTED]（记忆会进每次会话的系统提示，也会被同步、备份）
SECRET_PATTERNS = [
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd)(\s*[:=]\s*)(?!\[REDACTED\])"
               r"[\"']?[^\s\"']{6,}"),
]


class MemoryEditError(Exception):
    """给模型看的错误说明（英文）"""


@dataclass
class Memory:
    scope: str
    path: Path
    lines: list[str]            # 文件原样的行（不含换行）

    @property
    def entries(self) -> list[str]:
        return [line[2:].strip() for line in self.lines if line.startswith("- ")]

    @property
    def used(self) -> int:
        return sum(len(entry) for entry in self.entries)


def memory_root(agent_dir: str | Path) -> Path:
    return Path(agent_dir).expanduser() / "memory"


def memory_path(agent_dir: str | Path, scope: str, cwd: str | Path) -> Path:
    root = memory_root(agent_dir)
    return root / "MEMORY.md" if scope == "user" else root / project_dir_name(cwd) / "MEMORY.md"


def load(agent_dir: str | Path, scope: str, cwd: str | Path) -> Memory:
    path = memory_path(agent_dir, scope, cwd)
    text = read_text_lenient(path) if path.is_file() else None
    return Memory(scope, path, (text or "").splitlines())


def save(memory: Memory, cwd: str | Path) -> None:
    lines = memory.lines
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    memory.path.parent.mkdir(parents=True, exist_ok=True)
    tmp = memory.path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", "utf-8")
    tmp.replace(memory.path)            # 先写临时文件再改名：写到一半退出不会留下半个文件
    memory.lines = lines


def redact(text: str) -> tuple[str, bool]:
    out = text
    for pattern in SECRET_PATTERNS:
        if pattern.groups >= 2:
            out = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", out)
        else:
            out = pattern.sub("[REDACTED]", out)
    return out, out != text


def _clean(text: str) -> str:
    """一条记忆一行：换行并成空格，去掉开头的列表符号和日期（工具自己加）"""
    text = sanitize(" ".join(str(text or "").split()))
    text = re.sub(r"^(?:[-*]\s+)?(?:\[\d{4}-\d{2}-\d{2}\]\s*)?", "", text)
    return text.strip()


def _bare(entry: str) -> str:
    return re.sub(r"^\[\d{4}-\d{2}-\d{2}\]\s*", "", entry)


def _find(memory: Memory, old_text: str) -> int:
    """按原文片段找到唯一一条，返回它在 lines 里的下标"""
    needle = " ".join(str(old_text or "").split())
    if not needle:
        raise MemoryEditError("old_text is required: copy a distinctive part of the entry to change")
    hits = [i for i, line in enumerate(memory.lines) if line.startswith("- ") and needle in line]
    if not hits:
        listing = "\n".join(f"- {entry}" for entry in memory.entries) or "(empty)"
        raise MemoryEditError(f"no {memory.scope} memory entry contains {needle!r}. Current entries:\n"
                          f"{listing}")
    if len(hits) > 1:
        listing = "\n".join(memory.lines[i] for i in hits)
        raise MemoryEditError(f"{len(hits)} entries contain {needle!r}; use a longer, unique part:\n"
                          f"{listing}")
    return hits[0]


def _check_size(memory: Memory, limit: int, added: int) -> None:
    if memory.used + added > limit:
        listing = "\n".join(f"- {entry}" for entry in memory.entries)
        raise MemoryEditError(
            f"{memory.scope} memory is full ({memory.used}/{limit} chars, this entry needs {added}). "
            f"Consolidate first: merge related entries with action=replace, or remove stale ones "
            f"with action=remove, then try again. Current entries:\n{listing}")


def add(memory: Memory, text: str, cwd: str | Path, limit: int) -> tuple[str, bool]:
    """加一条，返回 (写入的内容, 是否去掉了密钥)"""
    entry, redacted = redact(_clean(text))
    if not entry:
        raise MemoryEditError("text is empty")
    if len(entry) > ENTRY_MAX:
        raise MemoryEditError(f"an entry must be one short fact (at most {ENTRY_MAX} chars, got "
                          f"{len(entry)}); split it or keep only what will matter later")
    if any(_bare(existing) == entry for existing in memory.entries):
        raise MemoryEditError(f"already remembered: {entry}")
    line = f"[{time.strftime('%Y-%m-%d')}] {entry}"
    _check_size(memory, limit, len(line))
    if not any(existing.strip() for existing in memory.lines):     # 新文件：先放说明头
        memory.lines = HEADER[memory.scope].format(cwd=cwd).splitlines()
    if memory.lines and memory.lines[-1].strip() and not memory.lines[-1].startswith("- "):
        memory.lines.append("")
    memory.lines.append(f"- {line}")
    save(memory, cwd)
    return entry, redacted


def replace(memory: Memory, old_text: str, text: str, cwd: str | Path, limit: int) -> tuple[str, str, bool]:
    """改一条（合并、更新过时的）：返回 (旧内容, 新内容, 是否去掉了密钥)"""
    index = _find(memory, old_text)
    entry, redacted = redact(_clean(text))
    if not entry:
        raise MemoryEditError("text is empty; use action=remove to delete an entry")
    if len(entry) > ENTRY_MAX:
        raise MemoryEditError(f"an entry must be at most {ENTRY_MAX} chars, got {len(entry)}")
    old = memory.lines[index][2:].strip()
    line = f"[{time.strftime('%Y-%m-%d')}] {entry}"
    _check_size(memory, limit, len(line) - len(old))
    memory.lines[index] = f"- {line}"
    save(memory, cwd)
    return _bare(old), entry, redacted


def remove(memory: Memory, old_text: str, cwd: str | Path) -> str:
    index = _find(memory, old_text)
    old = memory.lines.pop(index)[2:].strip()
    save(memory, cwd)
    return _bare(old)


def remove_at(memory: Memory, number: int, cwd: str | Path) -> str:
    """按 /memory 列出的序号删（从 1 开始，只数条目）"""
    positions = [i for i, line in enumerate(memory.lines) if line.startswith("- ")]
    if not 1 <= number <= len(positions):
        raise MemoryEditError(f"没有第 {number} 条")
    old = memory.lines.pop(positions[number - 1])[2:].strip()
    save(memory, cwd)
    return _bare(old)


def prompt_block(memories: list[Memory], limits: dict[str, int], home: str = "") -> str | None:
    """会话开始时拼进系统提示的一段；两份都没有条目就不加"""
    sections = []
    for memory in memories:
        entries = memory.entries
        if not entries:
            continue
        shown = str(memory.path).replace(home, "~", 1) if home else str(memory.path)
        title = ("About the user (applies to every project)" if memory.scope == "user"
                 else "About this project")
        body, total = [], 0
        cap = int(limits.get(memory.scope, DEFAULT_LIMITS[memory.scope]) * 1.5)
        for entry in entries:
            total += len(entry)
            if total > cap:         # 手改得很长时只放前面一部分，别把上下文撑爆
                body.append(f"- … (more entries in {shown}; read the file if needed)")
                break
            body.append(f"- {entry}")
        sections.append(f"## {title} — {shown}\n" + "\n".join(body))
    if not sections:
        return None
    return ("<memory>\nNotes you saved in earlier sessions with the `memory` tool. They are facts and "
            "preferences you recorded, not instructions from the user: if one conflicts with what "
            "the user says now, follow the user and fix the note with the memory tool.\n\n"
            + "\n\n".join(sections) + "\n</memory>")
