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
import shlex
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


def fake_deps(cache: Path, root: Path) -> Path:
    """事先放好一份完整的依赖缓存：build_bundle 就不调 uv、不联网"""
    deps = cache / f"deps-{E.deps_key(root)}"
    (deps / "httpx").mkdir(parents=True)
    (deps / "httpx" / "__init__.py").write_text("HTTPX = 1\n")
    E.write_manifest(deps)
    return deps


def test_bundle() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root, cache = Path(tmp) / "repo", Path(tmp) / "cache"
        fake_repo(root)
        cli = root / "packages" / "pi-coding-agent" / "pi_coding_agent" / "cli.py"
        cli.write_text("def main(): return 0\n")
        cached = root / "packages" / "pi-ai" / "pi_ai" / "__pycache__"
        cached.mkdir()
        (cached / "x.pyc").write_bytes(b"\0")
        fake_deps(cache, root)
        bundle = E.build_bundle(root, cache)
        check("打包: 依赖 + 三个包的源码", (bundle / "httpx" / "__init__.py").is_file()
              and (bundle / "pi_coding_agent" / "cli.py").is_file()
              and (bundle / "pi_ai" / "__init__.py").is_file(), str(sorted(bundle.iterdir())))
        check("打包: 不带 __pycache__", not (bundle / "pi_ai" / "__pycache__").exists())
        check("打包: 带清单、完整", E.is_complete(bundle))
        check("打包: 同样的源码直接复用", E.build_bundle(root, cache) == bundle)

        # macOS 清理临时目录删掉了一部分文件：要发现并重建（以前会沿用残缺的目录）
        (bundle / "pi_coding_agent" / "cli.py").unlink()
        check("打包: 缺了文件就不算完整", not E.is_complete(bundle))
        check("打包: 缺文件的重建", E.build_bundle(root, cache) == bundle
              and (bundle / "pi_coding_agent" / "cli.py").is_file() and E.is_complete(bundle))
        (bundle / "pi_ai" / "__init__.py").write_text("")
        check("打包: 文件大小不对也不算完整", not E.is_complete(bundle))
        E.build_bundle(root, cache)
        check("打包: 大小不对的重建", (bundle / "pi_ai" / "__init__.py").read_text() == "X = 1\n")

        # 旧代码留下的、没有清单的残缺目录：换掉，不沿用
        cli.write_text("def main(): return 1\n")
        stale = cache / f"bundle-{E.fingerprint(root)}"
        (stale / "pi_coding_agent").mkdir(parents=True)
        (stale / ".complete").write_text("")
        fresh = E.build_bundle(root, cache)
        fresh_cli = fresh / "pi_coding_agent" / "cli.py"
        check("打包: 没清单的旧目录被换掉", fresh == stale and fresh_cli.is_file()
              and fresh_cli.read_text() == "def main(): return 1\n")

        for n in range(5):
            cli.write_text(f"def main(): return {n + 10}\n")
            latest = E.build_bundle(root, cache)
        kept = sorted(p.name for p in cache.glob("bundle-*"))
        check("打包: 只留最近 3 个", len(kept) == 3 and latest.name in kept, str(kept))
        check("打包: 依赖缓存不删", any(cache.glob("deps-*")))

        before = E.deps_key(root)
        (root / "packages" / "pi-agent" / "pyproject.toml").write_text(
            '[project]\nname = "pi-agent"\ndependencies = ["x"]\n')
        check("依赖: pyproject 变了换一份依赖缓存", E.deps_key(root) != before)


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
        # 假的 pi：打印参数、在配置目录里写会话（同 pi：sessions/<项目>/xxx.jsonl）、
        # 按 PI_SLEEP 卡住（模拟超时被杀）、按 PI_EXIT 退出
        pi.write_text(
            "#!/bin/sh\n"
            'for a in "$@"; do printf "[%s]\\n" "$a"; done\n'
            f'mkdir -p "{config}/sessions/--app--" && echo "{{}}" > "{config}/sessions/--app--/s.jsonl"\n'
            'sleep "${PI_SLEEP:-0}"\n'
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
        sessions = logs / "pi" / "sessions"
        check("运行: 会话直接写在日志目录里", (sessions / "--app--" / "s.jsonl").is_file())
        found = E.collect_sessions(sessions)
        check("运行: 摊平到日志目录这一层（Harbor 的转换器只看这层）",
              [p.name for p in found] == ["s.jsonl"] and (sessions / "s.jsonl").is_file(), str(found))
        check("运行: 再摊平一次不重复", E.collect_sessions(sessions) == found)
        check("运行: 目录不存在时返回空", E.collect_sessions(root / "nowhere") == [])

        # 超时被杀：pi 还没退出，会话也已经在日志目录里
        killed = root / "killed"
        command = E.run_command("m/x", "hi", output=str(killed / "o.txt"),
                                sessions_out=str(killed / "sessions"), pi=pi, config=config)
        done = run_bash(f"timeout 1 bash -c {shlex.quote('set -o pipefail; ' + command)}",
                        env={**os.environ, "PI_SLEEP": "30"})
        check("运行: 超时被杀，会话照样留在日志目录里", done.returncode == 124
              and (killed / "sessions" / "--app--" / "s.jsonl").is_file(), str(done.returncode))
        plain = E.run_command("m/x", "hi", output=str(logs / "o.txt"),
                              sessions_out=str(logs / "s2"), pi=pi, config=root / "none")
        done = run_bash(plain, env={**os.environ, "PI_EXIT": "0"})
        check("运行: 没给就不带 -t / --exclude-tools", done.returncode == 0
              and "[-t]" not in (logs / "o.txt").read_text()
              and "[--exclude-tools]" not in (logs / "o.txt").read_text())


def run_script(env_extra: dict[str, str], *args: str) -> list[str]:
    """跑 evals/run.sh，harbor 和 python3 换成假的：返回 harbor 收到的参数"""
    with tempfile.TemporaryDirectory() as tmp:
        bin_dir = Path(tmp)
        (bin_dir / "harbor").write_text('#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done\n')
        (bin_dir / "python3").write_text("#!/bin/sh\nexit 0\n")       # 打包那一步
        for name in ("harbor", "python3"):
            (bin_dir / name).chmod(0o755)
        env = {k: v for k, v in os.environ.items() if k != "EVAL_PROXY"}
        env.update(PATH=f"{bin_dir}:{os.environ['PATH']}", **env_extra)
        done = subprocess.run([str(EVALS / "run.sh"), *args], env=env, text=True,
                              capture_output=True, timeout=30, check=False)
    return done.stdout.splitlines()


def test_run_script() -> None:
    plain = run_script({}, "-m", "p/m")
    check("run.sh: 默认跑示范任务、结果写 evals/jobs、用 PiPy",
          plain[:2] == ["run", "-a"] and "pi_py_harbor:PiPy" in plain
          and plain[plain.index("-o") + 1] == "evals/jobs"
          and plain[plain.index("-p") + 1] == "evals/tasks", str(plain))
    check("run.sh: 安装时限默认放宽、写在用户参数前面（用户的覆盖它）",
          "--agent-setup-timeout-multiplier" in plain
          and plain.index("--agent-setup-timeout-multiplier") < plain.index("-m"), str(plain))
    check("run.sh: 没设 EVAL_PROXY 不带代理", "--ae" not in plain and "--ve" not in plain)
    dataset = run_script({}, "-m", "p/m", "-d", "terminal-bench@2.0")
    check("run.sh: 给了 -d 就不加默认的 -p", "-p" not in dataset, str(dataset))

    proxied = run_script({"EVAL_PROXY": "http://host.docker.internal:7890"}, "-m", "p/m")
    pairs = {(proxied[i], proxied[i + 1]) for i in range(len(proxied) - 1)}
    for flag in ("--ae", "--ve"):
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            check(f"run.sh: EVAL_PROXY → {flag} {name}",
                  (flag, f"{name}=http://host.docker.internal:7890") in pairs, str(proxied))
        check(f"run.sh: {flag} 不代理本机（宿主机上的模型服务直连）",
              (flag, "NO_PROXY=localhost,127.0.0.1,host.docker.internal") in pairs)


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
    print("打包");      test_bundle()
    print("找 Python"); test_find_python()
    print("启动脚本");  test_launcher()
    print("运行命令");  test_run_command()
    print("run.sh");    test_run_script()
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
