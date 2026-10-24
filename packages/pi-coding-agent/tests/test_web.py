"""上网工具测试（web_search / web_fetch）：不联网，假的 Http 按 URL 返回预设内容。

    python tests/test_web.py
    pytest tests/test_web.py

覆盖 web/markup（HTML → Markdown）、web/fetch（重定向、内网拦截、类型和编码）、
web/search（各后端的解析、顺序和兜底）、builtin_extensions/web（分段、缓存、提示、真会话里调用）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))                     # packages/pi-coding-agent
sys.path.insert(0, str(HERE.parents[2] / "pi-agent"))
sys.path.insert(0, str(HERE.parents[2] / "pi-ai"))
sys.path.insert(0, str(HERE.parent))

from fakes import FAKE_MODEL, Sandbox, fake_models  # noqa: E402
from pi_ai import TextContent, ToolCall  # noqa: E402
from pi_coding_agent import (  # noqa: E402
    AgentSession,
    ModelRuntime,
    SessionManager,
    create_agent_session_services,
)
from pi_coding_agent.web import fetch as fetch_mod  # noqa: E402
from pi_coding_agent.web import http as http_mod  # noqa: E402
from pi_coding_agent.web import search as search_mod  # noqa: E402
from pi_coding_agent.web.http import HttpError, HttpResponse, decode  # noqa: E402
from pi_coding_agent.web.markup import to_markdown  # noqa: E402

FAILURES: list[str] = []


def under_pytest() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILURES.append(f"{name}: {detail}")
    print(f"  FAIL {name} {detail}")
    if under_pytest():
        raise AssertionError(f"{name}: {detail}")


class FakeHttp:
    """按 (方法, URL 前缀) 返回预设响应；记下每次请求。值可以是 HttpResponse、异常或函数"""

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.calls: list[dict] = []

    async def __call__(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        for (m, prefix), value in self.routes.items():
            if m == method and url.startswith(prefix):
                if callable(value) and not isinstance(value, HttpResponse):
                    value = value(url, kw)
                if isinstance(value, Exception):
                    raise value
                return value
        raise HttpError(f"没有预设：{method} {url}")


def html(body: str, status: int = 200, url: str = "", charset: str = "utf-8", **headers):
    return HttpResponse(status, url, {"content-type": f"text/html; charset={charset}", **headers},
                        body.encode(charset))


def jsonr(data, status: int = 200):
    return HttpResponse(status, "", {"content-type": "application/json"},
                        json.dumps(data, ensure_ascii=False).encode())


async def public(host: str) -> list[str]:
    return ["93.184.216.34"]


ARTICLE = """<html><head><title>pi 文档</title><script>var x=1</script></head><body>
<nav><a href="/">首页</a> <a href="/about">关于</a></nav>
<aside>广告</aside>
<article><h1>安装</h1><p>运行 <code>pip install pi</code> 即可，详见 <a href="guide.html">指南</a>。</p>
<h2>配置</h2><ul><li>模型<li>密钥<ul><li>auth.json</li></ul></ul>
<pre class="language-bash">pi --model glm
pi -t high</pre>
<table><tr><th>键</th><th>说明</th></tr><tr><td>theme</td><td>深浅 | 自动</td></tr></table>
<p>""" + "正文很长。" * 60 + """</p></article>
<footer>版权所有</footer></body></html>"""


# --------------------------------------------------------------------------- #
# 1. HTML → Markdown
# --------------------------------------------------------------------------- #

def test_markup() -> None:
    title, md = to_markdown(ARTICLE, "https://docs.example.com/zh/index.html")
    check("markup: 标题取 <title>", title == "pi 文档", title)
    check("markup: 去掉导航、侧栏、页脚、脚本", not any(w in md for w in ("首页", "广告", "版权", "var x")), md[:200])
    check("markup: 标题层级", "# 安装" in md and "## 配置" in md, md[:200])
    check("markup: 相对链接补成绝对地址",
          "[指南](https://docs.example.com/zh/guide.html)" in md, md[:300])
    check("markup: 行内代码", "`pip install pi`" in md)
    check("markup: 不闭合的 li 和嵌套列表", "- 模型\n- 密钥\n  - auth.json" in md, md)
    check("markup: 代码块保留换行和语言", "```bash\npi --model glm\npi -t high\n```" in md, md)
    check("markup: 表格（竖线转义）", "| 键 | 说明 |" in md and "| theme | 深浅 \\| 自动 |" in md, md)

    _, md = to_markdown("<main><p>" + "主要内容。" * 50 + "</p></main><div>页面别处</div>")
    check("markup: 有 <main> 只取 main", "主要内容" in md and "页面别处" not in md, md[-50:])
    _, md = to_markdown("<p>没有 body 标签<p>第二段 <b> 粗 </b>结束")
    check("markup: 乱写的 HTML 也能出文字", "没有 body 标签\n\n第二段 **粗** 结束" in md, repr(md))


def test_decode() -> None:
    gbk = '<html><head><meta charset="gbk"><title>中文</title></head><body>你好</body></html>'
    text = decode(gbk.encode("gbk"), "text/html")
    check("编码: 响应头没写 charset 时看 <meta charset>", "你好" in text and "中文" in text, text)
    check("编码: 响应头的 charset 优先", decode("é".encode("latin-1"), "text/html; charset=latin-1") == "é")
    check("编码: 认不出时按 UTF-8 且不崩", decode(b"\xff\xfe abc", "") .endswith("abc"))


# --------------------------------------------------------------------------- #
# 2. 读网页
# --------------------------------------------------------------------------- #

async def case_fetch() -> None:
    http = FakeHttp({
        ("GET", "https://example.com/old"): HttpResponse(301, "https://example.com/old",
                                                         {"location": "/new"}),
        ("GET", "https://example.com/new"): html(ARTICLE, url="https://example.com/new"),
        ("GET", "https://example.com/evil"): HttpResponse(302, "", {"location": "http://127.0.0.1:8080/admin"}),
        ("GET", "https://example.com/data.json"): jsonr({"a": 1}),
        ("GET", "https://example.com/a.pdf"): HttpResponse(200, "", {"content-type": "application/pdf"}, b"%PDF"),
        ("GET", "https://example.com/403"): html("no", status=403),
        ("GET", "https://example.com/spa"): html("<html><body><div id=app></div><script>x</script></body></html>"),
        ("GET", "https://down.example.com"): HttpError("超时（20 秒没有响应）"),
    })
    page = await fetch_mod.fetch_page("example.com/old", http, resolve=public)
    check("fetch: 没写协议补 https、跟随重定向", page.url == "https://example.com/new"
          and [c["url"] for c in http.calls] == ["https://example.com/old", "https://example.com/new"],
          str([c["url"] for c in http.calls]))
    check("fetch: 自己处理重定向（每一跳都检查地址）", all(c["follow_redirects"] is False for c in http.calls))
    check("fetch: 网页转成 Markdown", page.title == "pi 文档" and "# 安装" in page.text)

    async def fails(url: str, **kw) -> str:
        try:
            await fetch_mod.fetch_page(url, http, **{"resolve": public, **kw})
        except fetch_mod.FetchError as exc:
            return exc.message + " | " + exc.hint
        return ""

    for url in ("http://localhost:3000", "http://127.0.0.1/", "http://192.168.1.5/x", "http://[::1]/",
                "http://intranet/", "http://printer.local/", "http://169.254.169.254/latest/meta-data"):
        msg = await fails(url)
        check(f"fetch: 拒绝内网 {url}", "本机或内网" in msg and "curl" in msg, msg)
    check("fetch: 只认 http / https", "只能读 http" in await fails("file:///etc/passwd"))
    check("fetch: 网址里不能带密码", "用户名密码" in await fails("https://u:p@example.com/"))
    check("fetch: 被拒绝的地址一次请求都没发", not any("127.0.0.1" in c["url"] or "localhost" in c["url"]
                                                       for c in http.calls))
    check("fetch: 公网页面 302 到本机也拦住", "本机或内网" in await fails("https://example.com/evil"))

    async def resolves_private(host: str) -> list[str]:
        return ["10.0.0.8"]

    async def resolves_fake_ip(host: str) -> list[str]:
        return ["198.18.0.42"]

    check("fetch: 域名解析到内网也拦住", "本机或内网" in await fails("https://example.com/new", resolve=resolves_private))
    check("fetch: Clash / Surge 的 fake-ip（198.18/15）不算内网",
          await fails("https://example.com/new", resolve=resolves_fake_ip) == "")
    check("fetch: 直接写 198.18 段的 IP 仍然拒绝", "本机或内网" in await fails("http://198.18.0.1/"))

    page = await fetch_mod.fetch_page("https://example.com/data.json", http, resolve=public)
    check("fetch: JSON / 文本原样返回", page.text == '{"a": 1}' and page.title == "")
    check("fetch: PDF 报不支持并说明", "不支持的内容类型" in await fails("https://example.com/a.pdf"))
    check("fetch: 403 给出换来源的提示", "HTTP 403" in (msg := await fails("https://example.com/403"))
          and "拒绝了自动访问" in msg, msg)
    check("fetch: 要跑 JavaScript 的空壳页面说清楚", "没有可读的正文" in await fails("https://example.com/spa"))
    check("fetch: 连不上时提示代理", "HTTPS_PROXY" in await fails("https://down.example.com/"))


# --------------------------------------------------------------------------- #
# 3. 搜索后端
# --------------------------------------------------------------------------- #

BING = """<html><body><ol id="b_results">
<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&&p=x&u=a1aHR0cHM6Ly9kb2NzLnB5dGhvbi5vcmcvMy8&ntb=1">Python <strong>文档</strong></a></h2>
<div class="b_caption"><p class="b_lineclamp2">官方文档的 <strong>摘要</strong></p></div></li>
<li class="b_ad"><h2><a href="https://ad.example.com">广告</a></h2></li>
<li class="b_algo"><h2><a href="https://example.com/b">第二条</a></h2><p>摘要二</p></li>
</ol></body></html>"""

DDG = """<html><body>
<div class="result results_links result--ad"><a class="result__a" href="https://ad.example.com">广告</a></div>
<div class="result results_links"><h2><a class="result__a" href="//duckduckgo.com/l/?uddg=""" + \
    quote("https://example.org/a?x=1", safe="") + """&rut=abc">示例 A</a></h2>
<a class="result__snippet">片段 A</a></div>
</body></html>"""

EXA_SSE = "event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": [
    {"type": "text", "text": "Title: 第一篇\nURL: https://a.example.com\nPublished: 2026-09-01T00:00:00Z\n"
                             "Author: N/A\nHighlights:\n内容一\n\nTitle: 第二篇\nURL: https://b.example.com\n"
                             "Published: N/A\nText: 内容二"}]}}, ensure_ascii=False)


async def case_backends() -> None:
    http = FakeHttp({
        ("POST", "https://api.bochaai.com"): jsonr({"code": 200, "data": {"webPages": {"value": [
            {"name": "博查结果", "url": "https://bocha.example.com", "snippet": "短", "summary": "长摘要",
             "datePublished": "2026-10-01T08:00:00+08:00"}]}}}),
        ("POST", "https://api.tavily.com"): jsonr({"results": [
            {"title": "T", "url": "https://t.example.com", "content": "tavily 内容"}]}),
        ("GET", "https://api.search.brave.com"): jsonr({"web": {"results": [
            {"title": "B", "url": "https://br.example.com", "description": "有 <strong>粗体</strong>",
             "page_age": "2026-08-01T00:00:00"}]}}),
        ("POST", "https://api.exa.ai"): jsonr({"results": [
            {"title": "E", "url": "https://e.example.com", "text": "exa 内容", "publishedDate": "2026-07-01"}]}),
        ("POST", "https://mcp.exa.ai"): HttpResponse(200, "", {"content-type": "text/event-stream"},
                                                     EXA_SSE.encode()),
        ("GET", "https://www.bing.com"): html(BING),
        ("POST", "https://html.duckduckgo.com"): html(DDG),
    })
    run = lambda name: search_mod.BACKENDS[name]("q", 5, http, "KEY", 5)

    r = await run("bocha")
    check("博查: 用摘要、日期取到天", r[0].title == "博查结果" and r[0].snippet == "长摘要"
          and r[0].date == "2026-10-01", str(r))
    check("博查: key 放在 Authorization", http.calls[-1]["headers"]["Authorization"] == "Bearer KEY")
    r = await run("tavily")
    check("Tavily: 解析结果", r[0].url == "https://t.example.com" and r[0].snippet == "tavily 内容")
    r = await run("brave")
    check("Brave: 去掉摘要里的 HTML 标签", r[0].snippet == "有 粗体", str(r))
    check("Brave: key 放在 X-Subscription-Token", http.calls[-1]["headers"]["X-Subscription-Token"] == "KEY")
    r = await run("exa")
    check("Exa: 解析结果", r[0].title == "E" and r[0].date == "2026-07-01")
    r = await run("exa-free")
    check("Exa 免费服务: 解析 SSE 里的 Title/URL 块", [x.url for x in r] == ["https://a.example.com",
          "https://b.example.com"] and r[0].date == "2026-09-01" and r[1].date == "", str(r))
    check("Exa 免费服务: 正文去掉 Highlights/Text 标记", r[0].snippet == "内容一" and r[1].snippet == "内容二",
          str(r))
    r = await run("bing")
    check("必应: 解开 ck/a 包装的真实地址、跳过广告", [x.url for x in r] == [
        "https://docs.python.org/3/", "https://example.com/b"], str(r))
    check("必应: 标题和摘要去掉标签", r[0].title == "Python 文档" and r[0].snippet == "官方文档的 摘要", str(r))
    r = await run("duckduckgo")
    check("DuckDuckGo: 解开 uddg 跳转、跳过广告", [x.url for x in r] == ["https://example.org/a?x=1"]
          and r[0].snippet == "片段 A", str(r))

    blocked = FakeHttp({("GET", "https://www.bing.com"): html("<p>请完成验证 captcha</p>")})
    try:
        await search_mod.bing("q", 5, blocked, "", 5)
        msg = ""
    except search_mod.SearchError as exc:
        msg = exc.message
    check("必应: 人机验证页报错而不是当成没结果", "人机验证" in msg, msg)

    bad_key = FakeHttp({("POST", "https://api.tavily.com"): jsonr({"detail": "bad"}, status=401)})
    try:
        await search_mod.tavily("q", 5, bad_key, "x", 5)
        msg = ""
    except search_mod.SearchError as exc:
        msg = exc.message
    check("401: 说明 key 无效", "key 无效" in msg, msg)


async def case_search_order() -> None:
    keys = {"tavily": "T"}
    plan = search_mod.plan_backends("auto", True, lambda n: n in keys)
    check("顺序: 配了 key 的在前，再免 key 的", plan == ["tavily", "exa-free", "bing", "duckduckgo"], str(plan))
    check("顺序: 关掉免 key 时只剩有 key 的", search_mod.plan_backends("auto", False, lambda n: n in keys) == ["tavily"])
    check("顺序: 设置里写列表就按列表，缺 key 的去掉",
          search_mod.plan_backends(["bocha", "bing", "nope"], True, lambda n: False) == ["bing"])

    search_mod._cooldown.clear()
    http = FakeHttp({
        ("POST", "https://mcp.exa.ai"): HttpError("超时"),
        ("GET", "https://www.bing.com"): html("<html><body>没有结果</body></html>"),
        ("POST", "https://html.duckduckgo.com"): html(DDG),
    })
    outcome = await search_mod.search(" 问  题 ", 5, http, backends=["exa-free", "bing", "duckduckgo"],
                                      keys=lambda n: None)
    check("兜底: 失败和空结果都换下一个", outcome.backend == "duckduckgo" and outcome.keyless
          and [a[0] for a in outcome.attempts] == ["exa-free", "bing"], str(outcome))
    check("兜底: 查询词压掉多余空白", http.calls[0]["json"]["params"]["arguments"]["query"] == "问 题")

    http.calls.clear()
    await search_mod.search("再搜", 5, http, backends=["exa-free", "bing", "duckduckgo"], keys=lambda n: None)
    check("冷却: 刚超时的后端排到最后", http.calls[0]["url"].split("/")[2] == "www.bing.com",
          str([c["url"] for c in http.calls]))

    search_mod._cooldown.clear()
    empty = FakeHttp({("POST", "https://api.tavily.com"): jsonr({"results": []})})
    outcome = await search_mod.search("q", 5, empty, backends=["tavily"], keys=lambda n: "T")
    check("没结果: 后端正常回答空，不算失败", outcome.results == [] and outcome.backend == "tavily")

    try:
        await search_mod.search("q", 5, FakeHttp({}), backends=["exa-free", "bing"], keys=lambda n: None)
        msg = ""
    except search_mod.SearchError as exc:
        msg = exc.message
    check("全失败: 列出每个后端的原因", "exa-free" in msg and "bing" in msg, msg)
    search_mod._cooldown.clear()


# --------------------------------------------------------------------------- #
# 4. 扩展：真会话里调用
# --------------------------------------------------------------------------- #

def call(call_id: str, name: str, **args) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=args)


async def case_extension() -> None:
    long_page = "<article><h1>长文</h1>" + "".join(f"<p>第{i}段。" + "字" * 80 + "</p>" for i in range(60)) \
        + "</article>"
    http = FakeHttp({
        ("GET", "https://example.com/long"): html(long_page),
        ("GET", "https://www.bing.com"): html(BING),
        ("POST", "https://mcp.exa.ai"): HttpError("超时"),
        ("POST", "https://html.duckduckgo.com"): html(DDG),
        ("POST", "https://api.bochaai.com"): jsonr({"code": 200, "data": {"webPages": {"value": [
            {"name": "博查", "url": "https://bocha.example.com", "snippet": "s"}]}}}),
    })
    real_transport, real_resolve = http_mod.transport, fetch_mod.resolve_host
    http_mod.transport, fetch_mod.resolve_host = http, public
    search_mod._cooldown.clear()
    try:
        with Sandbox() as cwd:
            (cwd / ".pi").mkdir()
            (cwd / ".pi" / "settings.json").write_text(json.dumps({"web": {"fetch": {"maxChars": 1500}}}))
            script = [
                [call("s1", "web_search", query="python 文档")],
                [call("f1", "web_fetch", url="https://example.com/long")],
                [call("f2", "web_fetch", url="https://example.com/long", start=1500)],
                [call("f3", "web_fetch", url="http://localhost:8000")],
                [call("s2", "web_search", query="再搜一次")],
                [TextContent(text="好了")],
            ]
            models, _ = fake_models(script)
            runtime = ModelRuntime(models, cwd / "agent")
            services = create_agent_session_services(cwd=cwd, agent_dir=cwd / "agent", model_runtime=runtime)
            session = AgentSession(services, SessionManager.in_memory(cwd), model=FAKE_MODEL, tools=["read"])
            names = {t.name for t in session.agent.state.tools}
            check("扩展: 注册 web_search / web_fetch", {"web_search", "web_fetch"} <= names, str(names))
            ended = {}      # 从事件里拿结果：上下文小，旧的工具输出会被 L1 清理成占位
            session.subscribe(lambda e: ended.__setitem__(e.tool_call_id, e.result)
                              if e.type == "tool_execution_end" else None)
            await session.prompt("查一下")
            results = {k: SimpleNamespace(content=v.content, details=v.details, is_error=v.is_error)
                       for k, v in ended.items()}

            s1 = results["s1"].content[0].text
            check("搜索: 没 key 时用免 key 兜底，结果带编号、链接", "· duckduckgo ·" not in s1 and "1. Python 文档"
                  in s1 and "https://docs.python.org/3/" in s1, s1)
            check("搜索: 第一次用免 key 时提示配 key", "auth.json" in s1 and "bocha" in s1, s1)
            check("搜索: details 给界面用", results["s1"].details["count"] == 2
                  and results["s1"].details["backend"] == "bing", str(results["s1"].details))

            f1 = results["f1"].content[0].text
            check("读网页: 标注外部内容不是用户指令", "不是用户说的" in f1, f1[:200])
            check("读网页: 按 maxChars 分段并告诉模型怎么接着读", "start=1500" in f1
                  and results["f1"].details["end"] == 1500, f1[:300])
            check("读网页: 同一地址第二次走缓存", sum(c["url"] == "https://example.com/long" for c in http.calls) == 1,
                  str([c["url"] for c in http.calls]))
            f2 = results["f2"]
            check("读网页: start 接着读", f2.details["start"] == 1500 and "第 1500–3000 字" in f2.content[0].text,
                  f2.content[0].text[:200])
            check("读网页: 本机地址报错并指路 bash", results["f3"].is_error
                  and "curl" in results["f3"].content[0].text, results["f3"].content[0].text)
            check("搜索: 提示只说一次", "auth.json" not in results["s2"].content[0].text)

            # 配了 key：走博查，不再用免 key 的
            models.credentials.set("bocha", "BK")
            http.calls.clear()
            script2 = [[call("s3", "web_search", query="有 key")], [TextContent(text="好")]]
            models2, _ = fake_models(script2)
            models2.credentials.set("bocha", "BK")
            runtime2 = ModelRuntime(models2, cwd / "agent")
            services2 = create_agent_session_services(cwd=cwd, agent_dir=cwd / "agent", model_runtime=runtime2)
            session2 = AgentSession(services2, SessionManager.in_memory(cwd), model=FAKE_MODEL, tools=[])
            await session2.prompt("查")
            s3 = next(m for m in session2.messages if getattr(m, "role", "") == "toolResult")
            check("搜索: 配了 key 优先用 API", s3.details["backend"] == "bocha" and not s3.details["keyless"]
                  and [c["url"] for c in http.calls] == ["https://api.bochaai.com/v1/web-search"],
                  str([c["url"] for c in http.calls]))
            check("搜索: 用 API 时不提示配 key", "auth.json" not in s3.content[0].text)
    finally:
        http_mod.transport, fetch_mod.resolve_host = real_transport, real_resolve
        search_mod._cooldown.clear()


def test_permissions_and_plan() -> None:
    from pi_coding_agent.builtin_extensions import permissions, plan_mode
    check("权限: 两个工具都直接放行", {"web_search", "web_fetch"} <= permissions.SAFE_TOOLS)
    check("计划模式: 调研可以上网", set(plan_mode.RESEARCH) == {"web_search", "web_fetch"})


# --------------------------------------------------------------------------- #
# pytest 兼容层
# --------------------------------------------------------------------------- #

def test_fetch() -> None:
    asyncio.run(case_fetch())


def test_backends() -> None:
    asyncio.run(case_backends())


def test_search_order() -> None:
    asyncio.run(case_search_order())


def test_extension() -> None:
    asyncio.run(case_extension())


async def main() -> int:
    print("HTML → Markdown"); test_markup(); test_decode()
    print("读网页");           await case_fetch()
    print("搜索后端");         await case_backends()
    print("顺序与兜底");       await case_search_order()
    print("扩展");             await case_extension(); test_permissions_and_plan()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} 处失败：")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("全部通过 —— 上网工具可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
