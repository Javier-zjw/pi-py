"""输入框里 ``@`` 的文件补全：纯函数，不依赖 prompt_toolkit，可以离线测。

- ``@`` 后面带 ``/``（或 ``~``、``.`` 开头）：按目录逐级补，像 shell 的 Tab；
- 只是一个词：在整个项目里模糊找（文件名开头 > 文件名包含 > 路径包含，短的在前）；
- 跳过 .git、node_modules、虚拟环境、构建产物这类目录；隐藏文件只在明确输 ``.`` 时出现。

项目文件列表缓存几秒：每按一个键都遍历一遍大仓库会卡。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
             ".pytest_cache", ".ruff_cache", ".tox", "dist", "build", ".next", ".nuxt", "target",
             ".idea", ".vscode", ".DS_Store", ".cache", ".gradle", "Pods", ".terraform"}
MAX_FILES = 20_000
CACHE_SECONDS = 5.0
# 光标前正在输的 @词：@"带空格 / @普通路径（\ 转义的空格也算）。前面不能紧挨英文字母数字（邮箱不算），
# 紧挨中文可以——中文句子里 @ 前面一般不打空格（看看@app.py）；中文输入法打出的全角 ＠ 也认
_AT = re.compile(r'(?<![A-Za-z0-9_@＠])[@＠](?:"([^"\n]*)|((?:\\ |[^\s"，。；！？、）】」])*))$')


@dataclass
class PathCandidate:
    insert: str         # 补全后 @ 后面的文字（带空格的路径加引号）
    display: str        # 菜单里显示的路径
    is_dir: bool


def mention_token(text_before_cursor: str) -> tuple[str, int] | None:
    """光标是不是正停在一个 @词 里：是就返回 (@ 后面已经输入的路径, 实际敲了几个字符)，否则 None。
    字符数含引号和转义空格的反斜杠——补全时整段替换掉"""
    match = _AT.search(text_before_cursor)
    if not match:
        return None
    typed = len(match.group(0)) - 1          # 去掉 @
    if match.group(1) is not None:
        return match.group(1), typed
    return (match.group(2) or "").replace("\\ ", " "), typed


def mention_at(text_before_cursor: str) -> str | None:
    token = mention_token(text_before_cursor)
    return token[0] if token else None


def quote(path: str) -> str:
    return f'"{path}"' if " " in path else path


class ProjectIndex:
    """项目里的文件和目录（相对路径），缓存几秒"""

    def __init__(self) -> None:
        self._root = ""
        self._at = 0.0
        self._paths: list[tuple[str, bool]] = []

    def paths(self, root: str) -> list[tuple[str, bool]]:
        now = time.monotonic()
        if root != self._root or now - self._at > CACHE_SECONDS:
            self._root, self._at, self._paths = root, now, _walk(root)
        return self._paths


def _walk(root: str) -> list[tuple[str, bool]]:
    found: list[tuple[str, bool]] = []
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        rel = os.path.relpath(current, root)
        prefix = "" if rel == "." else rel.replace(os.sep, "/") + "/"
        found += [(prefix + d + "/", True) for d in dirs]
        found += [(prefix + f, False) for f in sorted(files) if not f.startswith(".")]
        if len(found) >= MAX_FILES:
            break
    return found


def _listing(root: str, partial: str, limit: int) -> list[PathCandidate]:
    """partial 带目录（或 ~ / . 开头）：列出那个目录里名字以最后一段开头的"""
    if partial == "~":
        return [PathCandidate("~/", "~/", True)]
    head, sep, tail = partial.rpartition("/")
    shown = head + "/" if sep else ""            # 补全结果前面保留用户写的目录部分
    base = Path((head or "/") if sep else ".").expanduser()
    if not base.is_absolute():
        base = Path(root) / base
    try:
        entries = sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError:
        return []
    out: list[PathCandidate] = []
    for entry in entries:
        name = entry.name
        if name.startswith(".") and not tail.startswith("."):
            continue
        if not name.lower().startswith(tail.lower()):
            continue
        is_dir = entry.is_dir()
        path = shown + name + ("/" if is_dir else "")
        out.append(PathCandidate(quote(path), path, is_dir))
        if len(out) >= limit:
            break
    return out


def path_candidates(root: str, partial: str, index: ProjectIndex | None = None,
                    limit: int = 50) -> list[PathCandidate]:
    if "/" in partial or partial.startswith(("~", ".")):
        return _listing(root, partial, limit)
    paths = (index or ProjectIndex()).paths(root)
    if not partial:
        top = [(p, d) for p, d in paths if p.count("/") == (1 if d else 0)]
        top.sort(key=lambda item: (not item[1], item[0].lower()))
        return [PathCandidate(quote(p), p, d) for p, d in top[:limit]]
    needle = partial.lower()
    scored = []
    for path, is_dir in paths:
        lower = path.lower()
        name = lower.rstrip("/").rsplit("/", 1)[-1]
        if name.startswith(needle):
            rank = 0
        elif needle in name:
            rank = 1
        elif needle in lower:
            rank = 2
        else:
            continue
        scored.append((rank, len(path), path, is_dir))
    scored.sort()
    return [PathCandidate(quote(p), p, d) for _, _, p, d in scored[:limit]]
