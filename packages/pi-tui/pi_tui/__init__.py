"""pi-tui —— 终端渲染原语。

零依赖，且不认识任何 pi-* 包：它只知道文本、颜色、宽度和屏幕区域。
agent、工具、会话这些概念属于上面的 pi-app。
"""

from .components import (
    answer_start,
    ARROW,
    BAR,
    BAR_HEAVY,
    CHECK,
    CROSS,
    Gutter,
    assistant_text,
    badge,
    code_block,
    diff_block,
    key_value,
    markdown_lite,
    rule,
    thinking_block,
    tool_call,
    tool_result,
    usage_line,
    user_message,
)
from .input import HAS_PROMPT_TOOLKIT, LineReader, missing_dependency_hint
from .live import LiveRegion, Spinner, StatusBar, Ticker, rendered_rows, TextStreamer
from .theme import (
    ACCENT,
    DEFAULT_THEME,
    ERROR,
    MUTED,
    SUCCESS,
    THINKING,
    WARN,
    Color,
    ColorMode,
    Theme,
    detect_color_mode,
)
from .width import (
    display_width, pad, prose_width, strip_ansi, terminal_width, truncate, wrap,
)

__version__ = "0.1.0"

__all__ = [
    "ACCENT", "ARROW", "BAR", "BAR_HEAVY", "CHECK", "CROSS", "Color", "ColorMode",
    "DEFAULT_THEME", "ERROR", "Gutter", "LiveRegion", "MUTED", "SUCCESS", "Spinner",
    "StatusBar", "THINKING", "Theme", "WARN", "assistant_text", "badge", "code_block",
    "detect_color_mode", "diff_block", "display_width", "key_value", "markdown_lite",
    "pad", "rule", "strip_ansi", "terminal_width", "thinking_block", "tool_call",
    "tool_result", "truncate", "usage_line", "user_message", "wrap", "LineReader", "missing_dependency_hint",
    "Ticker", "answer_start", "TextStreamer"
]
