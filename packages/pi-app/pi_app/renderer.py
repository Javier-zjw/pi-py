"""事件 → 界面。

三条设计原则，都是踩过坑之后定下来的：

1. **四类内容各有引导符号**：▌用户、╭╰思考、→✓工具、●回答。滚多远都能一眼
   分辨。只靠颜色不行——滚上去之后颜色相近的内容会连成一片。
2. **正文和思考都真流式**，但经过 TextStreamer 匀速吐字。provider 的 SSE 是
   成批到的，收到就整块写会显得"蹦"。
3. **一切耗时都显式标出**。思考几秒、每个工具几秒、整轮几秒，用户才有掌控感。
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any, TextIO

from pi_tui import (
    LiveRegion,
    Spinner,
    StatusBar,
    TextStreamer,
    Theme,
    Ticker,
    diff_block,
    key_value,
    markdown_lite,
    rule,
    terminal_width,
    thinking_block,
    tool_call,
    truncate,
    usage_line,
    user_message,
)

TOOL_SUMMARY_KEYS = ("path", "command", "pattern", "url", "query", "old_text")


def summarize_arguments(arguments: Any) -> str:
    if not isinstance(arguments, dict):
        return ""
    for key in TOOL_SUMMARY_KEYS:
        value = arguments.get(key)
        if value:
            return str(value).replace("\n", " ")[:120]
    try:
        return json.dumps(arguments, ensure_ascii=False)[:120]
    except (TypeError, ValueError):
        return ""


COMPACTION_REASONS = {"threshold": "上下文快满了", "manual": "手动", "overflow": "超出窗口"}


def _k(n: int | None) -> str:
    if n is None:
        return "?"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def first_line(text: str, limit: int) -> str:
    """工具结果只取首行。

    多行内容压平成一行完全没法读（行号和正文糊在一起），而用户在这里只需要
    知道"成功了、大概是什么"。要看细节是另一个命令的事。
    """
    for line in (text or "").split("\n"):
        if line.strip():
            return truncate(line.strip(), limit)
    return ""


@dataclass
class RenderState:
    """一轮之内的临时状态。渲染器不持有会话状态，只持有画到哪儿了。"""

    thinking: str = ""
    thinking_started: bool = False
    thinking_committed: bool = False
    thinking_at: float = 0.0
    text_started: bool = False
    turn_started: float = 0.0
    label: str = "等待中..."
    detail: str = ""
    tool_at: dict[str, float] = field(default_factory=dict)
    own_prompt: str = ""
    compaction_tail: str = ""


class SessionRenderer:
    def __init__(
        self,
        theme: Theme | None = None,
        stream: TextIO | None = None,
        show_thinking: bool = True,
        show_diff: bool = True,
    ) -> None:
        self.theme = theme or Theme()
        self.stream = stream or sys.stdout
        self.show_thinking = show_thinking
        self.show_diff = show_diff
        self.live = LiveRegion(stream=self.stream, theme=self.theme)
        self.status = StatusBar(theme=self.theme)
        self.spinner = Spinner(self.theme)
        self.ticker = Ticker(self._repaint, interval=0.1)
        self.streamer = TextStreamer(self.live.stream_text)
        self.state = RenderState()
        self.tool_count = 0
        self.tool_errors = 0
        # 压缩可能发生在一轮开始之前（手动 / 先压后跑），那时转圈要自己启停
        self.agent_running = False

    # -- 对外 ---------------------------------------------------------- #

    @property
    def width(self) -> int:
        return terminal_width()

    def header(self, cwd: str, model: str, thinking: str = "off") -> None:
        t = self.theme
        from pathlib import Path

        self._emit([
            t.bold("pi coding agent"),
            f"  {t.muted('工作目录')}  {Path(cwd).name}  {t.muted(cwd)}",
            f"  {t.muted('模型')}      {model}",
            f"  {t.muted('思考')}      {thinking}",
            f"  {t.muted('输入任务开始，/help 查看命令，/exit 退出')}",
            rule(self.width, theme=t),
            "",
        ])

    def prompt_echo(self, text: str) -> None:
        """只在输入层没有回显时才调用（管道输入、print 模式）。

        交互模式下输入行已经显示过一遍，再调这个就是同一句话打两遍。
        """
        self._emit(["", *user_message(text, self.width, self.theme), ""])

    def on_event(self, event: Any) -> None:
        handler = getattr(self, f"_on_{event.type}", None)
        if handler:
            handler(event)

    def stop(self) -> None:
        self.ticker.stop()
        self.streamer.stop()
        self.live.finalize()

    # -- 事件处理 ------------------------------------------------------ #

    def _on_agent_start(self, event) -> None:
        self.agent_running = True
        own = self.state.own_prompt
        self.state = RenderState(turn_started=time.time(), own_prompt=own)
        self.spinner.reset()
        self.tool_count = self.tool_errors = 0
        self.ticker.start()
        self._repaint()

    def _on_turn_start(self, event) -> None:
        self._set_status("等待中...")

    def _on_message_update(self, event) -> None:
        inner = event.assistant_message_event
        kind = inner.type

        if kind == "thinking_delta":
            self.state.thinking += inner.delta
            if not self.show_thinking:
                self._set_status("思考中...", self._tail(self.state.thinking))
                return
            if not self.state.thinking_started:
                self.state.thinking_started = True
                self.state.thinking_at = time.time()
                self.live.finalize(["", self.theme.muted("╭─ 思考")])
            self.streamer.feed(self.theme.thinking(inner.delta))

        elif kind == "text_delta":
            if not self.state.text_started:
                self.state.text_started = True
                self._commit_thinking()
                self.live.finalize([""])
            self.streamer.feed(inner.delta)

    def _on_message_end(self, event) -> None:
        message = event.message
        role = getattr(message, "role", "")

        if role in ("user", "custom"):
            text = message.text()
            if text.strip() == self.state.own_prompt.strip():
                self.state.own_prompt = ""
                return
            self._flush()
            self._emit(["", *user_message(message.text(), self.width, self.theme), ""])
            return
        if role != "assistant":
            return

        self._commit_thinking()
        streamed = self.state.text_started
        self._flush()
        self.live.finalize([""] if streamed else None)

        if not streamed:
            text = message.text()
            if text.strip():          # 兜底：provider 没发 delta 的实现
                self._emit(["", self.theme.accent("● 回答"), ""])
                self._emit(markdown_lite(text, self.theme).split("\n") + [""])

        if getattr(message, "stop_reason", "") == "error":
            self._emit([self.theme.error(f"  ✗ {message.error_message}"), ""])

        usage = getattr(message, "usage", None)
        if usage and usage.input:
            self.status.tokens += usage.total_tokens
            self.status.cost += usage.cost.total
            self._emit([
                usage_line(
                    usage.input, usage.output, usage.cost.total,
                    time.time() - (self.state.turn_started or time.time()), self.theme,
                ),
                "",
            ])
        self.state.text_started = False

    def _on_tool_execution_start(self, event) -> None:
        self.tool_count += 1
        self._commit_thinking()          # 先给思考收尾，再显示工具
        summary = summarize_arguments(event.arguments)
        self.state.tool_at[event.tool_call_id] = time.time()
        self._emit([tool_call(event.tool_name, summary, self.width, self.theme)])
        self._set_status(f"执行 {event.tool_name}", summary)

    def _on_tool_execution_update(self, event) -> None:
        preview = event.partial.content[0].text if event.partial.content else ""
        self._set_status(f"执行 {event.tool_name}", self._tail(preview))

    def _on_tool_execution_end(self, event) -> None:
        started = self.state.tool_at.pop(event.tool_call_id, None)
        took = f"{time.time() - started:.1f}s" if started else ""
        if event.is_error:
            self.tool_errors += 1

        t = self.theme
        mark = t.error("✗") if event.is_error else t.success("✓")
        result = event.result
        raw = result.content[0].text if result.content else ""
        preview = first_line(raw, max(self.width - 20, 20))
        line = f"  {mark} {t.muted(preview)}"
        if took:
            line += f"  {t.muted(took)}"
        lines = [line]

        details = result.details if isinstance(result.details, dict) else {}
        if self.show_diff and details.get("patch"):
            lines += diff_block(details["patch"], self.width, self.theme, max_lines=14)
        self._emit(lines)
        self._set_status("等待中...")

    def _on_agent_end(self, event) -> None:
        self.agent_running = False
        self.ticker.stop()
        self._flush()
        self.live.finalize()
        if event.reason not in ("stop", "terminated"):
            self._emit([self.theme.warn(f"  · 结束原因：{event.reason}"), ""])

    def _on_error(self, event) -> None:
        self.agent_running = False
        self.ticker.stop()
        self._flush()
        self.live.finalize()
        self._emit([self.theme.error(f"  ✗ {event.error}"), ""])

    # -- 压缩：同步进行，期间不执行任务，所以必须让用户看得到 ----------- #

    def _on_compaction_start(self, event) -> None:
        self._flush()
        self.state.compaction_tail = ""
        self.ticker.start()
        reason = COMPACTION_REASONS.get(event.reason, event.reason)
        self._set_status("正在压缩上下文", f"{_k(event.tokens_before)} tokens · {reason} · Ctrl+C 取消")

    def _on_compaction_progress(self, event) -> None:
        self.state.compaction_tail = (self.state.compaction_tail + event.delta)[-400:]
        label = "压缩校验未通过，重试中" if event.phase == "retrying" else "正在压缩上下文"
        chunk = f"第 {event.chunk}/{event.chunks} 块 · " if event.chunks > 1 else ""
        self._set_status(label, chunk + self._tail(self.state.compaction_tail))

    def _on_compaction_end(self, event) -> None:
        t = self.theme
        if not self.agent_running:
            self.ticker.stop()
        self._flush()
        self.live.finalize()
        if event.error:
            lines = [t.warn(f"  · 压缩未执行：{event.error}")]
        elif event.cancelled:
            lines = [t.warn("  · 已取消压缩，上下文保持原样")]
        elif event.skipped:
            return
        else:
            seq = (event.state or {}).get("seq")
            before, after = event.tokens_before, event.tokens_after or 0
            saved = f"，省 {round((1 - after / before) * 100)}%" if before else ""
            lines = [t.accent(f"  ⟳ 上下文已压缩（第 {seq} 次）：{_k(before)} → {_k(after)} tokens{saved}")]
            if event.degraded:
                lines.append(t.warn("    摘要未能由模型生成，已降级为确定性记录（用户原话、文件、命令）"))
            if event.restored:
                lines.append(t.muted(f"    补回了模型遗漏的 {len(event.restored)} 条约束/决策/认知"))
        self._emit([*lines, ""])
        self._set_status("等待中...")

    def _on_context_pruned(self, event) -> None:
        self._emit([self.theme.muted(
            f"  · 已清理 {event.count} 个旧工具输出（可重新读取）："
            f"{_k(event.tokens_before)} → {_k(event.tokens_after)} tokens"
        )])

    # -- 汇总 ---------------------------------------------------------- #

    def summary(self, model: str, turns: int, context_used: int, context_max: int) -> None:
        t = self.theme
        tools = str(self.tool_count)
        if self.tool_errors:
            tools += f"  {t.error(f'{self.tool_errors} 失败')}"
        elif self.tool_count:
            tools += f"  {t.success('全部成功')}"
        pct = f"{context_used / context_max * 100:.1f}%" if context_max else "?"
        self._emit([
            rule(self.width, "会话统计", t),
            *key_value([
                ("模型", model),
                ("轮次", str(turns)),
                ("工具调用", tools),
                ("上下文",
                 f"{context_used // 1000}k / {context_max // 1000}k  {t.muted(f'({pct})')}"),
            ], t),
            "",
        ])

    def status_line(self) -> str:
        return self.status.render(self.width)

    # -- 内部 ---------------------------------------------------------- #

    def _flush(self) -> None:
        """吐完缓冲里剩余的字。插入任何其它内容之前都要先调，否则顺序会乱。"""
        self.streamer.flush()

    def _commit_thinking(self) -> None:
        """给思考收尾。

        三个触发点：正文开始、工具开始、消息结束。只有第一个的话，"思考完
        直接调工具"那一轮的思考会没有结尾，和工具日志连成一片。
        """
        if self.state.thinking_committed or not self.state.thinking.strip():
            return
        self.state.thinking_committed = True
        if self.state.thinking_started:
            self._flush()
            elapsed = time.time() - self.state.thinking_at
            self.live.stream_text("\n")
            self._emit([self.theme.muted(f"╰─ 思考结束 · {elapsed:.1f}s"), ""])
            return
        if self.show_thinking:
            self._emit(thinking_block(self.state.thinking, self.width, self.theme) + [""])

    def _set_status(self, label: str, detail: str = "") -> None:
        self.state.label = label
        self.state.detail = detail
        self._repaint()

    def _repaint(self) -> None:
        # 流式写入期间不画状态行，否则转圈会插进句子中间
        if self.state.text_started or self.state.thinking_started:
            if not self.state.thinking_committed and self.state.thinking_started:
                return
            if self.state.text_started:
                return
        self.live.update([self.spinner.render(self.state.label, self.state.detail)])

    def _tail(self, text: str, limit: int = 60) -> str:
        flat = " ".join(text.split())
        return truncate(flat[-limit * 2:], limit) if flat else ""

    def _emit(self, lines: list[str]) -> None:
        self._flush()
        self.live.append(lines)