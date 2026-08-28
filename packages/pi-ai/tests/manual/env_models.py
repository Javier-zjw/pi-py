"""从 .env 构造 Models 注册表。

刻意放在包外面：读配置文件不是原子层的职责，pi_ai 只认 Model 对象和
CredentialStore。这个模块就是测试侧的胶水。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pi_ai import (
    AnthropicProvider,
    InMemoryCredentialStore,
    Model,
    ModelCost,
    Models,
    openai_compatible_provider,
)

# 各档位对应的思考 token 预算
BUDGETS = {
    "minimal": 1024,
    "low": 4096,
    "medium": 12288,
    "high": 24576,
    "xhigh": 32768,
    "max": 63999,
}
EFFORTS = {
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}
LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")


def has_surrogates(text: str) -> bool:
    return any(0xD800 <= ord(c) <= 0xDFFF for c in text)


def sanitize(text: str) -> str:
    """修复 surrogateescape 造成的孤立代理字符。

    locale 不是 UTF-8 时，Python 会用 surrogateescape 解码 argv / 环境变量，
    中文的每个字节被塞进 0xDC80-0xDCFF。这种损坏可逆：按 surrogateescape
    编码回原始字节，再按 UTF-8 解一次就还原了。不修的话，后面 json.dumps
    送进 httpx 时会炸 UnicodeEncodeError。
    """
    if not isinstance(text, str) or not has_surrogates(text):
        return text
    return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def parse_dotenv(path: str | Path) -> dict[str, str]:
    """极简 .env 解析：支持注释、export 前缀、单双引号。"""
    env: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return env
    for raw in p.read_text("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #")[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key.strip()] = value
    return env


def load_env(path: str | Path | None = None) -> dict[str, str]:
    """.env 优先，缺失的键回落到进程环境变量。"""
    root = Path(path) if path else Path(__file__).resolve().parents[2] / ".env"
    # os.environ 的值在非 UTF-8 locale 下是 surrogateescape 解码的，先修一遍
    env = {k: sanitize(v) for k, v in os.environ.items()}
    env.update(parse_dotenv(root))
    return env


@dataclass
class ModelSpec:
    """一个别名对应的完整配置。"""

    alias: str
    model: Model
    api_key: str
    thinking_level: str = "off"
    thinking_style: str = "none"
    extra: dict[str, Any] = field(default_factory=dict)

    def stream_extra(self, level: str | None = None) -> dict[str, Any]:
        """按档位算出要塞进请求体的 provider 私有参数。"""
        level = level or self.thinking_level
        if self.thinking_style != "thinking_type":
            return dict(self.extra)
        if level == "off":
            body = {"thinking": {"type": "disabled"}}
        else:
            body = {"thinking": {"type": "enabled"}}
            if BUDGETS.get(level):
                body["thinking"]["budget_tokens"] = BUDGETS[level]
        return {**self.extra, **body}

    def describe(self) -> str:
        think = (
            "off"
            if self.thinking_level == "off"
            else f"{self.thinking_level}({self.thinking_style})"
        )
        return (
            f"{self.alias:<12} {self.model.api:<20} {self.model.id:<28} "
            f"think={think:<16} {self.model.base_url}"
        )


def _get(env: dict[str, str], alias: str, key: str, default: str = "") -> str:
    return env.get(f"PI_MODEL_{alias}_{key}", default).strip()


def build_spec(env: dict[str, str], alias: str) -> ModelSpec:
    api = _get(env, alias, "API", "openai").lower()
    if api in ("anthropic", "anthropic-messages"):
        api_name = "anthropic-messages"
    elif api in ("openai", "openai-completions"):
        api_name = "openai-completions"
    else:
        raise ValueError(f"[{alias}] API 只能是 anthropic 或 openai，收到 {api!r}")

    model_id = _get(env, alias, "ID")
    if not model_id:
        raise ValueError(f"[{alias}] 缺少 PI_MODEL_{alias}_ID")
    base_url = _get(env, alias, "BASE_URL").rstrip("/")
    if not base_url:
        raise ValueError(f"[{alias}] 缺少 PI_MODEL_{alias}_BASE_URL")
    api_key = _get(env, alias, "API_KEY")

    level = _get(env, alias, "THINKING", "off").lower() or "off"
    if level not in LEVELS:
        raise ValueError(f"[{alias}] THINKING 只能是 {LEVELS}，收到 {level!r}")
    style = _get(env, alias, "THINKING_STYLE", "none").lower() or "none"
    if style not in ("none", "budget", "effort", "thinking_type"):
        raise ValueError(f"[{alias}] THINKING_STYLE 非法：{style!r}")

    # budget / effort 由 provider 自己发，所以要打开 model.reasoning；
    # thinking_type 是方舟私有参数，走 StreamOptions.extra，provider 不该插手
    reasoning = style in ("budget", "effort")
    level_map: dict[str, Any] = {}
    if style == "budget":
        level_map = dict(BUDGETS)
    elif style == "effort":
        level_map = dict(EFFORTS)

    model = Model(
        id=model_id,
        provider=alias,  # 别名即 provider id，多个 key 互不串台
        api=api_name,
        name=_get(env, alias, "NAME", model_id),
        cost=ModelCost(
            input=float(_get(env, alias, "COST_IN", "0") or 0),
            output=float(_get(env, alias, "COST_OUT", "0") or 0),
        ),
        context_window=int(_get(env, alias, "CONTEXT", "128000") or 128000),
        max_tokens=int(_get(env, alias, "MAX_TOKENS", "8192") or 8192),
        reasoning=reasoning,
        base_url=base_url,
        thinking_level_map=level_map,
    )
    return ModelSpec(
        alias=alias,
        model=model,
        api_key=api_key,
        thinking_level=level,
        thinking_style=style,
    )


def build_models(env: dict[str, str] | None = None) -> tuple[Models, dict[str, ModelSpec]]:
    """返回一个装好 provider、注册好模型、填好凭证的 Models。"""
    env = env if env is not None else load_env()
    aliases = [a.strip() for a in env.get("PI_MODELS", "").split(",") if a.strip()]
    if not aliases:
        raise RuntimeError("PI_MODELS 是空的：把 .env.example 复制成 .env 再填")

    credentials = InMemoryCredentialStore()
    models = Models(credentials=credentials, models=[])
    specs: dict[str, ModelSpec] = {}

    for alias in aliases:
        spec = build_spec(env, alias)
        specs[alias] = spec
        models.register_model(spec.model)
        credentials.set(alias, spec.api_key)
        if spec.model.api == "anthropic-messages":
            models.set_provider(_AliasedAnthropic(alias, spec.model.base_url))
        else:
            models.set_provider(openai_compatible_provider(alias, spec.model.base_url))
    return models, specs


class _AliasedAnthropic(AnthropicProvider):
    """AnthropicProvider 的 id 写死成 'anthropic'，这里按别名改掉。"""

    def __init__(self, alias: str, base_url: str | None) -> None:
        super().__init__(base_url=base_url)
        self.id = alias
        self.name = alias


def default_alias(env: dict[str, str], specs: dict[str, ModelSpec]) -> str:
    alias = env.get("PI_DEFAULT_MODEL", "").strip()
    if alias and alias in specs:
        return alias
    return next(iter(specs))