"""事件 → 界面。

一轮的样子（过程收起，结论突出）：

    ❯ 查看一下当前代码
      思考了 1.2s
    ▸ 4 个工具调用 · bash ×2, read ×2 · 1 失败   ctrl+o 展开
    ● 当前目录是一个很小的示例项目：
      - README.md：说明文档

    ✻ 用时 7.9s · 上下文 ↑4.1k · 输出 ↓86

设计原则：

1. **过程收起、结论突出**。思考和工具只留一行摘要；运行时底部只有一行状态，
   显示正在执行的那个工具（名字闪烁），不打印参数。细节按 ctrl+o（或 /expand）
   展开上一轮再看。
2. **正文真流式**，经 TextStreamer 匀速吐字；以 ● 开头，续行缩进两格。
3. **每轮一行统计**，亮色：用时、上下文大小、输出 token。不再每次调用模型都打一行。
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
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
    truncate,
    user_message,
    wrap,
)

TOOL_SUMMARY_KEYS = ("path", "command", "pattern", "url", "query", "old_text")
EXPAND_HINT = "ctrl+o 展开"
EXPAND_RESULT_LINES = 20
ANSWER_MARK = "●"
INDENT = "  "


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


def _seconds(s: float) -> str:
    return f"{s:.1f}s" if s < 60 else f"{int(s // 60)}m{int(s % 60):02d}s"


def first_line(text: str, limit: int) -> str:
    for line in (text or "").split("\n"):
        if line.strip():
            return truncate(line.strip(), limit)
    return ""


@dataclass
class ToolRecord:
    """一次工具调用，展开时用。"""

    call_id: str
    name: str
    arguments: Any
    started: float
    took: float | None = None
    is_error: bool = False
    output: str = ""
    patch: str | None = None


@dataclass
class TurnRecord:
    """一轮的过程记录：思考全文、全部工具调用。ctrl+o 展开的就是它。"""

    thinking: list[tuple[str, float]] = field(default_factory=list)
    tools: list[ToolRecord] = field(default_factory=list)


@dataclass
class RenderState:
    """一轮之内的临时状态。渲染器不持有会话状态，只持有画到哪儿了。"""

    thinking: str = ""
    # 这次调用模型的开始时间：思考时长从这里算（等第一个 token 也是思考的一部分）
    call_started: float = 0.0
    text_started: bool = False
    turn_started: float = 0.0
    label: str = "思考中"
    detail: str = ""
    own_prompt: str = ""
    compaction_tail: str = ""
    # 已执行完、还没打印摘要的工具（遇到正文或一轮结束时收成一行）
    pending_tools: list[ToolRecord] = field(default_factory=list)
    running: dict[str, ToolRecord] = field(default_factory=dict)
    context_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    tool_total: int = 0


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
        self.turn = TurnRecord()
        self.last_turn: TurnRecord | None = None
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

        think = f" · think {thinking}" if thinking != "off" else ""
        self._emit([
            (f"{t.accent('✻', bold=True)} {t.bold('pi coding agent')}  "
             f"{t.muted(f'{model}{think}')}"),
            f"  {t.muted(f'{Path(cwd).name}  {cwd}')}",
            f"  {t.muted('/help 查看命令 · ctrl+o 展开上一轮的思考和工具 · /exit 退出')}",
            "",
        ])

    def prompt_echo(self, text: str) -> None:
        """只在输入层没有回显时才调用（管道输入、print 模式）。"""
        self._emit(["", *user_message(text, self.width, self.theme), ""])

    def user_echo(self, text: str) -> None:
        """输入框提交后被擦掉（erase_when_done），用这一行把用户的话留在 scrollback 里"""
        t = self.theme
        lines = text.split("\n") or [""]
        out = [f"{t.accent('❯', bold=True)} {t.bold(lines[0])}"]
        out += [f"{INDENT}{t.bold(line)}" for line in lines[1:]]
        self._emit(out)

    def input_prompt(self) -> str:
        """输入框：一条带提示的细线 + ❯。配合 LineReader 的 erase_when_done，提交后整块擦掉

            ── glm-5.2 · 16.9k tok · ctrl+o 展开 · /help ──────────────
            ❯ _
        """
        bits = [self.status.model]
        if self.status.thinking and self.status.thinking != "off":
            bits.append(f"think {self.status.thinking}")
        if self.status.tokens:
            bits.append(f"{_k(self.status.tokens)} tok")
        if self.last_turn and (self.last_turn.tools or self.last_turn.thinking):
            bits.append(EXPAND_HINT)
        bits.append("/help")
        label = " · ".join(b for b in bits if b)
        return f"{rule(self.width, label, self.theme)}\n{self.theme.accent('❯', bold=True)} "

    def on_event(self, event: Any) -> None:
        handler = getattr(self, f"_on_{event.type}", None)
        if handler:
            handler(event)

    def stop(self) -> None:
        self.ticker.stop()
        self.streamer.stop()
        self.live.finalize()

    def expand_last_turn(self) -> None:
        """ctrl+o / /expand：打印上一轮的思考全文和每个工具的参数、结果"""
        t = self.theme
        turn = self.last_turn
        if turn is None or not (turn.tools or turn.thinking):
            self._emit([t.muted("  上一轮没有可展开的思考或工具调用"), ""])
            return
        width = self.width
        lines = [rule(width, "上一轮详情", t)]
        for text, seconds in turn.thinking:
            lines.append(t.thinking(f"✻ 思考 · {_seconds(seconds)}"))
            lines += [f"{t.thinking('│')} {t.muted(line)}" for line in wrap(text.strip(), width - 2)]
        for record in turn.tools:
            mark = t.error("✗") if record.is_error else t.success("✓")
            took = f"  {t.muted(_seconds(record.took))}" if record.took is not None else ""
            args = summarize_arguments(record.arguments)
            lines.append(f"{mark} {t.accent(record.name, bold=True)} {t.muted(truncate(args, width - 20))}{took}")
            if record.patch and self.show_diff:
                lines += diff_block(record.patch, width, t, max_lines=EXPAND_RESULT_LINES)
                continue
            output = record.output.rstrip("\n").split("\n") if record.output.strip() else []
            for line in output[:EXPAND_RESULT_LINES]:
                lines.append(f"  {t.muted('│')} {truncate(line, width - 4)}")
            if len(output) > EXPAND_RESULT_LINES:
                lines.append(f"  {t.muted(f'… 还有 {len(output) - EXPAND_RESULT_LINES} 行')}")
        # 不画收尾线：下面紧跟着输入框的上边线
        self._emit([*lines, ""])

    # -- 事件处理 ------------------------------------------------------ #

    def _on_agent_start(self, event) -> None:
        self.agent_running = True
        own = self.state.own_prompt
        self.state = RenderState(turn_started=time.time(), own_prompt=own)
        self.turn = TurnRecord()
        self.spinner.reset()
        self.ticker.start()
        self._set_status("思考中")

    def _on_turn_start(self, event) -> None:
        self.state.call_started = time.time()
        if not self.state.running:
            self._set_status("思考中")

    def _on_message_update(self, event) -> None:
        inner = event.assistant_message_event
        kind = inner.type

        if kind == "thinking_delta":
            self.state.thinking += inner.delta
            self._set_status("思考中")

        elif kind == "text_delta":
            if not self.state.text_started:
                self._begin_answer()
            # 续行缩进，和 ● 后面的第一行对齐
            self.streamer.feed(inner.delta.replace("\n", "\n" + INDENT))

    def _on_message_end(self, event) -> None:
        message = event.message
        role = getattr(message, "role", "")

        if role in ("user", "custom"):
            text = message.text()
            if text.strip() == self.state.own_prompt.strip():
                self.state.own_prompt = ""
                return
            # 途中插进来的消息（steering / follow-up）
            self._flush_tools()
            self.user_echo(text)
            return
        if role != "assistant":
            return

        self._commit_thinking()
        streamed = self.state.text_started
        self._flush()
        if streamed:
            self.live.finalize()
        else:
            text = message.text()
            if text.strip():          # 兜底：provider 没发 delta 的实现
                self._begin_answer()
                self._flush()
                self.live.stream_text(markdown_lite(text, self.theme).replace("\n", "\n" + INDENT))
                self.live.finalize()

        if getattr(message, "stop_reason", "") == "error":
            self._flush_tools()
            self._emit([self.theme.error(f"{INDENT}✗ {message.error_message}")])

        usage = getattr(message, "usage", None)
        if usage and (usage.input or usage.output):
            self.status.tokens += usage.total_tokens or (usage.input + usage.output)
            self.status.cost += usage.cost.total
            self.state.context_tokens = usage.input + usage.cache_read + usage.cache_write
            self.state.output_tokens += usage.output
            self.state.cost += usage.cost.total
        self.state.text_started = False
        if not self.state.running:
            self._set_status("思考中")

    def _on_tool_execution_start(self, event) -> None:
        self._commit_thinking()
        self._end_answer()
        record = ToolRecord(event.tool_call_id, event.tool_name, event.arguments, time.time())
        self.state.running[event.tool_call_id] = record
        self.turn.tools.append(record)
        self.tool_count += 1
        self.state.tool_total += 1
        self._repaint()

    def _on_tool_execution_update(self, event) -> None:
        self._repaint()

    def _on_tool_execution_end(self, event) -> None:
        record = self.state.running.pop(event.tool_call_id, None)
        if record is None:
            return
        record.took = time.time() - record.started
        record.is_error = bool(event.is_error)
        result = event.result
        record.output = "".join(getattr(c, "text", "") or "" for c in (result.content or []))
        details = result.details if isinstance(result.details, dict) else {}
        record.patch = details.get("patch")
        if record.is_error:
            self.tool_errors += 1
        self.state.pending_tools.append(record)
        self._repaint()

    def _on_agent_end(self, event) -> None:
        self.agent_running = False
        self.ticker.stop()
        self._commit_thinking()
        self._flush()
        self.live.finalize()
        self._flush_tools()
        lines = []
        if event.reason not in ("stop", "terminated"):
            lines.append(self.theme.warn(f"{INDENT}· 结束原因：{event.reason}"))
        lines += ["", self._turn_footer(), ""]
        self._emit(lines)
        self.last_turn = self.turn

    def _on_error(self, event) -> None:
        self.agent_running = False
        self.ticker.stop()
        self._flush()
        self.live.finalize()
        self._flush_tools()
        self._emit([self.theme.error(f"{INDENT}✗ {event.error}"), ""])
        self.last_turn = self.turn

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
            lines = [t.warn(f"{INDENT}· 压缩未执行：{event.error}")]
        elif event.cancelled:
            lines = [t.warn(f"{INDENT}· 已取消压缩，上下文保持原样")]
        elif event.skipped:
            return
        else:
            seq = (event.state or {}).get("seq")
            before, after = event.tokens_before, event.tokens_after or 0
            saved = f"，省 {round((1 - after / before) * 100)}%" if before else ""
            lines = [(f"{t.accent('⟳')} {t.muted(f'上下文已压缩（第 {seq} 次）：')}"
                      f"{t.info(f'{_k(before)} → {_k(after)}')}{t.muted(f' tokens{saved}')}")]
            if event.degraded:
                lines.append(t.warn(f"{INDENT}摘要未能由模型生成，已降级为确定性记录（用户原话、文件、命令）"))
            if event.restored:
                lines.append(t.muted(f"{INDENT}补回了模型遗漏的 {len(event.restored)} 条约束/决策/认知"))
        self._emit(lines)
        self._set_status("思考中")

    def _on_context_pruned(self, event) -> None:
        self._emit([self.theme.muted(
            f"{INDENT}已清理 {event.count} 个旧工具输出（可重新读取）："
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

    def _turn_footer(self) -> str:
        t = self.theme
        state = self.state
        elapsed = time.time() - (state.turn_started or time.time())
        parts = [f"{t.highlight('✻', bold=True)} {t.muted('用时')} {t.highlight(_seconds(elapsed), bold=True)}"]
        if state.context_tokens:
            parts.append(f"{t.muted('上下文')} {t.info('↑' + _k(state.context_tokens), bold=True)}")
        if state.output_tokens:
            parts.append(f"{t.muted('输出')} {t.success('↓' + _k(state.output_tokens), bold=True)}")
        if state.tool_total:
            parts.append(t.muted(f"{state.tool_total} 次工具"))
        if state.cost:
            parts.append(t.warn(f"¥{state.cost:.4f}"))
        return f" {t.muted('·')} ".join(parts)

    def _begin_answer(self) -> None:
        """正文开始：先把思考和工具摘要收好，再以 ● 起头"""
        self._commit_thinking()
        self._flush_tools()
        self._flush()
        self.live.finalize()
        self.state.text_started = True
        self.live.stream_text(f"{self.theme.accent(ANSWER_MARK)} ")

    def _end_answer(self) -> None:
        if self.state.text_started:
            self._flush()
            self.live.finalize()
            self.state.text_started = False

    def _flush_tools(self) -> None:
        """把攒着的工具收成一行：▸ 4 个工具调用 · bash ×2, read ×2 · 1 失败   ctrl+o 展开"""
        tools = self.state.pending_tools
        if not tools:
            return
        self.state.pending_tools = []
        t = self.theme
        counts = Counter(r.name for r in tools)
        names = ", ".join(f"{n} ×{c}" if c > 1 else n for n, c in counts.most_common())
        failed = sum(r.is_error for r in tools)
        line = (f"{t.muted('▸')} {t.muted(f'{len(tools)} 个工具调用')} {t.muted('·')} "
                f"{t.accent(truncate(names, max(self.width - 40, 20)))}")
        if failed:
            line += f" {t.muted('·')} {t.error(f'{failed} 失败')}"
        line += f"   {t.muted(EXPAND_HINT)}"
        self._flush()
        self.live.finalize()
        self.live.append([line])

    def _flush(self) -> None:
        """吐完缓冲里剩余的字。插入任何其它内容之前都要先调，否则顺序会乱。"""
        self.streamer.flush()

    def _commit_thinking(self) -> None:
        """思考收成一行：  思考了 1.2s（全文按 ctrl+o 展开）"""
        text = self.state.thinking
        if not text.strip():
            return
        self.state.thinking = ""
        seconds = time.time() - (self.state.call_started or self.state.turn_started or time.time())
        if self.show_thinking:
            self.turn.thinking.append((text, seconds))
        self._flush()
        self.live.finalize()
        self.live.append([self.theme.muted(f"{INDENT}思考了 {_seconds(seconds)}")])

    def _set_status(self, label: str, detail: str = "") -> None:
        self.state.label = label
        self.state.detail = detail
        self._repaint()

    def _status_text(self) -> str:
        t = self.theme
        frame = t.accent(self.spinner.frame())
        elapsed = t.muted(_seconds(time.time() - (self.state.turn_started or time.time())))
        running = list(self.state.running.values())
        if running:
            # 只显示正在执行的那个工具，名字闪烁；不打印参数
            current = running[-1]
            on = int(time.time() * 2) % 2 == 0
            name = t.accent(current.name, bold=True) if on else t.accent(current.name, dim=True)
            more = t.muted(f" 等 {len(running)} 个") if len(running) > 1 else ""
            took = t.muted(_seconds(time.time() - current.started))
            return f"{frame} {name}{more}  {took}"
        detail = f"  {t.muted(self.state.detail)}" if self.state.detail else ""
        return f"{frame} {t.muted(self.state.label + '…')}  {elapsed}{detail}"

    def _repaint(self) -> None:
        # 正文流式写入期间不画状态行，否则转圈会插进句子中间
        if self.state.text_started:
            return
        self.live.update([self._status_text()])

    def _tail(self, text: str, limit: int = 60) -> str:
        flat = " ".join(text.split())
        return truncate(flat[-limit * 2:], limit) if flat else ""

    def _emit(self, lines: list[str]) -> None:
        self._flush()
        self.live.append(lines)
