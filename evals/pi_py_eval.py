"""pi-py 接 Harbor 用的纯函数：宿主机打包、取密钥、拼容器里的命令。

不 import harbor——离线单元测试（tests/test_pi_py_eval.py）直接用系统 Python 跑。
Harbor 那一层（智能体类）在 pi_py_harbor.py。
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path, PurePosixPath
from typing import Literal

REPO = Path(__file__).resolve().parents[1]
PACKAGES = ("pi-ai", "pi-agent", "pi-coding-agent")
MIN_PYTHON = (3, 10)

REMOTE_ROOT = PurePosixPath("/installed-agent/pi-py")
REMOTE_LIB = REMOTE_ROOT / "lib"
REMOTE_BIN = REMOTE_ROOT / "bin" / "pi-py"
# pi 的配置目录（models.json、settings.json、会话）；不放 /logs 下，跑完只把会话复制出去
REMOTE_CONFIG = PurePosixPath("/tmp/pi-py-agent")

ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]

_bundle_lock = threading.Lock()


# -- 宿主机：打包 ------------------------------------------------------------ #

def source_files(repo: Path = REPO) -> list[Path]:
    """决定包内容的文件：三个包的 pyproject 和源码（不含测试、缓存）"""
    files: list[Path] = []
    for name in PACKAGES:
        root = repo / "packages" / name
        files.append(root / "pyproject.toml")
        for path in sorted(root.rglob("*")):
            parts = path.relative_to(root).parts
            if path.is_file() and parts[0] != "tests" and "__pycache__" not in parts \
                    and path.suffix not in (".pyc", ".pyo"):
                files.append(path)
    return files


def fingerprint(repo: Path = REPO) -> str:
    """源码指纹：内容变了才重新打包"""
    digest = hashlib.sha256()
    for path in source_files(repo):
        digest.update(str(path.relative_to(repo)).encode())
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()[:16]


def git_version(repo: Path = REPO) -> str:
    """记进轨迹的版本：提交号，工作区有改动加 -dirty"""
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repo, text=True,
                              capture_output=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "packages"], cwd=repo,
                               text=True, capture_output=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return head + ("-dirty" if dirty else "")


def build_bundle(repo: Path = REPO, cache_root: Path | None = None) -> Path:
    """把三个包和依赖装进一个目录（只含纯 Python 文件），返回它；同样的源码只打一次"""
    cache_root = cache_root or Path(tempfile.gettempdir()) / "pi-py-harbor"
    target = cache_root / fingerprint(repo)
    with _bundle_lock:
        if (target / ".complete").exists():
            return target
        uv = shutil.which("uv")
        if uv is None:
            raise RuntimeError("打包 pi-py 需要 uv（https://docs.astral.sh/uv/）")
        building = cache_root / f"{target.name}.{os.getpid()}.tmp"
        shutil.rmtree(building, ignore_errors=True)
        # --no-sources：按普通包装（否则工作区设置会让 pi-ai / pi-agent 装成指向本机路径的 editable）
        # --no-cache：uv 只按 pyproject 的修改时间判断本地包要不要重建，可能装进旧代码
        command = [uv, "pip", "install", "--quiet", "--no-sources", "--no-cache",
                   "--target", str(building), "--python-version", "{}.{}".format(*MIN_PYTHON),
                   *(str(repo / "packages" / name) for name in PACKAGES)]
        done = subprocess.run(command, cwd=repo, text=True, capture_output=True, check=False)
        if done.returncode != 0:
            raise RuntimeError(f"打包 pi-py 失败：{done.stderr.strip()[-2000:]}")
        native = [p.name for p in building.rglob("*") if p.suffix in (".so", ".pyd", ".dylib")]
        if native:
            # 容器可能是另一种 CPU（Mac 上是 arm64），带编译产物的依赖不能直接搬过去
            raise RuntimeError(f"依赖里有编译产物，不能跨平台使用：{native[:5]}")
        shutil.rmtree(building / "bin", ignore_errors=True)       # 本机路径的启动脚本，用不上
        (building / "VERSION").write_text(git_version(repo) + "\n")
        (building / ".complete").write_text("")
        if target.exists():
            shutil.rmtree(building)
        else:
            building.rename(target)
    return target


# -- 宿主机：模型和密钥 ------------------------------------------------------- #

def key_env_name(provider: str) -> str:
    """pi 读密钥的环境变量名（同 pi_ai.auth.EnvCredentialStore）"""
    return f"{provider.upper().replace('-', '_')}_API_KEY"


def resolve_api_key(provider: str, agent_dir: Path, env: dict[str, str] | None = None) -> str | None:
    """按 pi 的优先级取 key：auth.json > 环境变量"""
    env = os.environ if env is None else env
    try:
        auth = json.loads((agent_dir / "auth.json").read_text("utf-8"))
        key = (auth.get(provider) or {}).get("apiKey")
        if key:
            return key
    except (OSError, ValueError, AttributeError):
        pass
    return env.get(key_env_name(provider)) or None


# -- 容器里的命令 ------------------------------------------------------------ #

FIND_PYTHON = (
    "for c in python3.13 python3.12 python3.11 python3.10 python3 python; do "
    'p=$(command -v "$c" 2>/dev/null) || continue; '
    f"\"$p\" -c 'import sys; sys.exit(sys.version_info < {MIN_PYTHON!r})' 2>/dev/null "
    '&& { echo "$p"; exit 0; }; '
    "done; exit 1"
)

INSTALL_PYTHON = (
    # 镜像里没有 Python 3.10+：用 uv 装一个独立的（只在这种情况下需要联网）
    "set -euo pipefail; "
    f"curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR={REMOTE_ROOT}/uv "
    "UV_NO_MODIFY_PATH=1 sh >/dev/null; "
    f"export UV_PYTHON_INSTALL_DIR={REMOTE_ROOT}/python; "
    f"{REMOTE_ROOT}/uv/uv python install 3.12 >/dev/null; "
    f"{REMOTE_ROOT}/uv/uv python find 3.12"
)


def launcher(python: str, lib: PurePosixPath | Path = REMOTE_LIB) -> str:
    """容器里的 pi-py 命令。

    包放在 sys.path 最前面；-I 让当前目录（任务的工作目录）和 PYTHONPATH 不进 sys.path——
    否则任务里和标准库同名的文件（json.py、argparse.py……）会被 pi 自己 import 进去。"""
    code = (f"import sys; sys.path.insert(0, {str(lib)!r}); "
            "from pi_coding_agent.cli import main; sys.exit(main())")
    return f"#!/bin/sh\nexec {shlex.quote(python)} -I -c {shlex.quote(code)} \"$@\"\n"


def run_command(model: str, instruction: str, *, output: str, sessions_out: str,
                thinking: str | None = None, exclude_tools: tuple[str, ...] = (),
                pi: PurePosixPath | Path = REMOTE_BIN,
                config: PurePosixPath | Path = REMOTE_CONFIG) -> str:
    """跑一次 pi（print 模式，输出同时写进 output）并把会话复制到 sessions_out；退出码是 pi 的"""
    options = f"-t {shlex.quote(thinking)} " if thinking else ""
    if exclude_tools:
        options += f"--exclude-tools {shlex.quote(','.join(exclude_tools))} "
    sessions = shlex.quote(f"{config}/sessions")
    return (
        f"mkdir -p {shlex.quote(sessions_out)}; "
        f"{{ {shlex.quote(str(pi))} --mode print --agent-dir {shlex.quote(str(config))} "
        f"-m {shlex.quote(model)} {options}{shlex.quote(instruction)} </dev/null 2>&1 "
        f"| tee {shlex.quote(output)}; }}; "
        "status=$?; "
        f"if [ -d {sessions} ]; then find {sessions} -name '*.jsonl' "
        f"-exec cp {{}} {shlex.quote(sessions_out)}/ \\; ; fi; "
        "exit $status"
    )
