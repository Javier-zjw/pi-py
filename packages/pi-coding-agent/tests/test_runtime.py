"""簇 D · 运行时测试：model_runtime.py + compaction.py

    python tests/test_runtime.py
    python tests/test_runtime.py -v
    pytest tests/test_runtime.py          # PyCharm 默认走这个

不联网、不需要 key。compaction 的 complete_fn 是注入的，所以用假的就够。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parents[2] / "pi-agent"))
sys.path.insert(0, str(HERE.parents[2] / "pi-ai"))

from fakes import BIG_MODEL, FAKE_MODEL, Sandbox  # noqa: E402

from pi_agent import CustomMessage  # noqa: E402
from pi_ai import (  # noqa: E402
    AssistantMessage,
    Model,
    ModelCost,
    Models,
    TextContent,
    ToolResultMessage,
    Usage,
    UserMessage,
)

from pi_coding_agent.compaction import (  # noqa: E402
    COMPACTION_PROMPT,
    collect_file_activity,
    compact,
    estimate_tokens,
    last_reported_tokens,
    should_compact,
    split_tail,
)
from pi_coding_agent.model_runtime import ModelRuntime  # noqa: E402

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


def assistant(text: str, **kw) -> AssistantMessage:
    return AssistantMessage(content=[TextContent(text=text)], model="fake-1", **kw)


# --------------------------------------------------------------------------- #
# 1. ModelRuntime
# --------------------------------------------------------------------------- #


def test_runtime_bootstrap() -> None:
    with Sandbox() as tmp:
        agent_dir = tmp / "agent"
        runtime = ModelRuntime.create(agent_dir=agent_dir)

        providers = {p.id for p in runtime.models.list_providers()}
        check("运行时: 注册了 anthropic", "anthropic" in providers, str(providers))
        check("运行时: 注册了 openai", "openai" in providers, str(providers))
        check("运行时: 有内置模型", len(runtime.list_models()) > 0)
        check("运行时: stream_fn 就是 stream_simple",
              runtime.stream_fn == runtime.models.stream_simple)


def test_runtime_auth() -> None:
    with Sandbox() as tmp:
        agent_dir = tmp / "agent"
        agent_dir.mkdir(parents=True)
        (agent_dir / "auth.json").write_text(
            json.dumps({"anthropic": {"apiKey": "sk-from-file"}}), "utf-8"
        )
        runtime = ModelRuntime.create(agent_dir=agent_dir)
        check("鉴权: 从 auth.json 读出 key", runtime.check_auth("anthropic"))
        check("鉴权: 没配的 provider 为假", not runtime.check_auth("从没配过的厂商"))

        runtime.set_runtime_api_key("openai", "sk-runtime")
        check("鉴权: 运行时覆盖生效", runtime.check_auth("openai"))
        check("鉴权: 运行时 key 不落盘",
              "sk-runtime" not in (agent_dir / "auth.json").read_text("utf-8"))

    with Sandbox() as tmp:
        os.environ["ANTHROPIC_API_KEY"] = "sk-from-env"
        try:
            runtime = ModelRuntime.create(agent_dir=tmp / "agent")
            check("鉴权: 回落到环境变量", runtime.check_auth("anthropic"))
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)


def test_runtime_custom_models() -> None:
    with Sandbox() as tmp:
        agent_dir = tmp / "agent"
        agent_dir.mkdir(parents=True)
        (agent_dir / "models.json").write_text(json.dumps({
            "providers": [
                {"id": "ark", "name": "方舟", "baseUrl": "https://example.invalid/api/v3", "api": "anthropic-messages"}
            ],
            "models": [{
                "id": "doubao-seed-code",
                "provider": "ark",
                "api": "anthropic-messages",
                "name": "豆包 Code",
                "contextWindow": 256000,
                "maxTokens": 16384,
                "reasoning": True,
                "cost": {"input": 1.5, "output": 6.0, "cacheRead": 0.3},
                "baseUrl": "https://example.invalid/api/v3",
            }],
        }, ensure_ascii=False), "utf-8")
        (agent_dir / "auth.json").write_text(json.dumps({"ark": {"apiKey": "sk-ark"}}), "utf-8")

        runtime = ModelRuntime.create(agent_dir=agent_dir)
        providers = {p.id for p in runtime.models.list_providers()}
        check("自定义: 注册了自定义 provider", "ark" in providers, str(providers))

        model = runtime.get_model("ark", "doubao-seed-code")
        check("自定义: 模型可查到", model is not None)
        if model:
            check("自定义: 上下文窗口", model.context_window == 256000, str(model.context_window))
            check("自定义: 成本字段", model.cost.input == 1.5 and model.cost.cache_read == 0.3,
                  str(model.cost))
            check("自定义: reasoning 标记", model.reasoning is True)
            check("自定义: base_url", model.base_url.endswith("/api/v3"), str(model.base_url))
        check("自定义: 出现在可用列表",
              any(m.id == "doubao-seed-code" for m in runtime.available_models()),
              str([m.key for m in runtime.available_models()]))

    with Sandbox() as tmp:
        agent_dir = tmp / "agent"
        agent_dir.mkdir(parents=True)
        (agent_dir / "models.json").write_text("{ 坏掉的 json", "utf-8")
        runtime = ModelRuntime.create(agent_dir=agent_dir)
        check("自定义: 坏 models.json 不影响启动", len(runtime.list_models()) > 0)


def test_runtime_resolve() -> None:
    with Sandbox() as tmp:
        runtime = ModelRuntime.create(agent_dir=tmp / "agent")
        first = runtime.list_models()[0]

        check("解析: provider/id 形式",
              runtime.resolve(first.key) is not None, first.key)
        check("解析: 裸模型名",
              runtime.resolve(first.id) is not None, first.id)
        check("解析: 未知返回 None", runtime.resolve("没有/这个") is None)
        check("解析: 空串返回 None", runtime.resolve("") is None)

        runtime.set_runtime_api_key(first.provider, "sk-x")
        check("默认模型: 首选可用时用首选",
              runtime.default_model(first.key).key == first.key)
        check("默认模型: 首选无效时回落",
              runtime.default_model("不存在/模型") is not None)

    # 一个 key 都没有时，available_models 应为空（除非模型自带 base_url）
    empty = ModelRuntime(Models(models=[FAKE_MODEL]), Path(""))
    check("可用模型: 无 key 且无 base_url 时为空",
          empty.available_models() == [], str(empty.available_models()))
    check("默认模型: 没有可用模型时返回 None", empty.default_model() is None)


# --------------------------------------------------------------------------- #
# 2. 压缩：判定与切分
# --------------------------------------------------------------------------- #


def test_token_estimate() -> None:
    messages = [UserMessage(content="a" * 400), assistant("b" * 400)]
    est = estimate_tokens(messages)
    check("估算: 大致按四字符一 token", 150 < est < 250, str(est))
    check("估算: 空列表为 0", estimate_tokens([]) == 0)
    check("估算: 算上自定义消息",
          estimate_tokens([CustomMessage(custom_type="x", content="c" * 400)]) > 50)

    with_usage = [assistant("x", usage=Usage(input=3000, output=200, cache_read=500))]
    check("估算: 有真实用量时优先用它",
          last_reported_tokens(with_usage) == 3700, str(last_reported_tokens(with_usage)))
    check("估算: 没有用量时回落到字符估算",
          last_reported_tokens([UserMessage(content="a" * 400)]) > 50)
    check("估算: 取最后一条有用量的 assistant",
          last_reported_tokens([
              assistant("旧", usage=Usage(input=100)),
              assistant("新", usage=Usage(input=9000)),
          ]) == 9000)


def test_should_compact() -> None:
    over = [assistant("x", usage=Usage(input=900))]        # 窗口 1000
    under = [assistant("x", usage=Usage(input=100))]
    check("判定: 超过阈值要压缩", should_compact(over, FAKE_MODEL, 0.85))
    check("判定: 未超阈值不压缩", not should_compact(under, FAKE_MODEL, 0.85))
    check("判定: 阈值可调", not should_compact(over, FAKE_MODEL, 0.95))
    check("判定: 大窗口模型不触发", not should_compact(over, BIG_MODEL, 0.85))
    check("判定: 空对话不压缩", not should_compact([], FAKE_MODEL, 0.85))

    no_window = Model(id="x", provider="p", api="anthropic-messages",
                      cost=ModelCost(), context_window=0)
    check("判定: 没有窗口信息时不压缩", not should_compact(over, no_window, 0.85))


def test_split_tail() -> None:
    messages = []
    for i in range(6):
        messages.append(UserMessage(content=f"u{i}"))
        messages.append(assistant(f"a{i}"))

    older, tail = split_tail(messages, keep_last_turns=2)
    check("切分: 保留最后两轮", len(tail) == 4, str(len(tail)))
    check("切分: 从用户消息处断开", isinstance(tail[0], UserMessage) and tail[0].text() == "u4",
          tail[0].text())
    check("切分: 前半段是要摘要的", older[-1].text() == "a3", older[-1].text())
    check("切分: 两段拼起来是原文", older + tail == messages)

    short = [UserMessage(content="只有一轮"), assistant("回答")]
    older, tail = split_tail(short, keep_last_turns=4)
    check("切分: 轮次不够时全部摘要、不留尾巴", older == short and tail == [], str(tail))

    check("切分: 空列表", split_tail([], 4) == ([], []))


def test_collect_file_activity() -> None:
    messages = [
        ToolResultMessage(tool_call_id="1", tool_name="read",
                          content=[TextContent(text="x")], details={"path": "a.py"}),
        ToolResultMessage(tool_call_id="2", tool_name="read",
                          content=[TextContent(text="x")], details={"path": "a.py"}),
        ToolResultMessage(tool_call_id="3", tool_name="edit",
                          content=[TextContent(text="x")], details={"path": "b.py"}),
        ToolResultMessage(tool_call_id="4", tool_name="write",
                          content=[TextContent(text="x")], details={"path": "c.py"}),
        ToolResultMessage(tool_call_id="5", tool_name="bash",
                          content=[TextContent(text="x")], details={"command": "ls"}),
    ]
    activity = collect_file_activity(messages)
    check("文件活动: 读过的文件去重", activity["readFiles"] == ["a.py"], str(activity))
    check("文件活动: 改过的文件", set(activity["modifiedFiles"]) == {"b.py", "c.py"}, str(activity))
    check("文件活动: 没有 path 的工具被忽略",
          "ls" not in json.dumps(activity, ensure_ascii=False), str(activity))
    check("文件活动: 空输入不崩", collect_file_activity([]) == {"readFiles": [], "modifiedFiles": []})


# --------------------------------------------------------------------------- #
# 3. 压缩：真正执行（complete_fn 注入）
# --------------------------------------------------------------------------- #


async def case_compact() -> None:
    captured: dict = {}

    async def fake_complete(model, context, options=None):
        captured["context"] = context
        captured["model"] = model
        return AssistantMessage(
            content=[TextContent(text="这是摘要：改了 b.py，还差测试。")],
            model=model.id, usage=Usage(input=800, output=40),
        )

    messages = []
    for i in range(6):
        messages.append(UserMessage(content=f"用户第{i}轮"))
        messages.append(assistant(f"助手第{i}轮", usage=Usage(input=900)))
        if i == 1:
            # 放在会被摘要的前半段：文件活动是从"被压缩掉的部分"里提取的，
            # 目的就是让摘要里带上那些即将从上下文消失的操作痕迹
            messages.append(
                ToolResultMessage(tool_call_id="t", tool_name="edit",
                                  content=[TextContent(text="ok")], details={"path": "b.py"})
            )

    result = await compact(messages, FAKE_MODEL, fake_complete, keep_last_turns=2)

    check("压缩: 产出摘要文本", "改了 b.py" in result.summary, result.summary[:40])
    check("压缩: 记录压缩前 token 数", result.tokens_before == 900, str(result.tokens_before))
    check("压缩: 保留尾巴", len(result.retained_tail) > 0, str(len(result.retained_tail)))
    check("压缩: 尾巴从用户消息开始",
          isinstance(result.retained_tail[0], UserMessage), str(type(result.retained_tail[0])))
    check("压缩: 摘要调用的用量被记录",
          result.usage.input == 800 and result.usage.output == 40, str(result.usage))
    check("压缩: details 带文件活动",
          result.details.get("modifiedFiles") == ["b.py"], str(result.details))

    ctx = captured["context"]
    check("压缩: 用同一个模型做摘要", captured["model"] is FAKE_MODEL)
    check("压缩: 摘要请求不带工具", ctx.tools == [], str(ctx.tools))
    check("压缩: 最后一条是摘要指令",
          COMPACTION_PROMPT[:20] in ctx.messages[-1].text(), ctx.messages[-1].text()[:60])
    check("压缩: 只摘要前半段（尾巴不重复送）",
          "用户第5轮" not in " ".join(m.text() for m in ctx.messages[:-1]),
          str([m.text() for m in ctx.messages[:-1]][-3:]))

    result2 = await compact(messages, FAKE_MODEL, fake_complete, keep_last_turns=2,
                            custom_instructions="重点写清楚待办")
    check("压缩: 自定义指令被附加",
          "重点写清楚待办" in captured["context"].messages[-1].text(),
          captured["context"].messages[-1].text()[-60:])
    check("压缩: 自定义指令不影响摘要产出", bool(result2.summary))


async def case_compact_empty_summary() -> None:
    async def empty_complete(model, context, options=None):
        return AssistantMessage(content=[], model=model.id)

    result = await compact([UserMessage(content="x")], FAKE_MODEL, empty_complete)
    check("压缩: 模型没给摘要时有兜底文本", bool(result.summary.strip()), repr(result.summary))


# --------------------------------------------------------------------------- #
# pytest 兼容层
# --------------------------------------------------------------------------- #


def test_compact() -> None:
    asyncio.run(case_compact())


def test_compact_empty_summary() -> None:
    asyncio.run(case_compact_empty_summary())


async def main() -> int:
    print("ModelRuntime")
    test_runtime_bootstrap()
    test_runtime_auth()
    test_runtime_custom_models()
    test_runtime_resolve()
    print("压缩判定")
    test_token_estimate()
    test_should_compact()
    test_split_tail()
    test_collect_file_activity()
    print("压缩执行")
    await case_compact()
    await case_compact_empty_summary()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} 处失败：")
        for f in FAILURES:
            print(f"  - {f}")
        print("\n对照 tests/RUNTIME_CHECKLIST.md 定位实现位置")
        return 1
    print("全部通过 —— 运行时簇可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))