"""
针对不完整 JSON 进行尽力解析，用于流式工具调用参数。
"""

from __future__ import annotations

import json
from typing import Any


def parse_streaming_json(fragment: str) -> dict[str, Any]:
    """
    解析一段**可能被截断的 JSON 对象**，自动补齐悬空未闭合的语法结构
    """
    text = fragment.strip()
    if not text:
        return {}

    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        pass

    stack: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    repaired = text
    if in_string:
        repaired += '"'
    repaired = repaired.rstrip()
    if repaired.endswith((",", ":")):
        repaired = repaired[:-1]
        if repaired.rstrip().endswith(":"):
            repaired = repaired.rstrip()[:-1]
            cut = max(repaired.rfind('"'), 0)
            repaired = repaired[:cut].rstrip().rstrip(",")

    repaired += "".join(reversed(stack))

    try:
        value = json.loads(repaired)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}
