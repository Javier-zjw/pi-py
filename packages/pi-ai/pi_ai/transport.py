"""
**HTTP 传输层。**
各个服务商适配器依赖这份抽象协议（Protocol），而非直接硬编码依赖 `httpx`。
因此整套上层代码可以直接导入运行（并且能用伪造传输层做单元测试），**无需预先安装任何网络组件**。
`httpx` 在默认实现内部采用懒加载导入。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol


class LLMError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str | None = None,
                 retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.retry_after = retry_after      # 服务商在 Retry-After 头里要求等待的秒数


def parse_retry_after(value: str | None) -> float | None:
    """Retry-After 头：秒数，或 HTTP 日期。解析不了返回 None"""
    if not value:
        return None
    value = value.strip()
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        from datetime import datetime, timezone
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max((when - datetime.now(timezone.utc)).total_seconds(), 0.0)
    except (TypeError, ValueError, IndexError):
        return None


@dataclass
class SSEEvent:
    event: str | None
    data: dict[str, Any]


class HttpTransport(Protocol):
    def stream_sse(
            self,
            url: str,
            headers: dict[str, str],
            payload: dict[str, Any],
            timeout: float = 600.0
    ) -> AsyncIterator[SSEEvent]: ...


class HttpxTransport:

    def __init__(self, client: Any = None) -> None:
        self._client = client

    async def stream_sse(
            self,
            url: str,
            headers: dict[str, str],
            payload: dict[str, Any],
            timeout: float = 600.0
    ) -> AsyncIterator[SSEEvent]:
        try:
            import httpx
        except ImportError as exc:
            raise LLMError("httpx is required for network calls: pip install httpx") from exc

        client = self._client or httpx.AsyncClient(timeout=timeout)
        owns_client = self._client is None
        try:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    raise LLMError(
                        f"HTTP {response.status_code} from {url}", response.status_code, body,
                        retry_after=parse_retry_after(response.headers.get("retry-after")),
                    )

                event_name: str | None = None
                data_lines: list[str] = []
                async for raw in response.aiter_lines():
                    line = raw.rstrip("\r")

                    if not line:
                        if data_lines:
                            payload_text = "\n".join(data_lines)
                            data_lines = []
                            if payload_text.strip() == "[DONE]":
                                return
                            try:
                                yield SSEEvent(event_name, json.loads(payload_text))
                            except json.JSONDecodeError:
                                pass
                        event_name = None
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())

        except httpx.TimeoutException as exc:
            raise LLMError(f"请求超时（{timeout:.0f}s") from exc
        except httpx.ConnectError as exc:
            raise LLMError(f"连不上 {url}：检查网络，或 base_url 是否写错") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"网络错误 {type(exc).__name__}：{exc}") from exc
        finally:
            if owns_client:
                await client.aclose()


def default_transport() -> HttpxTransport:
    return HttpxTransport()
