"""
配置项：合并~/.pi/agent/settings.json <cwd>/.pi/settings.json。
项目目录配置优先级更高；嵌套对象按key逐个合并。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONFIG_DIR_NAME = ".pi"

DEFAULT_SETTINGS : dict[str, Any] = {
    "model": None,
    "thinkingLevel": "off",
    "tools": None,
    "excludeTools": [],
    "toolExecution": "parallel",
    # 一次请求调用工具达到 softLimit 次时提醒模型收尾，之后每 every 次再提醒；
    # 每 reanchorEvery 次把用户的请求和当前计划放回上下文末尾（builtin_extensions/tool_budget.py）
    "toolBudget": {"enabled": True, "softLimit": 40, "every": 20, "reanchorEvery": 15},
    # threshold：上下文达到"可用窗口（窗口 - reserveTokens）"的这个比例时压缩
    # thinking：压缩调用的思考档位，"inherit" 表示沿用会话当前档位
    # prune：L1 清理旧工具输出（不调模型），到 triggerRatio 时清到 targetRatio 以下，最近 protectTokens 不动
    # 模型调用临时失败（限流、过载、5xx、断网）时退避重试：baseDelay 起翻倍，封顶 maxDelay；
    # 服务商给了 Retry-After 就按它等（也封顶）
    "retry": {"enabled": True, "maxRetries": 3, "baseDelay": 2, "maxDelay": 60},
    "compaction": {"enabled": True, "threshold": 0.85, "reserveTokens": 16384,
                   "keepRecentTokens": 20000, "maxSummaryTokens": 8000, "thinking": "off",
                   "prune": {"enabled": True, "triggerRatio": 0.6, "targetRatio": 0.4,
                             "protectTokens": 40000, "minTokens": 200, "minBatchRatio": 0.1},
                   # rehydrate：压缩后补回最近修改的文件（上限另受可用窗口 8% 约束）
                   "rehydrate": {"enabled": True, "maxFiles": 5, "maxTokens": 10000}},
    # 改文件、跑命令前确认（builtin_extensions/permissions.py）：mode = ask / acceptEdits / auto；
    # allow / deny 规则如 "bash(git status*)"、"edit(docs/*)"；nonInteractive：没有界面可问时放行还是拒绝
    "permissions": {"mode": "ask", "allow": [], "deny": [], "nonInteractive": "allow"},
    "extensions": [],
    "disabledExtensions": [],
    "skills": {"enabled": True},
    # 终端界面：theme 是背景深浅（auto 自动探测 / dark / light）；notifications 是需要你时响铃——
    # 一轮跑完、或弹出确认 / 提问时，离你上次输入超过 minSeconds 秒才响（一直盯着屏幕就不打扰）；
    # desktop 额外发系统通知（OSC 9，iTerm2 / WezTerm / Ghostty 等支持）
    "theme": "auto",
    "notifications": {"bell": True, "desktop": False, "minSeconds": 15},
    # 上网（builtin_extensions/web.py）：fetch 每次最多给模型多少字；search 的后端顺序
    # （"auto" = 配了 key 的 API 在前、再试免 key 的；也可以写后端名列表）、是否用免 key 兜底
    # 子智能体（builtin_extensions/subagents.py）：model 留空用当前模型，可写 provider/model 换便宜的
    "subagents": {"model": "", "maxTurns": 30, "maxParallel": 4, "maxOutputChars": 20000},
    # 改完就检查（builtin_extensions/checks.py）：edit / write 后检查那个文件，有问题附在工具结果后面。
    # builtin：内置的 Python 语法 / JSON / TOML 解析；commands：{"*.py": "ruff check -q {file}"} 这样
    # 按文件名通配配命令，{file} 换成路径，退出码非 0 就把输出（最多 maxLines 行）给模型
    # 长期记忆（builtin_extensions/memory.py）：每份最多多少字，写满了模型要先合并再记
    "memory": {"enabled": True, "userChars": 2000, "projectChars": 4000},
    # /reflect（builtin_extensions/reflect.py）：回顾用的模型，留空用当前模型
    "reflect": {"model": ""},
    "checks": {"enabled": True, "builtin": True, "timeout": 30, "maxLines": 30, "commands": {}},
    "web": {
        "fetch": {"maxChars": 20000, "timeout": 20},
        "search": {"backends": "auto", "keyless": True, "limit": 5, "timeout": 12},
    },
}

def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)

    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value

    return out

def _read_json(path: Path) -> dict[str, Any]:

    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}

class SettingsManager:
    def __init__(
            self,
            settings: dict[str, Any] | None = None,
            global_path: Path | None = None,
            project_path: Path | None = None
    ) -> None:
        self.global_path = global_path
        self.project_path = project_path
        self._errors: list[str] = []
        self._settings = _deep_merge(DEFAULT_SETTINGS, settings or {})

    @classmethod
    def create(
            cls, cwd: str | Path = ".", agent_dir: str | Path | None = None
    ) -> "SettingsManager":
        from .session.manager import DEFAULT_AGENT_DIR

        global_path = Path(agent_dir or DEFAULT_AGENT_DIR).expanduser() / "settings.json"
        project_path = Path(cwd).expanduser() / CONFIG_DIR_NAME / "settings.json"
        merged = dict(DEFAULT_SETTINGS)
        if global_path.exists():
            merged = _deep_merge(merged, _read_json(global_path))
        if project_path.exists():
            merged = _deep_merge(merged, _read_json(project_path))

        return cls(merged, global_path=global_path, project_path=project_path)

    @classmethod
    def in_memory(cls, settings: dict[str, Any] | None = None) -> "SettingsManager":
        return cls(settings)

    def get(self, key: str, default: Any = None) -> Any:
        node: Any = self._settings
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]

        return node

    def set(self, key: str, value: Any) -> None:
        parts = key.split(".")
        node = self._settings
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def apply_overrides(self, overrides: dict[str, Any]) -> None:
        self._settings = _deep_merge(self._settings, overrides)

    def all(self) -> dict[str, Any]:
        return dict(self._settings)

    def flush(self) -> None:
        """持久化写入全局配置文件"""
        if self.global_path is None:
            return

        try:
            self.global_path.parent.mkdir(parents=True, exist_ok=True)
            self.global_path.write_text(json.dumps(self._settings, indent=2), "utf-8")
        except OSError as exc:
            self._errors.append(str(exc))

    def drain_errors(self) -> list[str]:
        errors, self._errors = self._errors, []
        return errors

