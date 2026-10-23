"""流式 Markdown 渲染：模型边吐字边出排好版的行。

和 Codex 一样按"整行"提交：一行源文本收齐（遇到换行）才渲染成终端行写进 scrollback，
还没收齐的半行由 ``preview()`` 给出，上层放在可重绘的活动区里，所以依然是实时的。
表格要等整张表收齐才能算列宽，期间也只出现在 preview 里。

支持：标题、段落、**粗体** *斜体* `行内码` ~~删除线~~ [链接](url)、有序/无序/任务列表（可嵌套）、
引用、代码块、分隔线、表格（中文对齐）。折行按显示宽度，带悬挂缩进，样式跨行不丢。
不认识的写法原样输出——模型的 markdown 经常是半截的，宁可朴素也不能吞字。
"""

from __future__ import annotations

import re

from .theme import ACCENT, DEFAULT_THEME, INFO, MUTED, RESET, Theme
from .width import _tokenize, char_width, display_width, pad

ESCAPE_RE = re.compile(r"(\033\[[0-9;]*m|\033\][^\007]*\007)")

_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})\s*([\w+#.-]*)")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_HR_RE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?(.*)$")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$")
_TASK_RE = re.compile(r"^\[([ xX])\]\s+(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")

BULLETS = ("•", "◦", "▪")


# -- 带样式的折行 ------------------------------------------------------- #

# 不能出现在行首的标点（中文排版的"避头"）：折行时把前一个字一起带到下一行
NO_LINE_START = set("，。、；：？！）》」』】〉…—,.;:?!)]}%")


def wrap_ansi(text: str, width: int) -> list[str]:
    """按显示宽度折行，忽略转义序列；某段样式跨行时，行尾关掉、下一行开头补上。
    英文按词断，中文按字断，超长的词硬断，句末标点不放行首。"""
    width = max(width, 1)
    lines: list[str] = []
    # 当前行的片段：(文字或转义序列, 显示宽度, 这一片之后生效的样式)
    pieces: list[tuple[str, int, str]] = []
    start_style = ""          # 当前行开头生效的样式
    style = ""
    used = 0

    def close(chunk: list[tuple[str, int, str]], opening: str) -> str:
        line = opening + "".join(piece for piece, _, _ in chunk)
        ending = chunk[-1][2] if chunk else opening
        return _rstrip_visible(line) + (RESET if ending else "")

    def newline(carry: int = 0) -> None:
        """换行；carry > 0 时把当前行最后 carry 个片段带到下一行"""
        nonlocal pieces, start_style, used
        keep, moved = (pieces[:-carry], pieces[-carry:]) if carry else (pieces, [])
        lines.append(close(keep, start_style))
        start_style = keep[-1][2] if keep else start_style
        pieces = moved
        used = sum(w for _, w, _ in moved)

    def add(piece: str, w: int) -> None:
        nonlocal used
        pieces.append((piece, w, style))
        used += w

    for part in ESCAPE_RE.split(text):
        if not part:
            continue
        if ESCAPE_RE.fullmatch(part):
            if part.startswith("\033["):
                style = "" if part in (RESET, "\033[m") else style + part
            add(part, 0)
            continue
        for token in _tokenize(part):
            w = display_width(token)
            if used + w > width and used > 0:
                if token.isspace():
                    continue
                carry = 0
                if token[0] in NO_LINE_START:
                    # 往回越过行尾连着的标点，找到最后一个普通字，从它开始（连同中间的转义序列）
                    # 一起带到下一行；最多带几个，带不动就算了
                    for k in range(len(pieces) - 1, max(len(pieces) - 8, 0), -1):
                        text_k = pieces[k][0]
                        if pieces[k][1] == 0 or text_k.isspace() or text_k[0] in NO_LINE_START:
                            continue
                        carry = len(pieces) - k
                        break
                newline(carry)
            if used == 0 and token.isspace() and lines:
                continue        # 续行开头不留空格
            if w > width:       # 比一行还长的词（URL、路径）：硬断
                for ch in token:
                    cw = char_width(ch)
                    if used + cw > width and used > 0:
                        newline()
                    add(ch, cw)
                continue
            add(token, w)
    lines.append(close(pieces, start_style))
    return lines


def _rstrip_visible(line: str) -> str:
    """去掉行尾空格，但保留空格后面的转义序列"""
    return re.sub(r" +((?:\033\[[0-9;]*m)*)$", r"\1", line)


# -- 行内 ---------------------------------------------------------------- #

_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__")
# 单个 * / _ 才是斜体：左右不能贴着字母数字（snake_case、2 * 3、a*b*c 都不算）
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])|(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)")
_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_SLOT_RE = re.compile(r"\x00(\d+)\x00")


def render_inline(text: str, theme: Theme = DEFAULT_THEME, base: str = "") -> str:
    """行内样式。可以嵌套（粗体里的行内码、斜体里的粗体）：内层样式结束时 RESET 会把外层也关掉，
    所以每层都在内层的 RESET 之后把自己补回来。base 是整段的外层样式（比如标题）"""
    t = theme
    slots: list[str] = []

    def keep(rendered: str) -> str:
        """渲染好的片段先换成占位符，后面的规则就不会再动它"""
        slots.append(rendered)
        return f"\x00{len(slots) - 1}\x00"

    def resolve(value: str) -> str:
        while _SLOT_RE.search(value):
            value = _SLOT_RE.sub(lambda m: slots[int(m.group(1))], value)
        return value

    def styled(content: str, color=None, **kw) -> str:
        inner = resolve(fmt(content))
        codes = t.codes(color, **kw)
        if not codes:
            return keep(inner)
        return keep(codes + inner.replace(RESET, RESET + codes) + RESET)

    def fmt(value: str) -> str:
        value = _BOLD_RE.sub(lambda m: styled(m.group(1) or m.group(2), bold=True), value)
        value = _ITALIC_RE.sub(lambda m: styled(m.group(1) or m.group(2), italic=True), value)
        return _STRIKE_RE.sub(lambda m: styled(m.group(1), strike=True), value)

    def link(m: re.Match) -> str:
        label, url = m.group(1), m.group(2).strip()
        shown = resolve(styled(label or url, ACCENT, underline=True))
        if label and label != url:
            shown += t.muted(f" ({url})")
        return keep(shown)

    # 转义的反引号最先处理，否则会被当成行内码的开头
    text = text.replace("\\`", keep("`"))
    # 行内码其次：里面的 * _ [ 都不当样式
    text = re.sub(r"(`+)(.+?)\1", lambda m: keep(t.paint(m.group(2).strip(), INFO)), text)
    text = re.sub(r"\\([\\*_{}\[\]()#+\-.!~|>])", lambda m: keep(m.group(1)), text)
    text = re.sub(r"!?\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", link, text)
    text = re.sub(r"<(https?://[^>\s]+)>", lambda m: keep(t.paint(m.group(1), ACCENT, underline=True)), text)
    text = resolve(fmt(text))
    if base:
        text = base + text.replace(RESET, RESET + base) + RESET
    return text


# -- 块 ------------------------------------------------------------------ #

class MarkdownStream:
    """喂入源文本，吐出排好版的终端行。

    - ``feed(text)``：返回这次可以定稿的行（源文本里已经收齐的行）
    - ``preview()``：还没定稿的部分（半行、没收齐的表格）现在的样子，给活动区用
    - ``finish()``：收尾，返回剩下的全部行

    ``first_prefix`` 只加在第一行（如 "● "），之后每行加 ``prefix``（如两个空格）。
    空行：开头的不要，连续的压成一个，结尾的不要。
    """

    def __init__(self, width: int, theme: Theme = DEFAULT_THEME,
                 first_prefix: str = "", prefix: str = "") -> None:
        self.theme = theme
        self.first_prefix = first_prefix
        self.prefix = prefix
        self.width = max(width - max(display_width(first_prefix), display_width(prefix)), 10)
        self._buffer = ""
        self._table: list[str] = []
        self._fence = ""                # 在代码块里时是开头的 ``` / ~~~
        self._started = False           # 已经输出过内容行
        self._pending_blank = False     # 空行先记着，后面有内容才输出

    # -- 对外 ---------------------------------------------------------- #

    def feed(self, text: str) -> list[str]:
        self._buffer += text.replace("\r", "")
        out: list[str] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            out += self._line(line)
        return out

    def preview(self) -> list[str]:
        """半行和没收齐的表格。不改变状态，不消耗"第一行"前缀"""
        body: list[str] = []
        if self._table:
            rows = self._table + ([self._buffer] if self._buffer.strip().startswith("|") else [])
            body += self._render_table(rows)
        if self._buffer.strip() and not (self._table and self._buffer.strip().startswith("|")):
            if self._fence:
                body += self._code_line(self._buffer)
            else:
                body += self._block(self._buffer)
        if not body:
            return []
        return self._prefixed(body, consume=False)

    def finish(self) -> list[str]:
        out: list[str] = []
        if self._buffer:
            line, self._buffer = self._buffer, ""
            out += self._line(line)
        out += self._flush_table()
        self._pending_blank = False
        return out

    # -- 内部 ---------------------------------------------------------- #

    def _prefixed(self, body: list[str], consume: bool = True) -> list[str]:
        out: list[str] = []
        first = not self._started
        for line in body:
            if line == "":
                out.append("")
                continue
            lead = self.first_prefix if first else self.prefix
            first = False
            out.append(f"{lead}{line}")
        if consume and any(line for line in body):
            self._started = True
        return out

    def _emit(self, body: list[str]) -> list[str]:
        """内容行前补上记着的空行；开头的空行不要"""
        out: list[str] = []
        if self._pending_blank and self._started:
            out.append("")
        self._pending_blank = False
        return out + self._prefixed(body)

    def _line(self, line: str) -> list[str]:
        if self._fence:
            if line.strip().startswith(self._fence):
                self._fence = ""
                return []
            return self._emit(self._code_line(line))

        stripped = line.strip()
        if self._table:
            if stripped.startswith("|") or (len(self._table) == 1 and _TABLE_SEP_RE.match(line)):
                self._table.append(line)
                return []
            out = self._flush_table()
            return out + self._line(line)
        if stripped.startswith("|") and stripped.count("|") >= 2:
            self._table = [line]
            return []

        fence = _FENCE_RE.match(line)
        if fence:
            self._fence = fence.group(1)[:3]
            return []
        if not stripped:
            self._pending_blank = True
            return []
        if _HEADING_RE.match(line) and self._started:
            self._pending_blank = True       # 标题前总空一行，和上文分开
        return self._emit(self._block(line))

    def _block(self, line: str) -> list[str]:
        """一行非代码、非表格的源文本 → 若干终端行（不带前缀）"""
        t = self.theme
        width = self.width
        heading = _HEADING_RE.match(line)
        if heading:
            level, text = len(heading.group(1)), heading.group(2)
            base = t.codes(ACCENT, bold=True) if level <= 2 else t.codes(None, bold=True)
            return wrap_ansi(render_inline(text, t, base), width)
        if _HR_RE.match(line):
            return [t.paint("─" * min(width, 60), MUTED)]
        quote = _QUOTE_RE.match(line)
        if quote:
            bar = t.paint("│", MUTED) + " "
            return [bar + part for part in wrap_ansi(render_inline(quote.group(1), t, t.codes(MUTED)), width - 2)]
        item = _LIST_RE.match(line)
        if item:
            indent, marker, text = item.groups()
            level = len(indent.expandtabs(4)) // 2
            if marker[0].isdigit():
                bullet = t.paint(marker, MUTED)
            else:
                bullet = t.paint(BULLETS[min(level, len(BULLETS) - 1)], MUTED)
                task = _TASK_RE.match(text)
                if task:
                    done = task.group(1).lower() == "x"
                    bullet = t.paint("☑" if done else "☐", ACCENT if done else MUTED)
                    text = task.group(2)
            lead = "  " * level
            hang = " " * (display_width(lead) + display_width(bullet) + 1)
            body = wrap_ansi(render_inline(text, t), width - display_width(hang))
            return [f"{lead}{bullet} {body[0]}"] + [hang + rest for rest in body[1:]]
        return wrap_ansi(render_inline(line.strip(), t), width)

    def _code_line(self, line: str) -> list[str]:
        """代码块：左边一条灰线，原样不做行内样式；太长就折行"""
        t = self.theme
        bar = t.paint("│", MUTED) + " "
        code = line.expandtabs(4).rstrip()
        return [bar + part for part in wrap_ansi(code, self.width - 2)] if code else [bar.rstrip()]

    def _flush_table(self) -> list[str]:
        rows, self._table = self._table, []
        if not rows:
            return []
        return self._emit(self._render_table(rows))

    def _render_table(self, rows: list[str]) -> list[str]:
        t = self.theme
        if len(rows) < 2 or not _TABLE_SEP_RE.match(rows[1]):
            # 不是表格（没有分隔行）：按普通行处理
            out: list[str] = []
            for row in rows:
                out += self._block(row)
            return out
        aligns = [("center" if c.startswith(":") and c.endswith(":") else
                   "right" if c.endswith(":") else "left")
                  for c in _cells(rows[1])]
        header = [render_inline(c, t, t.codes(None, bold=True)) for c in _cells(rows[0])]
        body = [[render_inline(c, t) for c in _cells(row)] for row in rows[2:]]
        columns = max(len(header), *(len(r) for r in body)) if body else len(header)
        grid = [r + [""] * (columns - len(r)) for r in [header, *body]]
        aligns += ["left"] * (columns - len(aligns))
        widths = [max(display_width(r[i]) for r in grid) for i in range(columns)]
        widths = [max(w, 1) for w in widths]
        # 太宽就从最宽的列开始收窄，格子里折行
        room = self.width - (3 * columns + 1)
        while sum(widths) > room and max(widths) > 4:
            widths[widths.index(max(widths))] -= 1

        line = lambda left, mid, right: t.paint(
            left + mid.join("─" * (w + 2) for w in widths) + right, MUTED)
        bar = t.paint("│", MUTED)
        out = [line("┌", "┬", "┐")]
        for index, row in enumerate(grid):
            wrapped = [wrap_ansi(cell, widths[i]) for i, cell in enumerate(row)]
            height = max(len(cell) for cell in wrapped)
            for k in range(height):
                parts = [pad(cell[k] if k < len(cell) else "", widths[i], aligns[i])
                         for i, cell in enumerate(wrapped)]
                out.append(bar + "".join(f" {p} {bar}" for p in parts))
            if index == 0:
                out.append(line("├", "┼", "┤"))
        out.append(line("└", "┴", "┘"))
        return out


def _cells(row: str) -> list[str]:
    row = row.strip()
    row = row.removeprefix("|")
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", row)]


def render_markdown(text: str, width: int, theme: Theme = DEFAULT_THEME,
                    first_prefix: str = "", prefix: str = "") -> list[str]:
    """一次性渲染整段 markdown"""
    stream = MarkdownStream(width, theme, first_prefix, prefix)
    return stream.feed(text) + stream.finish()
