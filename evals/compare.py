"""汇总 / 对比 Harbor 的评测结果（evals/jobs/<名字>/），回答"这次改动变好还是变差"。

    python evals/compare.py evals/jobs/基线                 # 一次运行的按任务汇总
    python evals/compare.py evals/jobs/基线 evals/jobs/改后  # 两次对比，标出变好 / 退步的任务
    python evals/compare.py A B --strict                    # 有任务退步就退出 1（可以接进 CI）

每个任务：通过（reward=1 的次数 / 次数）、全过（每次都过，同 pass^k，衡量稳定性）、异常（模型报错、
超时等，不是"做错了"）、平均工具调用次数、token（输入含缓存 + 输出）、费用、智能体耗时。
只用标准库，不 import harbor。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class Trial:
    task: str
    reward: float | None          # None：出了异常、没判分
    exception: str | None = None
    tool_calls: int = 0
    tokens: int = 0
    cost: float = 0.0
    seconds: float = 0.0


@dataclass
class TaskStats:
    task: str
    trials: list[Trial] = field(default_factory=list)

    @property
    def runs(self) -> int:
        return len(self.trials)

    @property
    def passed(self) -> int:
        return sum(1 for t in self.trials if (t.reward or 0) >= 1)

    @property
    def rate(self) -> float:
        return self.passed / self.runs if self.runs else 0.0

    @property
    def all_passed(self) -> bool:
        return self.runs > 0 and self.passed == self.runs

    @property
    def errors(self) -> int:
        return sum(1 for t in self.trials if t.exception)

    def mean(self, attr: str) -> float:
        return sum(getattr(t, attr) for t in self.trials) / self.runs if self.runs else 0.0


def _seconds(span: dict | None) -> float:
    try:
        start = datetime.fromisoformat(span["started_at"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(span["finished_at"].replace("Z", "+00:00"))
        return (end - start).total_seconds()
    except (TypeError, KeyError, ValueError):
        return 0.0


def _tool_calls(trial_dir: Path) -> int:
    try:
        steps = json.loads((trial_dir / "agent" / "trajectory.json").read_text())["steps"]
    except (OSError, ValueError, KeyError):
        return 0
    return sum(len(step.get("tool_calls") or []) for step in steps)


def load_trial(trial_dir: Path) -> Trial | None:
    try:
        data = json.loads((trial_dir / "result.json").read_text())
    except (OSError, ValueError):
        return None
    if "task_name" not in data:
        return None
    rewards = (data.get("verifier_result") or {}).get("rewards") or {}
    reward = rewards.get("reward")
    exception = (data.get("exception_info") or {}).get("exception_type")
    usage = data.get("agent_result") or {}
    return Trial(
        task=data["task_name"].split("/")[-1],
        reward=float(reward) if isinstance(reward, (int, float)) else None,
        exception=exception,
        tool_calls=_tool_calls(trial_dir),
        tokens=(usage.get("n_input_tokens") or 0) + (usage.get("n_output_tokens") or 0),
        cost=usage.get("cost_usd") or 0.0,
        seconds=_seconds(data.get("agent_execution")),
    )


def load_job(job_dir: Path) -> dict[str, TaskStats]:
    if not job_dir.is_dir():
        raise SystemExit(f"没有这个结果目录：{job_dir}")
    stats: dict[str, TaskStats] = {}
    for trial_dir in sorted(p for p in job_dir.iterdir() if p.is_dir()):
        trial = load_trial(trial_dir)
        if trial is not None:
            stats.setdefault(trial.task, TaskStats(trial.task)).trials.append(trial)
    if not stats:
        raise SystemExit(f"{job_dir} 里没有找到评测结果（要的是 harbor run 的输出目录）")
    return dict(sorted(stats.items()))


def _cells(s: TaskStats | None) -> list[str]:
    if s is None:
        return ["-"] * 7
    return [f"{s.passed}/{s.runs}", "✓" if s.all_passed else "✗", str(s.errors) if s.errors else "",
            f"{s.mean('tool_calls'):.1f}", f"{s.mean('tokens') / 1000:.1f}k",
            f"${s.mean('cost'):.4f}", f"{s.mean('seconds'):.0f}s"]


def _table(rows: list[list[str]]) -> str:
    def width(text: str) -> int:
        return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)
    widths = [max(width(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = []
    for n, row in enumerate(rows):
        lines.append("  ".join(cell + " " * (widths[i] - width(cell)) for i, cell in enumerate(row)).rstrip())
        if n == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


HEAD = ["通过", "全过", "异常", "工具", "token", "费用", "耗时"]


def _total(job: dict[str, TaskStats]) -> TaskStats:
    total = TaskStats("合计")
    for s in job.values():
        total.trials.extend(s.trials)
    return total


def summarize(job: dict[str, TaskStats]) -> str:
    rows = [["任务", *HEAD]]
    rows += [[name, *_cells(s)] for name, s in job.items()]
    rows.append(["合计", *_cells(_total(job))])
    return _table(rows)


def verdict(old: TaskStats | None, new: TaskStats | None) -> str:
    if old is None or new is None:
        return "新增" if old is None else "缺失"
    if new.rate > old.rate:
        return "↑ 变好"
    if new.rate < old.rate:
        return "↓ 退步"
    return ""


def compare(old: dict[str, TaskStats], new: dict[str, TaskStats]) -> tuple[str, list[str]]:
    names = sorted(set(old) | set(new))
    rows = [["任务", "通过（前 → 后）", "工具", "token", "费用", "耗时", ""]]
    regressed = []
    for name in [*names, "合计"]:
        a = _total(old) if name == "合计" else old.get(name)
        b = _total(new) if name == "合计" else new.get(name)
        ca, cb = _cells(a), _cells(b)
        mark = verdict(a, b)
        if mark == "↓ 退步" and name != "合计":
            regressed.append(name)
        rows.append([name, f"{ca[0]} → {cb[0]}", f"{ca[3]} → {cb[3]}", f"{ca[4]} → {cb[4]}",
                     f"{ca[5]} → {cb[5]}", f"{ca[6]} → {cb[6]}", mark])
    return _table(rows), regressed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("jobs", nargs="+", type=Path, help="一个或两个 harbor run 的结果目录")
    parser.add_argument("--strict", action="store_true", help="有任务退步就退出 1")
    args = parser.parse_args(argv)
    if len(args.jobs) > 2:
        parser.error("最多对比两次运行")
    jobs = [load_job(j) for j in args.jobs]
    if len(jobs) == 1:
        print(summarize(jobs[0]))
        return 0
    table, regressed = compare(*jobs)
    print(table)
    runs = min(s.runs for job in jobs for s in job.values())
    if runs < 3:
        print(f"\n注意：每个任务只跑了 {runs} 次，差一两次可能只是运气。用 -k 3 以上再下结论。")
    if regressed:
        print(f"\n退步的任务：{', '.join(regressed)}（看 evals/jobs/<名字>/<任务>__*/agent/ 里的轨迹）")
    return 1 if regressed and args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
