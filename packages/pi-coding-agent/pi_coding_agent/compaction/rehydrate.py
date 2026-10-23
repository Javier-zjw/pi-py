"""
L3：压缩后补回关键上下文。

刚改过的文件一旦被摘要掉，模型最容易出错：拿旧内容去 edit 匹配失败，或者重复修改。
所以摘要之后附上"压缩那一刻"最近修改文件的当前内容（有总量上限），
以及扩展提供的上下文（比如计划模式的当前计划）。

- 保留尾部里已经读过 / 改过的文件不重复附：模型能直接看到。
- 读不了的（不存在、二进制、过大）跳过，只列路径。
- 附的是磁盘当前内容，不是对话里某一刻的快照。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pi_agent import AgentMessage
from pi_ai import ToolResultMessage

from ..text import sanitize
from .budget import estimate_text_tokens
from .state import _relative

MAX_FILE_BYTES = 512 * 1024


@dataclass
class Rehydration:
    text: str = ""
    files: list[str] = field(default_factory=list)       # 附了内容的
    listed: list[str] = field(default_factory=list)      # 只列了路径的


def _touched_paths(messages: Sequence[AgentMessage], tools: tuple[str, ...]) -> list[str]:
    """按时间倒序（最近的在前）去重"""
    out: list[str] = []
    for m in reversed(messages):
        if (
                isinstance(m, ToolResultMessage)
                and m.tool_name in tools
                and not m.is_error
                and isinstance(m.details, dict)
                and m.details.get("path")
        ):
            path = str(m.details["path"])
            if path not in out:
                out.append(path)
    return out


def recent_modified_files(
        summarized: Sequence[AgentMessage], retained: Sequence[AgentMessage]
) -> list[str]:
    """被压缩区间里改过、且保留尾部里没再碰过的文件，最近的在前（绝对路径或原样路径）"""
    visible = set(_touched_paths(retained, ("read", "write", "edit")))
    return [p for p in _touched_paths(summarized, ("write", "edit")) if p not in visible]


def _read(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:8192]:
        return None
    try:
        return sanitize(data.decode("utf-8"))
    except UnicodeDecodeError:
        return None


def _fit(text: str, budget_tokens: int) -> tuple[str, bool]:
    """按行截到预算内，返回 (内容, 是否截断)"""
    if estimate_text_tokens(text) <= budget_tokens:
        return text, False
    lines: list[str] = []
    used = 0
    for line in text.splitlines():
        cost = estimate_text_tokens(line) + 1
        if used + cost > budget_tokens:
            break
        lines.append(line)
        used += cost
    return "\n".join(lines), True


def build_rehydration(
        paths: Sequence[str],
        cwd: str | None,
        max_files: int,
        max_tokens: int,
        extra_sections: Sequence[str] = (),
) -> Rehydration:
    result = Rehydration()
    sections: list[str] = []
    remaining = max_tokens - sum(estimate_text_tokens(s) for s in extra_sections)
    base = Path(cwd) if cwd else Path.cwd()

    blocks: list[str] = []
    for raw in paths:
        shown = _relative(raw, cwd)
        if len(result.files) >= max_files or remaining < 200:
            result.listed.append(shown)
            continue
        content = _read(Path(raw) if Path(raw).is_absolute() else base / raw)
        if content is None:
            result.listed.append(shown)
            continue
        # 单个文件最多占剩余预算的一半，给后面的文件留余地
        body, truncated = _fit(content, min(remaining, max_tokens // 2) - 40)
        if not body.strip():
            result.listed.append(shown)
            continue
        suffix = Path(shown).suffix.lstrip(".")
        block = f"### {shown}\n```{suffix}\n{body}\n```"
        if truncated:
            block += "\n（文件较长，只附了开头部分；需要时用 read 查看其余内容）"
        blocks.append(block)
        result.files.append(shown)
        remaining -= estimate_text_tokens(block)

    if blocks:
        sections.append("## 最近修改的文件（压缩时的磁盘内容）\n\n" + "\n\n".join(blocks))
    if result.listed:
        sections.append("## 其余最近修改的文件（未附内容，需要时 read）\n"
                        + "\n".join(f"- {p}" for p in result.listed))
    sections += [s.strip() for s in extra_sections if s and s.strip()]
    if sections:
        result.text = (
            "<recent_context>\n以下内容由系统在压缩时附上，帮助你接续工作。\n\n"
            + "\n\n".join(sections)
            + "\n</recent_context>"
        )
    return result
