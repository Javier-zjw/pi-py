"""Harbor 接入的离线测试（pi_py_eval.py 的纯函数 + 任务集结构）：不联网、不用 Docker、不装 Harbor。

    python evals/tests/test_pi_py_eval.py
    pytest evals/tests

覆盖：密钥环境变量名和 pi 一致、取密钥的优先级；源码指纹（改代码才变，测试和缓存不算）；容器里找
Python（太旧的跳过）；启动脚本（-I：PYTHONPATH 和当前目录里的同名包挡不住它）；运行命令（指令原样传、
退出码是 pi 的、会话复制出来）；示范任务的文件齐全、判定放在独立容器。
真正的端到端（oracle / nop / 假模型）要 Docker，见 evals/README.md。
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
EVALS = HERE.parents[1]
REPO = HERE.parents[2]
sys.path.insert(0, str(EVALS))
sys.path.insert(0, str(REPO / "packages" / "pi-ai"))

import pi_py_eval as E
from pi_ai.auth import EnvCredentialStore

FAILURES: list[str] = []


def under_pytest() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILURES.append(f"{name}: {detail}")
    print(f"  FAIL {name} {detail}")
    if under_pytest():
        raise AssertionError(f"{name}: {detail}")


def test_keys() -> None:
    for provider in ("anthropic", "deepseek", "my-proxy", "kimi"):
        check(f"密钥: {provider} 的变量名和 pi 一致",
              E.key_env_name(provider) == EnvCredentialStore()._var(provider), E.key_env_name(provider))
    with tempfile.TemporaryDirectory() as tmp:
        agent = Path(tmp)
        env = {"DEEPSEEK_API_KEY": "from-env", "OPENAI_API_KEY": "openai-env"}
        check("密钥: 没有 auth.json 用环境变量", E.resolve_api_key("deepseek", agent, env) == "from-env")
        (agent / "auth.json").write_text('{"deepseek": {"apiKey": "from-auth"}}')
        check("密钥: auth.json 优先", E.resolve_api_key("deepseek", agent, env) == "from-auth")
        check("密钥: auth.json 没有这个 provider 时用环境变量",
              E.resolve_api_key("openai", agent, env) == "openai-env")
        check("密钥: 都没有返回 None", E.resolve_api_key("kimi", agent, env) is None)
        (agent / "auth.json").write_text("{坏的")
        check("密钥: auth.json 坏了退回环境变量", E.resolve_api_key("deepseek", agent, env) == "from-env")


def fake_repo(root: Path) -> None:
    for name in E.PACKAGES:
        package = root / "packages" / name
        module = package / name.replace("-", "_")
        module.mkdir(parents=True)
        (package / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n')
        (module / "__init__.py").write_text("X = 1\n")


def test_fingerprint() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fake_repo(root)
        first = E.fingerprint(root)
        check("指纹: 同样的源码结果相同", E.fingerprint(root) == first)
        tests = root / "packages" / "pi-ai" / "tests"
        tests.mkdir()
        (tests / "test_x.py").write_text("assert True\n")
        cache = root / "packages" / "pi-ai" / "pi_ai" / "__pycache__"
        cache.mkdir()
        (cache / "x.pyc").write_bytes(b"\0")
        check("指纹: 测试和 __pycache__ 不算", E.fingerprint(root) == first)
        (root / "packages" / "pi-agent" / "pi_agent" / "__init__.py").write_text("X = 2\n")
        second = E.fingerprint(root)
        check("指纹: 改了源码就变", second != first)
        (root / "packages" / "pi-coding-agent" / "pyproject.toml").write_text("[project]\n")
        check("指纹: 改了 pyproject 也变", E.fingerprint(root) != second)


def run_bash(command: str, **kw) -> subprocess.CompletedProcess:
    # Harbor 执行智能体命令时会在前面加 set -o pipefail
    bash = shutil.which("bash") or "bash"
    return subprocess.run([bash, "-c", f"set -o pipefail; {command}"], text=True,
                          capture_output=True, timeout=60, check=False, **kw)


def test_find_python() -> None:
    done = run_bash(E.FIND_PYTHON)
    found = done.stdout.strip()
    version = subprocess.run([found, "-c", "import sys; print(sys.version_info >= (3, 10))"],
                             text=True, capture_output=True, check=False).stdout.strip() if found else ""
    check("找 Python: 本机能找到 3.10+", done.returncode == 0 and version == "True",
          f"{done.returncode} {found!r}")
    with tempfile.TemporaryDirectory() as tmp:
        old = Path(tmp) / "python3"
        old.write_text("#!/bin/sh\nexit 1\n")          # 版本检查不过 = 太旧
        old.chmod(0o755)
        done = run_bash(E.FIND_PYTHON, env={"PATH": f"{tmp}:/usr/bin/false-dir"})
        check("找 Python: 只有太旧的就失败（去装一个）", done.returncode == 1 and not done.stdout.strip(),
              f"{done.returncode} {done.stdout!r}")


def test_launcher() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        lib = root / "lib"
        lib.mkdir()
        for name in E.PACKAGES:
            module = name.replace("-", "_")
            (lib / module).symlink_to(REPO / "packages" / name / module)
        bogus = root / "work"
        (bogus / "pi_coding_agent").mkdir(parents=True)
        (bogus / "pi_coding_agent" / "__init__.py").write_text("raise SystemExit('冒牌的')\n")
        # 任务目录里和标准库同名的文件（cli 要 import argparse）
        (bogus / "argparse.py").write_text("raise SystemExit('冒牌的 argparse')\n")
        script = root / "pi-py"
        script.write_text(E.launcher(sys.executable, lib))
        script.chmod(0o755)
        done = subprocess.run([str(script), "--help"], cwd=bogus, text=True, capture_output=True,
                              env={**os.environ, "PYTHONPATH": str(bogus)}, timeout=60,
                              check=False)
        check("启动脚本: 当前目录和 PYTHONPATH 里的同名包、和标准库同名的文件都挡不住它",
              done.returncode == 0 and "--agent-dir" in done.stdout, done.stderr[-500:])


def test_run_command() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config, logs = root / "config", root / "logs"
        pi = root / "pi"
        # 假的 pi：打印参数、在配置目录里留一个会话、按 PI_EXIT 退出
        pi.write_text(
            "#!/bin/sh\n"
            'for a in "$@"; do printf "[%s]\\n" "$a"; done\n'
            f'mkdir -p "{config}/sessions/--app--" && echo "{{}}" > "{config}/sessions/--app--/s.jsonl"\n'
            'exit "${PI_EXIT:-0}"\n')
        pi.chmod(pi.stat().st_mode | stat.S_IEXEC)
        instruction = "改一下 `a.py` 里的 $HOME 和 \"引号\"\n第二行 'x'"
        command = E.run_command("deepseek/deepseek-chat", instruction,
                                output=str(logs / "pi-py.txt"), sessions_out=str(logs / "pi/sessions"),
                                thinking="high", exclude_tools=("web_search", "web_fetch"),
                                pi=pi, config=config)
        done = run_bash(command, env={**os.environ, "PI_EXIT": "3"})
        output = (logs / "pi-py.txt").read_text() if (logs / "pi-py.txt").exists() else ""
        check("运行: 退出码是 pi 的（tee 不吞掉）", done.returncode == 3, str(done.returncode))
        check("运行: 指令原样传过去（不展开 $、引号、换行）", f"[{instruction}]" in output, output)
        check("运行: 模型、思考档位、print 模式、配置目录",
              all(f"[{a}]" in output for a in ("deepseek/deepseek-chat", "-t", "high", "--mode",
                                                "print", "--agent-dir", str(config))), output)
        check("运行: 关掉上网工具", "[--exclude-tools]\n[web_search,web_fetch]" in output, output)
        check("运行: 失败了会话也复制出来", (logs / "pi" / "sessions" / "s.jsonl").is_file())
        plain = E.run_command("m/x", "hi", output=str(logs / "o.txt"),
                              sessions_out=str(logs / "s2"), pi=pi, config=root / "none")
        done = run_bash(plain, env={**os.environ, "PI_EXIT": "0"})
        check("运行: 没给就不带 -t / --exclude-tools", done.returncode == 0
              and "[-t]" not in (logs / "o.txt").read_text()
              and "[--exclude-tools]" not in (logs / "o.txt").read_text())


def test_tasks() -> None:
    tasks = sorted(p for p in (EVALS / "tasks").iterdir() if p.is_dir())
    check("任务: 至少 5 个", len(tasks) >= 5, str([t.name for t in tasks]))
    for task in tasks:
        toml = (task / "task.toml").read_text()
        missing = [f for f in ("instruction.md", "environment/Dockerfile", "tests/test.sh",
                               "solution/solve.sh") if not (task / f).is_file()]
        check(f"任务 {task.name}: 文件齐全", not missing, str(missing))
        check(f"任务 {task.name}: 指令不空", bool((task / "instruction.md").read_text().strip()))
        executable = all(os.access(task / f, os.X_OK) for f in ("tests/test.sh", "solution/solve.sh"))
        check(f"任务 {task.name}: 脚本可执行", executable)
        check(f"任务 {task.name}: 判定写 reward.txt",
              "/logs/verifier/reward.txt" in (task / "tests/test.sh").read_text())
        check(f"任务 {task.name}: 名字和目录一致", f'name = "pi-py/{task.name}"' in toml)
        check(f"任务 {task.name}: 判定在独立容器、/app 作为产物传过去",
              'environment_mode = "separate"' in toml and 'artifacts = ["/app"]' in toml)
        check(f"任务 {task.name}: 有隐藏测试", any((task / "tests").glob("test_*.py")))


def main() -> int:
    print("密钥");      test_keys()
    print("指纹");      test_fingerprint()
    print("找 Python"); test_find_python()
    print("启动脚本");  test_launcher()
    print("运行命令");  test_run_command()
    print("任务集");    test_tasks()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} 处失败：")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("全部通过 —— Harbor 接入的离线部分可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
