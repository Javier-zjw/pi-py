"""
凭证解析逻辑
解析优先级与 pi 保持一致：运行时传入覆盖值 → 持久存储凭证 → 环境变量
该模块不感知 Agent、会话相关逻辑；凭证存储仅仅是基于字符串键的查询
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class CredentialStore(Protocol):
    def get(self, provider_id: str) -> str | None: ...

    def set(self, provider_id: str, api_key: str) -> None: ...

    def delete(self, provider_id: str) -> None: ...


class InMemoryCredentialStore:
    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self._data: dict[str, str] = dict(initial or {})

    def get(self, provider_id: str) -> str | None:
        return self._data.get(provider_id)

    def set(self, provider_id: str, api_key: str) -> None:
        self._data[provider_id] = api_key

    def delete(self, provider_id: str) -> None:
        self._data.pop(provider_id, None)


class EnvCredentialStore:

    def __init__(self, env_vars: dict[str, str] | None = None) -> None:
        self.env_vars = env_vars or {}

    def _var(self, provider_id: str) -> str:
        return self.env_vars.get(provider_id) or f"{provider_id.upper().replace('-', '_')}_API_KEY"

    def get(self, provider_id: str) -> str | None:
        return os.environ.get(self._var(provider_id)) or None

    def set(self, provider_id: str, api_key: str) -> None:
        os.environ[self._var(provider_id)] = api_key

    def delete(self, provider_id: str) -> None:
        os.environ.pop(self._var(provider_id), None)


class FileCredentialStore:
    """``auth.json``: ``{"anthropic": {"apiKey": "..."}}``."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict) -> None:

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), "utf-8")

        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def get(self, provider_id: str) -> str | None:
        entry = self._load().get(provider_id)
        if isinstance(entry, str):
            return entry
        if isinstance(entry, dict):
            return entry.get("apiKey")
        return None

    def set(self, provider_id: str, api_key: str) -> None:
        data = self._load()
        data[provider_id] = {"apiKey": api_key}
        self._save(data)

    def delete(self, provider_id: str) -> None:
        data = self._load()
        if data.pop(provider_id, None) is not None:
            self._save(data)


class ChainedCredentialStore:
    """第一个返回有效结果的存储源优先生效；写入时：数据写入第一个具备可写能力的存储源。"""

    def __init__(self, *stores: CredentialStore) -> None:
        self.stores = list(stores)

    def get(self, provider_id: str) -> str | None:
        for store in self.stores:
            key = store.get(provider_id)
            if key:
                return key

        return None

    def set(self, provider_id: str, api_key: str) -> None:
        if self.stores:
            self.stores[0].set(provider_id, api_key)

    def delete(self, provider_id: str) -> None:
        for store in self.stores:
            store.delete(provider_id)


def default_credential_store(auth_path: str | Path | None = None) -> ChainedCredentialStore:
    stores: list[CredentialStore] = [InMemoryCredentialStore()]
    if auth_path:
        stores.append(FileCredentialStore(auth_path))
    stores.append(EnvCredentialStore())

    return ChainedCredentialStore(*stores)
