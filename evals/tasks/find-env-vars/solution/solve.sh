#!/bin/bash
# 标准答案：验证任务本身做得出来（harbor run -a oracle 应该全过）
printf '%s\n' APP_DB_POOL APP_DB_URL APP_DEBUG APP_REGION APP_TIMEOUT PORT > /app/env_vars.txt
