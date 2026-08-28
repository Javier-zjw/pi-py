"""
输出截断逻辑。
工具返回内容是导致智能体上下文窗口溢出的首要原因，因此所有工具输出统一经过此处处理。
截断提示信息具备可执行指引：告知模型如何获取剩余内容。
"""

from __future__ import annotations

DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50_000

def truncate_tail(
        text: str,
        max_lines: int = DEFAULT_MAX_LINES,
        max_bytes: int = DEFAULT_MAX_BYTES,
        hint: str = ""
) -> tuple[str, bool]:
    """保留头部内容，丢弃尾部内容。"""
    truncated = False
    lines = text.split("\n")
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True
    out = "\n".join(lines)
    if len(out.encode("utf-8")) > max_bytes:
        out = out.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")
        truncated = True
    if truncated:
        out += f"\n\n[truncated: showing first {len(out.split(chr(10)))} lines"
        out += f". {hint}]" if hint else "]"

    return out, truncated

def truncate_head(
        text: str,
        max_lines: int = DEFAULT_MAX_LINES,
        max_bytes: int = DEFAULT_MAX_BYTES,
) -> [str, bool]:
    """保留尾部内容，丢弃头部内容——适用于结果末尾信息更为关键的命令输出场景。"""
    truncated = False
    lines = text.split("\n")
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        truncated = True
    out = "\n".join(lines)
    encoded = out.encode("utf-8")
    if len(encoded) > max_bytes:
        out = encoded[-max_bytes:].decode("utf-8", "ignore")
        truncated = True
    if truncated:
        out = "[earlier output truncated]\n" + out

    return out, truncated

