"""统计文本文件的行数和词数。

用法：python wordstat.py FILE
"""

import argparse
import re

WORD = re.compile(r"[A-Za-z']+")


def stats(text):
    return {"lines": len(text.splitlines()), "words": len(WORD.findall(text))}


def main(argv=None):
    parser = argparse.ArgumentParser(description="count lines and words")
    parser.add_argument("file")
    args = parser.parse_args(argv)
    with open(args.file, encoding="utf-8") as f:
        text = f.read()
    result = stats(text)
    print(f"lines: {result['lines']}")
    print(f"words: {result['words']}")


if __name__ == "__main__":
    main()
