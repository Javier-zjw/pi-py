"""渲染组件。

设计原则：**用左侧 gutter 标记代替画框**。满屏 ╭─╮ 边框是业余感的主要来源，
而且窗口一窄就崩。一根竖线 + 缩进就能表达从属关系，还天然支持复制粘贴。
"""

from __future__ import annotations

from dataclasses import dataclass

from .theme import ACCENT, DEFAULT_THEME, ERROR, MUTED, SUCCESS, THINKING, WARN, Color, Theme
from .width import display_width, pad, truncate, wrap

# 统一一套细线字符，不用 ASCII 的 +---+
BAR = "│"
BAR_HEAVY = "▌"
RULE = "─"
DOT = "●"
CHECK = "✓"
CROSS = "✗"
ARROW = "→"
SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


@dataclass
class Gutter:
    """左侧标记 + 缩进内容，是整个界面的基本单元。"""

    mark: str
    color: Color | None = None
    theme: Theme = DEFAULT_THEME

    def render(self, body: str, width: int, *, dim_body: bool = False) -> list[str]:
        indent = display_width(self.mark) + 1
        lines: list[str] = []
        for i, line in enumerate(wrap(body, max(width - indent, 20))):
            head = self.mark if i == 0 else " " * display_width(self.mark)
            head = self.theme.paint(head, self.color) if self.color else head
            text = self.theme.muted(line) if dim_body else line
            lines.append(f"{head} {text}")
        return lines


def rule(width: int, label: str = "", theme: Theme = DEFAULT_THEME) -> str:
    """极细的分隔线，只用来分隔轮次，不要滥用。"""
    if not label:
        return theme.muted(RULE * width)
    text = f"{RULE * 2} {label} "
    return theme.muted(text + RULE * max(0, width - display_width(text)))


def badge(text: str, color: Color = ACCENT, theme: Theme = DEFAULT_THEME) -> str:
    """不加底色的标签——加底色在浅色终端上必翻车。"""
    return theme.paint(f"[{text}]", color)


def key_value(pairs: list[tuple[str, str]], theme: Theme = DEFAULT_THEME, indent: str = "  ") -> list[str]:
    """状态行。key 右对齐让值形成一条竖直的视觉线，比左对齐整齐得多。"""
    if not pairs:
        return []
    key_width = max(display_width(k) for k, _ in pairs)
    return [f"{indent}{theme.muted(pad(k, key_width, 'right'))}  {v}" for k, v in pairs]


def user_message(text: str, width: int, theme: Theme = DEFAULT_THEME) -> list[str]:
    return Gutter(BAR_HEAVY, ACCENT, theme).render(theme.bold(text), width)


def assistant_text(text: str, width: int, theme: Theme = DEFAULT_THEME) -> list[str]:
    """助手正文不加 gutter：主体内容应该占满版面，没有装饰。"""
    return wrap(markdown_lite(text, theme), width)


def thinking_block(text: str, width: int, theme: Theme = DEFAULT_THEME, collapsed_at: int = 3) -> list[str]:
    """思考内容整体压暗并折叠——它是过程，不该和结论抢注意力。"""
    lines = wrap(text, width - 2)
    if len(lines) > collapsed_at:
        shown = lines[:collapsed_at]
        shown.append(f"… 还有 {len(lines) - collapsed_at} 行")
        lines = shown
    return [f"{theme.thinking(BAR)} {theme.muted(line)}" for line in lines]


def tool_call(name: str, summary: str, width: int, theme: Theme = DEFAULT_THEME) -> str:
    head = theme.accent(f"{ARROW} {name}")
    return f"{head} {theme.muted(truncate(summary, max(width - display_width(name) - 4, 10)))}"


def tool_result(ok: bool, preview: str, width: int, theme: Theme = DEFAULT_THEME) -> str:
    mark = theme.success(f"  {CHECK}") if ok else theme.error(f"  {CROSS}")
    return f"{mark} {theme.muted(truncate(preview.replace(chr(10), ' '), width - 6))}"


def answer_start(theme: Theme = DEFAULT_THEME) -> list[str]:
    """正文开始前的空行。工具日志和模型结论必须在视觉上分开。"""
    return [""]


def code_block(code: str, width: int, theme: Theme = DEFAULT_THEME, language: str = "", first_line: int = 1) -> list[str]:
    """代码块用行号列 + 竖线，比整块染色克制得多，也更好复制。"""
    lines = code.rstrip("\n").split("\n")
    gutter_width = len(str(first_line + len(lines) - 1))
    out: list[str] = []
    if language:
        out.append(f"  {theme.muted(language)}")
    for i, line in enumerate(lines):
        number = theme.muted(pad(str(first_line + i), gutter_width, "right"))
        out.append(f"  {number} {theme.muted(BAR)} {truncate(line, width - gutter_width - 6)}")
    return out


def diff_block(patch: str, width: int, theme: Theme = DEFAULT_THEME, max_lines: int = 20) -> list[str]:
    """只给增删行上色，上下文行保持默认——满屏彩色反而看不出改了什么。"""
    out: list[str] = []
    lines = patch.split("\n")
    for line in lines[:max_lines]:
        body = truncate(line, width - 2)
        if line.startswith("+++") or line.startswith("---"):
            out.append(f"  {theme.muted(body)}")
        elif line.startswith("@@"):
            out.append(f"  {theme.paint(body, THINKING)}")
        elif line.startswith("+"):
            out.append(f"  {theme.success(body)}")
        elif line.startswith("-"):
            out.append(f"  {theme.error(body)}")
        else:
            out.append(f"  {theme.muted(body)}")
    if len(lines) > max_lines:
        out.append(f"  {theme.muted(f'… 还有 {len(lines) - max_lines} 行')}")
    return out


def markdown_lite(text: str, theme: Theme = DEFAULT_THEME) -> str:
    """只处理 **粗体**、`行内码`、# 标题。

    刻意不做完整 markdown：终端里过度渲染会让输出变成花花绿绿的圣诞树，
    而且模型输出的 markdown 经常是半截的。
    """
    import re

    def bold_sub(m):
        return theme.bold(m.group(1))

    def code_sub(m):
        return theme.paint(m.group(1), ACCENT)

    lines = []
    for line in text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            content = stripped[level:].strip()
            lines.append(theme.bold(content) if level <= 2 else theme.accent(content))
            continue
        if stripped.startswith(("- ", "* ")):
            line = line.replace("- ", f"{theme.muted('·')} ", 1).replace("* ", f"{theme.muted('·')} ", 1)
        line = re.sub(r"\*\*(.+?)\*\*", bold_sub, line)
        line = re.sub(r"`([^`]+?)`", code_sub, line)
        lines.append(line)
    return "\n".join(lines)


def usage_line(
    tokens_in: int, tokens_out: int, cost: float, elapsed: float, theme: Theme = DEFAULT_THEME
) -> str:
    bits = [f"↑{tokens_in}", f"↓{tokens_out}", f"{elapsed:.1f}s"]
    if cost:
        bits.append(f"¥{cost:.4f}")
    return theme.muted("  " + "  ".join(bits))
