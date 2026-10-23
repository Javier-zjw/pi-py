"""
上下文压缩。

- budget.py   token 计量与预算（CJK 感知、以服务商实报 usage 为锚）
- cut.py      切点：按 token 预算保留尾部，绝不拆开工具调用与结果
- prune.py    L1：把旧工具输出换成占位副本（不调模型，可重新获取所以基本无损）
- state.py    结构化状态：代码提取的确定性字段 + 模型字段的校验、合并、渲染、裁剪
- prompt.py   压缩指令（追加在与正文相同的前缀之后，命中前缀缓存）
- engine.py   编排：流式调用、校验重试、降级、分块；只算结果，不改会话
- events.py   compaction_start / progress / end，让压缩过程可见
"""

from .budget import (
    STATE_MESSAGE_TYPE,
    CompactionBudget,
    context_tokens,
    estimate_message_tokens,
    estimate_text_tokens,
    estimate_tokens,
)
from .cut import CutPoint, find_cut
from .engine import (
    CompactionCancelled,
    CompactionFailed,
    CompactionOutcome,
    CompactionProgress,
    CompactionRequest,
    make_state_message,
    run_compaction,
)
from .events import (
    CompactionEndEvent,
    CompactionEvent,
    CompactionProgressEvent,
    CompactionStartEvent,
)
from .prune import PRUNE_ENTRY_TYPE, PrunePlan, apply_prune, is_pruned, plan_prune
from .state import render_markdown

__all__ = [
    "STATE_MESSAGE_TYPE",
    "CompactionBudget",
    "CompactionCancelled",
    "CompactionEndEvent",
    "CompactionEvent",
    "CompactionFailed",
    "CompactionOutcome",
    "CompactionProgress",
    "CompactionProgressEvent",
    "CompactionRequest",
    "CompactionStartEvent",
    "CutPoint",
    "PRUNE_ENTRY_TYPE",
    "PrunePlan",
    "apply_prune",
    "is_pruned",
    "plan_prune",
    "context_tokens",
    "estimate_message_tokens",
    "estimate_text_tokens",
    "estimate_tokens",
    "find_cut",
    "make_state_message",
    "render_markdown",
    "run_compaction",
]
