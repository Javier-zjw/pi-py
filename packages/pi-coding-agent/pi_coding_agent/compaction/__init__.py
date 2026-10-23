"""
上下文压缩。

分层：
- budget.py   token 计量与预算（CJK 感知、以服务商实报 usage 为锚）
- cut.py      切点：按 token 预算保留尾部，绝不拆开工具调用与结果
- state.py    结构化状态：代码提取的确定性字段 + 模型字段的校验、合并、渲染、裁剪
"""

from ._legacy import (
    COMPACTION_PROMPT,
    CompactionResult,
    collect_file_activity,
    compact,
    estimate_tokens,
    last_reported_tokens,
    should_compact,
    split_tail,
)

__all__ = [
    "COMPACTION_PROMPT",
    "CompactionResult",
    "collect_file_activity",
    "compact",
    "estimate_tokens",
    "last_reported_tokens",
    "should_compact",
    "split_tail",
]
