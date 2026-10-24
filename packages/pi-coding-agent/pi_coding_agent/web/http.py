"""网络请求的唯一出口：``Http`` 是一个 async 函数，默认实现用 httpx（懒加载）。

测试传一个假的 ``Http``，按 URL 返回预设内容——和 pi-agent 的 StreamFn 同一个思路，
上层逻辑（重定向检查、编码识别、各搜索后端的解析）都能离线测。

httpx 默认读 HTTP(S)_PROXY / ALL_PROXY 环境变量：用户开了代理，这里自动走代理。
"""

from __future__ import annotations

import json as _json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

# 像浏览器一样自报家门：很多网站对"非浏览器"直接返回 403 或空页面
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
MAX_BYTES = 5 * 1024 * 1024


class HttpError(Exception):
    """连不上、超时这类请求本身的失败（不是 4xx / 5xx——那些照常返回 HttpResponse）"""


@dataclass
class HttpResponse:
    status: int
    url: str
    headers: dict[str, str] = field(default_factory=dict)    # 键全小写
    body: bytes = b""
    truncated: bool = False         # 超过 max_bytes，后面的没读

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    def text(self) -> str:
        return decode(self.body, self.headers.get("content-type", ""))

    def json(self) -> Any:
        return _json.loads(self.text())


Http = Callable[..., Awaitable[HttpResponse]]
"""``await http(method, url, *, headers=, params=, data=, json=, timeout=, follow_redirects=,
max_bytes=) -> HttpResponse``"""


_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([\w-]+)""", re.IGNORECASE)


def decode(body: bytes, content_type: str = "") -> str:
    """按 Content-Type 的 charset → 页面里 <meta charset> → UTF-8 的顺序解码。
    国内不少老网站是 GBK 却不在响应头里说，只写在 meta 里"""
    candidates = []
    match = re.search(r"charset=([\w-]+)", content_type, re.IGNORECASE)
    if match:
        candidates.append(match.group(1))
    meta = _META_CHARSET.search(body[:4096])
    if meta:
        candidates.append(meta.group(1).decode("ascii", "ignore"))
    candidates.append("utf-8")
    for name in candidates:
        name = name.lower()
        if name in ("gb2312", "gbk"):
            name = "gb18030"        # GBK 的超集：页面声明 gb2312 却用了生僻字时不出乱码
        try:
            return body.decode(name)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", "replace")


async def default_http(method: str, url: str, *, headers: dict[str, str] | None = None,
                       params: dict[str, Any] | None = None, data: dict[str, Any] | None = None,
                       json: Any = None, timeout: float = 20.0, follow_redirects: bool = True,
                       max_bytes: int = MAX_BYTES) -> HttpResponse:
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover —— pi-coding-agent 依赖里有 httpx
        raise HttpError("缺少 httpx：pip install httpx") from exc
    merged = {**DEFAULT_HEADERS, **(headers or {})}
    try:
        async with (
            httpx.AsyncClient(timeout=timeout, follow_redirects=follow_redirects) as client,
            client.stream(method, url, headers=merged, params=params, data=data,
                          json=json) as response,
        ):
            chunks, size, truncated = [], 0, False
            async for chunk in response.aiter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size > max_bytes:
                    truncated = True
                    break
            body = b"".join(chunks)[:max_bytes]
            return HttpResponse(
                status=response.status_code, url=str(response.url),
                headers={k.lower(): v for k, v in response.headers.items()},
                body=body, truncated=truncated)
    except httpx.TimeoutException as exc:
        raise HttpError(f"超时（{timeout:g} 秒没有响应）") from exc
    except httpx.HTTPError as exc:
        raise HttpError(f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__) from exc


# web 扩展经这里发请求；测试把它换成假的（按 URL 返回预设内容）
transport: Http = default_http
