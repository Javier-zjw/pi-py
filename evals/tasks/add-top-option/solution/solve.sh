#!/bin/bash
# 标准答案：验证任务本身做得出来（harbor run -a oracle 应该全过）
cat > /app/wordstat.py <<'PY'
"""统计文本文件的行数和词数。

用法：python wordstat.py FILE [--top N]
"""

import argparse
import re
from collections import Counter

WORD = re.compile(r"[A-Za-z']+")


def stats(text):
    return {"lines": len(text.splitlines()), "words": len(WORD.findall(text))}


def top_words(text, n):
    counts = Counter(w.lower() for w in WORD.findall(text))
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]


def main(argv=None):
    parser = argparse.ArgumentParser(description="count lines and words")
    parser.add_argument("file")
    parser.add_argument("--top", type=int)
    args = parser.parse_args(argv)
    with open(args.file, encoding="utf-8") as f:
        text = f.read()
    result = stats(text)
    print(f"lines: {result['lines']}")
    print(f"words: {result['words']}")
    if args.top:
        for word, count in top_words(text, args.top):
            print(f"{word} {count}")


if __name__ == "__main__":
    main()
PY
