#!/usr/bin/env bash
# 用 Harbor 评测当前工作区的 pi-py（说明见 evals/README.md）
#
#   evals/run.sh -m deepseek/deepseek-chat                         # 示范任务各跑一次
#   evals/run.sh -m deepseek/deepseek-chat -k 3                    # 各跑 3 次，看稳定性
#   evals/run.sh -m deepseek/deepseek-chat -p evals/tasks/fix-pagination   # 只跑一个任务
#   evals/run.sh -m deepseek/deepseek-chat -d terminal-bench@2.0   # 公开基准
#
# 其余参数原样交给 harbor run；结果写在 evals/jobs/。
set -euo pipefail
cd "$(dirname "$0")/.."

source_given=0
for arg in "$@"; do
  case "$arg" in -p|--path|-d|--dataset) source_given=1 ;; esac
done
if [ "$source_given" = 0 ]; then
  set -- -p evals/tasks "$@"
fi

# 先在本机把 pi-py 打好包：失败（比如第一次下载依赖时连不上 PyPI）就在这里报出来，
# 而不是每道题在 Harbor 的安装阶段各自重试、各自超时
python3 evals/pi_py_eval.py build >/dev/null

# 安装时限默认放宽到 3 倍：镜像里没有 Python 时要先用 apt 装，x86 镜像在 M 芯片上模拟运行更慢。
# 写在用户参数前面，用户自己给了就以用户的为准（同一个选项后面的覆盖前面的）
PYTHONPATH="evals${PYTHONPATH:+:$PYTHONPATH}" exec harbor run -a pi_py_harbor:PiPy -o evals/jobs \
  --agent-setup-timeout-multiplier 3 "$@"
