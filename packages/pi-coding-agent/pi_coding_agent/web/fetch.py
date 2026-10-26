"""读一个网页：取回 → 识别类型和编码 → 网页转 Markdown、文本原样。

安全边界：web_fetch 不经过权限确认（只读），所以不让它碰本机和内网——
localhost、私有 / 链路本地地址、不带点的主机名一律拒绝，每一跳重定向都重新检查
（防止公网页面 302 到 http://127.0.0.1:xxxx）。真要读本机的开发服务器，用 bash 的 curl，
那会经过权限确认。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from .http import Http, HttpError, default_http
from .markup import to_markdown

MAX_REDIRECTS = 5
TEXT_TYPES = ("text/plain", "text/markdown", "text/x-markdown", "application/json",
              "text/csv", "application/xml", "text/xml", "application/javascript",
              "text/javascript", "text/css", "application/x-yaml", "text/yaml")
HTML_TYPES = ("text/html", "application/xhtml+xml")

Resolver = Callable[[str], Awaitable[list[str]]]
# Clash / Surge 的 fake-ip 模式把所有域名解析到这一段（真实连接由代理去做）：
# DNS 结果落在这里不算内网，否则开着代理的用户一个网页都读不了。直接写这段 IP 仍然拒绝
FAKE_IP = ipaddress.ip_network("198.18.0.0/15")


class FetchError(Exception):
    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


@dataclass
class Page:
    url: str            # 重定向之后的最终地址
    title: str
    text: str           # Markdown（网页）或原文（文本）
    content_type: str
    truncated: bool = False


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise FetchError("url 是空的")
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        raise FetchError(f"只能读 http / https 网页，不支持 {parts.scheme}:")
    if not parts.hostname:
        raise FetchError(f"网址不完整：{url}")
    if parts.username or parts.password:
        raise FetchError("网址里不能带用户名密码")
    return url


async def resolve_host(host: str) -> list[str]:
    """主机名 → IP 列表；解析失败返回空（交给请求本身去报错）"""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return []
    return [info[4][0] for info in infos]


def _private_ip(text: str, resolved: bool = False) -> bool:
    try:
        ip = ipaddress.ip_address(text.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if resolved and ip in FAKE_IP:
        return False
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified)


async def check_public(url: str, resolve: Resolver = resolve_host) -> None:
    """不是公网地址就抛 FetchError"""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    hint = "本机和内网地址请用 bash（curl），那会经过权限确认"
    if (host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan"))
            or ("." not in host and ":" not in host)):
        raise FetchError(f"不能读本机或内网地址：{host}", hint)
    if _private_ip(host) or any(_private_ip(ip, resolved=True) for ip in await resolve(host)):
        raise FetchError(f"不能读本机或内网地址：{host}", hint)


async def fetch_page(url: str, http: Http = default_http, *, timeout: float = 20.0,
                     resolve: Resolver = resolve_host) -> Page:
    url = normalize_url(url)
    for _ in range(MAX_REDIRECTS + 1):
        await check_public(url, resolve)
        try:
            response = await http("GET", url, timeout=timeout, follow_redirects=False)
        except HttpError as exc:
            raise FetchError(f"连不上 {urlsplit(url).hostname}：{exc}",
                             "检查网址；需要代理的话设置 HTTPS_PROXY 环境变量") from exc
        if response.status in (301, 302, 303, 307, 308) and response.headers.get("location"):
            url = normalize_url(urljoin(url, response.headers["location"]))
            continue
        break
    else:
        raise FetchError(f"重定向超过 {MAX_REDIRECTS} 次")

    if response.status >= 400:
        hint = {
            401: "这个页面要登录才能看",
            403: "网站拒绝了自动访问（反爬或要登录），换一个来源",
            404: "页面不存在，核对网址",
            429: "网站限流了，过一会儿再试或换一个来源",
        }.get(response.status, "网站暂时出错，可以换一个来源" if response.status >= 500 else "")
        raise FetchError(f"HTTP {response.status}", hint)

    kind = response.content_type
    if kind in HTML_TYPES or (not kind and response.body.lstrip()[:1] == b"<"):
        title, text = to_markdown(response.text(), response.url or url)
        if not text.strip():
            raise FetchError("网页里没有可读的正文",
                             "可能是要运行 JavaScript 才显示内容的页面，换一个来源")
    elif kind.startswith("text/") or kind in TEXT_TYPES or kind.endswith(("+json", "+xml")):
        title, text = "", response.text()
    else:
        raise FetchError(f"不支持的内容类型：{kind or '未知'}",
                         "只能读网页和文本；PDF、图片、压缩包等读不了")
    return Page(url=response.url or url, title=title, text=text, content_type=kind or "text/html",
                truncated=response.truncated)
