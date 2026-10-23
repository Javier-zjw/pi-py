"""簇 F · 上下文压缩测试：compaction/ 包（计量、切点、结构化状态）

    python tests/test_compaction.py
    python tests/test_compaction.py -v
    pytest tests/test_compaction.py

不联网、不需要 key。
"""

from __future__ import annotations

import asyncio
import itertools
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parents[2] / "pi-agent"))
sys.path.insert(0, str(HERE.parents[2] / "pi-ai"))

from fakes import BIG_MODEL  # noqa: E402

from pi_agent import CustomMessage  # noqa: E402
from pi_ai import (  # noqa: E402
    AssistantMessage,
    DoneEvent,
    ErrorEvent,
    Model,
    StartEvent,
    TextContent,
    TextDeltaEvent,
    Tool,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)

from pi_coding_agent.compaction.budget import (  # noqa: E402
    STATE_MESSAGE_TYPE,
    CompactionBudget,
    context_tokens,
    estimate_text_tokens,
)
from pi_coding_agent.compaction.cut import chunk_ranges, find_cut, is_valid_boundary  # noqa: E402
from pi_coding_agent.compaction.prune import (  # noqa: E402
    STUB_PREFIX,
    apply_prune,
    is_pruned,
    plan_prune,
    stub_text,
)
from pi_coding_agent.compaction.engine import (  # noqa: E402
    CompactionCancelled,
    CompactionFailed,
    CompactionRequest,
    run_compaction,
)
from pi_coding_agent.compaction.state import (  # noqa: E402
    build_state,
    degraded_llm_state,
    extract_facts,
    merge_facts,
    normalize_llm_state,
    parse_json_block,
    render_markdown,
    restore_dropped,
    state_tokens,
    trim_to_budget,
)

VERBOSE = "-v" in sys.argv
FAILURES: list[str] = []


def under_pytest() -> bool:
    # 必须在调用时判断：import（收集）阶段 pytest 还没设置这个环境变量
    return "PYTEST_CURRENT_TEST" in os.environ


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILURES.append(f"{name}: {detail}")
    print(f"  FAIL {name} {detail}")
    if under_pytest():
        raise AssertionError(f"{name}: {detail}")


def assistant(text: str = "", calls: list[ToolCall] | None = None, usage: Usage | None = None
              ) -> AssistantMessage:
    content: list = [TextContent(text=text)] if text else []
    content += calls or []
    return AssistantMessage(content=content, usage=usage or Usage(), stop_reason="stop")


def tool_turn(call_id: str, name: str, args: dict, output: str, details: dict | None = None,
              text: str = "") -> list:
    return [
        assistant(text, [ToolCall(id=call_id, name=name, arguments=args)]),
        ToolResultMessage(tool_call_id=call_id, tool_name=name,
                          content=[TextContent(text=output)], details=details),
    ]


def state_message(state_text: str, tail_length: int) -> CustomMessage:
    return CustomMessage(custom_type=STATE_MESSAGE_TYPE, content=state_text,
                         details={"tailLength": tail_length})


# --------------------------------------------------------------------------- #
# 1. 计量
# --------------------------------------------------------------------------- #


def test_estimate() -> None:
    check("计量: 空串为 0", estimate_text_tokens("") == 0)
    check("计量: 英文约 4 字符/token", estimate_text_tokens("a" * 400) == 100)
    zh = estimate_text_tokens("中" * 400)
    check("计量: 中文不再被低估 (≥400)", zh >= 400, str(zh))
    mixed = estimate_text_tokens("hello 世界")
    check("计量: 中英混合", mixed == 2 + 2, str(mixed))


def test_context_tokens() -> None:
    msgs = [UserMessage(content="q"),
            assistant("a", usage=Usage(input=1000, output=50, cache_read=200, cache_write=10)),
            UserMessage(content="x" * 400)]
    tokens = context_tokens(msgs)
    check("计量: 以实报 usage 为锚并加上后续估算",
          tokens == 1000 + 50 + 200 + 10 + 4 + 100, str(tokens))

    errored = [UserMessage(content="q"),
               AssistantMessage(content=[], usage=Usage(input=9999), stop_reason="error")]
    check("计量: 出错的消息不当锚", context_tokens(errored) < 9999)

    # 压缩后保留尾部里的 assistant usage 是压缩前的，不能当锚
    after = [state_message("状态", tail_length=2),
             UserMessage(content="q"),
             assistant("a", usage=Usage(input=150_000))]
    check("计量: 保留尾部的旧 usage 不当锚", context_tokens(after) < 1000, str(context_tokens(after)))
    after.append(UserMessage(content="新问题"))
    after.append(assistant("b", usage=Usage(input=3000)))
    check("计量: 压缩后的新 usage 可当锚", context_tokens(after) == 3000, str(context_tokens(after)))

    pruned = [UserMessage(content="q"), assistant("a", usage=Usage(input=90_000)),
              UserMessage(content="x" * 400)]
    check("计量: min_anchor 之前的 usage 不当锚（清理之后）",
          context_tokens(pruned, min_anchor=2) < 1000, str(context_tokens(pruned, min_anchor=2)))


def test_budget() -> None:
    b = CompactionBudget(context_window=200_000)
    check("预算: 可用 = 窗口 - 预留", b.usable == 200_000 - 16_384)
    check("预算: 触发线 85%", b.trigger == int(b.usable * 0.85))
    check("预算: 保留尾部取较小值", b.keep_recent == 20_000)
    small = CompactionBudget(context_window=32_000, reserve_tokens=30_000)
    check("预算: 预留过大时至少留一半窗口", small.usable == 16_000)
    check("预算: 小窗口保留尾部按 20% 缩", small.keep_recent == 3_200)
    check("预算: 摘要下限 1.5k", b.summary_budget(1_000) == 1_500)
    check("预算: 摘要按 6% 增长", b.summary_budget(100_000) == 6_000)
    check("预算: 摘要上限", b.summary_budget(10_000_000) == 8_000)
    check("预算: 达到触发线才压缩", b.should_compact(b.trigger) and not b.should_compact(b.trigger - 1))
    check("预算: 无窗口不压缩", not CompactionBudget(context_window=0).should_compact(10**9))
    check("预算: 清理触发线 60%", b.prune_trigger == int(b.usable * 0.6))
    check("预算: 清理目标 40%", b.prune_target == int(b.usable * 0.4))
    check("预算: 保护区取较小值", b.protect == 40_000 and small.protect == 4_800)
    check("预算: 到线才清理", b.should_prune(b.prune_trigger) and not b.should_prune(b.prune_trigger - 1))
    check("预算: 可关闭清理",
          not CompactionBudget(context_window=200_000, prune_enabled=False).should_prune(10**9))

    settings = {"compaction.threshold": 0.7, "compaction.keepRecentTokens": 5000,
                "compaction.prune.triggerRatio": 0.5}
    fb = CompactionBudget.from_settings(BIG_MODEL, lambda k, d: settings.get(k, d))
    check("预算: 从设置读取", fb.trigger_ratio == 0.7 and fb.keep_recent_tokens == 5000
          and fb.context_window == 200_000 and fb.prune_trigger_ratio == 0.5)


# --------------------------------------------------------------------------- #
# 2. 切点
# --------------------------------------------------------------------------- #


def test_cut_prefers_user_boundary() -> None:
    msgs = []
    for i in range(6):
        msgs += [UserMessage(content=f"需求{i} " + "x" * 400), assistant("y" * 400)]
    cut = find_cut(msgs, keep_recent_tokens=500)
    check("切点: 落在用户消息上", cut is not None and isinstance(msgs[cut.cut], UserMessage),
          str(cut))
    check("切点: 非拆分轮", cut is not None and not cut.split_turn)
    kept = sum(len(m.text()) for m in msgs[cut.cut:]) // 4
    check("切点: 保留部分不超预算", kept <= 500, str(kept))


def test_cut_single_long_turn() -> None:
    msgs: list = [UserMessage(content="长任务：把所有模块迁移到新 API")]
    for i in range(30):
        msgs += tool_turn(f"t{i}", "read", {"path": f"f{i}.py"}, "z" * 800)
    cut = find_cut(msgs, keep_recent_tokens=2000)
    check("切点: 单个长轮也能切", cut is not None and cut.cut > 1, str(cut))
    check("切点: 标记为拆分轮", cut is not None and cut.split_turn)
    check("切点: 记下本轮请求原文", cut is not None and cut.turn_request == "长任务：把所有模块迁移到新 API")
    check("切点: 不落在工具结果上", cut is not None and is_valid_boundary(msgs, cut.cut))
    check("切点: 保留了尾部", cut is not None and len(msgs) - cut.cut >= 2)


def test_cut_never_splits_tool_pairs() -> None:
    msgs: list = [UserMessage(content="q")]
    for i in range(10):
        msgs += tool_turn(f"t{i}", "bash", {"command": "ls"}, "o" * (100 * (i + 1)))
    for budget in (50, 300, 700, 1500, 3000):
        cut = find_cut(msgs, keep_recent_tokens=budget)
        if cut is None:
            continue
        ok = not isinstance(msgs[cut.cut], ToolResultMessage)
        check(f"切点: 预算 {budget} 不拆工具对", ok, str(cut))


def test_cut_huge_last_group() -> None:
    msgs: list = [UserMessage(content="q"), assistant("先看看")]
    msgs += tool_turn("t1", "read", {"path": "big"}, "B" * 100_000)
    cut = find_cut(msgs, keep_recent_tokens=100)
    check("切点: 最后一组超预算时至少保留它", cut is not None and cut.cut == 2, str(cut))


def test_cut_nothing_to_do() -> None:
    check("切点: 空对话", find_cut([], 100) is None)
    check("切点: 只有一条", find_cut([UserMessage(content="q")], 100) is None)
    only_state = [state_message("s", 0), UserMessage(content="q")]
    check("切点: 只有状态和一条消息", find_cut(only_state, 100) is None)


def test_cut_skips_state_message() -> None:
    msgs: list = [state_message("上一份状态", tail_length=0)]
    for i in range(4):
        msgs += [UserMessage(content=f"q{i} " + "x" * 400), assistant("y" * 400)]
    cut = find_cut(msgs, keep_recent_tokens=300)
    check("切点: 从状态消息之后开始", cut is not None and cut.start == 1, str(cut))


def test_chunk_ranges() -> None:
    msgs: list = [UserMessage(content="q")]
    for i in range(12):
        msgs += tool_turn(f"t{i}", "read", {"path": "a"}, "c" * 400)
    ranges = chunk_ranges(msgs, 0, len(msgs), max_tokens=350)
    check("分块: 覆盖全部且连续",
          ranges[0][0] == 0 and ranges[-1][1] == len(msgs)
          and all(a[1] == b[0] for a, b in itertools.pairwise(ranges)), str(ranges))
    check("分块: 边界都合法", all(is_valid_boundary(msgs, s) for s, _ in ranges), str(ranges))
    check("分块: 确实分了多块", len(ranges) > 3, str(ranges))


# --------------------------------------------------------------------------- #
# 3. 结构化状态
# --------------------------------------------------------------------------- #


def sample_messages() -> list:
    msgs: list = [UserMessage(content="把登录改成 JWT，别动数据库结构")]
    msgs += tool_turn("t1", "read", {"path": "src/auth.py"}, "...",
                      {"path": "/w/src/auth.py", "lines": 10})
    msgs += tool_turn("t2", "edit", {"path": "src/auth.py"}, "ok",
                      {"path": "/w/src/auth.py", "patch": "-a\n+b"})
    msgs += tool_turn("t3", "edit", {"path": "src/auth.py"}, "ok",
                      {"path": "/w/src/auth.py", "patch": "-b\n+c"})
    msgs += tool_turn("t4", "write", {"path": "src/jwt.py"}, "ok",
                      {"path": "/w/src/jwt.py", "created": True})
    msgs += tool_turn("t5", "bash", {"command": "pytest"}, "1 failed",
                      {"command": "pytest -q", "exit_code": 1})
    msgs += tool_turn("t6", "read", {"path": "nope"}, "not found", {"path": "/w/nope"})
    msgs[-1].is_error = True
    msgs.append(UserMessage(content="长" * 5000))
    return msgs


def test_extract_facts() -> None:
    facts = extract_facts(sample_messages(), cwd="/w")
    texts = [u["text"] for u in facts["userMessages"]]
    check("事实: 用户原话逐字", texts[0] == "把登录改成 JWT，别动数据库结构", str(texts[:1]))
    check("事实: 超长原话截中间", "省略" in texts[1] and len(texts[1]) < 2100, str(len(texts[1])))
    ledger = facts["fileLedger"]
    check("事实: 读取记录（相对路径）", ledger["read"] == ["src/auth.py"], str(ledger))
    check("事实: 修改次数", {"path": "src/auth.py", "edits": 2} in ledger["modified"], str(ledger))
    check("事实: 新建文件", ledger["created"] == ["src/jwt.py"], str(ledger))
    check("事实: 出错的工具不进账", "nope" not in ledger["read"])
    check("事实: 命令与退出码", facts["commands"] == [{"cmd": "pytest -q", "exit": 1}],
          str(facts["commands"]))

    merged = merge_facts(facts, extract_facts(sample_messages()[:5], cwd="/w"))
    check("事实: 合并后编辑次数累加",
          {"path": "src/auth.py", "edits": 3} in merged["fileLedger"]["modified"],
          str(merged["fileLedger"]["modified"]))
    check("事实: 合并后原话追加", len(merged["userMessages"]) == 3)
    check("事实: 合并不改动入参", len(facts["userMessages"]) == 2)


def test_parse_json_block() -> None:
    check("解析: 代码块", parse_json_block('说明\n```json\n{"a": 1}\n```') == {"a": 1})
    check("解析: 裸 JSON", parse_json_block('好的 {"a": {"b": 2}} 完毕') == {"a": {"b": 2}})
    check("解析: 坏 JSON", parse_json_block("```json\n{a: 1}\n```") is None)
    check("解析: 非对象", parse_json_block("[1, 2]") is None)
    check("解析: 空", parse_json_block("") is None)


def test_normalize() -> None:
    llm, errors = normalize_llm_state({
        "task": {"goal": "迁移到 JWT", "status": "写测试"},
        "constraints": [{"id": "c1", "text": "不改表结构"}, "用中文注释"],
        "progress": {"done": [{"text": "改了 auth.py"}]},
        "nextSteps": ["跑 pytest"],
    })
    check("校验: 合法输入无错误", errors == [], str(errors))
    check("校验: 字符串条目转对象并补 id",
          llm["constraints"][1] == {"id": "c2", "text": "用中文注释"}, str(llm["constraints"]))
    check("校验: progress 缺 id 也补", llm["progress"]["done"][0]["id"].startswith("p"))
    check("校验: 缺失字段补空", llm["decisions"] == [] and llm["openQuestions"] == [])

    _, errors = normalize_llm_state({"task": {}})
    check("校验: goal 为空是硬错误", any("goal" in e for e in errors), str(errors))
    _, errors = normalize_llm_state({"task": {"goal": "g"}, "decisions": "不是数组"})
    check("校验: 类型错误是硬错误", any("decisions" in e for e in errors), str(errors))
    _, errors = normalize_llm_state([1])
    check("校验: 非对象", errors != [])


def test_restore_dropped() -> None:
    prev, _ = normalize_llm_state({
        "task": {"goal": "g"},
        "constraints": [{"id": "c1", "text": "不改表结构"}],
        "decisions": [{"id": "d1", "decision": "用 PyJWT"}, {"id": "d2", "decision": "HS256"}],
        "knowledge": [{"id": "k1", "fact": "auth.py:40 有缓存"}],
    })
    new, _ = normalize_llm_state({
        "task": {"goal": "g"},
        "decisions": [{"id": "d1", "decision": "用 PyJWT"}],
        "superseded": [{"id": "d2", "text": "HS256", "reason": "改用 RS256"}],
    })
    restored = restore_dropped(new, prev)
    check("合并: 悄悄丢掉的条目被补回", sorted(restored) == ["c1", "k1"], str(restored))
    check("合并: 明确推翻的不补", all(d["id"] != "d2" for d in new["decisions"]))
    check("合并: 补回内容原样", new["constraints"][0]["text"] == "不改表结构")
    check("合并: 首次压缩无需补", restore_dropped(new, None) == [])


def full_state(seq: int = 1) -> dict:
    llm, _ = normalize_llm_state({
        "task": {"goal": "迁移到 JWT", "status": "修测试"},
        "constraints": [{"id": "c1", "text": "不改表结构"}],
        "progress": {"done": [{"id": "p1", "text": "改 auth.py"}],
                     "inProgress": [{"id": "p2", "text": "修 test_login"}],
                     "blocked": [{"id": "p3", "text": "部署", "reason": "缺密钥"}]},
        "decisions": [{"id": "d1", "decision": "用 PyJWT", "reason": "已在依赖里"}],
        "knowledge": [{"id": "k1", "fact": "token 在中间件里校验", "where": "src/mw.py:12"}],
        "errors": [{"id": "e1", "error": "KeyError: 'sub'", "fix": "补 claim", "status": "fixed"}],
        "files": [{"path": "src/auth.py", "role": "登录入口"}],
        "openQuestions": ["过期时间多久？"],
        "nextSteps": ["跑 pytest tests/test_login.py"],
    })
    facts = extract_facts(sample_messages(), cwd="/w")
    return build_state(llm, facts, seq=seq, meta={"turnRequest": "继续修测试"})


def test_render() -> None:
    md = render_markdown(full_state(seq=2))
    for needle, label in [
        ('<compacted_state seq="2">', "标签与序号"), ("## 目标", "目标"), ("当前：修测试", "状态"),
        ("## 进行中的请求", "拆分轮原文"), ("把登录改成 JWT", "用户原话"),
        ("[c1] 不改表结构", "约束"), ("受阻：[p3] 部署（原因：缺密钥）", "受阻原因"),
        ("用 PyJWT —— 因为 已在依赖里", "决策理由"), ("src/mw.py:12", "认知位置"),
        ("KeyError: 'sub' → 补 claim", "错误与修复"),
        ("| src/auth.py | 修改×2 | 登录入口 |", "文件表合并作用"), ("| src/jwt.py | 新建 |", "新建文件"),
        ("`pytest -q` → exit 1", "命令"), ("过期时间多久", "待确认"), ("1. 跑 pytest", "下一步"),
        ("</compacted_state>", "闭合"),
    ]:
        check(f"渲染: {label}", needle in md, needle)
    if VERBOSE:
        print(md)

    degraded = build_state(degraded_llm_state(None, "摘要失败"), extract_facts([]), seq=1,
                           meta={"degraded": True})
    md = render_markdown(degraded)
    check("渲染: 降级提示", "⚠" in md and "摘要失败" in md)


def test_degraded() -> None:
    prev = full_state()["llm"]
    llm = degraded_llm_state(prev, "第 3 次压缩未生成摘要")
    check("降级: 沿用上一份模型字段", llm["decisions"] == prev["decisions"])
    check("降级: 标注缺口", llm["task"]["status"] == "第 3 次压缩未生成摘要")
    check("降级: 不修改入参", prev["task"]["status"] == "修测试")


def test_trim() -> None:
    state = full_state()
    state["llm"]["progress"]["done"] = [{"id": f"p{i}", "text": f"完成第 {i} 项 " + "细节" * 30}
                                        for i in range(40)]
    state["commands"] = [{"cmd": f"cmd{i}", "exit": 0} for i in range(10)]
    state["userMessages"] = [{"at": i, "text": "话" * 1500} for i in range(8)]
    before = state_tokens(state)
    trimmed, steps = trim_to_budget(state, budget_tokens=before // 3)
    check("裁剪: 确实变小", state_tokens(trimmed) < before, f"{before} → {state_tokens(trimmed)}")
    check("裁剪: 记录步骤", len(steps) >= 2, str(steps))
    check("裁剪: 已完成合并后保留最近 8 条", len(trimmed["llm"]["progress"]["done"]) == 9)
    check("裁剪: 约束和决策不动", trimmed["llm"]["constraints"] == state["llm"]["constraints"]
          and trimmed["llm"]["decisions"] == state["llm"]["decisions"])
    check("裁剪: 最近一条原话完整", len(trimmed["userMessages"][-1]["text"]) == 1500)
    check("裁剪: 其余原话截短但保留首尾",
          all("省略" in u["text"] and len(u["text"]) < 600 for u in trimmed["userMessages"][:-1]))
    check("裁剪: 不改动入参", len(state["llm"]["progress"]["done"]) == 40)
    same, steps = trim_to_budget(state, budget_tokens=10**9)
    check("裁剪: 预算内不动", steps == [] and same == state)


# --------------------------------------------------------------------------- #
# 4. 编排：请求构造、重试、降级、取消、分块
# --------------------------------------------------------------------------- #


GOOD_JSON = """好的。
```json
{"task": {"goal": "迁移到 JWT", "status": "修测试"},
 "constraints": [{"id": "c1", "text": "不改表结构"}],
 "decisions": [{"id": "d1", "decision": "用 PyJWT", "reason": "已在依赖里"}],
 "progress": {"done": [{"id": "p1", "text": "改了 auth.py"}]},
 "nextSteps": ["跑 pytest"]}
```"""

MID_MODEL = Model(id="fake-mid", provider="fake", api="anthropic-messages", name="Mid",
                  context_window=40_000, max_tokens=4096)


class FakeSummarizer:
    """按剧本回复。剧本元素：文本 / ("error", 原因) / ("tool", 文本) / ("length", 文本)。"""

    def __init__(self, replies: list, on_delta=None) -> None:
        self.replies = list(replies)
        self.calls: list = []
        self.on_delta = on_delta

    def __call__(self, model, context, options=None):
        self.calls.append((context, options))
        reply = self.replies.pop(0) if self.replies else GOOD_JSON

        async def gen():
            message = AssistantMessage(usage=Usage(input=100, output=20))
            yield StartEvent(partial=message)
            kind, text = ("text", reply) if isinstance(reply, str) else (reply[0], reply[1])
            if kind == "error":
                message.stop_reason = "error"
                message.error_message = text
                yield ErrorEvent(error=text, message=message)
                return
            if kind == "tool":
                message.content.append(ToolCall(id="x", name="read", arguments={"path": "a"}))
            for piece in (text[:10], text[10:]):
                if piece:
                    if self.on_delta:
                        self.on_delta()
                    yield TextDeltaEvent(content_index=0, delta=piece, partial=message)
            message.content.append(TextContent(text=text))
            message.stop_reason = "length" if kind == "length" else "stop"
            yield DoneEvent(reason=message.stop_reason, message=message)

        return gen()


def long_history(turns: int = 8, size: int = 2000) -> list:
    msgs: list = []
    for i in range(turns):
        msgs.append(UserMessage(content=f"需求{i}：" + "x" * size))
        msgs += tool_turn(f"t{i}", "read", {"path": f"src/m{i}.py"}, "y" * size,
                          {"path": f"/w/src/m{i}.py"})
        msgs.append(assistant("完成 " + "z" * size))
    return msgs


def request(messages, fake, model=BIG_MODEL, **kw) -> CompactionRequest:
    from pi_coding_agent.compaction.budget import CompactionBudget
    budget = kw.pop("budget", CompactionBudget(context_window=model.context_window,
                                               keep_recent_tokens=3000))
    return CompactionRequest(messages=messages, model=model, stream_fn=fake, budget=budget,
                             system_prompt="你是编码助手", cwd="/w", **kw)


TOOLS = [Tool(name="read", description="读文件", parameters={"type": "object", "properties": {}})]


async def case_engine_success() -> None:
    msgs = long_history()
    fake = FakeSummarizer([GOOD_JSON])
    deltas: list[str] = []
    out = await run_compaction(request(msgs, fake, tools=TOOLS),
                               on_progress=lambda p: deltas.append(p.delta))
    check("编排: 产出结果", out is not None)
    context, options = fake.calls[0]
    check("编排: 复用正文 system prompt", context.system_prompt == "你是编码助手")
    check("编排: 带同一套工具定义", [t.name for t in context.tools] == ["read"])
    check("编排: 禁止调用工具", options.tool_choice == "none", str(options.tool_choice))
    check("编排: 前缀与原消息一致（可命中缓存）",
          context.messages[:out.cut.cut] == msgs[:out.cut.cut])
    check("编排: 指令追加在末尾", "<compaction_request>" in context.messages[-1].text())
    check("编排: 只调用一次", len(fake.calls) == 1)
    check("编排: 流式进度", "".join(deltas) == GOOD_JSON, str(deltas[:2]))
    check("编排: 模型字段进状态", out.state["llm"]["task"]["goal"] == "迁移到 JWT")
    check("编排: 确定性字段进状态", "src/m0.py" in out.state["fileLedger"]["read"],
          str(out.state["fileLedger"]))
    check("编排: 用户原话完整保留", out.state["userMessages"][0]["text"].startswith("需求0："))
    check("编排: 保留尾部未被压缩", out.retained == msgs[out.cut.cut:] and len(out.retained) > 0)
    check("编排: 压缩后变小", out.tokens_after < out.tokens_before,
          f"{out.tokens_before} → {out.tokens_after}")
    check("编排: 第一次压缩 seq=1", out.state["seq"] == 1)
    check("编排: 状态消息带 tailLength",
          out.state_message().details["tailLength"] == len(out.retained))
    check("编排: usage 累计", out.usage.input == 100)
    check("编排: 未降级", not out.degraded)


async def case_engine_no_tools() -> None:
    fake = FakeSummarizer([GOOD_JSON])
    await run_compaction(request(long_history(), fake))
    check("编排: 无工具时不传 tool_choice", fake.calls[0][1].tool_choice is None)


async def case_engine_format_retry() -> None:
    fake = FakeSummarizer(["我来总结一下，但忘了 JSON", GOOD_JSON])
    out = await run_compaction(request(long_history(), fake))
    check("重试: 格式错误后重试成功", out is not None and not out.degraded)
    check("重试: 共调用两次", len(fake.calls) == 2)
    retry_context = fake.calls[1][0]
    check("重试: 带上错误原因", "没有找到可解析的 JSON" in retry_context.messages[-1].text())
    check("重试: 保留第一次的前缀", retry_context.messages[:-2] == fake.calls[0][0].messages)

    fake = FakeSummarizer([("tool", GOOD_JSON), GOOD_JSON])
    out = await run_compaction(request(long_history(), fake))
    check("重试: 调用工具算格式错误", len(fake.calls) == 2 and not out.degraded)
    check("重试: 提示不要调工具", "不要调用工具" in fake.calls[1][0].messages[-1].text())

    fake = FakeSummarizer([("length", '```json\n{"task": '), GOOD_JSON])
    out = await run_compaction(request(long_history(), fake))
    check("重试: 输出被截断要求精简", "截断" in fake.calls[1][0].messages[-1].text())


async def case_engine_degrade() -> None:
    msgs = long_history()
    fake = FakeSummarizer(["不是 JSON", '```json\n{"task": {}}\n```'])
    out = await run_compaction(request(msgs, fake))
    check("降级: 两次不合格后降级", out is not None and out.degraded)
    check("降级: 只调用两次", len(fake.calls) == 2)
    check("降级: 用户原话仍然完整", len(out.state["userMessages"]) >= 1)
    check("降级: 文件记录仍然完整", len(out.state["fileLedger"]["read"]) >= 1)
    check("降级: 摘要里有提示", "⚠" in out.summary)
    check("降级: 记录错误原因", any("goal" in e for e in out.errors), str(out.errors))


async def case_engine_provider_failure() -> None:
    fake = FakeSummarizer([("error", "429 rate limited"), GOOD_JSON])
    out = await run_compaction(request(long_history(), fake))
    check("失败: 服务商出错重试一次成功", out is not None and not out.degraded and len(fake.calls) == 2)

    fake = FakeSummarizer([("error", "429"), ("error", "503 overloaded")])
    try:
        await run_compaction(request(long_history(), fake))
        check("失败: 连续出错抛 CompactionFailed", False, "没有抛")
    except CompactionFailed as exc:
        check("失败: 连续出错抛 CompactionFailed", "503" in str(exc), str(exc))


async def case_engine_cancel() -> None:
    cancel = asyncio.Event()
    fake = FakeSummarizer([GOOD_JSON], on_delta=cancel.set)
    try:
        await run_compaction(request(long_history(), fake), cancel_event=cancel)
        check("取消: 抛 CompactionCancelled", False, "没有抛")
    except CompactionCancelled:
        check("取消: 抛 CompactionCancelled", True)


async def case_engine_nothing() -> None:
    fake = FakeSummarizer([])
    out = await run_compaction(request([UserMessage(content="hi")], fake))
    check("编排: 没有可压缩内容返回 None", out is None and fake.calls == [])


async def case_engine_incremental() -> None:
    first = await run_compaction(request(long_history(), FakeSummarizer([GOOD_JSON])))
    msgs = [first.state_message(), *first.retained]
    for i in range(6):
        msgs.append(UserMessage(content=f"新需求{i}：" + "n" * 2000))
        msgs.append(assistant("好 " + "m" * 2000))
    dropped = """```json
{"task": {"goal": "迁移到 JWT", "status": "联调"},
 "decisions": [{"id": "d1", "decision": "用 PyJWT"}],
 "progress": {"done": [{"id": "p1", "text": "改了 auth.py"}]}}
```"""
    fake = FakeSummarizer([dropped])
    second = await run_compaction(request(msgs, fake, previous_state=first.state))
    check("增量: seq 递增", second.state["seq"] == 2)
    check("增量: 指令要求合并上一份状态", "<compacted_state> 是上一次压缩" in
          fake.calls[0][0].messages[-1].text())
    check("增量: 上一份状态在前缀里",
          fake.calls[0][0].messages[0].text().startswith('<compacted_state seq="1">'))
    check("增量: 悄悄丢掉的约束被补回", second.restored == ["c1"], str(second.restored))
    check("增量: 用户原话累积",
          len(second.state["userMessages"]) > len(first.state["userMessages"]))
    check("增量: 摘要里仍有约束", "不改表结构" in second.summary)


async def case_engine_chunked() -> None:
    from pi_coding_agent.compaction.budget import CompactionBudget
    msgs = long_history(turns=20, size=3000)
    budget = CompactionBudget(context_window=MID_MODEL.context_window, reserve_tokens=4096,
                              keep_recent_tokens=2000)
    fake = FakeSummarizer([])
    progress: list = []
    out = await run_compaction(request(msgs, fake, model=MID_MODEL, budget=budget),
                               on_progress=progress.append)
    check("分块: 放不下时分块调用", out is not None and out.chunks > 1 and len(fake.calls) == out.chunks,
          f"chunks={out.chunks if out else None} calls={len(fake.calls)}")
    check("分块: 后续块带上滚动状态",
          "<compacted_state" in fake.calls[1][0].messages[0].text())
    check("分块: 每块不超窗口", all(
        sum(len(m.text()) if hasattr(m, "text") else 0 for m in c.messages) // 4
        < MID_MODEL.context_window for c, _ in fake.calls))
    check("分块: 进度带块序号", {p.chunk for p in progress} == set(range(1, out.chunks + 1)))
    check("分块: 结果覆盖全部用户原话",
          len(out.state["userMessages"]) == sum(isinstance(m, UserMessage)
                                                for m in msgs[:out.cut.cut]))


async def case_engine_shrinks_tail() -> None:
    from pi_coding_agent.compaction.budget import CompactionBudget
    msgs = long_history(turns=6, size=4000)
    budget = CompactionBudget(context_window=20_000, reserve_tokens=2000,
                              keep_recent_tokens=50_000)
    out = await run_compaction(request(msgs, FakeSummarizer([]), budget=budget))
    check("预算: 保留尾部过大时自动缩小", out.tokens_after <= budget.usable,
          f"after={out.tokens_after} usable={budget.usable}")


def test_engine_success() -> None:
    asyncio.run(case_engine_success())


def test_engine_no_tools() -> None:
    asyncio.run(case_engine_no_tools())


def test_engine_format_retry() -> None:
    asyncio.run(case_engine_format_retry())


def test_engine_degrade() -> None:
    asyncio.run(case_engine_degrade())


def test_engine_provider_failure() -> None:
    asyncio.run(case_engine_provider_failure())


def test_engine_cancel() -> None:
    asyncio.run(case_engine_cancel())


def test_engine_nothing() -> None:
    asyncio.run(case_engine_nothing())


def test_engine_incremental() -> None:
    asyncio.run(case_engine_incremental())


def test_engine_chunked() -> None:
    asyncio.run(case_engine_chunked())


def test_engine_shrinks_tail() -> None:
    asyncio.run(case_engine_shrinks_tail())


# --------------------------------------------------------------------------- #
# 5. L1 清理旧工具输出
# --------------------------------------------------------------------------- #


def prune_history() -> list:
    msgs: list = [UserMessage(content="重构认证模块")]
    for i in range(10):
        msgs += tool_turn(f"r{i}", "read", {"path": f"src/m{i}.py"}, "源码" * 600,
                          {"path": f"/w/src/m{i}.py"})
    msgs += tool_turn("b1", "bash", {"command": "pytest -q"}, "失败日志" * 400,
                      {"command": "pytest -q", "exit_code": 1})
    msgs += tool_turn("s1", "ls", {"path": "."}, "a b c", {"path": "/w"})
    msgs.append(assistant("看完了"))
    return msgs


def test_stub_text() -> None:
    read = ToolResultMessage(tool_call_id="1", tool_name="read",
                             content=[TextContent(text="x")], details={"path": "/w/a.py"})
    call = ToolCall(id="1", name="read", arguments={"path": "a.py", "offset": 10, "limit": 50})
    text = stub_text(read, call, 8200)
    check("占位: read 带路径和行号", "/w/a.py（第 10-59 行）" in text and "8.2k" in text, text)
    check("占位: 以固定前缀开头", text.startswith(STUB_PREFIX))
    bash = ToolResultMessage(tool_call_id="2", tool_name="bash", content=[TextContent(text="x")],
                             details={"command": "make test", "exit_code": 2})
    text = stub_text(bash, None, 500)
    check("占位: bash 带命令和退出码", "`make test`，exit 2" in text and "副作用" in text, text)
    err = ToolResultMessage(tool_call_id="3", tool_name="grep", is_error=True,
                            content=[TextContent(text="regex parse error: (")])
    text = stub_text(err, ToolCall(id="3", name="grep", arguments={"pattern": "("}), 300)
    check("占位: 报错保留开头", "regex parse error" in text and "'pattern': '('" in text, text)


def test_plan_prune() -> None:
    msgs = prune_history()
    from pi_coding_agent.compaction.budget import estimate_tokens
    now = estimate_tokens(msgs)
    plan = plan_prune(msgs, tokens_now=now, target_tokens=now // 2, protect_tokens=3000)
    check("清理: 从最旧的开始", plan.tool_call_ids[:2] == ["r0", "r1"], str(plan.tool_call_ids))
    check("清理: 降到目标以下就停", now - plan.tokens_saved <= now // 2
          and "r9" not in plan.tool_call_ids, f"{now} - {plan.tokens_saved}; {plan.tool_call_ids}")
    check("清理: 小结果不清", "s1" not in plan.tool_call_ids)

    everything = plan_prune(msgs, tokens_now=now, target_tokens=0, protect_tokens=3000)
    check("清理: 受保护的最近区间不动", "b1" not in everything.tool_call_ids
          and "r9" not in everything.tool_call_ids, str(everything.tool_call_ids))
    check("清理: 目标为 0 时清掉全部可清的", "r8" in everything.tool_call_ids)
    check("清理: 统计节省量", everything.tokens_saved > now // 2, str(everything.tokens_saved))
    check("清理: 已在目标以下不清", plan_prune(msgs, now, now, 3000).tool_call_ids == [])


def test_apply_prune() -> None:
    msgs = prune_history()
    out, count = apply_prune(msgs, ["r0", "b1"])
    check("替换: 条数", count == 2)
    r0 = next(m for m in out if getattr(m, "tool_call_id", "") == "r0")
    check("替换: 内容换成占位", is_pruned(r0) and "src/m0.py" in r0.text(), r0.text())
    check("替换: details 保留", r0.details == {"path": "/w/src/m0.py"})
    original = next(m for m in msgs if getattr(m, "tool_call_id", "") == "r0")
    check("替换: 原对象不动（会话文件 / 分支用的是它）", not is_pruned(original))
    check("替换: 工具调用本身保留", len(out) == len(msgs)
          and all(a is b for a, b in zip(msgs, out) if not isinstance(a, ToolResultMessage)))
    again, count = apply_prune(out, ["r0"])
    check("替换: 不重复处理", count == 0 and again[2] is out[2])
    facts = extract_facts(out, cwd="/w")
    check("替换: 不影响文件记录提取", "src/m0.py" in facts["fileLedger"]["read"])
    cut = find_cut(out, 300)
    check("替换: 切点仍不落在工具结果上", cut is not None and is_valid_boundary(out, cut.cut))


async def main() -> int:
    print("计量与预算")
    test_estimate()
    test_context_tokens()
    test_budget()
    print("切点")
    test_cut_prefers_user_boundary()
    test_cut_single_long_turn()
    test_cut_never_splits_tool_pairs()
    test_cut_huge_last_group()
    test_cut_nothing_to_do()
    test_cut_skips_state_message()
    test_chunk_ranges()
    print("结构化状态")
    test_extract_facts()
    test_parse_json_block()
    test_normalize()
    test_restore_dropped()
    test_render()
    test_degraded()
    test_trim()
    print("编排")
    await case_engine_success()
    await case_engine_no_tools()
    await case_engine_format_retry()
    await case_engine_degrade()
    await case_engine_provider_failure()
    await case_engine_cancel()
    await case_engine_nothing()
    await case_engine_incremental()
    await case_engine_chunked()
    await case_engine_shrinks_tail()
    print("L1 清理")
    test_stub_text()
    test_plan_prune()
    test_apply_prune()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} 处失败：")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("全部通过 —— 压缩簇可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
