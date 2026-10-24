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



def line_count(text: str) -> int:
    """行数；结尾的换行不算多出一行（"a\\nb\\n" 是 2 行）"""
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _fits_tail(raw: bytes, budget: int) -> str:
    """字节串末尾 budget 字节，按字符边界对齐（不留半个汉字）"""
    return raw[-budget:].decode("utf-8", "ignore") if budget > 0 else ""


def truncate_middle(
        text: str,
        max_lines: int = DEFAULT_MAX_LINES,
        max_bytes: int = DEFAULT_MAX_BYTES,
        head_ratio: float = 0.4,
        full_path: str | None = None,
) -> tuple[str, bool]:
    """超限时保留开头和结尾、截掉中间——命令输出的报错、测试汇总一般在最后，
    开头说明命令在干什么（比如编译最先报的那个错），两头都有用。

    没超限原样返回，不加任何标记。超限时按整行截（单行就超预算时才按字符截，
    不切半个字），中间换成一行说明：省略了多少、完整输出在哪、怎么看。"""
    data = text.encode("utf-8")
    if line_count(text) <= max_lines and len(data) <= max_bytes:
        return text, False
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()

    head_lines = max(int(max_lines * head_ratio), 1)
    tail_lines = max(max_lines - head_lines, 1)
    head_bytes = max(int(max_bytes * head_ratio), 1)
    tail_bytes = max(max_bytes - head_bytes, 1)

    head: list[str] = []
    used = 0
    for line in lines:
        size = len(line.encode("utf-8")) + 1
        if len(head) >= head_lines or used + size > head_bytes:
            break
        head.append(line)
        used += size
    tail: list[str] = []
    used = 0
    for line in reversed(lines[len(head):]):
        size = len(line.encode("utf-8")) + 1
        if len(tail) >= tail_lines or used + size > tail_bytes:
            break
        tail.append(line)
        used += size
    tail.reverse()

    # 一行就超过预算（压缩过的 JSON、进度条）：只好在行内按字符截，这一行的头尾都留
    whole = len(head) + len(tail)
    if not head and lines:
        head = [lines[0].encode("utf-8")[:head_bytes].decode("utf-8", "ignore")]
    if not tail and lines:
        tail = [_fits_tail(lines[-1].encode("utf-8"), tail_bytes)]

    omitted = max(len(lines) - whole - (len(head) + len(tail) - whole), 0)
    kept = len("\n".join(head + tail).encode("utf-8"))
    size = f"{(len(data) - kept) / 1024:.1f} KB"
    unit = "line" if omitted == 1 else "lines"
    note = f"[... {omitted} {unit} ({size}) omitted" if omitted else f"[... {size} omitted"
    if full_path:
        note += f"; full output: {full_path} (read it with offset/limit, or grep it)"
    note += " ...]"
    return "\n".join([*head, note, *tail]), True
