"""代码块语法着色：逐行、按正则切词，不引入第三方依赖（pi-tui 必须零依赖）。

只求"一眼分得清关键字、字符串、注释、数字"，不追求完整的语法分析：
跨行字符串、嵌套模板之类按普通文本处理。认不出的语言只给字符串、数字上色；没写语言的不上色。
"""

from __future__ import annotations

import re

from .theme import ACCENT, DEFAULT_THEME, ERROR, HIGHLIGHT, INFO, MUTED, SUCCESS, WARN, Theme

_PY_KEYWORDS = (
    "and as assert async await break class continue def del elif else except finally for from "
    "global if import in is lambda nonlocal not or pass raise return try while with yield match case"
)
_JS_KEYWORDS = (
    "async await break case catch class const continue debugger default delete do else export "
    "extends finally for function if import in instanceof let new of return static super switch "
    "this throw try typeof var void while with yield interface type enum implements readonly as "
    "from declare namespace public private protected abstract"
)
_GO_KEYWORDS = (
    "break case chan const continue default defer else fallthrough for func go goto if import "
    "interface map package range return select struct switch type var"
)
_RUST_KEYWORDS = (
    "as async await break const continue crate dyn else enum extern fn for if impl in let loop "
    "match mod move mut pub ref return self Self static struct super trait type unsafe use where while"
)
_SH_KEYWORDS = "if then else elif fi for while until do done case esac in function return export local"

LANGUAGES: dict[str, dict] = {
    "python": {"keywords": set(_PY_KEYWORDS.split()), "comment": "#",
               "constants": {"True", "False", "None", "self", "cls"}},
    "javascript": {"keywords": set(_JS_KEYWORDS.split()), "comment": "//",
                   "constants": {"true", "false", "null", "undefined", "NaN"}},
    "go": {"keywords": set(_GO_KEYWORDS.split()), "comment": "//",
           "constants": {"true", "false", "nil", "iota"}},
    "rust": {"keywords": set(_RUST_KEYWORDS.split()), "comment": "//",
             "constants": {"true", "false", "None", "Some", "Ok", "Err"}},
    "bash": {"keywords": set(_SH_KEYWORDS.split()), "comment": "#", "constants": set()},
    "json": {"keywords": set(), "comment": "", "constants": {"true", "false", "null"}},
}
ALIASES = {
    "py": "python", "python3": "python", "js": "javascript", "jsx": "javascript", "ts": "javascript",
    "tsx": "javascript", "typescript": "javascript", "mjs": "javascript", "node": "javascript",
    "golang": "go", "rs": "rust", "sh": "bash", "shell": "bash", "zsh": "bash", "console": "bash",
    "jsonc": "json",
}

_TOKEN = re.compile(
    r"(?P<string>\"(?:\\.|[^\"\\])*\"?|'(?:\\.|[^'\\])*'?|`(?:\\.|[^`\\])*`?)"
    r"|(?P<number>\b(?:0[xob][0-9a-fA-F_]+|\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?)\b)"
    r"|(?P<decorator>@[\w.]+)"
    r"|(?P<word>[A-Za-z_$][\w$]*)"
    r"|(?P<other>.)",
    re.S,
)


def language(name: str) -> str:
    name = (name or "").strip().lower()
    return ALIASES.get(name, name)


def highlight(line: str, lang: str, theme: Theme = DEFAULT_THEME) -> str:
    """给一行代码上色，返回带 ANSI 的字符串"""
    t = theme
    lang = language(lang)
    if not lang:
        return line         # 没写语言：猜错了反而难看（Markdown 的 # 不是注释），原样
    if lang == "diff":
        return _diff(line, t)
    spec = LANGUAGES.get(lang, {"keywords": set(), "comment": "", "constants": set()})
    comment = spec["comment"]
    out: list[str] = []
    i = 0
    while i < len(line):
        if comment and line.startswith(comment, i):
            out.append(t.paint(line[i:], MUTED, italic=True))
            break
        match = _TOKEN.match(line, i)
        kind, text = match.lastgroup, match.group()
        i = match.end()
        if kind == "string":
            # JSON 的键（字符串后面跟冒号）和值分开上色
            is_key = lang == "json" and line[i:].lstrip().startswith(":")
            out.append(t.paint(text, INFO if is_key else SUCCESS))
        elif kind == "number":
            out.append(t.paint(text, HIGHLIGHT))
        elif kind == "decorator" and lang == "python":
            out.append(t.paint(text, WARN))
        elif kind == "word":
            if text in spec["keywords"]:
                out.append(t.paint(text, ACCENT, bold=True))
            elif text in spec["constants"]:
                out.append(t.paint(text, WARN))
            elif line[i:i + 1] == "(" and lang not in ("bash", "json"):
                out.append(t.paint(text, INFO))         # 函数调用 / 定义
            else:
                out.append(text)
        else:
            out.append(text)
    return "".join(out)


def _diff(line: str, t: Theme) -> str:
    if line.startswith(("+++", "---")):
        return t.paint(line, None, bold=True)
    if line.startswith("+"):
        return t.paint(line, SUCCESS)
    if line.startswith("-"):
        return t.paint(line, ERROR)
    if line.startswith("@@"):
        return t.paint(line, INFO)
    return line
