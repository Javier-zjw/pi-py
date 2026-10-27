#!/bin/bash
# 标准答案：验证任务本身做得出来（harbor run -a oracle 应该全过）
cd /app
grep -rl calc_total . | xargs sed -i 's/calc_total/order_total/g'
