"""
资源发现
所有用户提供的资源——扩展、技能、提示模板、AGENTS.md，
全部经由 `ResourceLoader` 加载。因此SDK嵌入方可以替换整套资源发现机制，而无需改动会话层代码。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol

from .extensions import EventBus, ExtensionAPI, LoadedExtension, create_event_bus, load_extensions
from .prompt import BASE_SYSTEM_PROMPT, ContextFile, find_context_files
from .session.manager import DEFAULT_AGENT_DIR
from .skills import PromptTemplate, Skill, discover_prompts, discover_skills

CONFIG_DIR_NAME = ".pi"

class ResourceLoader(Protocol):
    def reload(self) -> None: ...
    def get_extensions(self) -> list[LoadedExtension]: ...
    def get_extension_api(self) -> ExtensionAPI: ...
    def get_skills(self) -> list[Skill]: ...
    def get_prompts(self) -> list[PromptTemplate]: ...
    def get_context_files(self) -> list[ContextFile]: ...
    def get_system_prompt(self) -> str: ...

class DefaultResourceLoader:
    def __init__(
            self,
            cwd: str | Path = ".",
            agent_dir: str | Path | None = None,
            system_prompt_override: Callable[[], str] | None = None,
            additional_extension_paths: list[str | Path] | None = None,
            extension_factories: list[Callable[[ExtensionAPI], None]] | None = None,
            event_bus: EventBus | None = None
    ) -> None:
        self.cwd = Path(cwd).expanduser().resolve()
        self.agent_dir = Path(agent_dir or DEFAULT_AGENT_DIR).expanduser()
        self.system_prompt_override = system_prompt_override
        self.additional_extension_paths = [Path(p) for p in (additional_extension_paths or [])]
        self.extension_factories = extension_factories or []
        self.events = event_bus or create_event_bus()

        self.api = ExtensionAPI(cwd=str(self.cwd), events=self.events)
        self._extensions: list[LoadedExtension] = []
        self._skills: list[Skill] = []
        self._prompts: list[PromptTemplate] = []
        self._context_files: list[ContextFile] = []

    def reload(self) -> None:
        self.api = ExtensionAPI(cwd=str(self.cwd), events=self.events)

        self._skills = discover_skills(
            [
                (self.cwd / CONFIG_DIR_NAME / "skills", "project"),
                (self.cwd / ".agents" / "skills", "project"),
                (self.agent_dir / "skills", "global"),
                (Path.home() / ".agents" / "skills", "global")
            ]
        )

        self._prompts = discover_prompts(
            [
                (self.cwd / CONFIG_DIR_NAME / "prompts", "project"),
                (self.agent_dir / "prompts", "global"),
            ]
        )

        self._context_files = find_context_files(self.cwd, self.agent_dir)

        directories = [self.agent_dir / "extensions", self.cwd / CONFIG_DIR_NAME / "extensions"]
        self._extensions = load_extensions(directories, self.api)
        for path in self.additional_extension_paths:
            from .extensions import load_extension_file

            self._extensions.append(load_extension_file(Path(path), self.api))

        for factory in self.extension_factories:
            try:
                factory(self.api)
                self._extensions.append(LoadedExtension(getattr(factory, "__name__", "inline"), None))
            except Exception as exc:
                self._extensions.append(
                    LoadedExtension(getattr(factory, "__name__", "inline"), None, str(exc))
                )


    def get_extensions(self) -> list[LoadedExtension]:
        return list(self._extensions)

    def get_extension_api(self) -> ExtensionAPI:
        return self.api

    def get_skills(self) -> list[Skill]:
        return list(self._skills)

    def get_prompts(self) -> list[PromptTemplate]:
        return list(self._prompts)

    def get_context_files(self) -> list[ContextFile]:
        return list(self._context_files)

    def get_system_prompt(self) -> str:
        from .prompt import build_system_prompt
        from .skills import skills_block

        base = self.system_prompt_override() if self.system_prompt_override else BASE_SYSTEM_PROMPT
        return build_system_prompt(
            self.cwd,
            base=base,
            context_files=self._context_files,
            skills_block=skills_block(self._skills)
        )