#!/bin/bash
# 标准答案：验证任务本身做得出来（harbor run -a oracle 应该全过）
cat >> /app/duration.py <<'PY'


import re as _re

_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}
_PART = _re.compile(r"\s*(\d+)\s*([A-Za-z])")


def parse_duration(text):  # noqa: F811  标准答案：覆盖上面的占位实现
    text = text.strip()
    if not text:
        raise ValueError("empty duration")
    total, seen, pos = 0, set(), 0
    while pos < len(text):
        match = _PART.match(text, pos)
        if not match:
            raise ValueError(f"invalid duration: {text!r}")
        unit = match.group(2).lower()
        if unit not in _UNITS or unit in seen:
            raise ValueError(f"invalid duration: {text!r}")
        seen.add(unit)
        total += int(match.group(1)) * _UNITS[unit]
        pos = match.end()
    return total
PY
