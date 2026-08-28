"""
所有从操作系统进来的字符串——命令行参数、终端输入、文件名、子进程输出、
环境变量——必须先过这里，再进入 Context 或写进会话文件。

为什么必须在入口修：非 UTF-8 locale 下，Python 用 surrogateescape 解码
argv、环境变量和文件名，坏字节被塞进 0xDC80-0xDCFF 的代理区。这种字符串
在内存里活得好好的，直到某次 json.dumps().encode("utf-8") 才炸——那时它
可能已经流经了三层，离案发现场十万八千里。

pi_ai 和 pi_agent 不需要这个模块：前者只有一个解码入口且用了 replace，
后者根本不做 I/O。编码问题天然只属于合成层。
"""

from __future__ import annotations

import codecs
from typing import Any


def has_surrogates(text: str) -> bool:
    """字符串里是否含孤立代理字符。"""
    return any(0xD800 <= ord(c) <= 0xDFFF for c in text)


def sanitize(text: str) -> str:
    """
    修复 surrogateescape 造成的损坏。
    可逆还原，不是粗暴替换：按 surrogateescape 编码回原始字节，再按 UTF-8
    解一次。原本就是合法 UTF-8 的字节会完整还原成正确的中文；真正的非法
    字节退化成 U+FFFD。干净字符串原样返回，零开销。
    """

    if not isinstance(text, str) or not has_surrogates(text):
        return text
    return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def sanitize_deep(value: Any) -> Any:
    """递归处理 dict / list / str，用于工具参数和 details"""

    if isinstance(value, str):
        return sanitize(value)
    if isinstance(value, dict):
        return {sanitize_deep(k): sanitize_deep(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_deep(v) for v in value]

    return value

def stream_decoder() -> codecs.IncrementalDecoder:
    """
    给 bash 用的增量解码器。
    子进程输出按固定字节数读取，多字节字符会被切在块边界上。逐块调用
    bytes.decode() 会把切开的字符变成 U+FFFD——中文输出大约每 4KB 拦一个字。
    增量解码器跨块保留半个字符的状态，读完后用 decode(b"", final=True) 冲干净。
    """
    return codecs.getincrementaldecoder("utf-8")("replace")

def read_text_lenient(path, encodings: tuple[str, ...] = ("utf-8", "gbk", "latin-1")) -> str | None:
    """
    尽力读一个文本文件，读不出来返回 None。
    只用于配置类文件（AGENTS.md、SKILL.md、settings.json）——它们读失败不该让整个 CLI 起不来。
    工具里的 read 走严格模式，因为那时候明确报错比猜编码更有价值。
    """
    from pathlib import Path

    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError:
        return None

    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue

    return raw.decode("utf-8", "replace")

if __name__ == '__main__':
    broken = "中文".encode("utf-8").decode("ascii", "surrogateescape")
    assert sanitize(broken) == "中文"
    assert sanitize("正常") == "正常"
