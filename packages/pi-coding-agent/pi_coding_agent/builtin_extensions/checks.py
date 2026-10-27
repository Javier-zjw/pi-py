"""
改完就检查：edit / write 成功后立刻检查那个文件，有问题附在工具结果后面（逻辑见 pi_coding_agent/checks.py）。

设置（settings.json）：

    "checks": {"enabled": true, "builtin": true, "timeout": 30, "maxLines": 30,
               "commands": {"*.py": "ruff check -q {file}", "*.ts": "npx eslint {file}"}}

builtin 是内置的语法 / 解析检查（Python、JSON、TOML）；commands 按文件名通配配额外命令，
{file} 换成文件路径，退出码非 0 就把输出给模型。不要这个功能就把 checks 写进 disabledExtensions。
"""

from __future__ import annotations

from pi_coding_agent.checks import check_after_edit

NAME = "checks"
DESCRIPTION = "改完文件立刻检查（语法、解析、配置的检查命令），有问题告诉模型"


def activate(pi) -> None:
    pi.after_tool_call(check_after_edit)
