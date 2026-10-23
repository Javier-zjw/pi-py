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
    TextContent,
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

    settings = {"compaction.threshold": 0.7, "compaction.keepRecentTokens": 5000}
    fb = CompactionBudget.from_settings(BIG_MODEL, lambda k, d: settings.get(k, d))
    check("预算: 从设置读取", fb.trigger_ratio == 0.7 and fb.keep_recent_tokens == 5000
          and fb.context_window == 200_000)


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
    check("裁剪: 最近 3 条原话完整", all(len(u["text"]) == 1500 for u in trimmed["userMessages"][-3:]))
    check("裁剪: 不改动入参", len(state["llm"]["progress"]["done"]) == 40)
    same, steps = trim_to_budget(state, budget_tokens=10**9)
    check("裁剪: 预算内不动", steps == [] and same == state)


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
