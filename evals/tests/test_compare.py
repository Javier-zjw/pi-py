"""evals/compare.py 的离线测试：用手造的 Harbor 结果目录。

    python evals/tests/test_compare.py
    pytest evals/tests

覆盖：读 trial 结果（奖励、异常、token、费用、耗时、从轨迹数工具调用）；跳过不是 trial 的目录；
按任务汇总（通过、全过、异常）；两次对比标出变好 / 退步 / 新增；--strict 有退步退出 1；次数少时提醒。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))

import compare as C

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


def trial(job: Path, task: str, n: int, reward: float | None, *, exception: str | None = None,
          tools: int = 2, tokens: tuple[int, int] = (1000, 100), cost: float = 0.01) -> None:
    folder = job / f"{task}__{n}"
    (folder / "agent").mkdir(parents=True)
    (folder / "result.json").write_text(json.dumps({
        "task_name": f"pi-py/{task}",
        "verifier_result": None if reward is None else {"rewards": {"reward": reward}},
        "exception_info": {"exception_type": exception} if exception else None,
        "agent_result": {"n_input_tokens": tokens[0], "n_output_tokens": tokens[1], "cost_usd": cost},
        "agent_execution": {"started_at": "2026-10-27T08:00:00Z",
                            "finished_at": "2026-10-27T08:00:30Z"},
    }))
    steps = [{"source": "agent", "tool_calls": [{"function_name": "bash"}] * tools}]
    (folder / "agent" / "trajectory.json").write_text(json.dumps({"steps": steps}))


def run_main(*args: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = C.main(list(args))
    return code, out.getvalue()


def test_load_and_summarize() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        job = Path(tmp) / "base"
        trial(job, "fix", 1, 1.0, tools=3)
        trial(job, "fix", 2, 0.0, tools=5)
        trial(job, "find", 1, None, exception="NonZeroAgentExitCodeError")
        (job / "result.json").write_text("{}")             # 整个 job 的汇总，不是 trial
        (job / "not-a-trial").mkdir()
        stats = C.load_job(job)
        check("读取: 按任务分组、跳过非 trial", sorted(stats) == ["find", "fix"], str(sorted(stats)))
        fix = stats["fix"]
        check("读取: 通过次数", fix.passed == 1 and fix.runs == 2 and not fix.all_passed)
        check("读取: 工具调用从轨迹数", fix.mean("tool_calls") == 4.0, str(fix.mean("tool_calls")))
        check("读取: token = 输入 + 输出、耗时", fix.mean("tokens") == 1100 and fix.mean("seconds") == 30)
        find = stats["find"]
        check("读取: 异常不算通过、单独计数", find.passed == 0 and find.errors == 1
              and find.trials[0].reward is None)
        _, out = run_main(str(job))
        check("汇总: 每个任务一行加合计", "fix" in out and "1/2" in out and "合计" in out
              and "1/3" in out, out)


def test_compare() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base, new = Path(tmp) / "base", Path(tmp) / "new"
        for n in range(3):
            trial(base, "fix", n, 1.0)
            trial(base, "rename", n, 0.0 if n else 1.0)
            trial(new, "fix", n, 0.0 if n == 0 else 1.0)      # 3/3 → 2/3
            trial(new, "rename", n, 1.0)                        # 1/3 → 3/3
            trial(new, "added", n, 1.0)
        code, out = run_main(str(base), str(new))
        rows = {line.split()[0]: line for line in out.splitlines() if line.strip()}
        check("对比: 退步的标出来", "↓ 退步" in rows.get("fix", "") and "3/3 → 2/3" in rows["fix"], out)
        check("对比: 变好的标出来", "↑ 变好" in rows.get("rename", ""), out)
        check("对比: 新加的任务", "新增" in rows.get("added", ""), out)
        check("对比: 列出退步的任务", "退步的任务：fix" in out, out)
        check("对比: 不加 --strict 退出 0", code == 0)
        check("对比: 跑了 3 次不提醒运气", "只跑了" not in out, out)
        code, _ = run_main(str(base), str(new), "--strict")
        check("对比: --strict 有退步退出 1", code == 1)
        code, out = run_main(str(new), str(base), "--strict")
        check("对比: 方向反过来照样认出退步", code == 1 and "退步的任务：rename" in out, out)
        one = Path(tmp) / "one"
        trial(one, "fix", 0, 1.0)
        trial(one, "rename", 0, 1.0)
        code, out = run_main(str(one), str(one), "--strict")
        check("对比: 一样的结果没有退步、次数少时提醒", code == 0 and "↓" not in out
              and "只跑了 1 次" in out, out)


def main() -> int:
    print("读取和汇总"); test_load_and_summarize()
    print("对比");       test_compare()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} 处失败：")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("全部通过 —— compare.py 可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
