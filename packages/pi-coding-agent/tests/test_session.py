"""簇 B · 会话树测试：entries.py + session/manager.py

    python tests/test_session.py
    python tests/test_session.py -v
    pytest tests/test_session.py          # PyCharm 默认走这个

全部同步用例，不需要 asyncio。所有文件都在临时目录里。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))                  # packages/pi-coding-agent
sys.path.insert(0, str(HERE.parents[2] / "pi-agent"))
sys.path.insert(0, str(HERE.parents[2] / "pi-ai"))

from pi_agent import CustomMessage  # noqa: E402
from pi_ai import (  # noqa: E402
    AssistantMessage,
    Cost,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)

from pi_coding_agent.session import (  # noqa: E402
    BranchSummaryEntry,
    CompactionEntry,
    CustomEntry,
    CustomMessageEntry,
    LabelEntry,
    MessageEntry,
    ModelChangeEntry,
    SessionHeader,
    SessionInfoEntry,
    SessionManager,
    ThinkingLevelChangeEntry,
    entry_from_dict,
    entry_to_dict,
    project_dir_name,
)
from pi_coding_agent.session.entries import header_from_dict, header_to_dict, new_entry_id  # noqa: E402

VERBOSE = "-v" in sys.argv
UNDER_PYTEST = "PYTEST_CURRENT_TEST" in os.environ
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILURES.append(f"{name}: {detail}")
    print(f"  FAIL {name} {detail}")
    if UNDER_PYTEST:
        raise AssertionError(f"{name}: {detail}")


class Sandbox:
    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix="pi-session-"))
        return self.path

    def __exit__(self, *exc) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def assistant(text: str, **kw) -> AssistantMessage:
    return AssistantMessage(content=[TextContent(text=text)], model="fake-1", **kw)


def fresh(cwd: Path) -> SessionManager:
    """带文件的会话，但不写进 ~/.pi。"""
    sm = SessionManager(cwd=str(cwd), session_file=cwd / "s.jsonl")
    sm._write_header()
    return sm


# --------------------------------------------------------------------------- #
# 1. entries：类型与序列化
# --------------------------------------------------------------------------- #


def test_entry_ids() -> None:
    ids = {new_entry_id() for _ in range(500)}
    check("entry: id 不重复", len(ids) == 500, str(len(ids)))
    check("entry: id 长度一致", len({len(i) for i in ids}) == 1, str({len(i) for i in ids}))


def test_entry_roundtrip() -> None:
    samples = [
        MessageEntry(id="a1", parent_id=None, message=UserMessage(content="中文提问")),
        MessageEntry(id="a2", parent_id="a1", message=assistant("回答")),
        MessageEntry(
            id="a3", parent_id="a2",
            message=ToolResultMessage(
                tool_call_id="t1", tool_name="read",
                content=[TextContent(text="文件内容")], details={"path": "a.py"},
            ),
        ),
        ModelChangeEntry(id="b1", parent_id="a3", provider="anthropic", model_id="claude-x"),
        ThinkingLevelChangeEntry(id="b2", parent_id="b1", thinking_level="high"),
        CompactionEntry(
            id="c1", parent_id="b2", summary="摘要", tokens_before=48000,
            retained_tail=[UserMessage(content="保留的尾巴")],
            details={"modifiedFiles": ["a.py"]}, usage=Usage(input=5, output=6),
        ),
        BranchSummaryEntry(id="d1", parent_id="c1", from_id="a3", summary="被放弃的分支"),
        CustomEntry(id="e1", parent_id="d1", custom_type="ext_state", data={"n": 1}),
        CustomMessageEntry(id="e2", parent_id="e1", custom_type="note", content="给模型看"),
        LabelEntry(id="f1", parent_id="e2", target_id="a1", label="重要"),
        SessionInfoEntry(id="g1", parent_id="f1", name="重构缓存"),
    ]
    for entry in samples:
        raw = entry_to_dict(entry)
        text = json.dumps(raw, ensure_ascii=False)
        back = entry_from_dict(json.loads(text))
        check(f"entry: {entry.type} 往返", type(back) is type(entry) and back.id == entry.id,
              f"{type(back).__name__} / {back.id}")
        check(f"entry: {entry.type} 保留 parent_id", back.parent_id == entry.parent_id)

    raw = entry_to_dict(samples[0])
    check("entry: 线上用 camelCase", "parentId" in raw and "parent_id" not in raw, str(raw.keys()))
    raw = entry_to_dict(samples[5])
    for key in ("tokensBefore", "retainedTail"):
        check(f"entry: compaction 的 {key}", key in raw, str(raw.keys()))
    raw = entry_to_dict(samples[3])
    check("entry: model_change 用 modelId", "modelId" in raw, str(raw.keys()))
    raw = entry_to_dict(samples[9])
    check("entry: label 用 targetId", "targetId" in raw, str(raw.keys()))

    compaction = entry_from_dict(entry_to_dict(samples[5]))
    check("entry: compaction 的 retained_tail 是消息对象",
          compaction.retained_tail and compaction.retained_tail[0].text() == "保留的尾巴",
          str(compaction.retained_tail))
    check("entry: compaction 的 usage 恢复", compaction.usage and compaction.usage.input == 5)

    try:
        entry_from_dict({"type": "从没见过的类型", "id": "x"})
        check("entry: 未知类型报错", False, "居然接受了")
    except (ValueError, KeyError):
        check("entry: 未知类型报错", True)


def test_header() -> None:
    header = SessionHeader(id="sid", cwd="/tmp/proj")
    raw = header_to_dict(header)
    check("header: 带版本号", isinstance(raw.get("version"), int), str(raw))
    check("header: type 是 session", raw.get("type") == "session")
    back = header_from_dict(raw)
    check("header: 往返", back.id == "sid" and back.cwd == "/tmp/proj")
    check("header: 无父会话时不写字段", "parentSession" not in raw, str(raw.keys()))


# --------------------------------------------------------------------------- #
# 2. 落盘与恢复
# --------------------------------------------------------------------------- #


def test_persistence() -> None:
    with Sandbox() as cwd:
        sm = fresh(cwd)
        check("落盘: 报告已持久化", sm.is_persisted())
        sm.append_message(UserMessage(content="中文问题"))
        sm.append_message(assistant("中文回答"))

        lines = (cwd / "s.jsonl").read_text("utf-8").strip().split("\n")
        check("落盘: 一行一个对象", len(lines) == 3, str(len(lines)))
        check("落盘: 每行都是合法 JSON", all(json.loads(l) for l in lines))
        check("落盘: 第一行是 header", json.loads(lines[0])["type"] == "session")
        check("落盘: 中文直接可读（非 \\u 转义）", "中文问题" in lines[1], lines[1][:60])

        reopened = SessionManager.open(cwd / "s.jsonl")
        check("恢复: 条目数一致", len(reopened.get_entries()) == 2)
        check("恢复: leaf 指向最后一条", reopened.get_leaf_id() == sm.get_leaf_id())
        check("恢复: 内容正确",
              reopened.get_entries()[0].message.text() == "中文问题",
              reopened.get_entries()[0].message.text())
        check("恢复: cwd 从 header 读回", reopened.get_cwd() == str(cwd.resolve()),
              reopened.get_cwd())

    with Sandbox() as cwd:
        sm = SessionManager.in_memory(cwd)
        check("内存会话: 不持久化", not sm.is_persisted() and sm.get_session_file() is None)
        sm.append_message(UserMessage(content="x"))
        check("内存会话: 条目仍在内存里", len(sm.get_entries()) == 1)
        check("内存会话: 没写出文件", not list(cwd.glob("*.jsonl")))


def test_v1_linear_session() -> None:
    """老格式：没有 parentId 的线性会话，读回来要能重建链条。"""
    with Sandbox() as cwd:
        path = cwd / "old.jsonl"
        lines = [
            {"type": "session", "version": 1, "id": "old", "cwd": str(cwd), "timestamp": "x"},
            {"type": "message", "id": "m1", "timestamp": "x",
             "message": {"role": "user", "content": "一"}},
            {"type": "message", "id": "m2", "timestamp": "x",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "二"}]}},
        ]
        path.write_text("\n".join(json.dumps(l, ensure_ascii=False) for l in lines) + "\n", "utf-8")
        sm = SessionManager.open(path)
        branch = [e.id for e in sm.get_branch()]
        check("兼容: v1 线性会话重建成链", branch == ["m1", "m2"], str(branch))


def test_project_dir_name() -> None:
    name = project_dir_name("/home/me/work/proj")
    check("目录名: 用横杠替换分隔符", "/" not in name and "proj" in name, name)
    check("目录名: 同路径稳定",
          project_dir_name("/home/me/work/proj") == project_dir_name("/home/me/work/proj/"))


# --------------------------------------------------------------------------- #
# 3. 树结构
# --------------------------------------------------------------------------- #


def test_tree_basics() -> None:
    with Sandbox() as cwd:
        sm = fresh(cwd)
        a = sm.append_message(UserMessage(content="一"))
        b = sm.append_message(assistant("二"))
        c = sm.append_message(UserMessage(content="三"))

        check("树: 第一条无父", sm.get_entry(a).parent_id is None)
        check("树: 后续挂在前一条上", sm.get_entry(c).parent_id == b)
        check("树: leaf 跟着走", sm.get_leaf_id() == c)
        check("树: get_branch 从根到叶", [e.id for e in sm.get_branch()] == [a, b, c],
              str([e.id for e in sm.get_branch()]))
        check("树: 指定起点取分支", [e.id for e in sm.get_branch(b)] == [a, b])
        check("树: get_children", [e.id for e in sm.get_children(a)] == [b])
        check("树: get_entry 找不到返回 None", sm.get_entry("不存在") is None)
        check("树: leaf entry", sm.get_leaf_entry().id == c)


def test_branching() -> None:
    with Sandbox() as cwd:
        sm = fresh(cwd)
        a = sm.append_message(UserMessage(content="加缓存"))
        b = sm.append_message(assistant("用了 LRU"))
        c = sm.append_message(UserMessage(content="再加 TTL"))

        sm.branch(b)
        check("分支: leaf 回到指定条目", sm.get_leaf_id() == b)
        d = sm.append_message(UserMessage(content="改用 redis"))

        check("分支: 新路径正确", [e.id for e in sm.get_branch()] == [a, b, d],
              str([e.id for e in sm.get_branch()]))
        check("分支: 旧路径仍在", sm.get_entry(c) is not None)
        check("分支: 同一父下两个孩子", len(sm.get_children(b)) == 2,
              str(len(sm.get_children(b))))
        check("分支: 都在同一个文件里", len(sm.get_entries()) == 4)

        tree = sm.get_tree()
        check("分支: get_tree 按父分组", len(tree[b]) == 2, str({k: len(v) for k, v in tree.items()}))

        try:
            sm.branch("不存在的 id")
            check("分支: 未知 id 报错", False, "居然允许了")
        except KeyError:
            check("分支: 未知 id 报错", True)

        sid = sm.branch_with_summary(a, "这条路走不通")
        entry = sm.get_entry(sid)
        check("分支: branch_with_summary 产生摘要条目",
              isinstance(entry, BranchSummaryEntry) and entry.from_id == d, str(entry))
        check("分支: 摘要挂在新 leaf 上", entry.parent_id == a)


def test_labels_and_name() -> None:
    with Sandbox() as cwd:
        sm = fresh(cwd)
        a = sm.append_message(UserMessage(content="一"))
        check("标签: 默认没有", sm.get_label(a) is None)
        sm.append_label_change(a, "重要")
        check("标签: 读得到", sm.get_label(a) == "重要")
        sm.append_label_change(a, "更重要")
        check("标签: 后写覆盖先写", sm.get_label(a) == "更重要")
        sm.append_label_change(a, None)
        check("标签: 可清除", sm.get_label(a) is None)

        check("会话名: 默认为空", sm.get_session_name() is None)
        sm.append_session_info("重构缓存")
        check("会话名: 读得到", sm.get_session_name() == "重构缓存")


def test_branched_session_file() -> None:
    with Sandbox() as cwd:
        sm = SessionManager.create(cwd, agent_dir=cwd / "agent")
        a = sm.append_message(UserMessage(content="一"))
        b = sm.append_message(assistant("二"))
        sm.append_message(UserMessage(content="三"))

        forked = sm.create_branched_session(b, agent_dir=cwd / "agent")
        check("导出分支: 新文件", forked.get_session_file() != sm.get_session_file())
        check("导出分支: 只含选中路径", len(forked.get_entries()) == 2,
              str(len(forked.get_entries())))
        check("导出分支: 记录父会话",
              forked.get_header().parent_session == str(sm.get_session_file()),
              str(forked.get_header().parent_session))
        check("导出分支: id 重新生成",
              {e.id for e in forked.get_entries()}.isdisjoint({a, b}))

        listed = SessionManager.list(cwd, agent_dir=cwd / "agent")
        check("列表: 能找到两个会话", len(listed) == 2, str(len(listed)))
        check("列表: 按时间倒序",
              listed[0].stat().st_mtime >= listed[1].stat().st_mtime)


# --------------------------------------------------------------------------- #
# 4. 上下文重建（全簇最关键）
# --------------------------------------------------------------------------- #


def test_context_without_compaction() -> None:
    with Sandbox() as cwd:
        sm = fresh(cwd)
        sm.append_message(UserMessage(content="一"))
        sm.append_message(assistant("二"))
        sm.append_message(UserMessage(content="三"))

        entries = sm.build_context_entries()
        check("上下文: 无压缩时等于整条分支", len(entries) == 3, str(len(entries)))

        ctx = sm.build_session_context()
        texts = [m.text() for m in ctx["messages"]]
        check("上下文: 消息顺序", texts == ["一", "二", "三"], str(texts))
        check("上下文: 默认思考档位", ctx["thinking_level"] == "off", str(ctx["thinking_level"]))
        check("上下文: 无模型记录时为 None", ctx["model"] is None)


def test_context_with_compaction() -> None:
    with Sandbox() as cwd:
        sm = fresh(cwd)
        sm.append_message(UserMessage(content="很久以前"))
        sm.append_message(assistant("很久以前的回答"))
        sm.append_compaction(
            summary="前面在做缓存改造",
            tokens_before=48000,
            retained_tail=[UserMessage(content="保留的尾巴")],
        )
        sm.append_message(assistant("压缩之后的回答"))

        entries = sm.build_context_entries()
        check("压缩: 检查点之前的条目被裁掉",
              isinstance(entries[0], CompactionEntry) and len(entries) == 2, str(len(entries)))

        ctx = sm.build_session_context()
        texts = [m.text() for m in ctx["messages"]]
        check("压缩: 摘要在最前", "前面在做缓存改造" in texts[0], texts[0][:40])
        check("压缩: 摘要是 CustomMessage", isinstance(ctx["messages"][0], CustomMessage))
        check("压缩: retained_tail 被展开", "保留的尾巴" in texts, str(texts))
        check("压缩: 之后的消息保留", "压缩之后的回答" in texts, str(texts))
        check("压缩: 老消息不再出现", "很久以前" not in " ".join(texts), str(texts))
        if VERBOSE:
            print("   ", texts)


# 保留下来的消息为什么为custom -> user -> assistant -> assistant
def test_context_first_kept_entry() -> None:
    """另一种压缩形态：不带 retained_tail，而是记住"从哪条开始保留"。"""
    with Sandbox() as cwd:
        sm = fresh(cwd)
        a = sm.append_message(UserMessage(content="很老的消息"))
        keep_from = sm.append_message(UserMessage(content="从这条开始保留"))
        sm.append_message(assistant("这条也要保留"))
        cid = sm.append_compaction(summary="老部分的摘要", tokens_before=9000, retained_tail=[])
        entry = sm.get_entry(cid)
        entry.first_kept_entry_id = keep_from          # 模拟这种压缩形态
        sm.append_message(assistant("压缩之后"))

        entries = sm.build_context_entries()
        ids = [e.id for e in entries]
        check("压缩(first_kept): 摘要在最前", isinstance(entries[0], CompactionEntry), str(ids))
        check("压缩(first_kept): 保留起点之后的条目还在", keep_from in ids, str(ids))
        check("压缩(first_kept): 起点之前的被裁掉", a not in ids, str(ids))

        texts = [m.text() for m in sm.build_session_context()["messages"]]
        joined = " ".join(texts)
        check("压缩(first_kept): 老消息不进上下文", "很老的消息" not in joined, str(texts))
        check("压缩(first_kept): 保留段进上下文", "从这条开始保留" in joined, str(texts))
        check("压缩(first_kept): 压缩后的消息也在", "压缩之后" in joined, str(texts))


def test_context_retained_tail_wins() -> None:
    """两个字段同时存在时，retained_tail 优先——它是自包含检查点。"""
    with Sandbox() as cwd:
        sm = fresh(cwd)
        sm.append_message(UserMessage(content="很老的消息"))
        keep_from = sm.append_message(UserMessage(content="first_kept 指向这条"))
        cid = sm.append_compaction(
            summary="摘要", tokens_before=9000,
            retained_tail=[UserMessage(content="自包含的尾巴")],
        )
        sm.get_entry(cid).first_kept_entry_id = keep_from   # 两个都设上
        sm.append_message(assistant("压缩之后"))

        ids = [e.id for e in sm.build_context_entries()]
        check("压缩优先级: retained_tail 胜出，不回头翻旧条目",
              keep_from not in ids, str(ids))
        texts = " ".join(m.text() for m in sm.build_session_context()["messages"])
        check("压缩优先级: 上下文里是自带的尾巴", "自包含的尾巴" in texts, texts[:80])
        check("压缩优先级: 老消息没漏进来",
              "first_kept 指向这条" not in texts and "很老的消息" not in texts, texts[:120])


def test_context_two_compactions() -> None:
    with Sandbox() as cwd:
        sm = fresh(cwd)
        sm.append_message(UserMessage(content="第一段"))
        sm.append_compaction(summary="第一次摘要", tokens_before=1000, retained_tail=[])
        sm.append_message(UserMessage(content="第二段"))
        sm.append_compaction(summary="第二次摘要", tokens_before=2000, retained_tail=[])
        sm.append_message(UserMessage(content="第三段"))

        texts = [m.text() for m in sm.build_session_context()["messages"]]
        joined = " ".join(texts)
        check("压缩: 只应用最后一次", "第二次摘要" in joined and "第一次摘要" not in joined,
              str(texts))
        check("压缩: 最后一次之后的消息在", "第三段" in joined, str(texts))


def test_context_custom_entries() -> None:
    with Sandbox() as cwd:
        sm = fresh(cwd)
        sm.append_message(UserMessage(content="正常消息"))
        sm.append_custom_entry("ext_state", {"counter": 3})
        sm.append_custom_message_entry("note", "扩展插的话")
        sm.append_branch_summary("x", "放弃分支的摘要")

        texts = [m.text() for m in sm.build_session_context()["messages"]]
        joined = " ".join(texts)
        check("上下文: custom_message 进上下文", "扩展插的话" in joined, str(texts))
        check("上下文: custom 条目不进上下文", "counter" not in joined, str(texts))
        check("上下文: branch_summary 进上下文", "放弃分支的摘要" in joined, str(texts))


def test_context_settings_from_full_path() -> None:
    """模型/档位要从完整分支读，不能被压缩裁掉。"""
    with Sandbox() as cwd:
        sm = fresh(cwd)
        sm.append_model_change("anthropic", "claude-x")
        sm.append_thinking_level_change("high")
        sm.append_message(UserMessage(content="一"))
        sm.append_compaction(summary="摘要", tokens_before=100, retained_tail=[])
        sm.append_message(UserMessage(content="二"))

        ctx = sm.build_session_context()
        check("上下文: 压缩后仍能读回模型",
              ctx["model"] == ("anthropic", "claude-x"), str(ctx["model"]))
        check("上下文: 压缩后仍能读回思考档位",
              ctx["thinking_level"] == "high", str(ctx["thinking_level"]))

        sm.append_model_change("openai", "gpt-x")
        check("上下文: 取最后一次模型变更",
              sm.build_session_context()["model"] == ("openai", "gpt-x"))


def test_context_after_branch() -> None:
    """分支之后，另一条路的消息不该出现在上下文里。"""
    with Sandbox() as cwd:
        sm = fresh(cwd)
        a = sm.append_message(UserMessage(content="共同前缀"))
        sm.append_message(UserMessage(content="路线甲"))
        sm.branch(a)
        sm.append_message(UserMessage(content="路线乙"))

        texts = [m.text() for m in sm.build_session_context()["messages"]]
        check("分支上下文: 只含当前路径", texts == ["共同前缀", "路线乙"], str(texts))


def test_total_usage() -> None:
    with Sandbox() as cwd:
        sm = fresh(cwd)
        sm.append_message(assistant("一", usage=Usage(input=10, output=5, cost=Cost(total=0.1))))
        sm.append_message(assistant("二", usage=Usage(input=20, output=7, cost=Cost(total=0.2))))
        sm.append_compaction("摘要", 100, [], usage=Usage(input=3, output=1, cost=Cost(total=0.05)))

        total = sm.total_usage()
        check("用量: 累加 assistant", total.input == 33, str(total.input))
        check("用量: 把压缩也算进去", total.output == 13, str(total.output))
        check("用量: 成本累加", abs(total.cost.total - 0.35) < 1e-9, str(total.cost.total))


# --------------------------------------------------------------------------- #
# 5. 脏数据与边界
# --------------------------------------------------------------------------- #


def test_dirty_text_guard() -> None:
    with Sandbox() as cwd:
        sm = fresh(cwd)
        broken = "报告.txt".encode("gbk").decode("utf-8", "surrogateescape")
        sm.append_message(UserMessage(content=broken))
        raw = (cwd / "s.jsonl").read_text("utf-8")
        check("脏数据: 落盘不炸", "message" in raw, raw[:60])
        reopened = SessionManager.open(cwd / "s.jsonl")
        check("脏数据: 还能读回来", len(reopened.get_entries()) == 1)


def test_empty_and_edge() -> None:
    with Sandbox() as cwd:
        sm = fresh(cwd)
        check("边界: 空会话 branch 为空", sm.get_branch() == [])
        check("边界: 空会话上下文为空", sm.build_session_context()["messages"] == [])
        check("边界: 空会话 leaf 为 None", sm.get_leaf_id() is None)
        check("边界: 空会话用量为零", sm.total_usage().input == 0)

        a = sm.append_message(UserMessage(content="一"))
        sm.reset_leaf()
        check("边界: reset_leaf 后无 leaf", sm.get_leaf_id() is None)
        b = sm.append_message(UserMessage(content="新根"))
        check("边界: reset 后成为新根", sm.get_entry(b).parent_id is None)
        check("边界: 两个根都在", len(sm.get_children(None)) == 2,
              str(len(sm.get_children(None))))


def test_tool_result_roundtrip() -> None:
    """工具结果里带 details，是最容易在序列化时出问题的一种消息。"""
    with Sandbox() as cwd:
        sm = fresh(cwd)
        sm.append_message(
            assistant("调用工具") if False else AssistantMessage(
                content=[ToolCall(id="t1", name="edit", arguments={"path": "a.py"})],
                model="fake-1",
            )
        )
        sm.append_message(
            ToolResultMessage(
                tool_call_id="t1", tool_name="edit",
                content=[TextContent(text="Edited a.py")],
                details={"path": "a.py", "patch": "@@ -1 +1 @@\n-a\n+b", "match": "exact"},
            )
        )
        reopened = SessionManager.open(cwd / "s.jsonl")
        messages = [e.message for e in reopened.get_entries()]
        check("工具结果: toolCall 往返",
              messages[0].tool_calls()[0].arguments == {"path": "a.py"},
              str(messages[0].content))
        check("工具结果: details 往返",
              messages[1].details.get("match") == "exact", str(messages[1].details))
        check("工具结果: patch 里的换行没坏",
              "\n" in messages[1].details["patch"], repr(messages[1].details["patch"]))


# --------------------------------------------------------------------------- #


def main() -> int:
    print("entries")
    test_entry_ids()
    test_entry_roundtrip()
    test_header()
    print("落盘与恢复")
    test_persistence()
    test_v1_linear_session()
    test_project_dir_name()
    print("树结构")
    test_tree_basics()
    test_branching()
    test_labels_and_name()
    test_branched_session_file()
    print("上下文重建")
    test_context_without_compaction()
    test_context_with_compaction()
    test_context_first_kept_entry()
    test_context_retained_tail_wins()
    test_context_two_compactions()
    test_context_custom_entries()
    test_context_settings_from_full_path()
    test_context_after_branch()
    test_total_usage()
    print("脏数据与边界")
    test_dirty_text_guard()
    test_empty_and_edge()
    test_tool_result_roundtrip()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} 处失败：")
        for f in FAILURES:
            print(f"  - {f}")
        print("\n对照 tests/SESSION_CHECKLIST.md 定位实现位置")
        return 1
    print("全部通过 —— 会话簇可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())