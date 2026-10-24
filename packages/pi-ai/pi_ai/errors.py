"""
服务商错误的归一化。

- describe_llm_error：HTTP 错误时把响应体里可读的那句话（和错误码）拿出来。
  只写 "HTTP 400 from …" 的话，用户和上层都不知道到底错在哪。
- is_context_overflow：识别"上下文超长"。上层据此压缩后重试，而不是直接报错结束。
  各家措辞不同，这里按已知说法匹配；限流（tokens per minute）不算超长。
- is_retryable：限流、服务端出错、过载、断网/超时——等一会儿再发同一个请求大概率能成。
  请求本身有问题（400 参数错、401 密钥错、余额不足、上下文超长）重试没用，不算。
- retry_after：服务商要求等待的秒数（Retry-After 头，经 describe_llm_error 写进错误文本）。
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
    text = f"HTTP {exc.status}: {detail}" if detail else str(exc)
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is not None:
        text += f" [retry after {retry_after:g}s]"
    return text


# 408 超时、409 冲突（Anthropic 偶发）、425、429 限流、5xx 服务端、529 过载
_RETRYABLE_STATUS = re.compile(r"^HTTP (?:408|409|425|429|5\d\d)\b")
_RETRYABLE_TEXT = re.compile(
    r"overloaded|rate.?limit|too many requests|temporarily unavailable|service unavailable"
    r"|server error|bad gateway|gateway timeout|try again"
    r"|请求超时|连不上|网络错误|服务繁忙|稍后重试",
    re.IGNORECASE,
)
# 这些是账户 / 请求本身的问题，等多久都没用
_NOT_RETRYABLE = re.compile(
    r"insufficient_quota|quota exceeded|exceeded your current quota|billing|余额不足|欠费"
    r"|invalid.?api.?key|authentication|permission denied",
    re.IGNORECASE,
)
_RETRY_AFTER = re.compile(r"\[retry after (\d+(?:\.\d+)?)s\]")


def _error_text(error: Any) -> str:
    if hasattr(error, "stop_reason"):
        if error.stop_reason != "error":
            return ""
        error = getattr(error, "error_message", "") or ""
    return str(error or "")


def is_retryable(error: Any) -> bool:
    """``error`` 可以是 AssistantMessage 或错误文本"""
    text = _error_text(error)
    if not text or is_context_overflow(text) or _NOT_RETRYABLE.search(text):
        return False
    return bool(_RETRYABLE_STATUS.match(text) or _RETRYABLE_TEXT.search(text))


def retry_after(error: Any) -> float | None:
    """服务商要求等待的秒数；没要求返回 None"""
    match = _RETRY_AFTER.search(_error_text(error))
    return float(match.group(1)) if match else None


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
