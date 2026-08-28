"""
ModelRuntime：凭证管理、自定义模型、Provider装配。
这是 coding‑agent 对 pi‑ai 的定制化组装层。
pi‑ai 本身不对密钥存储方式做任何预设约束。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pi_ai import (
    ChainedCredentialStore,
    CredentialStore,
    EnvCredentialStore,
    FileCredentialStore,
    InMemoryCredentialStore,
    Model,
    ModelCost,
    Models,
    anthropic_provider,
    openai_compatible_provider,
    openai_provider
)

from .session.manager import DEFAULT_AGENT_DIR

def _model_from_json(d: dict[str, Any]) -> Model:
    cost = d.get("cost") or {}
    return Model(
        id=d["id"],
        provider=d.get("provider", "custom"),
        api=d.get("api", "openai-completions"),
        name=d.get("name", d["id"]),
        cost=ModelCost(
            input=cost.get("input", 0.0),
            output=cost.get("output", 0.0),
            cache_read=cost.get("cacheRead", 0.0),
            cache_write=cost.get("cacheWrite", 0.0),
        ),
        context_window=d.get("contextWindow", 128_000),
        max_tokens=d.get("maxTokens", 8192),
        reasoning=d.get("reasoning", False),
        base_url=d.get("baseUrl")
    )

class ModelRuntime:
    """对 `pi_ai.Models` 做封装，增加磁盘侧鉴权与自定义模型定义能力"""

    def __init__(self, models: Models, agent_dir: Path) -> None:
        self.models = models
        self.agent_dir = agent_dir
        self._runtime_keys = InMemoryCredentialStore()

    @classmethod
    def create(
            cls,
            agent_dir: str | Path | None = None,
            auth_path: str | Path | None = None,
            models_path: str | Path | None = None,
            credentials: CredentialStore | None = None
    ) -> "ModelRuntime":
        directory = Path(agent_dir or DEFAULT_AGENT_DIR).expanduser()
        runtime_keys = InMemoryCredentialStore()
        store = credentials or ChainedCredentialStore(
            runtime_keys,
            FileCredentialStore(auth_path or directory / "auth.json"),
            EnvCredentialStore(),
        )
        models = Models(credentials=store)
        models.set_provider(anthropic_provider())
        models.set_provider(openai_provider())

        path = Path(models_path or directory / "models.json").expanduser()
        if path.exists():
            try:
                data = json.loads(path.read_text("utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}

            for provider in data.get("providers") or []:
                api = provider.get("api", "openai-completions")
                if api == "anthropic-messages":
                    p = anthropic_provider(base_url=provider.get("baseUrl"))
                    p.id = provider.get("id")
                    p.name = provider.get("name", p.id)
                else:
                    p = openai_compatible_provider(
                        provider["id"], provider["baseUrl"], provider.get("name")
                    )
                models.set_provider(p)

            for entry in data.get("models") or []:
                models.register_model(_model_from_json(entry))

        runtime = cls(models, directory)
        runtime._runtime_keys = runtime_keys
        return runtime

    def get_model(self, prvider: str, model_id: str) -> Model | None:
        return self.models.get_model(prvider, model_id)

    def list_models(self) -> list[Model]:
        return self.models.list_models()

    def available_models(self) -> list[Model]:
        return self.models.available_models()

    def resolve(self, spec: str) -> Model | None:
        """解析 `provider/model-id` 格式，也支持直接传入裸模型ID"""

        if "/" in spec:
            provider, model_id = spec.split("/", 1)
            return self.models.get_model(provider, model_id)

        for m in self.models.list_models():
            if m.id == spec:
                return m
        return None

    def set_runtime_api_key(self, provider_id: str, api_key: str) -> None:
        """不会持久化保存到磁盘"""
        self._runtime_keys.set(provider_id, api_key)

    def check_auth(self, provider_id: str) -> bool:
        return bool(self.models.resolve_api_key(provider_id))

    @property
    def stream_fn(self):
        """Agent层所期望的 `StreamFn` 确切可调用对象"""
        return self.models.stream_simple

    def default_model(self, preferred: str | None = None) -> Model | None:
        if preferred:
            model = self.resolve(preferred)
            if model:
                return model

        available = self.available_models()
        return available[0] if available else None
    


