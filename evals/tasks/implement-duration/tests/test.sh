#!/bin/bash
# 判定：在独立的判定容器里对智能体交出的 /app 跑隐藏测试，全过 reward=1，否则 0
cd /app
if PYTHONPATH=/app python3 -m unittest discover -s /tests -p 'test_*.py' -v; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
