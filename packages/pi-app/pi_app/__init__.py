"""pi-app —— 最上层：把 agent 事件流接到终端界面上。

依赖 pi-coding-agent（业务）和 pi-tui（渲染），两者互不相识。
没有任何包依赖 pi-app。
"""

from .app import main, main_async, run
from .renderer import RenderState, SessionRenderer, summarize_arguments

__version__ = "0.1.0"
__all__ = ["RenderState", "SessionRenderer", "main", "main_async", "run", "summarize_arguments"]
