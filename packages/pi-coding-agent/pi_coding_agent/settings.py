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
    "compaction": {"enabled": True, "threshold": 0.85, "keepLastTurns": 4},
    "retry": {"enabled": True, "maxRetries": 3},
    "extensions": [],
    "skills": {"enabled": True}
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

