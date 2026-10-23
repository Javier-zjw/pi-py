"""
结构化压缩状态。

磁盘上存 JSON（CompactionEntry.details["state"]），发给模型时渲染成 Markdown。

    {
      "version": 1, "seq": 2,
      "llm":   {...模型输出、已校验的字段...},
      "userMessages": [{"at": 1723..., "text": "用户原话"}],          # 代码提取
      "fileLedger": {"read": [], "modified": [{"path", "edits"}], "created": []},  # 代码提取
      "commands": [{"cmd": "pytest", "exit": 1}],                     # 代码提取
      "meta": {...}
    }

分工原则：能用代码确定性保留的（用户原话、文件、命令）绝不交给模型，模型只写需要"理解"的部分。
"""

from __future__ import annotations

import copy
import json
import os
import re
from collections.abc import Sequence
from typing import Any

from pi_agent import AgentMessage
from pi_ai import ToolResultMessage, UserMessage

from .budget import estimate_text_tokens

STATE_VERSION = 1
USER_MESSAGE_MAX_CHARS = 2_000
MAX_COMMANDS = 10

# 模型字段
ID_LIST_FIELDS = ("constraints", "decisions", "superseded", "knowledge", "errors", "files")
STRING_LIST_FIELDS = ("openQuestions", "nextSteps")
PROGRESS_KEYS = ("done", "inProgress", "blocked")
# 已有条目不得被悄悄删除：消失且不在 superseded 里的，由代码补回
STICKY_FIELDS = ("constraints", "decisions", "knowledge")
ID_PREFIX = {
    "constraints": "c", "decisions": "d", "superseded": "s", "knowledge": "k",
    "errors": "e", "files": "f", "done": "p", "inProgress": "p", "blocked": "p",
}


# --------------------------------------------------------------------------- #
# 代码提取的确定性字段
# --------------------------------------------------------------------------- #


def _clip_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.75)
    tail = limit - head
    return f"{text[:head]}…[省略 {len(text) - limit} 字]…{text[-tail:]}"


def _relative(path: str, cwd: str | None) -> str:
    if cwd and os.path.isabs(path):
        try:
            rel = os.path.relpath(path, cwd)
        except ValueError:
            return path
        if not rel.startswith(".."):
            return rel
    return path


def empty_facts() -> dict[str, Any]:
    return {"userMessages": [], "fileLedger": {"read": [], "modified": [], "created": []},
            "commands": []}


def extract_facts(messages: Sequence[AgentMessage], cwd: str | None = None) -> dict[str, Any]:
    facts = empty_facts()
    ledger = facts["fileLedger"]
    modified: dict[str, int] = {}

    for m in messages:
        if isinstance(m, UserMessage):
            text = m.text().strip()
            if text:
                facts["userMessages"].append(
                    {"at": m.timestamp, "text": _clip_middle(text, USER_MESSAGE_MAX_CHARS)}
                )
        elif isinstance(m, ToolResultMessage) and isinstance(m.details, dict):
            details = m.details
            if m.tool_name == "bash" and details.get("command"):
                facts["commands"].append(
                    {"cmd": _clip_middle(str(details["command"]), 300),
                     "exit": details.get("exit_code")}
                )
                continue
            path = details.get("path")
            if not path or m.is_error:
                continue
            path = _relative(str(path), cwd)
            if m.tool_name == "read" and path not in ledger["read"]:
                ledger["read"].append(path)
            elif m.tool_name in ("write", "edit"):
                modified[path] = modified.get(path, 0) + 1
                if details.get("created") and path not in ledger["created"]:
                    ledger["created"].append(path)

    ledger["modified"] = [{"path": p, "edits": n} for p, n in modified.items()]
    facts["commands"] = facts["commands"][-MAX_COMMANDS:]
    return facts


def merge_facts(previous: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    if not previous:
        return copy.deepcopy(new)
    merged = empty_facts()
    merged["userMessages"] = list(previous.get("userMessages") or []) + list(new["userMessages"])

    prev_ledger = previous.get("fileLedger") or {}
    for key in ("read", "created"):
        merged["fileLedger"][key] = list(prev_ledger.get(key) or [])
        for path in new["fileLedger"][key]:
            if path not in merged["fileLedger"][key]:
                merged["fileLedger"][key].append(path)
    edits: dict[str, int] = {}
    for item in list(prev_ledger.get("modified") or []) + new["fileLedger"]["modified"]:
        edits[item["path"]] = edits.get(item["path"], 0) + int(item.get("edits", 1))
    merged["fileLedger"]["modified"] = [{"path": p, "edits": n} for p, n in edits.items()]

    merged["commands"] = (list(previous.get("commands") or []) + new["commands"])[-MAX_COMMANDS:]
    return merged


# --------------------------------------------------------------------------- #
# 模型字段：解析、校验、合并
# --------------------------------------------------------------------------- #


_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def parse_json_block(text: str) -> dict[str, Any] | None:
    """优先取 ```json 代码块；没有就取第一个 { 到最后一个 }。"""
    candidates = [m.group(1) for m in _FENCE.finditer(text or "")]
    if not candidates and text:
        first, last = text.find("{"), text.rfind("}")
        if 0 <= first < last:
            candidates.append(text[first:last + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _normalize_items(items: Any, field: str, errors: list[str]) -> list[dict[str, Any]]:
    if items is None:
        return []
    if not isinstance(items, list):
        errors.append(f"{field} 必须是数组")
        return []
    out: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            errors.append(f"{field} 的条目必须是对象")
            continue
        item = {k: v for k, v in item.items() if v is not None}
        if not isinstance(item.get("id"), str) or not item["id"].strip():
            item["id"] = ""
        out.append(item)
    return out


def _assign_ids(llm: dict[str, Any]) -> None:
    """给缺 id 的条目补一个不冲突的 id。"""
    used = {item["id"] for item in _all_items(llm) if item.get("id")}
    counters: dict[str, int] = {}
    for item_field, item in _iter_fields(llm):
        if item.get("id"):
            continue
        prefix = ID_PREFIX.get(item_field, "x")
        n = counters.get(prefix, 0)
        while True:
            n += 1
            candidate = f"{prefix}{n}"
            if candidate not in used:
                break
        counters[prefix] = n
        item["id"] = candidate
        used.add(candidate)


def _iter_fields(llm: dict[str, Any]):
    for name in ID_LIST_FIELDS:
        for item in llm.get(name, []):
            yield name, item
    for key in PROGRESS_KEYS:
        for item in llm.get("progress", {}).get(key, []):
            yield key, item


def _all_items(llm: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for _, item in _iter_fields(llm)]


def normalize_llm_state(raw: Any) -> tuple[dict[str, Any], list[str]]:
    """把模型输出整理成规范结构。返回 (state, 硬错误列表)；有硬错误时调用方应重试。"""
    errors: list[str] = []
    if not isinstance(raw, dict):
        return {}, ["顶层必须是 JSON 对象"]

    task = raw.get("task")
    if not isinstance(task, dict):
        errors.append("缺少 task 对象")
        task = {}
    goal = str(task.get("goal") or "").strip()
    if not goal:
        errors.append("task.goal 不能为空")
    llm: dict[str, Any] = {"task": {"goal": goal, "status": str(task.get("status") or "").strip()}}

    for name in ID_LIST_FIELDS:
        llm[name] = _normalize_items(raw.get(name), name, errors)

    progress = raw.get("progress")
    if progress is not None and not isinstance(progress, dict):
        errors.append("progress 必须是对象")
        progress = {}
    llm["progress"] = {
        key: _normalize_items((progress or {}).get(key), f"progress.{key}", errors)
        for key in PROGRESS_KEYS
    }

    for name in STRING_LIST_FIELDS:
        value = raw.get(name)
        if value is None:
            llm[name] = []
        elif isinstance(value, list):
            llm[name] = [str(v) for v in value if str(v).strip()]
        else:
            errors.append(f"{name} 必须是字符串数组")
            llm[name] = []

    _assign_ids(llm)
    return llm, errors


def restore_dropped(llm: dict[str, Any], previous: dict[str, Any] | None) -> list[str]:
    """上一份状态里的 constraints/decisions/knowledge 条目，若在新状态中消失且未被标记为
    superseded，则原样补回。返回补回的 id，供诊断与界面提示。"""
    if not previous:
        return []
    superseded = {item.get("id") for item in llm.get("superseded", [])}
    restored: list[str] = []
    for name in STICKY_FIELDS:
        present = {item.get("id") for item in llm.get(name, [])}
        for item in previous.get(name) or []:
            item_id = item.get("id")
            if item_id and item_id not in present and item_id not in superseded:
                llm.setdefault(name, []).append(copy.deepcopy(item))
                restored.append(item_id)
    return restored


# --------------------------------------------------------------------------- #
# 组装、渲染、裁剪
# --------------------------------------------------------------------------- #


def build_state(
        llm: dict[str, Any],
        facts: dict[str, Any],
        seq: int,
        meta: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {"version": STATE_VERSION, "seq": seq, "llm": llm, **copy.deepcopy(facts),
            "meta": dict(meta or {})}


def degraded_llm_state(previous: dict[str, Any] | None, note: str) -> dict[str, Any]:
    """模型摘要失败时的兜底：沿用上一份模型字段，标注缺口。确定性字段仍然完整。"""
    if previous:
        llm = copy.deepcopy(previous)
    else:
        llm, _ = normalize_llm_state({"task": {"goal": "（未能生成摘要，见下方用户原话）"}})
    llm["task"]["status"] = note
    llm.setdefault("openQuestions", []).append(note)
    return llm


def _item_text(item: dict[str, Any]) -> str:
    return str(item.get("text") or item.get("fact") or item.get("decision") or "").strip()


def render_markdown(state: dict[str, Any]) -> str:
    llm = state.get("llm") or {}
    meta = state.get("meta") or {}
    lines = [f'<compacted_state seq="{state.get("seq", 1)}">',
             (f"此前的对话已被压缩（第 {state.get('seq', 1)} 次），原始内容不可见。"
              "以下是截至压缩时的工作状态，之后保留的消息是原文。")]
    if meta.get("degraded"):
        lines.append("⚠ 本次压缩未能由模型生成摘要，进展部分可能不完整，请结合用户原话与文件记录确认。")

    task = llm.get("task") or {}
    lines += ["", "## 目标", task.get("goal") or "（未知）"]
    if task.get("status"):
        lines.append(f"当前：{task['status']}")

    if meta.get("turnRequest"):
        lines += ["", "## 进行中的请求（原文，本轮尚未完成）", meta["turnRequest"]]

    users = state.get("userMessages") or []
    if meta.get("turnRequest") and users and users[-1]["text"] == _clip_middle(
            meta["turnRequest"].strip(), USER_MESSAGE_MAX_CHARS):
        users = users[:-1]          # 已在"进行中的请求"里原文给出，不重复
    if users:
        lines += ["", "## 用户原话（按时间）"]
        lines += [f"{i}. {u['text']}" for i, u in enumerate(users, 1)]

    def section(title: str, items: list[dict[str, Any]], fmt) -> None:
        if items:
            lines.extend(["", f"## {title}", *[fmt(item) for item in items]])

    section("约束与偏好", llm.get("constraints", []),
            lambda c: f"- [{c['id']}] {_item_text(c)}")

    progress = llm.get("progress") or {}
    if any(progress.get(k) for k in PROGRESS_KEYS):
        lines += ["", "## 进展"]
        for key, label in (("done", "已完成"), ("inProgress", "进行中"), ("blocked", "受阻")):
            for item in progress.get(key, []):
                reason = f"（原因：{item['reason']}）" if item.get("reason") else ""
                lines.append(f"- {label}：[{item['id']}] {_item_text(item)}{reason}")

    section("关键决策", llm.get("decisions", []),
            lambda d: f"- [{d['id']}] {d.get('decision', '')}"
                      + (f" —— 因为 {d['reason']}" if d.get("reason") else ""))
    section("已推翻", llm.get("superseded", []),
            lambda s: f"- [{s['id']}] {_item_text(s)}"
                      + (f"（{s['reason']}）" if s.get("reason") else ""))
    section("关键认知", llm.get("knowledge", []),
            lambda k: f"- [{k['id']}] {_item_text(k)}"
                      + (f"（{k['where']}）" if k.get("where") else ""))
    section("错误与修复", llm.get("errors", []),
            lambda e: f"- [{e['id']}] {e.get('error', '')} → {e.get('fix', '未解决')}"
                      + (f" [{e['status']}]" if e.get("status") else ""))

    ledger = state.get("fileLedger") or {}
    roles = {f.get("path"): f.get("role", "") for f in llm.get("files", []) if f.get("path")}
    rows: list[str] = []
    seen: set[str] = set()
    for item in ledger.get("modified") or []:
        op = "新建" if item["path"] in (ledger.get("created") or []) else f"修改×{item['edits']}"
        rows.append(f"| {item['path']} | {op} | {roles.get(item['path'], '')} |")
        seen.add(item["path"])
    for path in ledger.get("read") or []:
        if path not in seen:
            rows.append(f"| {path} | 读取 | {roles.get(path, '')} |")
            seen.add(path)
    for path, role in roles.items():
        if path not in seen:
            rows.append(f"| {path} | — | {role} |")
    if rows:
        lines += ["", "## 文件", "| 路径 | 操作 | 作用 |", "| --- | --- | --- |", *rows]

    commands = state.get("commands") or []
    if commands:
        lines += ["", "## 最近命令"]
        lines += [f"- `{c['cmd']}` → exit {c.get('exit')}" for c in commands]

    if llm.get("openQuestions"):
        lines += ["", "## 待确认问题", *[f"- {q}" for q in llm["openQuestions"]]]
    if llm.get("nextSteps"):
        lines += ["", "## 下一步", *[f"{i}. {s}" for i, s in enumerate(llm["nextSteps"], 1)]]

    lines.append("</compacted_state>")
    return "\n".join(lines)


def state_tokens(state: dict[str, Any]) -> int:
    return estimate_text_tokens(render_markdown(state))


def trim_to_budget(state: dict[str, Any], budget_tokens: int) -> tuple[dict[str, Any], list[str]]:
    """超预算时按优先级由代码裁剪，不再调用模型。约束、决策、认知（P0/P1）不裁。"""
    state = copy.deepcopy(state)
    steps: list[str] = []
    if state_tokens(state) <= budget_tokens:
        return state, steps

    done = state["llm"].get("progress", {}).get("done", [])
    if len(done) > 8:
        older, recent = done[:-8], done[-8:]
        summary = "；".join(_item_text(i) for i in older)
        merged = {"id": older[0]["id"], "text": f"（较早完成的 {len(older)} 项）{_clip_middle(summary, 400)}"}
        state["llm"]["progress"]["done"] = [merged, *recent]
        steps.append(f"合并较早的 {len(older)} 条已完成")
        if state_tokens(state) <= budget_tokens:
            return state, steps

    if len(state.get("commands") or []) > 5:
        state["commands"] = state["commands"][-5:]
        steps.append("命令记录只保留最近 5 条")
        if state_tokens(state) <= budget_tokens:
            return state, steps

    users = state.get("userMessages") or []
    if len(users) > 3:
        for u in users[:-3]:
            u["text"] = _clip_middle(u["text"], 200)
        steps.append("较早的用户原话截短到 200 字")
        if state_tokens(state) <= budget_tokens:
            return state, steps

    if len(users) > 1:
        # 仍超预算：除最近一条外再截到 500 字（首尾保留，中间标注省略字数）
        for u in users[:-1]:
            u["text"] = _clip_middle(u["text"], 500)
        steps.append("除最近一条外的用户原话截短到 500 字")

    return state, steps
