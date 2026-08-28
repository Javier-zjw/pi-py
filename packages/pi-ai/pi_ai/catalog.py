"""
**轻量手动维护的模型目录。**
pi（TypeScript 端）在构建阶段通过实时调用服务商接口自动生成这份目录；而本 Python 实现采用静态子集版本。
你可以直接扩展静态列表，或是在运行时通过 `Models.register_model()`、上层目录的 `models.json` 文件注册自定义模型。
计价单位：美元 / 百万 tokens。
"""

from __future__ import annotations

from .types import Model, ModelCost

BUILTIN_MODELS: list[Model] = [
    Model(
        id="claude-sonnet-4-5",
        provider="anthropic",
        api="anthropic-messages",
        name="Claude Sonnet 4.5",
        cost=ModelCost(input=3.0, output=15.0, cache_read=0.3, cache_write=3.75),
        context_window=200_200,
        max_tokens=64_000,
        reasoning=True,
        input_modalities=("text", "image"),
        thinking_level_map={"minimal": 1024, "low": 4096, "medium": 12288, "high": 24576, "xhigh": 32768, "max": 63999}
    ),
    Model(
        id="claude-haiku-4-5",
        provider="anthropic",
        api="anthropic-messages",
        name="Claude Haiku 4.5",
        cost=ModelCost(input=1.0, output=5.0, cache_read=0.1, cache_write=1.25),
        context_window=200_000,
        max_tokens=32_000,
        reasoning=True,
        input_modalities=("text", "image"),
    ),
    Model(
        id="gpt-5",
        provider="openai",
        api="openai-completions",
        name="GPT-5",
        cost=ModelCost(input=1.25, output=10.0, cache_read=0.125),
        context_window=400_000,
        max_tokens=128_000,
        reasoning=True,
        input_modalities=("text", "image"),
    ),
    Model(
        id="gpt-4.1-mini",
        provider="openai",
        api="openai-completions",
        name="GPT-4.1 mini",
        cost=ModelCost(input=0.4, output=1.6, cache_read=0.1),
        context_window=1_000_000,
        max_tokens=32_000,
        input_modalities=("text", "image"),
    ),
    Model(
        id="deepseek-chat",
        provider="deepseek",
        api="openai-completions",
        name="DeepSeek Chat",
        cost=ModelCost(input=0.27, output=1.1, cache_read=0.07),
        context_window=128_000,
        max_tokens=8_192,
        base_url="https://api.deepseek.com/v1",
    )
]
