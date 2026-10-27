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

PYTHONPATH="evals${PYTHONPATH:+:$PYTHONPATH}" exec harbor run -a pi_py_harbor:PiPy -o evals/jobs "$@"
