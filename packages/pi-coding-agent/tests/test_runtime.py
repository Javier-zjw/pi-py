"""簇 D · 运行时测试：model_runtime.py（压缩见 test_compaction.py）

    python tests/test_runtime.py
    python tests/test_runtime.py -v
    pytest tests/test_runtime.py          # PyCharm 默认走这个

不联网、不需要 key。
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

from fakes import FAKE_MODEL, Sandbox  # noqa: E402

from pi_ai import (  # noqa: E402
    AssistantMessage,
    Models,
    TextContent,
)

from pi_coding_agent.model_runtime import ModelRuntime  # noqa: E402

VERBOSE = "-v" in sys.argv
def under_pytest() -> bool:
    # 必须在调用时判断：import（收集）阶段 pytest 还没设置这个环境变量
    return "PYTEST_CURRENT_TEST" in os.environ
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILURES.append(f"{name}: {detail}")
    print(f"  FAIL {name} {detail}")
    if under_pytest():
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


async def main() -> int:
    print("ModelRuntime")
    test_runtime_bootstrap()
    test_runtime_auth()
    test_runtime_custom_models()
    test_runtime_resolve()

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