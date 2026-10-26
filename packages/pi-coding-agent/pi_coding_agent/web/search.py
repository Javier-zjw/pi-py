"""网页搜索：一串后端按顺序试，前一个失败换下一个（思路同 Agent-Reach 的"首选 + 备选"）。

两类后端：
- **要 key 的搜索 API**（稳定、结果干净）：bocha（博查，国内）、tavily、brave、exa。
  key 和模型的 key 放在一起：``~/.pi/agent/auth.json`` 里 ``{"bocha": {"apiKey": "..."}}``，
  或环境变量 ``BOCHA_API_KEY`` / ``TAVILY_API_KEY`` / ``BRAVE_API_KEY`` / ``EXA_API_KEY``。
- **免 key 的兜底**：exa-free（Exa 公开的免费 MCP 服务）、bing、duckduckgo（解析网页版结果页）。
  开箱就能用，但会限流、页面改版就失效，不保证稳定。

默认顺序（``web.search.backends = "auto"``）：配了 key 的 API 在前，再试免 key 的
（``web.search.keyless = false`` 时不试）。也可以在设置里直接写一个后端名列表。
连不上 / 限流的免 key 后端冷却几分钟、先试别的——国内连不上某个服务时不用每次都等它超时。
"""

from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlsplit

from .http import Http, HttpError, HttpResponse
from .markup import Node, parse

KEY_BACKENDS = ("bocha", "tavily", "brave", "exa")
KEYLESS_BACKENDS = ("exa-free", "bing", "duckduckgo")
COOLDOWN = 300.0        # 免 key 后端失败后先跳过几秒
SNIPPET_CHARS = 300


class SearchError(Exception):
    """一个后端的失败。final=True 表示换后端也没用（例如查询词为空）"""

    def __init__(self, message: str, final: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.final = final


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    date: str = ""

    def as_dict(self) -> dict:
        return {"title": self.title, "url": self.url, "snippet": self.snippet, "date": self.date}


@dataclass
class SearchOutcome:
    results: list[SearchResult]
    backend: str = ""
    keyless: bool = False
    attempts: list[tuple[str, str]] = field(default_factory=list)   # (后端, 失败原因)


Backend = Callable[[str, int, Http, str, float], Awaitable[list[SearchResult]]]
_cooldown: dict[str, float] = {}


def _clean(text: object, limit: int = SNIPPET_CHARS) -> str:
    text = re.sub(r"<[^>]+>", "", str(text or ""))      # Brave 等会在摘要里加 <strong>
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _check(response: HttpResponse, name: str) -> None:
    if response.status in (401, 403):
        raise SearchError(f"{name} 拒绝了请求（HTTP {response.status}）：key 无效或没有权限")
    if response.status == 429:
        raise SearchError(f"{name} 限流了（HTTP 429）")
    if response.status >= 400:
        raise SearchError(f"{name} 出错（HTTP {response.status}）：{_clean(response.text(), 200)}")


def _json(response: HttpResponse, name: str) -> dict:
    _check(response, name)
    try:
        data = response.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise SearchError(f"{name} 返回的不是 JSON") from exc
    return data if isinstance(data, dict) else {}


# -- 要 key 的 API -------------------------------------------------------- #

async def bocha(query: str, limit: int, http: Http, key: str, timeout: float) -> list[SearchResult]:
    response = await http("POST", "https://api.bochaai.com/v1/web-search", timeout=timeout,
                          headers={"Authorization": f"Bearer {key}"},
                          json={"query": query, "count": limit, "summary": True})
    data = _json(response, "博查")
    if data.get("code") not in (None, 200, "200"):
        raise SearchError(f"博查出错：{_clean(data.get('msg') or data.get('message'), 200)}")
    pages = ((data.get("data") or data).get("webPages") or {}).get("value") or []
    return [SearchResult(_clean(p.get("name"), 120), p.get("url", ""),
                         _clean(p.get("summary") or p.get("snippet")),
                         (p.get("datePublished") or p.get("dateLastCrawled") or "")[:10])
            for p in pages if p.get("url")]


async def tavily(query: str, limit: int, http: Http, key: str, timeout: float) -> list[SearchResult]:
    response = await http("POST", "https://api.tavily.com/search", timeout=timeout,
                          headers={"Authorization": f"Bearer {key}"},
                          json={"query": query, "max_results": limit})
    data = _json(response, "Tavily")
    return [SearchResult(_clean(r.get("title"), 120), r.get("url", ""), _clean(r.get("content")),
                         (r.get("published_date") or "")[:10])
            for r in data.get("results") or [] if r.get("url")]


async def brave(query: str, limit: int, http: Http, key: str, timeout: float) -> list[SearchResult]:
    response = await http("GET", "https://api.search.brave.com/res/v1/web/search", timeout=timeout,
                          headers={"X-Subscription-Token": key, "Accept": "application/json"},
                          params={"q": query, "count": limit})
    data = _json(response, "Brave")
    return [SearchResult(_clean(r.get("title"), 120), r.get("url", ""), _clean(r.get("description")),
                         _clean(r.get("page_age") or r.get("age") or "", 10))
            for r in (data.get("web") or {}).get("results") or [] if r.get("url")]


async def exa(query: str, limit: int, http: Http, key: str, timeout: float) -> list[SearchResult]:
    response = await http("POST", "https://api.exa.ai/search", timeout=timeout,
                          headers={"x-api-key": key},
                          json={"query": query, "numResults": limit,
                                "contents": {"text": {"maxCharacters": SNIPPET_CHARS}}})
    data = _json(response, "Exa")
    return [SearchResult(_clean(r.get("title"), 120), r.get("url", ""), _clean(r.get("text")),
                         (r.get("publishedDate") or "")[:10])
            for r in data.get("results") or [] if r.get("url")]


# -- 免 key ---------------------------------------------------------------- #

_EXA_FIELD = re.compile(r"^(Title|URL|Published Date|Published|Author):\s*(.*)$")


async def exa_free(query: str, limit: int, http: Http, key: str, timeout: float) -> list[SearchResult]:
    """Exa 公开的免费 MCP 服务（不要 key，有频率限制）：JSON-RPC 调 web_search_exa"""
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "web_search_exa", "arguments": {"query": query, "numResults": limit}}}
    response = await http("POST", "https://mcp.exa.ai/mcp", timeout=timeout, json=body,
                          headers={"Accept": "application/json, text/event-stream"})
    _check(response, "Exa 免费服务")
    raw = response.text()
    # 可能是 SSE（data: {...}）也可能是一个 JSON
    payloads = [line[5:].strip() for line in raw.splitlines() if line.startswith("data:")] or [raw]
    try:
        message = json.loads(payloads[-1])
    except ValueError as exc:
        raise SearchError("Exa 免费服务返回了看不懂的内容") from exc
    if message.get("error"):
        raise SearchError(f"Exa 免费服务：{_clean(message['error'].get('message'), 200)}")
    result = message.get("result") or {}
    text = "\n".join(c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text")
    if result.get("isError"):
        raise SearchError(f"Exa 免费服务：{_clean(text, 200)}")
    results = []
    for block in re.split(r"\n(?=Title: )", "\n" + text.strip()):
        fields, rest = {}, []
        for line in block.strip().splitlines():
            match = _EXA_FIELD.match(line)
            if match and not rest:
                fields[match.group(1).split()[0].lower()] = match.group(2).strip()
            else:   # 正文前的 "Highlights:" / "Text:" 标记（单独一行或和内容同一行）
                rest.append(re.sub(r"^\s*(Highlights|Text):\s*", "", line))
        if fields.get("url"):
            date = fields.get("published", "")
            results.append(SearchResult(_clean(fields.get("title"), 120), fields["url"],
                                        _clean("\n".join(rest)), "" if date == "N/A" else date[:10]))
    if not results and text.strip():
        raise SearchError("Exa 免费服务返回了看不懂的内容")
    return results


def _bing_url(href: str) -> str:
    """必应有时把结果链接包成 bing.com/ck/a?...&u=a1<base64>，解开拿真实地址"""
    parts = urlsplit(href)
    if parts.netloc.endswith("bing.com") and parts.path.startswith("/ck/"):
        u = parse_qs(parts.query).get("u", [""])[0]
        if u.startswith("a1"):
            try:
                return base64.urlsafe_b64decode(u[2:] + "=" * (-len(u[2:]) % 4)).decode()
            except (ValueError, UnicodeDecodeError):
                pass
    return href


def _no_results_or_blocked(root: Node, name: str) -> None:
    text = root.text().lower()
    if any(word in text for word in ("captcha", "验证", "unusual traffic", "are you a robot")):
        raise SearchError(f"{name} 要求人机验证（被当成机器人了）")


async def bing(query: str, limit: int, http: Http, key: str, timeout: float) -> list[SearchResult]:
    response = await http("GET", "https://www.bing.com/search", timeout=timeout,
                          params={"q": query, "count": max(limit, 10)})
    _check(response, "必应")
    root = parse(response.text())
    results = []
    for item in root.find_all("li", "b_algo"):
        heading = item.find("h2")
        link = heading.find("a") if heading else None
        if not link or not link.attrs.get("href", "").startswith("http"):
            continue
        caption = item.find(cls="b_caption") or item
        snippet = caption.find("p") or caption.find(cls="b_lineclamp2") or caption
        results.append(SearchResult(_clean(link.text(), 120), _bing_url(link.attrs["href"]),
                                    _clean(snippet.text())))
    if not results:
        _no_results_or_blocked(root, "必应")
    return results[:limit]


def _ddg_url(href: str) -> str:
    """DuckDuckGo 网页版的结果链接是 //duckduckgo.com/l/?uddg=<真实地址>"""
    if href.startswith("//"):
        href = "https:" + href
    parts = urlsplit(href)
    if parts.netloc.endswith("duckduckgo.com") and parts.path.startswith("/l/"):
        target = parse_qs(parts.query).get("uddg", [""])[0]
        return unquote(target) if target else href
    return href


async def duckduckgo(query: str, limit: int, http: Http, key: str,
                     timeout: float) -> list[SearchResult]:
    response = await http("POST", "https://html.duckduckgo.com/html/", timeout=timeout,
                          data={"q": query})
    _check(response, "DuckDuckGo")
    root = parse(response.text())
    results = []
    for item in root.find_all(cls="result"):
        if "result--ad" in item.classes():
            continue
        link = item.find("a", "result__a")
        if not link or not link.attrs.get("href"):
            continue
        snippet = item.find(cls="result__snippet")
        results.append(SearchResult(_clean(link.text(), 120), _ddg_url(link.attrs["href"]),
                                    _clean(snippet.text() if snippet else "")))
    if not results:
        _no_results_or_blocked(root, "DuckDuckGo")
    return results[:limit]


BACKENDS: dict[str, Backend] = {
    "bocha": bocha, "tavily": tavily, "brave": brave, "exa": exa,
    "exa-free": exa_free, "bing": bing, "duckduckgo": duckduckgo,
}


def plan_backends(setting: object, keyless: bool, has_key: Callable[[str], bool]) -> list[str]:
    """要试的后端，按顺序。setting 是 "auto" 或一个后端名列表"""
    if isinstance(setting, list) and setting:
        names = [str(n) for n in setting if str(n) in BACKENDS]
        return [n for n in names if n not in KEY_BACKENDS or has_key(n)]
    names = [n for n in KEY_BACKENDS if has_key(n)]
    return names + (list(KEYLESS_BACKENDS) if keyless else [])


async def search(query: str, limit: int, http: Http, *, backends: list[str],
                 keys: Callable[[str], str | None], timeout: float = 12.0) -> SearchOutcome:
    query = " ".join((query or "").split())
    if not query:
        raise SearchError("query 是空的", final=True)
    if not backends:
        raise SearchError("没有可用的搜索后端：配置一个搜索 API 的 key，或打开 web.search.keyless",
                          final=True)
    now = time.monotonic()
    # 冷却中的免 key 后端排到最后（不是跳过：别的都失败时还是要试一下）
    order = sorted(backends, key=lambda n: _cooldown.get(n, 0) > now)
    outcome = SearchOutcome(results=[])
    empty = ""
    for name in order:
        try:
            results = await BACKENDS[name](query, limit, http, keys(name) or "", timeout)
        except (SearchError, HttpError) as exc:
            outcome.attempts.append((name, getattr(exc, "message", None) or str(exc)))
            if name in KEYLESS_BACKENDS:
                _cooldown[name] = time.monotonic() + COOLDOWN
            continue
        except Exception as exc:  # noqa: BLE001 —— 某个后端改版解析崩了：换下一个
            outcome.attempts.append((name, f"{type(exc).__name__}: {exc}"))
            continue
        _cooldown.pop(name, None)
        results = [r for r in results if r.url][:limit]
        if not results:
            # 可能真的搜不到，也可能是结果页改版解析不出来：记下来，接着问下一个
            outcome.attempts.append((name, "没有结果"))
            empty = empty or name
            continue
        outcome.results, outcome.backend = results, name
        outcome.keyless = name in KEYLESS_BACKENDS
        return outcome
    if empty:   # 有后端正常回答了"没有结果"：就是搜不到，不算失败
        outcome.backend, outcome.keyless = empty, empty in KEYLESS_BACKENDS
        return outcome
    reasons = "；".join(f"{name}：{why}" for name, why in outcome.attempts)
    raise SearchError(f"所有搜索后端都失败了（{reasons}）")
