"""
压缩提示词。

压缩请求复用正文请求的 system prompt + tools + 消息前缀，只在末尾追加这条指令，
所以前缀能命中服务商的前缀缓存；这条指令也因此必须自带全部规则。
"""

from __future__ import annotations

SCHEMA_EXAMPLE = """```json
{
  "task": {"goal": "整体目标，一两句", "status": "当前处在哪一步"},
  "constraints": [{"id": "c1", "text": "用户明确的约束或偏好"}],
  "progress": {
    "done": [{"id": "p1", "text": "..."}],
    "inProgress": [{"id": "p2", "text": "..."}],
    "blocked": [{"id": "p3", "text": "...", "reason": "..."}]
  },
  "decisions": [{"id": "d1", "decision": "...", "reason": "..."}],
  "superseded": [{"id": "d0", "text": "被推翻的内容", "reason": "..."}],
  "knowledge": [{"id": "k1", "fact": "对代码库的关键认知", "where": "路径:行号"}],
  "errors": [{"id": "e1", "error": "报错关键行原文", "fix": "怎么解决的或尚未解决", "status": "fixed|open"}],
  "files": [{"path": "...", "role": "这个文件在任务中的作用"}],
  "openQuestions": ["..."],
  "nextSteps": ["接手后第一步做什么，要具体"]
}
```"""


def build_instruction(
        has_previous: bool,
        budget_tokens: int,
        split_turn: bool = False,
        custom_instructions: str | None = None,
) -> str:
    parts = [
        "<compaction_request>",
        "【系统通知，不是用户的新请求】上下文即将达到上限，现在需要压缩。",
        "本次回复：不要调用任何工具，不要继续执行任务，只输出一个 ```json 代码块。",
        "",
        "## 你的任务",
        "把上面的对话整理成一份\"工作状态文档\"，交给接手工作的你自己。",
        "接手者看不到被压缩的原始对话，只能看到：这份文档 + 系统保留的最近几轮原始消息。",
    ]
    if has_previous:
        parts += [
            "对话开头的 <compacted_state> 是上一次压缩得到的状态，请在它的基础上合并之后发生的事件：",
            "- 已有条目保留原 id；没有被新事件改变的条目原样保留",
            "- 已完成的 inProgress 移入 done；被推翻的决策移入 superseded 并写明原因",
            "- constraints、decisions、knowledge 中的已有条目不得因\"看起来不重要\"而删除",
        ]
    if split_turn:
        parts += [
            ("最后一轮用户请求还在执行中，它的原文由系统另行保留；"
             "请在 task.status 写清这一轮已经做到哪一步、还差什么。"),
        ]
    parts += [
        "",
        "## 规则",
        "1. 只记录对话中真实出现过的信息。不推测、不补全；不确定的写进 openQuestions。",
        ("2. 必须逐字保留：文件路径、函数/类名、命令、报错关键行、配置键和值、版本号、"
         "用户给出的具体数值和命名。"),
        ("3. 能重新获取的内容（文件内容、命令输出、搜索结果）不要抄录，只记\"在哪里、得出了什么结论\"，"
         "例如 \"src/a.py:120 parse() 未处理空串\"。"),
        "4. 失败的尝试合并成一条：试了什么、为什么失败，避免接手者重复踩坑。",
        "5. 用户原话、文件操作清单、命令记录由系统另行保存，不要复述，只提炼其中的约束、偏好和结论。",
        "6. 用用户使用的语言书写。",
        (f"7. 总长度不超过 {budget_tokens} tokens。超出时只允许把 done 合并成更粗的条目，"
         "其他字段不要合并。"),
    ]
    if custom_instructions:
        parts += ["", "## 用户对本次压缩的额外要求", custom_instructions.strip()]
    parts += ["", "## 输出格式（只输出这一个代码块）", SCHEMA_EXAMPLE, "</compaction_request>"]
    return "\n".join(parts)


def build_retry_message(errors: list[str]) -> str:
    reasons = "\n".join(f"- {e}" for e in errors)
    return (
        "<compaction_request>\n"
        f"上次输出未通过校验：\n{reasons}\n"
        "请不要调用工具，只输出修正后的完整 ```json 代码块。\n"
        "</compaction_request>"
    )
