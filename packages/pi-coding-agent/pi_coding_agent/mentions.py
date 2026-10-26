"""``@文件``：发送前把消息里提到的文件、目录、图片变成模型认识的内容（同 Claude Code / Codex）。

模型不认识"文件"，只认文字和图片。所以：

- 文本文件 → 内容（带行号，同 read 工具）放进 ``<file path="…">`` 块，附在用户原话后面；
  ``@app.py:10-40`` 只附这几行；太长按 read 的上限截断，告诉模型用 read 接着看；
- 目录 → 文件列表；
- 图片（png / jpg / gif / webp）→ 图片块，只发给 ``input_modalities`` 里有 image 的模型；
  看不了图的模型只收到一句说明，界面提醒用户；
- 二进制（PDF、压缩包……）→ 不附内容，只说明是什么；
- 不存在的 ``@xxx`` 原样留着——``@property``、邮箱、@某人都不是文件。

另外认拖进终端的图片：拖拽会把图片的绝对路径（空格转义成 ``\\ ``）插进输入框，这种路径也当附图。

用户原话和附件分成两个文本块：原话还是原话（压缩时提取用户原话不会被文件内容撑大），
附件块在后面，图片块最后。
"""

from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from pi_ai import ImageContent

from .text import sanitize
from .tools.files import sniff_mime
from .tools.truncate import truncate_tail

MAX_FILE_LINES = 2000
MAX_TOTAL_CHARS = 200_000           # 一条消息附的文字总量上限，超过的只附路径
MAX_IMAGE_BYTES = 5 * 1024 * 1024   # Anthropic 单张图片上限
DIR_ENTRIES = 200
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")
TRAILING = ",.;!?)]}，。；！？）】」、"

# @"带空格的路径" / @路径（空格可以用 \ 转义）；前面不能紧挨字母数字（邮箱 a@b.com 不算）；
# 中文标点直接结束路径——中文句子里 @文件 后面常常紧跟逗号、句号，没有空格
MENTION = re.compile(r'(?<![\w@])@(?:"([^"\n]+)"|((?:\\ |[^\s"，。；！？、）】」])+))')
# 拖进终端的图片：绝对路径或 ~/，可能带引号或 \ 转义的空格
DROPPED = re.compile(
    r"""(?:(?<=\s)|^)(?:'((?:/|~/)[^'\n]+?\.(?:png|jpe?g|gif|webp))'|"((?:/|~/)[^"\n]+?\.(?:png|jpe?g|gif|webp))"|"""
    r"""((?:/|~/)(?:\\ |[^\s])+?\.(?:png|jpe?g|gif|webp)))(?=\s|$)""",
    re.IGNORECASE)
RANGE = re.compile(r"^(.*?):(\d+)(?:-(\d+))?$")


@dataclass
class Attachment:
    label: str          # 显示用：相对路径（目录带 /）
    kind: str           # file / dir / image / binary / skipped
    note: str = ""      # 显示用：120 行、目录 18 项、图片、……


@dataclass
class Expanded:
    text: str                                   # 用户原话（不变）
    context: str = ""                           # 附件文字块（没有就空）
    images: list[ImageContent] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _resolve(cwd: str, raw: str) -> Path:
    path = Path(raw.replace("\\ ", " ")).expanduser()
    return path if path.is_absolute() else Path(cwd) / path


def _label(cwd: str, path: Path) -> str:
    try:
        rel = os.path.relpath(path, cwd)
    except ValueError:      # Windows 上不同盘符
        rel = str(path)
    if rel.startswith(".."):
        rel = str(path).replace(str(Path.home()), "~", 1)
    return rel + ("/" if path.is_dir() and not rel.endswith("/") else "")


def _locate(cwd: str, token: str) -> tuple[Path, int | None, int | None] | None:
    """@ 后面的字 → (路径, 起始行, 结束行)。认行号后缀，末尾的标点不算路径"""
    candidates = [token]
    stripped = token.rstrip(TRAILING)
    if stripped != token:
        candidates.append(stripped)
    for text in candidates:
        path = _resolve(cwd, text)
        if path.exists():
            return path, None, None
        match = RANGE.match(text)
        if match:
            path = _resolve(cwd, match.group(1))
            if path.is_file():
                start = int(match.group(2))
                end = int(match.group(3)) if match.group(3) else start
                return path, max(start, 1), max(end, start)
    return None


def _file_block(cwd: str, path: Path, start: int | None, end: int | None,
                budget: int) -> tuple[str, Attachment]:
    label = _label(cwd, path)
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        size = len(data)
        return (f'<file path="{label}">\n[binary file, {size} bytes — content not included]\n</file>',
                Attachment(label, "binary", "二进制，没有附内容"))
    lines = text.split("\n")
    if len(lines) > 1 and lines[-1] == "":
        lines.pop()                 # 末尾换行不算一行
    total = len(lines)
    first = start or 1
    last = min(end or total, total, first + MAX_FILE_LINES - 1)
    selected = lines[first - 1:last]
    numbered = "\n".join(f"{first + i:6d}\t{line}" for i, line in enumerate(selected))
    body, _ = truncate_tail(sanitize(numbered), max_bytes=min(50_000, max(budget, 2000)),
                            hint=f"use the read tool with offset={last + 1} to see more")
    shown_end = first + len(selected) - 1
    attrs = f' lines="{first}-{shown_end} of {total}"' if (start or shown_end < total) else ""
    if shown_end < total and not end:
        body += f"\n[file has {total} lines; use the read tool with offset={shown_end + 1} to see more]"
    note = f"第 {first}-{shown_end} 行" if start else f"{total} 行"
    return f'<file path="{label}"{attrs}>\n{body}\n</file>', Attachment(label, "file", note)


def _dir_block(cwd: str, path: Path) -> tuple[str, Attachment]:
    label = _label(cwd, path)
    try:
        entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as exc:
        return f'<directory path="{label}">\n[cannot list: {exc}]\n</directory>', Attachment(label, "dir", "列不出")
    names = [sanitize(e.name) + ("/" if e.is_dir() else "") for e in entries]
    shown = names[:DIR_ENTRIES]
    more = f"\n… {len(names) - DIR_ENTRIES} more" if len(names) > DIR_ENTRIES else ""
    return (f'<directory path="{label}">\n' + "\n".join(shown) + more + "\n</directory>",
            Attachment(label, "dir", f"目录 {len(names)} 项"))


def expand_mentions(text: str, cwd: str, *, vision: bool = True) -> Expanded:
    """找出 @文件 和拖进来的图片路径，读成附件。vision=False 时图片不发，只附说明"""
    result = Expanded(text=text)
    seen: set[tuple] = set()
    blocks: list[str] = []
    budget = MAX_TOTAL_CHARS
    found: list[tuple[int, Path, int | None, int | None]] = []
    for match in MENTION.finditer(text):
        located = _locate(cwd, match.group(1) or match.group(2))
        if located:
            found.append((match.start(), *located))
    for match in DROPPED.finditer(text):
        raw = match.group(1) or match.group(2) or match.group(3)
        path = _resolve(cwd, raw)
        if path.is_file():
            found.append((match.start(), path, None, None))
    found.sort(key=lambda item: item[0])

    for _, path, start, end in found:
        key = (str(path.resolve()), start, end)
        if key in seen:
            continue
        seen.add(key)
        label = _label(cwd, path)
        if path.is_dir():
            block, attachment = _dir_block(cwd, path)
        else:
            try:
                head = path.open("rb").read(16)
            except OSError as exc:
                result.warnings.append(f"{label} 读不了：{exc.strerror or exc}")
                continue
            mime = sniff_mime(head)
            if mime == "image/webp" and head[8:12] != b"WEBP":
                mime = None         # RIFF 开头的不一定是图片（WAV、AVI 也是）
            if mime:
                size = path.stat().st_size
                index = len(result.images) + 1
                if not vision:
                    blocks.append(f'<image path="{label}">[image attached by the user, but the current '
                                  f"model cannot view images]</image>")
                    result.attachments.append(Attachment(label, "skipped", "图片（当前模型看不了）"))
                    result.warnings.append(f"当前模型不支持看图，{label} 没有发送"
                                           "（自定义模型在 models.json 里加 \"input\": [\"text\", \"image\"]）")
                    continue
                if size > MAX_IMAGE_BYTES:
                    blocks.append(f'<image path="{label}">[image too large to send: {size} bytes]</image>')
                    result.attachments.append(Attachment(label, "skipped", "图片太大"))
                    result.warnings.append(f"{label} 有 {size / 1024 / 1024:.1f}MB，超过 5MB 上限，没有发送")
                    continue
                result.images.append(ImageContent(base64.b64encode(path.read_bytes()).decode("ascii"),
                                                  mime_type=mime))
                blocks.append(f'<image path="{label}" index="{index}"/>')
                result.attachments.append(Attachment(label, "image", "图片"))
                continue
            if budget <= 0:
                blocks.append(f'<file path="{label}">[not included: too much attached in one message; '
                              f"use the read tool]</file>")
                result.attachments.append(Attachment(label, "skipped", "附件太多，没附内容"))
                continue
            try:
                block, attachment = _file_block(cwd, path, start, end, budget)
            except OSError as exc:
                result.warnings.append(f"{label} 读不了：{exc.strerror or exc}")
                continue
            if attachment.kind == "binary":
                result.warnings.append(f"{label} 是二进制文件（PDF、压缩包等），没有附内容")
        blocks.append(block)
        budget -= len(block)
        result.attachments.append(attachment)

    if blocks:
        result.context = ("<attachments>\nFiles the user referenced with @ (contents as of now):\n\n"
                          + "\n\n".join(blocks) + "\n</attachments>")
    return result


def user_words(message) -> str:
    """用户消息里用户自己说的话：第一个文本块（后面的文本块是 @ 附上的文件内容）"""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    for block in content or []:
        if getattr(block, "type", "") == "text":
            return getattr(block, "text", "") or ""
    return ""
