"""事件 → 界面。

一轮的样子（同 Codex：过程只占一块位置，结论留下来）：

运行中，问题下面只有一块会重绘的区域——第一行是转圈 + 本轮已用时间，下面是"当前在干什么"。
思考、过渡的话、工具共用这一块，后来的顶掉先来的：

    ❯ 分析当前代码
    ⠋ 12.3s · 思考中 · 5 次工具
      让我再看一下几个核心模块，来更深入地分析代码……     ← 思考（只显示最新几行）

    ⠙ 15.1s · 执行工具 · 6 次工具
      read 等 3 个  1.2s                                  ← 工具顶掉了思考（名字闪烁，不带参数）

结束后 scrollback 里只留：

    ❯ 分析当前代码
    ✻ 42.3s · 12 次工具 bash ×6, read ×6 · 1 失败   ctrl+o 展开
    ● 最终回答……
      ……

    ↑4.1k · ↓344

设计原则：

1. **过程不落盘**：思考、工具、工具前那句"让我先看看目录"之类的过渡话都只在活动区里显示，
   被下一步顶掉；全文按 ctrl+o（/expand）展开上一轮再看（TurnRecord.steps，按发生顺序）。
2. **只有最终回答进 scrollback**。正文开始时还不知道是不是最终回答：先放在活动区，
   这次调用以工具调用收尾 → 是过渡话，丢掉；没有工具调用 → 是回答，写出去。
   超过活动区行数的长文字直接当回答流式写出（过渡话不会这么长）。
3. **本轮用时在最上面**：回答开始前写一行「✻ 用时 · 工具」，回答结束后一行 token 统计。
4. **回答按 Markdown 排版**（pi_tui.MarkdownStream，同 Codex 按整行提交）：一行源文本收齐才渲染成
   终端行写进 scrollback，还在写的半行和没收齐的表格显示在活动区里，所以仍是实时的。
   以 ● 开头，折行带悬挂缩进；首尾空行去掉，连续空行压成一个。
5. **留白**：每次提问前空一行，用时行和回答之间空一行，一问一答之间不粘在一起。
6. **输入框**上下各一条细线，线上不放模型、token 之类的文字。
"""

from __future__ import annotations

import json
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, TextIO

from pi_tui import (
    LiveRegion,
    MarkdownStream,
    Spinner,
    StatusBar,
    Theme,
    Ticker,
    diff_block,
    key_value,
    render_markdown,
    rule,
    terminal_height,
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
SLOT_LINES = 6        # 活动区里思考 / 过渡话最多显示几行（只显示最新的）
PREVIEW_ROWS = 12     # 回答还没定稿的部分（半行、没收齐的表格）在活动区里最多显示几行
PLAN_TOOL = "update_plan"   # 任务计划（内置扩展 todo）：不算工具调用，显示成勾选单
SUBMIT_TOOL = "submit_plan"  # 计划模式提交计划：不算工具调用，计划本身单独成块显示
ASK_TOOL = "ask_user"         # 计划模式向用户提问：界面弹选择器，答案单独成行显示
QUIET_TOOLS = (PLAN_TOOL, SUBMIT_TOOL, ASK_TOOL)
PLAN_ROWS = 8         # 计划最多显示几步
ECHO_ROWS = (4, 8)    # 回显用户输入最多几行（终端高度的 1/3，夹在这个范围里），多的折叠


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


def _message_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content.strip()
    return "".join(getattr(block, "text", "") for block in content or []
                   if getattr(block, "type", "") == "text").strip()


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
    """一轮的过程记录。ctrl+o 展开的就是它。

    steps 按发生顺序：("thinking", (全文, 秒)) / ("text", 过渡话) / ("tool", ToolRecord)
    / ("plan", 计划步骤列表)
    """

    thinking: list[tuple[str, float]] = field(default_factory=list)
    tools: list[ToolRecord] = field(default_factory=list)
    steps: list[tuple[str, Any]] = field(default_factory=list)
    prompt: str = ""        # 回显时折叠了的用户输入（全文），展开时放在最前面


@dataclass
class RenderState:
    """一轮之内的临时状态。渲染器不持有会话状态，只持有画到哪儿了。"""

    thinking: str = ""              # 这次调用还没收尾的思考
    # 这次调用模型的开始时间：思考时长从这里算（等第一个 token 也是思考的一部分）
    call_started: float = 0.0
    text: str = ""                  # 这次调用的正文，还不知道是不是最终回答
    answering: bool = False         # 已确定是最终回答，正在流式写进 scrollback
    markdown: MarkdownStream | None = None   # 正在写的回答
    # 活动区当前显示什么："thinking" / "text" / "tools" / ""；后来的顶掉先来的
    slot: str = ""
    shown: str = ""                 # slot 为 thinking / text 时显示的文字
    batch: list[ToolRecord] = field(default_factory=list)   # slot 为 tools 时的这一批
    summary_done: bool = False      # 本轮的「✻ 用时 · 工具」已经写出
    segment_start: int = 0          # 这一段从第几个工具算起（回答之后被提醒 / 插话续跑算新的一段）
    # 本轮动过（或带着没做完的）任务清单：运行中常驻活动区，回答开始前写在用时行下面
    plan_touched: bool = False
    turn_started: float = 0.0
    label: str = "思考中"
    detail: str = ""
    own_prompt: str = ""
    compaction_tail: str = ""
    running: dict[str, ToolRecord] = field(default_factory=dict)
    context_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    tool_total: int = 0
    retries: int = 0                # 本轮模型调用临时失败后重试了几次（并进用时行）


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
        # 最后写出的一行是不是空行：提问前补空行时用，避免连着两个空行
        self._blank_tail = True
        # 本轮清理旧工具输出的次数和省下的 tokens（写进「✻ 用时」那一行后清零）
        self._prune_times = 0
        self._pruned_tokens = 0
        # 任务清单跨轮保留：批准计划时在一轮开始前生成，执行时一轮轮往下勾
        self.plan: list[dict] | None = None
        self.mode = "chat"      # 当前工作模式，输入框下面显示
        self.permission = ""    # 不是默认（每次确认）时的权限模式名，输入框下面显示
        self._folded_prompt = ""    # 刚回显时折叠了的用户输入，下一轮开始时记进 TurnRecord
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
        """输入框提交后被擦掉（erase_when_done），用这一行把用户的话留在 scrollback 里。
        太长的（粘贴了一大段）只留开头几行 + 一行说明，全文按 ctrl+o 看——否则小窗口里
        整屏都是自己刚输的话，模型的输出只能一点点往上挤"""
        t = self.theme
        rows = [row for line in text.split("\n") for row in (wrap(line, self.width - 2) or [""])]
        limit = max(ECHO_ROWS[0], min(ECHO_ROWS[1], terminal_height() // 3))
        hidden = 0
        self._folded_prompt = ""        # 只记最近这一句：上一句可能是没触发运行的命令
        if len(rows) > limit:
            hidden = len(rows) - (limit - 1)
            rows = rows[:limit - 1]
            self._folded_prompt = text
        out = [] if self._blank_tail else [""]      # 和上一问一答分开
        out.append(f"{t.accent('❯', bold=True)} {t.bold(rows[0])}")
        out += [f"{INDENT}{t.bold(row)}" for row in rows[1:]]
        if hidden:
            out.append(f"{INDENT}{t.muted(f'… 还有 {hidden} 行（共 {len(text)} 字）· ctrl+o 看全文')}")
        self._emit(out)

    def input_prompt(self) -> str:
        """输入框上沿 + ❯。和 Claude Code 一样上下各一条细线把输入夹住，线上不带任何文字；
        下沿由 LineReader 的 footer 画（input_footer）。配合 erase_when_done，提交后整块擦掉

            ────────────────────────────
            ❯ _
            ────────────────────────────
        """
        return f"{rule(self.width, '', self.theme)}\n{self.theme.accent('❯', bold=True)} "

    def input_footer(self) -> str:
        """输入框下沿 + 当前模式（shift+tab 切换，同 Claude Code）"""
        t = self.theme
        if self.mode == "plan":
            hint = (f"{t.info('⏸ 计划模式', bold=True)}"
                    f"{t.muted(' · 只读调研，先出计划再动手 · shift+tab 切换')}")
        else:
            hint = t.muted("⏵ 执行模式 · shift+tab 切换到计划模式")
        if self.permission:
            hint += f"  {t.warn('⚠ ' + self.permission)}"
        return f"{rule(self.width, '', t)}\n  {hint}"

    def on_event(self, event: Any) -> None:
        handler = getattr(self, f"_on_{event.type}", None)
        if handler:
            handler(event)

    def stop(self) -> None:
        self.ticker.stop()
        self.live.finalize()

    def expand_last_turn(self) -> None:
        """ctrl+o / /expand：按发生顺序打印上一轮的思考全文、过渡话、每个工具的参数和结果"""
        t = self.theme
        turn = self.last_turn
        if turn is None or not (turn.steps or turn.prompt):
            self._emit([t.muted("  上一轮没有可展开的思考或工具调用"), ""])
            return
        width = self.width
        lines = [rule(width, "上一轮详情", t)]
        if turn.prompt:
            lines.append(t.accent("❯ 你的输入（全文）", bold=True))
            lines += [f"{t.accent('│')} {line}" for raw in turn.prompt.split("\n")
                      for line in (wrap(raw, width - 2) or [""])]
        for kind, value in turn.steps:
            if kind == "thinking":
                text, seconds = value
                lines.append(t.thinking(f"✻ 思考 · {_seconds(seconds)}"))
                lines += [f"{t.thinking('│')} {t.muted(line)}" for line in wrap(text.strip(), width - 2)]
            elif kind == "text":
                lines += [f"{t.muted('◦')} {t.muted(line)}" for line in wrap(value, width - 2)]
            elif kind == "plan":
                marks = {"completed": "✓", "in_progress": "▸"}
                lines.append(t.accent("☰ 计划"))
                lines += [f"  {marks.get(item.get('status'), '○')} {truncate(str(item.get('step', '')), width - 4)}"
                          for item in value]
            else:
                record = value
                mark = t.error("✗") if record.is_error else t.success("✓")
                took = f"  {t.muted(_seconds(record.took))}" if record.took is not None else ""
                args = summarize_arguments(record.arguments)
                lines.append(f"{mark} {t.accent(record.name, bold=True)} "
                             f"{t.muted(truncate(args, width - 20))}{took}")
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
        self.state = RenderState(turn_started=time.time(), own_prompt=own,
                                 plan_touched=self._plan_open())
        self.turn = TurnRecord(prompt=self._folded_prompt)
        self._folded_prompt = ""
        self.spinner.reset()
        self.ticker.start()
        self._set_status("思考中")

    def _on_turn_start(self, event) -> None:
        self.state.call_started = time.time()
        self.state.text = ""
        if not self.state.running:
            self._set_status("思考中")

    def _on_message_update(self, event) -> None:
        inner = event.assistant_message_event
        kind = inner.type

        if kind == "thinking_delta":
            self.state.thinking += inner.delta
            if not self.state.answering:
                self._show("thinking", inner.delta)
                self._set_status("思考中")

        elif kind == "thinking_end":
            self._commit_thinking()

        elif kind == "text_delta":
            if self.state.answering:
                self._feed_answer(inner.delta)
                return
            self.state.text += inner.delta
            self._show("text", inner.delta)
            if len(self._slot_lines(self.state.text, limit=None)) > SLOT_LINES:
                # 过渡话不会这么长：当成最终回答，开始流式写进 scrollback
                self._begin_answer(self.state.text)
            else:
                self._set_status("输出中")

    def _on_message_end(self, event) -> None:
        message = event.message
        role = getattr(message, "role", "")

        if role in ("user", "custom"):
            details = getattr(message, "details", None) if role == "custom" else None
            if isinstance(details, dict) and details.get("notice"):
                # 扩展插进来的提醒（如 tool_budget）：一行说明，不当成用户说的话回显；
                # quiet 的（定期重锚）不打扰，只记进本轮过程，ctrl+o 看得到
                if details.get("quiet"):
                    self.turn.steps.append(("text", f"↺ {details['notice']}"))
                    return
                self._emit([f"{INDENT}{self.theme.warn('⚑')} {self.theme.muted(details['notice'])}"])
                self._new_segment()
                return
            text = message.text()
            if text.strip() == self.state.own_prompt.strip():
                self.state.own_prompt = ""
                return
            # 途中插进来的消息（steering / follow-up）
            self.user_echo(text)
            self._new_segment()
            return
        if role != "assistant":
            return

        self._commit_thinking()
        if self.state.answering:
            self._end_answer()
        else:
            # 没收到 delta 的 provider 也走这里
            text = (self.state.text or message.text()).strip()
            has_tools = any(getattr(b, "type", "") == "toolCall" for b in message.content or [])
            if text and has_tools:
                # 工具前的过渡话（"让我先看看目录"）：只在活动区里显示，工具开始就被顶掉
                self.turn.steps.append(("text", text))
            elif text:
                self._begin_answer(text)
                self._end_answer()

        if getattr(message, "stop_reason", "") == "error":
            self._commit_summary()
            self._emit([self.theme.error(f"{INDENT}✗ {message.error_message}")])

        usage = getattr(message, "usage", None)
        if usage and (usage.input or usage.output):
            self.status.tokens += usage.total_tokens or (usage.input + usage.output)
            self.status.cost += usage.cost.total
            self.state.context_tokens = usage.input + usage.cache_read + usage.cache_write
            self.state.output_tokens += usage.output
            self.state.cost += usage.cost.total
        self.state.text = ""

    def _on_tool_execution_start(self, event) -> None:
        self._commit_thinking()
        self._end_answer()
        if event.tool_name in QUIET_TOOLS:
            return      # 计划相关的工具不算工具调用，内容经 todo_updated / plan_proposed 事件显示
        record = ToolRecord(event.tool_call_id, event.tool_name, event.arguments, time.time())
        self.state.running[event.tool_call_id] = record
        if self.state.slot != "tools":
            # 工具顶掉思考 / 过渡话
            self.state.slot, self.state.shown, self.state.batch = "tools", "", []
        self.state.batch.append(record)
        self.turn.tools.append(record)
        self.turn.steps.append(("tool", record))
        self.tool_count += 1
        self.state.tool_total += 1
        self._set_status("执行工具")

    def _on_tool_execution_update(self, event) -> None:
        self._repaint()

    def _on_tool_execution_end(self, event) -> None:
        if event.tool_name == ASK_TOOL:
            self._show_answers(event.result)
        if event.tool_name in QUIET_TOOLS:
            return
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
        if not self.state.running:
            self._set_status("思考中")
        else:
            self._repaint()

    def _on_agent_end(self, event) -> None:
        self.agent_running = False
        self.ticker.stop()
        self._commit_thinking()
        self._end_answer()
        self.live.finalize()
        # 没有回答（中断、出错、只调了工具）时，用时在这里写
        self._commit_summary()
        lines = []
        if event.reason not in ("stop", "terminated"):
            lines.append(self.theme.warn(f"{INDENT}· 结束原因：{event.reason}"))
        if self.state.plan_touched and self._plan_open() and self.mode != "plan":
            left = sum(s.get("status") != "completed" for s in self.plan)
            lines.append(self.theme.muted(f"{INDENT}○ 任务清单还有 {left} 项没完成，说「继续」接着做"))
        tokens = self._token_footer()
        if tokens:
            lines += ["", tokens]
        self._emit([*lines, ""])
        self.last_turn = self.turn

    def _on_error(self, event) -> None:
        self.agent_running = False
        self.ticker.stop()
        self._commit_thinking()
        self._end_answer()
        self.live.finalize()
        self._commit_summary()
        self._emit([self.theme.error(f"{INDENT}✗ {event.error}"), ""])
        self.last_turn = self.turn

    # -- 压缩：同步进行，期间不执行任务，所以必须让用户看得到 ----------- #

    def _on_compaction_start(self, event) -> None:
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

    def _on_retry(self, event) -> None:
        """模型调用临时失败（限流、过载、断网），等一会儿重试：活动区显示原因和等多久，不往 scrollback 写
        （可能连着好几次）；失败那次已经显示的思考 / 正文作废，次数并进本轮的用时行"""
        self.state.retries += 1
        self.state.thinking = self.state.text = ""
        if self.state.slot in ("thinking", "text"):
            self.state.slot, self.state.shown = "", ""
        reason = first_line(event.error, 48) or "服务暂时不可用"
        step = f"⟳ 第 {event.attempt}/{event.max_attempts} 次重试（等 {event.delay:g}s）：{event.error}"
        self.turn.steps.append(("text", step))
        self._set_status("等待重试", f"{reason} · {event.delay:g}s 后第 {event.attempt}/{event.max_attempts} 次")

    def _on_todo_updated(self, event) -> None:
        """任务清单变了（update_plan、批准计划生成清单）：活动区实时刷新。
        模型调整了清单（带 explanation）就留一行原因——用户批准的计划变了，得让人看到为什么"""
        self.plan = list(event.plan or [])
        self.state.plan_touched = True
        self.turn.steps.append(("plan", self.plan))
        explanation = " ".join(str(getattr(event, "explanation", "") or "").split())
        if explanation:
            self.turn.steps.append(("text", f"调整计划：{explanation}"))
            self._emit([f"{INDENT}{self.theme.warn('↻')} {self.theme.muted('调整计划：' + explanation)}"])
            return
        self._repaint()

    def _on_mode_changed(self, event) -> None:
        # 可能在输入框里按 shift+tab 时触发：只改状态，不打印（输入框重画时自己读）
        self.mode = event.mode

    def _on_plan_proposed(self, event) -> None:
        """计划模式下模型提交了计划：用时行之后单独成块显示，下面等用户确认"""
        from pi_coding_agent.builtin_extensions.plan_mode import plan_markdown

        t = self.theme
        plan = event.plan
        self._commit_thinking()
        self._commit_summary()
        self.live.finalize()
        bar = t.info("│", bold=True) + " "
        body = render_markdown(plan_markdown(plan), self.width, t, bar, bar)
        title = plan.get("title") or "计划"
        self._emit(["", f"{t.info('╭', bold=True)} {t.info('计划', bold=True)} {t.bold(title)}",
                    *[line if line else bar.rstrip() for line in body], t.info("╰", bold=True)])
        self.turn.steps.append(("text", f"提交计划：{title}"))

    # -- 向用户提问（ask_user） ------------------------------------------ #

    def pause(self) -> None:
        """运行中要弹选择器：停掉转圈、收起活动区，把屏幕让给选择器"""
        self.ticker.stop()
        self._commit_thinking()
        self.live.finalize()

    def resume(self) -> None:
        if self.agent_running:
            self.ticker.start()
            self._repaint()

    def _show_answers(self, result: Any) -> None:
        """用户答完的问题留在 scrollback 里：一题一行「? 主题 → 回答」"""
        details = getattr(result, "details", None)
        if not isinstance(details, dict) or "answers" not in details:
            return      # 没问成（没有界面、参数不对）：模型会在回复里问
        t = self.theme
        answers = details["answers"]
        if not answers:
            lines = [t.muted(f"{INDENT}? 跳过了提问，按推荐的选项继续")]
        else:
            lines = []
            headers = {q["question"]: q.get("header") for q in details.get("questions") or []}
            for answer in answers:
                topic = headers.get(answer["question"]) or answer["question"]
                topic = truncate(topic, max(self.width // 2, 20))
                lines.append(f"{INDENT}{t.accent('?', bold=True)} {topic} {t.muted('→')} "
                             f"{t.bold(answer['answer'])}")
        # ctrl+o 展开时给完整的问题
        record = [f"提问：{a['question']} → {a['answer']}" for a in answers] or ["跳过了提问"]
        self.turn.steps.append(("text", "\n".join(record)))
        self._emit(lines)

    def _plan_open(self) -> bool:
        return bool(self.plan) and any(s.get("status") != "completed" for s in self.plan)

    def _on_context_pruned(self, event) -> None:
        """L1 清理不单独占一行（一轮里可能有好几次，会刷屏）：计数，运行中显示在活动区第一行，
        结束后并进本轮的「✻ 用时」那一行。清理可能发生在一轮开始之前，所以计数不放在 RenderState"""
        self._prune_times += 1
        self._pruned_tokens += max(event.tokens_before - event.tokens_after, 0)
        self._repaint()

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

    def print_lines(self, lines: list[str]) -> None:
        """命令输出等非事件内容"""
        self._emit(lines)

    def reset(self) -> None:
        """换会话（/clear、/resume）：清掉上一轮记录和统计，ctrl+o 不再展开旧会话的内容"""
        self.stop()
        self.state = RenderState()
        self.turn = TurnRecord()
        self.last_turn = None
        self.tool_count = 0
        self.tool_errors = 0
        self._prune_times = self._pruned_tokens = 0
        self.plan = None        # 换了会话：清单由调用方按新会话的记录重新给
        self.status.tokens = 0
        self.status.cost = 0.0

    def recap(self, messages: list[Any], exchanges: int = 2, answer_lines: int = 3) -> list[str]:
        """恢复会话时回顾最后几轮：用户的话 + 回答的前几行。工具、思考、压缩摘要都不显示"""
        t = self.theme
        pairs: list[tuple[str, str]] = []
        for message in messages:
            role = getattr(message, "role", None)
            if role == "user":
                text = _message_text(message)
                if text:
                    pairs.append((text, ""))
            elif role == "assistant" and pairs:
                text = _message_text(message)
                if text:
                    pairs[-1] = (pairs[-1][0], text)
        if not pairs:
            return []
        width = max(20, self.width - 4)
        lines = [rule(self.width, "最近的对话", t)]
        for question, answer in pairs[-exchanges:]:
            lines.append(f"{t.muted('❯')} {t.muted(first_line(question, width))}")
            if answer:
                body = [line for line in answer.splitlines() if line.strip()]
                for i, line in enumerate(body[:answer_lines]):
                    lead = ANSWER_MARK if i == 0 else " "
                    lines.append(t.muted(f"{lead} {truncate(line, width)}"))
                if len(body) > answer_lines:
                    lines.append(t.muted(f"  …（还有 {len(body) - answer_lines} 行）"))
        lines.append(rule(self.width, "", t))
        return lines

    # -- 内部 ---------------------------------------------------------- #

    def _turn_summary(self) -> str:
        """本轮第一行：✻ 42.3s · 12 次工具 bash ×6, read ×6 · 1 失败   ctrl+o 展开"""
        t = self.theme
        elapsed = time.time() - (self.state.turn_started or time.time())
        parts = [f"{t.highlight('✻', bold=True)} {t.highlight(_seconds(elapsed), bold=True)}"]
        tools = self.turn.tools[self.state.segment_start:]
        if tools:
            counts = Counter(r.name for r in tools)
            names = ", ".join(f"{n} ×{c}" if c > 1 else n for n, c in counts.most_common())
            parts.append(f"{t.muted(f'{len(tools)} 次工具')} "
                         f"{t.accent(truncate(names, max(self.width - 50, 20)))}")
            failed = sum(r.is_error for r in tools)
            if failed:
                parts.append(t.error(f"{failed} 失败"))
        if self._prune_times:
            parts.append(t.muted(f"清理旧输出 {self._prune_times} 次 −{_k(self._pruned_tokens)}"))
        if self.state.retries:
            parts.append(t.warn(f"重试 {self.state.retries} 次"))
        line = f" {t.muted('·')} ".join(parts)
        if self.turn.steps:
            line += f"   {t.muted(EXPAND_HINT)}"
        return line

    def _token_footer(self) -> str:
        """本轮最后一行：↑上下文 · ↓输出（· 费用）"""
        t = self.theme
        state = self.state
        parts = []
        if state.context_tokens:
            parts.append(t.info("↑" + _k(state.context_tokens), bold=True))
        if state.output_tokens:
            parts.append(t.success("↓" + _k(state.output_tokens), bold=True))
        if parts and state.cost:
            parts.append(t.warn(f"¥{state.cost:.4f}"))
        return f" {t.muted('·')} ".join(parts)

    def _new_segment(self) -> None:
        """回答写出之后运行还在继续（计划没做完被提醒、用户插话）：后面算新的一段，
        下次回答前重新写「✻ 用时 · 工具」和任务清单——否则屏幕上停在旧进度，接着就是最终回答"""
        if not self.state.summary_done:
            return
        self.state.summary_done = False
        self.state.turn_started = time.time()
        self.state.segment_start = len(self.turn.tools)

    def _commit_summary(self) -> None:
        if self.state.summary_done or not self.state.turn_started:
            return
        self.state.summary_done = True
        self.live.finalize()
        self._emit([self._turn_summary(), *self._plan_lines()])
        self._prune_times = self._pruned_tokens = 0

    def _begin_answer(self, text: str) -> None:
        """确定是最终回答：活动区收起，先写本轮用时，空一行，再以 ● 起头按 Markdown 排版写正文"""
        self._commit_thinking()
        self._commit_summary()
        self.state.answering = True
        self.state.slot, self.state.shown, self.state.text = "", "", ""
        self.state.markdown = MarkdownStream(
            self.width, self.theme, first_prefix=f"{self.theme.accent(ANSWER_MARK)} ", prefix=INDENT)
        self.live.finalize()
        self._emit([""])
        self._feed_answer(text)

    def _feed_answer(self, delta: str) -> None:
        if self.state.markdown is None:
            return
        lines = self.state.markdown.feed(delta)
        if lines:
            # 定稿的行写进 scrollback，活动区直接换成新的预览（还没写完的半行、没收齐的表格）
            self.live.append(lines, live=self._answer_preview())
            self._blank_tail = lines[-1] == ""
        else:
            self._repaint()

    def _end_answer(self) -> None:
        if not self.state.answering:
            return
        markdown = self.state.markdown
        self.state.answering = False
        self.state.markdown = None
        self.live.finalize()
        if markdown is not None:
            lines = markdown.finish()
            if lines:
                self._emit(lines)

    def _commit_thinking(self) -> None:
        """一段思考结束：记进 TurnRecord（ctrl+o 展开用）。活动区里的思考留着，直到被下一步顶掉"""
        text = self.state.thinking
        self.state.thinking = ""
        if not text.strip():
            return
        seconds = time.time() - (self.state.call_started or self.state.turn_started or time.time())
        self.turn.thinking.append((text, seconds))
        self.turn.steps.append(("thinking", (text, seconds)))

    def _show(self, slot: str, delta: str) -> None:
        """活动区换成 slot（思考 / 过渡话）；同一种就接着往后写"""
        if self.state.slot != slot:
            self.state.slot, self.state.shown, self.state.batch = slot, "", []
        self.state.shown += delta

    def _slot_lines(self, text: str, limit: int | None = SLOT_LINES) -> list[str]:
        """活动区里的文字：去掉首尾空行、压掉连续空行、按宽度折行，只留最后 limit 行"""
        text = re.sub(r"\n\s*\n", "\n", text.replace("\r", "").strip())
        width = max(self.width - len(INDENT) - 1, 10)
        lines = [truncate(line, width) for line in wrap(text, width)] if text else []
        return lines if limit is None else lines[-limit:]

    def _set_status(self, label: str, detail: str = "") -> None:
        self.state.label = label
        self.state.detail = detail
        self._repaint()

    def _live_lines(self) -> list[str]:
        """活动区：第一行转圈 + 本轮用时 + 在干什么；下面是思考 / 过渡话 / 工具，三者共用"""
        t = self.theme
        frame = t.accent(self.spinner.frame())
        detail = f"  {t.muted(self.state.detail)}" if self.state.detail else ""
        if not self.agent_running:
            # 一轮之外（手动压缩等）
            return [f"{frame} {t.muted(self.state.label + '…')}{detail}"]
        elapsed = t.highlight(_seconds(time.time() - (self.state.turn_started or time.time())))
        head = f"{frame} {elapsed} {t.muted('· ' + self.state.label)}"
        if self.state.tool_total:
            head += t.muted(f" · {self.state.tool_total} 次工具")
        if self._prune_times:
            head += t.muted(f" · 清理 {self._prune_times} 次")
        lines = [head + detail, *self._plan_lines()]
        slot = self.state.slot
        if slot == "tools" and self.state.batch:
            lines.append(self._tools_line())
        elif slot == "thinking" and self.show_thinking:
            lines += [t.thinking(f"{INDENT}{line}", italic=True) for line in self._slot_lines(self.state.shown)]
        elif slot == "text":
            lines += [f"{INDENT}{line}" for line in self._slot_lines(self.state.shown)]
        return lines

    def _plan_lines(self) -> list[str]:
        """任务清单：标题行带进度，下面每项一行带序号——✓ 做完的、▸ 正在做的、○ 还没做的。
        项太多时以正在做的那项为中心显示 PLAN_ROWS 项"""
        plan = self.plan if self.state.plan_touched else None
        if not plan:
            return []
        t = self.theme
        done = sum(item.get("status") == "completed" for item in plan)
        mark = t.success("✓") if done == len(plan) else t.accent("☰")
        lines = [f"{INDENT}{mark} {t.bold('任务')} {t.muted(f'{done}/{len(plan)}')}"]
        current = next((i for i, item in enumerate(plan) if item.get("status") != "completed"), 0)
        start = max(0, min(current - 1, len(plan) - PLAN_ROWS))
        shown = plan[start:start + PLAN_ROWS]
        number_width = len(str(len(plan)))
        width = max(self.width - 10 - number_width, 10)
        if start:
            lines.append(f"{INDENT}  {t.muted(f'… 前 {start} 项已完成' if start <= done else f'… 前面还有 {start} 项')}")
        for i, item in enumerate(shown, start + 1):
            step = truncate(str(item.get("step", "")), width)
            number = f"{i}.".rjust(number_width + 1)
            status = item.get("status")
            if status == "completed":
                lines.append(f"{INDENT}  {t.success('✓')} {t.muted(f'{number} {step}')}")
            elif status == "in_progress":
                lines.append(f"{INDENT}  {t.accent('▸', bold=True)} {t.bold(f'{number} {step}')}")
            else:
                lines.append(f"{INDENT}  {t.muted(f'○ {number} {step}')}")
        rest = len(plan) - start - len(shown)
        if rest > 0:
            lines.append(f"{INDENT}  {t.muted(f'… 还有 {rest} 项')}")
        return lines

    def _tools_line(self) -> str:
        """这一批工具：在跑的显示最新一个（名字闪烁、不带参数），跑完了显示 ✓ 名字"""
        t = self.theme
        running = list(self.state.running.values())
        if running:
            current = running[-1]
            on = int(time.time() * 2) % 2 == 0
            name = t.accent(current.name, bold=True) if on else t.accent(current.name, dim=True)
            more = t.muted(f" 等 {len(running)} 个") if len(running) > 1 else ""
            return f"{INDENT}{name}{more}  {t.muted(_seconds(time.time() - current.started))}"
        batch = self.state.batch
        counts = Counter(r.name for r in batch)
        names = ", ".join(f"{n} ×{c}" if c > 1 else n for n, c in counts.most_common())
        failed = sum(r.is_error for r in batch)
        mark = t.error("✗") if failed else t.success("✓")
        return f"{INDENT}{mark} {t.muted(names)}"

    def _repaint(self) -> None:
        if self.state.answering:
            self.live.update(self._answer_preview())
            return
        self.live.update(self._live_lines())

    def _answer_preview(self) -> list[str]:
        """写回答时活动区只放还没定稿的部分（半行、没收齐的表格），太高就只留最后几行"""
        preview = self.state.markdown.preview() if self.state.markdown else []
        return preview[-PREVIEW_ROWS:]

    def _tail(self, text: str, limit: int = 60) -> str:
        flat = " ".join(text.split())
        return truncate(flat[-limit * 2:], limit) if flat else ""

    def _emit(self, lines: list[str]) -> None:
        if not lines:
            return
        self.live.append(lines)
        self._blank_tail = lines[-1] == ""
