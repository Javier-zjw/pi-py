"""
pi‑coding‑agent — 聚合组装层。
内置编码工具、JSONL会话树、上下文压缩、技能系统、扩展插件、配置以及命令行。
依赖 pi‑agent（间接依赖 pi‑ai）；没有其他模块会依赖本项目。
"""

from .agent_session import (
    AgentSession,
    AgentSessionServices,
    create_agent_session,
    create_agent_session_services,
)
from .compaction import CompactionResult, compact, estimate_tokens, should_compact
from .extensions import (
    Command,
    EventBus,
    ExtensionAPI,
    LoadedExtension,
    create_event_bus,
    load_extension_file,
    load_extensions,
)
from .model_runtime import ModelRuntime
from .prompt import BASE_SYSTEM_PROMPT, ContextFile, build_system_prompt, find_context_files
from .resources import DefaultResourceLoader, ResourceLoader
from .session import SessionManager
from .settings import CONFIG_DIR_NAME, SettingsManager
from .skills import PromptTemplate, Skill, discover_prompts, discover_skills, skills_block
from .text import has_surrogates, read_text_lenient, sanitize, sanitize_deep, stream_decoder
from .tools import (
    create_all_tools,
    create_coding_tools,
    create_read_only_tools,
    create_tools,
    define_tool,
)

__version__ = "0.1.0"

__all__ = [
    "AgentSession",
    "AgentSessionServices",
    "BASE_SYSTEM_PROMPT",
    "CONFIG_DIR_NAME",
    "Command",
    "CompactionResult",
    "ContextFile",
    "DefaultResourceLoader",
    "EventBus",
    "ExtensionAPI",
    "LoadedExtension",
    "ModelRuntime",
    "PromptTemplate",
    "ResourceLoader",
    "SessionManager",
    "SettingsManager",
    "Skill",
    "build_system_prompt",
    "compact",
    "create_agent_session",
    "create_agent_session_services",
    "create_all_tools",
    "create_coding_tools",
    "create_event_bus",
    "create_read_only_tools",
    "create_tools",
    "define_tool",
    "discover_prompts",
    "discover_skills",
    "estimate_tokens",
    "find_context_files",
    "has_surrogates",
    "load_extension_file",
    "load_extensions",
    "read_text_lenient",
    "sanitize",
    "sanitize_deep",
    "should_compact",
    "stream_decoder",
    "skills_block",
]