"""
服务商错误的归一化。

- describe_llm_error：HTTP 错误时把响应体里可读的那句话（和错误码）拿出来。
  只写 "HTTP 400 from …" 的话，用户和上层都不知道到底错在哪。
- is_context_overflow：识别"上下文超长"。上层据此压缩后重试，而不是直接报错结束。
  各家措辞不同，这里按已知说法匹配；限流（tokens per minute）不算超长。
"""

from __future__ import annotations

import json
import re
from typing import Any

from .transport import LLMError

_OVERFLOW_PATTERNS = [
    r"prompt is too long",
    r"context[_ ]length[_ ]exceeded",
    r"maximum context length",
    r"context (?:length|window|limit).{0,40}exceed",
    r"exceeds? (?:the )?(?:model'?s? )?(?:maximum )?context",
    r"exceed(?:s|ed)? context limit",
    r"input (?:is )?too long",
    r"request_too_large",
    r"reduce the length of the (?:messages|prompt|input)",
    r"too many (?:input )?tokens",
    r"(?:上下文|prompt|输入|消息|请求).{0,12}(?:超长|过长|超出|超过|太长)",
    r"(?:超出|超过).{0,8}(?:最大|上下文).{0,8}(?:长度|窗口|token)",
]
_OVERFLOW = re.compile("|".join(_OVERFLOW_PATTERNS), re.IGNORECASE)
# 限流也会说 "too many tokens"，但那是速率问题，压缩没用
_RATE_LIMIT = re.compile(r"per minute|rate.?limit|\btpm\b|\brpm\b|频率|限流", re.IGNORECASE)


def _message_from_body(body: str) -> str:
    try:
        data: Any = json.loads(body)
    except ValueError:
        return " ".join(body.split())[:500]
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        message = err.get("message") or ""
        code = err.get("code") or err.get("type") or ""
        return f"{message} ({code})" if code and str(code) not in message else str(message)
    if isinstance(err, str):
        return err
    if isinstance(data, dict) and data.get("message"):
        return str(data["message"])
    return " ".join(body.split())[:500]


def describe_llm_error(exc: LLMError) -> str:
    """一行可读的错误：``HTTP 400: prompt is too long: 210000 tokens > 200000 maximum (invalid_request_error)``"""
    if exc.status is None:
        return str(exc)
    detail = _message_from_body(exc.body or "").strip()
    return f"HTTP {exc.status}: {detail}" if detail else str(exc)


def is_context_overflow(error: Any) -> bool:
    """``error`` 可以是 AssistantMessage（看它的 stop_reason / error_message）或错误文本"""
    if hasattr(error, "stop_reason"):
        if error.stop_reason != "error":
            return False
        error = getattr(error, "error_message", "") or ""
    text = str(error or "")
    if not text:
        return False
    if text.startswith("HTTP 413"):
        return True
    return bool(_OVERFLOW.search(text)) and not _RATE_LIMIT.search(text)
