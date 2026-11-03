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


MANIFEST = "MANIFEST.json"


def default_cache_root() -> Path:
    """持久缓存目录。不用系统临时目录：macOS 会删掉临时目录里几天没碰过的文件，
    留下残缺的包（曾经因此报 No module named 'pi_coding_agent.cli'）"""
    return Path(os.environ.get("PI_PY_HARBOR_CACHE") or Path.home() / ".cache" / "pi-py-harbor")


def write_manifest(folder: Path) -> None:
    """记下目录里每个文件和大小；is_complete 用它核对缓存有没有被删坏"""
    files = {str(p.relative_to(folder)): p.stat().st_size
             for p in sorted(folder.rglob("*")) if p.is_file() and p.name != MANIFEST}
    (folder / MANIFEST).write_text(json.dumps(files, indent=0, sort_keys=True))


def is_complete(folder: Path) -> bool:
    """缓存目录完整：有清单，清单里的文件都在、大小都对"""
    try:
        files = json.loads((folder / MANIFEST).read_text())
    except (OSError, ValueError):
        return False
    for name, size in files.items():
        try:
            if (folder / name).stat().st_size != size:
                return False
        except OSError:
            return False
    return True


def _install(building: Path, target: Path) -> Path:
    """把建好的目录换到 target：target 已经完整（别的进程刚建好）就丢掉自己的，残缺的先删掉"""
    write_manifest(building)
    if is_complete(target):
        shutil.rmtree(building, ignore_errors=True)
        return target
    shutil.rmtree(target, ignore_errors=True)
    building.rename(target)
    return target


def deps_key(repo: Path = REPO) -> str:
    """依赖由三个包的 pyproject 决定（加上目标 Python 版本）"""
    digest = hashlib.sha256(repr(MIN_PYTHON).encode())
    for name in PACKAGES:
        digest.update((repo / "packages" / name / "pyproject.toml").read_bytes())
    return digest.hexdigest()[:16]


def build_deps(repo: Path = REPO, cache_root: Path | None = None) -> Path:
    """三个包的第三方依赖（httpx 等，纯 Python），只在 pyproject 变了时联网装一次"""
    cache_root = cache_root or default_cache_root()
    target = cache_root / f"deps-{deps_key(repo)}"
    if is_complete(target):
        return target
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("打包 pi-py 需要 uv（https://docs.astral.sh/uv/）")
    cache_root.mkdir(parents=True, exist_ok=True)
    building = cache_root / f"{target.name}.{os.getpid()}.tmp"
    shutil.rmtree(building, ignore_errors=True)
    # --no-sources：按普通包装（否则工作区设置会让 pi-ai / pi-agent 装成指向本机路径的 editable）；
    # 装完把我们自己的三个包删掉——它们每次从源码现拷，这里只留依赖
    command = [uv, "pip", "install", "--quiet", "--no-sources",
               "--target", str(building), "--python-version", "{}.{}".format(*MIN_PYTHON),
               *(str(repo / "packages" / name) for name in PACKAGES)]
    done = subprocess.run(command, cwd=repo, text=True, capture_output=True, check=False)
    if done.returncode != 0:
        shutil.rmtree(building, ignore_errors=True)
        raise RuntimeError(f"下载 pi-py 的依赖失败（要能访问 PyPI，只在第一次和改了 pyproject 后需要）："
                           f"{done.stderr.strip()[-1500:]}")
    native = [p.name for p in building.rglob("*") if p.suffix in (".so", ".pyd", ".dylib")]
    if native:
        # 容器可能是另一种 CPU（Mac 上是 arm64），带编译产物的依赖不能直接搬过去
        shutil.rmtree(building, ignore_errors=True)
        raise RuntimeError(f"依赖里有编译产物，不能跨平台使用：{native[:5]}")
    shutil.rmtree(building / "bin", ignore_errors=True)       # 本机路径的启动脚本，用不上
    for name in PACKAGES:
        module = name.replace("-", "_")
        shutil.rmtree(building / module, ignore_errors=True)
        for info in building.glob(f"{module}-*.dist-info"):
            shutil.rmtree(info, ignore_errors=True)
    return _install(building, target)


def build_bundle(repo: Path = REPO, cache_root: Path | None = None) -> Path:
    """依赖 + 三个包的源码（直接拷，不联网）拼成一个目录，返回它；同样的源码只拼一次"""
    cache_root = cache_root or default_cache_root()
    target = cache_root / f"bundle-{fingerprint(repo)}"
    with _bundle_lock:
        if is_complete(target):
            return target
        deps = build_deps(repo, cache_root)
        building = cache_root / f"{target.name}.{os.getpid()}.tmp"
        shutil.rmtree(building, ignore_errors=True)
        shutil.copytree(deps, building, ignore=shutil.ignore_patterns(MANIFEST))
        for name in PACKAGES:
            module = name.replace("-", "_")
            shutil.copytree(repo / "packages" / name / module, building / module,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
        _install(building, target)
        prune_bundles(cache_root, keep=target)
        return target


def prune_bundles(cache_root: Path, keep: Path, limit: int = 3) -> None:
    """每改一次代码多一个包，只留最近用过的 limit 个（含 keep）"""
    bundles = sorted((p for p in cache_root.glob("bundle-*") if p.is_dir() and p != keep),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    for old in bundles[limit - 1:]:
        shutil.rmtree(old, ignore_errors=True)


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
    """跑一次 pi（print 模式，输出同时写进 output）；退出码是 pi 的。

    会话目录（配置目录下的 sessions）链接到 sessions_out：pi 边跑边写进日志目录，
    超时被强制结束也留得下记录——跑完再复制的话，最需要看的那次（卡住、超时）恰好什么都没有。
    pi 按项目分子目录（sessions/<项目>/xxx.jsonl），转换前由 collect_sessions 摊平。"""
    options = f"-t {shlex.quote(thinking)} " if thinking else ""
    if exclude_tools:
        options += f"--exclude-tools {shlex.quote(','.join(exclude_tools))} "
    sessions = shlex.quote(f"{config}/sessions")
    return (
        f"mkdir -p {shlex.quote(sessions_out)} {shlex.quote(str(config))} && "
        f"rm -rf {sessions} && ln -s {shlex.quote(sessions_out)} {sessions} && "
        f"{shlex.quote(str(pi))} --mode print --agent-dir {shlex.quote(str(config))} "
        f"-m {shlex.quote(model)} {options}{shlex.quote(instruction)} </dev/null 2>&1 "
        f"| tee {shlex.quote(output)}"
    )


def collect_sessions(root: Path) -> list[Path]:
    """把 root 下各项目子目录里的会话文件复制到 root 本层（Harbor 的 pi 转换器只看这一层），返回它们"""
    if not root.is_dir():
        return []
    for path in sorted(root.rglob("*.jsonl")):
        if path.parent != root and not (root / path.name).exists():
            (root / path.name).write_bytes(path.read_bytes())
    return sorted(root.glob("*.jsonl"))


def main(argv: list[str] | None = None) -> int:
    """python3 evals/pi_py_eval.py build：先打好包（evals/run.sh 在启动 Harbor 前调用）。
    打包失败在这一步就报出来，而不是每道题在 Harbor 的安装阶段各自重试、各自超时"""
    import sys

    args = sys.argv[1:] if argv is None else argv
    if args != ["build"]:
        print("用法：python3 evals/pi_py_eval.py build", file=sys.stderr)
        return 2
    try:
        print(build_bundle())
    except RuntimeError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
