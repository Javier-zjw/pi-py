"""
运行时注册表：统一管理服务商实现、模型目录、凭证解析整套能力
"""

from __future__ import annotations

from typing import AsyncIterator, Iterator

from .auth import CredentialStore, default_credential_store
from .catalog import BUILTIN_MODELS
from .events import AssistantMessageEvent, DoneEvent, ErrorEvent
from .providers.base import Provider
from .transport import LLMError
from .types import AssistantMessage, Context, Model, SimpleStreamOptions, StreamOptions


def get_model(provider: str, model_id: str) -> Model | None:
    for m in BUILTIN_MODELS:
        if m.provider == provider and m.id == model_id:
            return m

    return None


class Models:
    """
    服务商与模型的集合容器。
    它是上层业务唯一允许调用的入口。该模块完全不感知智能体、工具循环、会话逻辑；它只负责把一份 `Context` 转换成事件流。
    """

    def __init__(
            self,
            credentials: CredentialStore | None = None,
            models: Iterator[Model] | None = None,
    ) -> None:
        self._providers: dict[str, Provider] = {}
        self._models: dict[str, Model] = {}
        self.credentials = credentials or default_credential_store()
        for m in models if models is not None else BUILTIN_MODELS:
            self.register_model(m)

    def set_provider(self, provider: Provider) -> None:
        self._providers[provider.id] = provider

    def get_provider(self, provider_id: str) -> Provider | None:
        return self._providers.get(provider_id)

    def list_providers(self) -> list[Provider]:
        return list(self._providers.values())

    def register_model(self, model: Model) -> None:
        self._models[model.key] = model

    def get_model(self, provider: str, model_id: str) -> Model | None:
        return self._models.get(f"{provider}/{model_id}")

    def list_models(self, provider: str | None = None) -> list[Model]:
        return [m for m in self._models.values() if provider is None or m.provider == provider]

    def available_models(self) -> list[Model]:

        out = []
        for m in self._models.values():
            provider = self._providers.get(m.provider)
            if provider is None:
                continue
            if self.credentials.get(m.provider) or m.base_url:
                out.append(m)

        return out

    def resolve_api_key(self, provider_id: str) -> str | None:
        return self.credentials.get(provider_id)

    async def stream(
            self,
            model: Model,
            context: Context,
            options: StreamOptions | None = None
    ) -> AsyncIterator[AssistantMessageEvent]:
        provider = self._providers.get(model.provider)

        if provider is None:
            raise LLMError(f"provider '{model.provider}' is not registered")

        api_key = (options.api_key if options else None) or self.resolve_api_key(model.provider)

        async for event in provider.stream(model, context, options, api_key):
            yield event

    def stream_simple(
            self,
            model: Model,
            context: Context,
            options: SimpleStreamOptions | None = None
    ) -> AsyncIterator[AssistantMessageEvent]:
        return self.stream(model, context, options or SimpleStreamOptions())

    async def complete(
            self,
            model: Model,
            context: Context,
            options: StreamOptions | None = None
    ) -> AssistantMessage:
        message: AssistantMessage | None = None
        async for event in self.stream(model, context, options):
            if isinstance(event, (DoneEvent, ErrorEvent)):
                message = event.message

        if message is None:
            raise LLMError("stream ended without a terminal event")

        return message

    async def complete_simple(
            self,
            model: Model,
            context: Context,
            options: SimpleStreamOptions | None = None
    ) -> AssistantMessage:
        return await self.complete(model, context, options or SimpleStreamOptions())


def create_models(credentials: CredentialStore | None = None, **kwargs) -> Models:
    return Models(credentials=credentials, **kwargs)
