"""
技能与提示词模板。
技能由一份 ``SKILL.md`` 文件定义，头部包含类YAML前置元数据（名称/描述）。
仅将描述信息纳入系统提示；主体内容由Agent按需读取，以此控制上下文开销。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .text import read_text_lenient, sanitize

@dataclass
class Skill:
    name: str
    description: str
    file_path: str
    base_dir: str
    source: str = "project"

@dataclass
class PromptTemplate:
    name: str
    description: str
    content: str
    source: str = "project"

def parse_front_matter(text: str) -> tuple[dict[str, str], str]:

    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {} ,text

    raw = text[3: end].strip("\n")
    body = text[end + 4:].lstrip("\n")
    meta: dict[str, str] = {}
    for line in raw.split("\n"):
        if ":" in line and not line.startswith(" "):
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip().strip("'\"")

    return meta, body

def load_skill(skill_file: Path, source: str = "project") -> Skill | None:
    text = read_text_lenient(skill_file)
    if text is None:
        return None
    meta, body = parse_front_matter(text)
    name = meta.get("name") or skill_file.parent.name
    description = meta.get("description") or body.strip().split("\n")[0][:200]
    return Skill(
        name=name,
        description=description,
        file_path=sanitize(str(skill_file)),
        base_dir=sanitize(str(skill_file.parent)),
        source=source
    )

def discover_skills(directories: list[tuple[Path, str]]) -> list[Skill]:
    """``directories`` 是一组 (路径, 来源标签) 二元组构成的列表。"""
    skills: list[Skill] = []
    seen: set[str] = set()
    for directory, source in directories:
        if not directory.exists():
            continue
        for skill_file in sorted(directory.glob("*/SKILL.md")):
            skill = load_skill(skill_file, source)
            if skill and skill.name not in seen:
                seen.add(skill.name)
                skills.append(skill)

    return skills

def skills_block(skills: list[Skill]) -> str:
    if not skills:
        return ""

    lines = [
        "<skills>",
        "These skills are available. When one is relevant, read its SKILL.md first "
        "and follow it.",
    ]
    for s in skills:
        lines.append(f"- {s.name}: {s.description} (read: {s.file_path})")
    lines.append("</skills>")
    return "\n".join(lines)

def discover_prompts(directories: list[tuple[Path, str]]) -> list[PromptTemplate]:

    prompts: list[PromptTemplate] = []
    seen: set[str] = set()
    for directory, source in directories:
        if not directory.exists():
            continue
        for prompt_file in sorted(directory.glob("*.md")):
            text = read_text_lenient(prompt_file)
            if text is None:
                continue
            meta, body = parse_front_matter(text)
            name = meta.get("name") or prompt_file.stem
            if name in seen:
                continue
            seen.add(name)
            prompts.append(
                PromptTemplate(
                    name=name,
                    description=meta.get("description", ""),
                    content=body,
                    source=source
                )
            )

    return prompts