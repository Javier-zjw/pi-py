"""pi-server —— 顶层：把 coding agent 暴露成 HTTP + SSE。

依赖 pi-coding-agent，和 pi-app 平级。没有任何包依赖它。
dto.py 和 registry.py 不 import fastapi，可以脱离 web 框架单测。
"""

from .dto import EventTranslator, sse_frame, summarize_arguments, usage_dto
from .registry import LiveSession, SessionRegistry

__version__ = "0.1.0"
__all__ = [
    "EventTranslator",
    "LiveSession",
    "SessionRegistry",
    "sse_frame",
    "summarize_arguments",
    "usage_dto",
]


def create_app(*args, **kwargs):
    """延迟导入：没装 fastapi 也能 import 本包做单测。"""
    from .app import create_app as _create_app

    return _create_app(*args, **kwargs)
