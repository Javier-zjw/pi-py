"""HTML → 一棵简单的树 → Markdown。只用标准库（html.parser），打包后不用装任何东西。

读网页时给模型的是"正文的 Markdown"：
- 去掉脚本、样式、导航、页脚、侧栏这些不是正文的部分；
- 页面里有 <article> / <main> 时只取它（多数文章页、文档页的正文都包在里面）；
- 标题、段落、列表、表格、代码块、链接、强调都转成 Markdown，链接按页面地址补成绝对地址。

不追求完整的 HTML 规范：常见的"不闭合"（<p>、<li>、<td>）按浏览器的习惯补上，
其余乱写的标签尽量不崩、内容不丢。搜索结果页的解析（search.py）也复用这棵树。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
        "source", "track", "wbr"}
# 整个子树都不要：不是给人读的内容
DROP = {"script", "style", "noscript", "template", "svg", "canvas", "iframe", "object", "head",
        "button", "select", "textarea", "form", "dialog"}
# 页面框架：导航、页脚、侧栏
CHROME = {"nav", "footer", "aside"}
BLOCKS = {"p", "div", "section", "article", "main", "header", "figure", "figcaption", "address",
          "details", "summary", "center", "body", "html", "fieldset", "hgroup"}
HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
# 开始这些标签时，栈顶是哪些就先关掉（浏览器对"不闭合"的处理）
AUTO_CLOSE = {
    "li": {"li", "p"}, "dt": {"dt", "dd", "p"}, "dd": {"dt", "dd", "p"},
    "tr": {"tr", "td", "th", "p"}, "td": {"td", "th", "p"}, "th": {"td", "th", "p"},
    "option": {"option"},
}
CLOSES_P = BLOCKS | set(HEADINGS) | {"ul", "ol", "dl", "table", "pre", "blockquote", "hr"}


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Node | str] = field(default_factory=list)

    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def iter(self):
        """先序遍历所有元素（含自己）"""
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.iter()

    def find_all(self, tag: str | None = None, cls: str | None = None) -> list[Node]:
        return [n for n in self.iter() if (tag is None or n.tag == tag)
                and (cls is None or cls in n.classes())]

    def find(self, tag: str | None = None, cls: str | None = None) -> Node | None:
        found = self.find_all(tag, cls)
        return found[0] if found else None

    def text(self) -> str:
        """纯文字，空白压成一个空格"""
        parts: list[str] = []

        def walk(node: Node) -> None:
            if node.tag in DROP:
                return
            for child in node.children:
                if isinstance(child, str):
                    parts.append(child)
                else:
                    if child.tag == "br" or child.tag in CLOSES_P:
                        parts.append(" ")
                    walk(child)

        walk(self)
        return " ".join("".join(parts).split())


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        closes = AUTO_CLOSE.get(tag, set()) | ({"p"} if tag in CLOSES_P else set())
        while len(self.stack) > 1 and self.stack[-1].tag in closes:
            self.stack.pop()
        node = Node(tag, {k: v or "" for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Node(tag, {k: v or "" for k, v in attrs}))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if data:
            self.stack[-1].children.append(data)


def parse(html: str) -> Node:
    builder = _TreeBuilder()
    try:
        builder.feed(html)
        builder.close()
    except Exception:  # noqa: BLE001, S110 —— 乱到解析器都崩的页面：已解析的部分照用
        pass
    return builder.root


def page_title(root: Node) -> str:
    title = root.find("title")
    if title and title.text():
        return title.text()
    for meta in root.find_all("meta"):
        if meta.attrs.get("property") == "og:title" and meta.attrs.get("content"):
            return " ".join(meta.attrs["content"].split())
    h1 = root.find("h1")
    return h1.text() if h1 else ""


def main_content(root: Node) -> Node:
    """正文所在的元素：最长的 <article>，其次 <main> / role=main，再次 <body>"""
    articles = [n for n in root.find_all("article") if len(n.text()) > 200]
    if articles:
        return max(articles, key=lambda n: len(n.text()))
    for node in root.iter():
        if (node.tag == "main" or node.attrs.get("role") == "main") and len(node.text()) > 200:
            return node
    return root.find("body") or root


def to_markdown(html: str, base_url: str = "") -> tuple[str, str]:
    """返回 (标题, 正文 Markdown)"""
    root = parse(html)
    title = page_title(root)
    text = _Renderer(base_url).render(main_content(root))
    return title, text


class _Renderer:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    def render(self, node: Node) -> str:
        text = self.block(node)
        # 行内元素两边各带一个空格时会连成两个（"第二段 " + " **粗**"）；代码块里的不动
        parts = re.split(r"(\n`{3,}[^\n]*\n.*?\n`{3,}\n)", text, flags=re.DOTALL)
        text = "".join(part if i % 2 else re.sub(r"(?<=\S) {2,}", " ", part)
                       for i, part in enumerate(parts))
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    # -- 行内 ---------------------------------------------------------- #

    def inline(self, node: Node) -> str:
        out: list[str] = []
        for child in node.children:
            if isinstance(child, str):
                out.append(re.sub(r"\s+", " ", child))
            else:
                out.append(self.element(child))
        return "".join(out)

    def block(self, node: Node) -> str:
        return self.inline(node)

    def element(self, node: Node) -> str:
        tag = node.tag
        if tag in DROP or tag in CHROME or node.attrs.get("hidden") is not None \
                or node.attrs.get("aria-hidden") == "true":
            return ""
        if tag in HEADINGS:
            text = " ".join(self.inline(node).split())
            return f"\n\n{'#' * HEADINGS[tag]} {text}\n\n" if text else ""
        if tag in BLOCKS or tag in ("dl", "dt", "dd"):
            inner = self.inline(node).strip()
            if tag == "dt" and inner:
                inner = f"**{inner}**"
            return f"\n\n{inner}\n\n" if inner else ""
        if tag == "br":
            return "\n"
        if tag == "hr":
            return "\n\n---\n\n"
        if tag == "pre":
            return self.pre(node)
        if tag in ("ul", "ol"):
            return self.list(node, ordered=tag == "ol")
        if tag == "li":     # 列表外面的散 li
            return f"\n- {self.inline(node).strip()}\n"
        if tag == "blockquote":
            inner = self.inline(node).strip()
            quoted = "\n".join(f"> {line}" if line else ">" for line in inner.split("\n"))
            return f"\n\n{quoted}\n\n" if inner else ""
        if tag == "table":
            return self.table(node)
        if tag == "a":
            return self.link(node)
        if tag == "img":
            alt = " ".join(node.attrs.get("alt", "").split())
            return f"[图：{alt}]" if alt else ""
        if tag in ("strong", "b"):
            return self.wrap(self.inline(node), "**")
        if tag in ("em", "i"):
            return self.wrap(self.inline(node), "*")
        if tag in ("del", "s", "strike"):
            return self.wrap(self.inline(node), "~~")
        if tag == "code":
            code = "".join(node.text())
            return f"`{code}`" if code else ""
        return self.inline(node)

    @staticmethod
    def wrap(text: str, mark: str) -> str:
        stripped = text.strip()
        if not stripped:
            return text
        lead = " " if text[:1].isspace() else ""
        tail = " " if text[-1:].isspace() else ""
        return f"{lead}{mark}{stripped}{mark}{tail}"

    def link(self, node: Node) -> str:
        text = " ".join(self.inline(node).split())
        href = node.attrs.get("href", "").strip()
        if not text:
            return ""
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            return text
        return f"[{text}]({urljoin(self.base_url, href)})"

    # -- 块 ------------------------------------------------------------ #

    def pre(self, node: Node) -> str:
        raw: list[str] = []

        def walk(n: Node) -> None:
            for child in n.children:
                if isinstance(child, str):
                    raw.append(child)
                elif child.tag == "br":
                    raw.append("\n")
                else:
                    walk(child)

        walk(node)
        code = "".join(raw).strip("\n")
        if not code.strip():
            return ""
        lang = ""
        for n in node.iter():
            for cls in n.classes():
                if cls.startswith(("language-", "lang-")):
                    lang = cls.split("-", 1)[1]
                    break
            if lang:
                break
        fence = "````" if "```" in code else "```"
        return f"\n\n{fence}{lang}\n{code}\n{fence}\n\n"

    def list(self, node: Node, ordered: bool) -> str:
        items = [child for child in node.children if isinstance(child, Node) and child.tag == "li"]
        lines: list[str] = []
        for number, item in enumerate(items, 1):
            marker = f"{number}. " if ordered else "- "
            body = self.inline(item).strip()
            body = re.sub(r"\n{2,}", "\n", body)
            if not body:
                continue
            first, *rest = body.split("\n")
            lines.append(marker + first)
            lines += [" " * len(marker) + line if line else "" for line in rest]
        return "\n\n" + "\n".join(lines) + "\n\n" if lines else ""

    def table(self, node: Node) -> str:
        rows: list[list[str]] = []
        for tr in node.find_all("tr"):
            cells = [child for child in tr.children if isinstance(child, Node)
                     and child.tag in ("td", "th")]
            row = [" ".join(self.inline(c).split()).replace("|", "\\|") for c in cells]
            if any(row):
                rows.append(row)
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        if width == 1:      # 单列表格多半是排版用的，按段落输出
            return "\n\n" + "\n\n".join(r[0] for r in rows) + "\n\n"
        lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
        lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return "\n\n" + "\n".join(lines) + "\n\n"
