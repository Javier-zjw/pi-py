"""pi-coding-agent 工具簇测试：不需要网络、不需要 key。

    python tests/test_tools.py
    python tests/test_tools.py -v      # 打印每个工具的实际输出
    pytest tests/test_tools.py         # 也能跑

覆盖 text.py + tools/ 六个文件。全部在临时目录里操作，不碰你的项目文件。
"""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))                     # packages/pi-coding-agent
sys.path.insert(0, str(HERE.parents[2] / "pi-agent"))
sys.path.insert(0, str(HERE.parents[2] / "pi-ai"))

from pi_agent import ToolContext, validate_tool_arguments  # noqa: E402
from pi_ai import ImageContent, TextContent  # noqa: E402

from pi_coding_agent.text import has_surrogates, sanitize, stream_decoder  # noqa: E402
from pi_coding_agent.tools import (  # noqa: E402
    ALL_TOOL_NAMES,
    DEFAULT_TOOL_NAMES,
    READ_ONLY_TOOL_NAMES,
    create_all_tools,
    create_coding_tools,
    create_read_only_tools,
    create_tools,
    define_tool,
    fuzzy_find,
    generate_unified_patch,
    truncate_head,
    truncate_tail,
)

VERBOSE = "-v" in sys.argv
UNDER_PYTEST = "PYTEST_CURRENT_TEST" in os.environ
FAILURES: list[str] = []

PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILURES.append(f"{name}: {detail}")
    print(f"  FAIL {name} {detail}")
    if UNDER_PYTEST:
        raise AssertionError(f"{name}: {detail}")


def ctx() -> ToolContext:
    return ToolContext(tool_call_id="t")


def process_alive(pid: int) -> bool:
    """判断进程是否真的还在跑。

    注意不能只用 os.kill(pid, 0)：进程被杀之后如果父进程已经先死了，
    它会变成僵尸（状态 Z）挂在 init 下面，此时 kill(pid, 0) 依然成功，
    看起来像"没杀掉"。必须看进程状态。
    """
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():                      # Linux
        try:
            return stat.read_text().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
        except (OSError, IndexError):
            return True
    try:                                   # macOS / BSD
        import subprocess

        out = subprocess.run(
            ["ps", "-o", "state=", "-p", str(pid)], capture_output=True, text=True
        ).stdout.strip()
        return bool(out) and not out.startswith("Z")
    except OSError:
        return True


def text_of(result) -> str:
    for block in result.content:
        if isinstance(block, TextContent):
            return block.text
    return ""


class Sandbox:
    """临时工作目录，退出时清理。"""

    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix="pi-tools-"))
        return self.path

    def __exit__(self, *exc) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def tool(name: str, cwd: Path):
    created = create_tools([name], cwd)
    if not created:
        raise AssertionError(f"create_tools 没能造出 {name}")
    return created[0]


# --------------------------------------------------------------------------- #
# 0. text.py 编码边界
# --------------------------------------------------------------------------- #


def test_text_boundary() -> None:
    broken = "中文报告".encode("utf-8").decode("ascii", "surrogateescape")
    check("text: 检出孤立代理字符", has_surrogates(broken))
    check("text: 无损还原", sanitize(broken) == "中文报告", repr(sanitize(broken)))
    check("text: 干净字符串原样返回", sanitize("已经正常") == "已经正常")
    check("text: 非字符串不炸", sanitize(None) is None)

    payload = ("中文输出" * 500).encode("utf-8")
    chunks = [payload[i : i + 4096] for i in range(0, len(payload), 4096)]
    naive = "".join(c.decode("utf-8", "replace") for c in chunks)
    check("text: 朴素分块确实会烂", "\ufffd" in naive)
    decoder = stream_decoder()
    fixed = "".join(decoder.decode(c) for c in chunks) + decoder.decode(b"", True)
    check("text: 增量解码跨块正确", fixed == "中文输出" * 500)


# --------------------------------------------------------------------------- #
# 1. truncate
# --------------------------------------------------------------------------- #


def test_truncate() -> None:
    short = "a\nb\nc"
    out, cut = truncate_tail(short, max_lines=10)
    check("truncate: 不超限时原样返回", out == short and not cut)

    out, cut = truncate_tail("\n".join(str(i) for i in range(100)), max_lines=10)
    check("truncate: 行数截断", cut and out.split("\n")[0] == "0", out[:20])
    check("truncate: 截断后有说明", "truncated" in out or "截断" in out, out[-60:])

    out, cut = truncate_tail("x" * 5000, max_bytes=1000)
    check("truncate: 字节截断", cut and len(out.encode("utf-8")) < 2000)

    out, cut = truncate_tail("行" * 3000, max_bytes=1000)
    check("truncate: 中文字节截断不切半个字", "\ufffd" not in out and cut)

    hinted, _ = truncate_tail("\n".join(str(i) for i in range(100)), max_lines=5,
                              hint="用 offset=6 继续")
    check("truncate: 提示可操作", "offset" in hinted, hinted[-60:])

    out, cut = truncate_head("\n".join(str(i) for i in range(100)), max_lines=10)
    check("truncate_head: 保留尾部", out.strip().endswith("99") and cut, out[-20:])


# --------------------------------------------------------------------------- #
# 2. read
# --------------------------------------------------------------------------- #


async def case_read() -> None:
    with Sandbox() as cwd:
        (cwd / "a.py").write_text("def foo():\n    return 1\n\n\nprint(foo())\n", "utf-8")
        read = tool("read", cwd)

        r = await read.execute({"path": "a.py"}, ctx())
        body = text_of(r)
        if VERBOSE:
            print("   ", repr(body[:60]))
        check("read: 带行号", "1\tdef foo" in body or "1  def foo" in body, repr(body[:40]))
        check("read: 内容完整", "print(foo())" in body)
        check("read: 不是错误", not r.is_error)
        check("read: details 有路径", isinstance(r.details, dict) and "path" in r.details)

        r = await read.execute({"path": "a.py", "offset": 2, "limit": 1}, ctx())
        body = text_of(r)
        check("read: offset/limit 生效",
              "return 1" in body and "def foo" not in body, repr(body))
        check("read: 行号跟着 offset 走", body.strip().startswith("2"), repr(body[:10]))

        r = await read.execute({"path": "没有这个.py"}, ctx())
        check("read: 文件不存在报错", r.is_error and "not found" in text_of(r).lower()
              or r.is_error, text_of(r))

        (cwd / "sub").mkdir()
        r = await read.execute({"path": "sub"}, ctx())
        check("read: 目录报错而不是崩", r.is_error, text_of(r))

        (cwd / "pic.png").write_bytes(PNG_HEADER)
        r = await read.execute({"path": "pic.png"}, ctx())
        images = [b for b in r.content if isinstance(b, ImageContent)]
        check("read: 图片按 magic bytes 识别", len(images) == 1, str(r.content[:1]))
        if images:
            check("read: 图片 MIME 正确", images[0].mime_type == "image/png", images[0].mime_type)
            check("read: 图片是 base64",
                  base64.b64decode(images[0].data)[:8] == PNG_HEADER[:8])

        (cwd / "gbk.txt").write_bytes("中文内容".encode("gbk"))
        r = await read.execute({"path": "gbk.txt"}, ctx())
        check("read: 非 UTF-8 明确报错而不是塞脏数据", r.is_error, text_of(r)[:60])
        check("read: 报错信息不含代理字符", not has_surrogates(text_of(r)))

        big = "\n".join(f"line {i}" for i in range(5000))
        (cwd / "big.txt").write_text(big, "utf-8")
        r = await read.execute({"path": "big.txt"}, ctx())
        body = text_of(r)
        check("read: 大文件被截断", len(body.split("\n")) < 4000, str(len(body.split("\n"))))
        check("read: 截断后给出续读方式", "offset" in body, body[-80:])

        (cwd / "中文名.txt").write_text("内容", "utf-8")
        r = await read.execute({"path": "中文名.txt"}, ctx())
        check("read: 中文文件名", not r.is_error and "内容" in text_of(r), text_of(r)[:40])


# --------------------------------------------------------------------------- #
# 3. write
# --------------------------------------------------------------------------- #


async def case_write() -> None:
    with Sandbox() as cwd:
        write = tool("write", cwd)

        r = await write.execute({"path": "new.txt", "content": "hello\n"}, ctx())
        check("write: 创建文件", (cwd / "new.txt").read_text("utf-8") == "hello\n")
        check("write: 不是错误", not r.is_error)
        check("write: details 标记新建",
              isinstance(r.details, dict) and r.details.get("created") is True, str(r.details))

        r = await write.execute({"path": "new.txt", "content": "覆盖了\n"}, ctx())
        check("write: 覆盖已有文件", (cwd / "new.txt").read_text("utf-8") == "覆盖了\n")
        check("write: details 标记非新建", r.details.get("created") is False, str(r.details))

        await write.execute({"path": "a/b/c/deep.txt", "content": "x"}, ctx())
        check("write: 自动建父目录", (cwd / "a/b/c/deep.txt").exists())

        await write.execute({"path": "cn.txt", "content": "中文内容\n第二行\n"}, ctx())
        check("write: 中文写入正确",
              (cwd / "cn.txt").read_text("utf-8") == "中文内容\n第二行\n")

        # 同一路径的并发写不能交错
        await asyncio.gather(
            write.execute({"path": "race.txt", "content": "A" * 2000}, ctx()),
            write.execute({"path": "race.txt", "content": "B" * 2000}, ctx()),
        )
        content = (cwd / "race.txt").read_text("utf-8")
        check("write: 并发写不交错",
              content in ("A" * 2000, "B" * 2000), f"长度 {len(content)} 首字符 {content[:1]}")


# --------------------------------------------------------------------------- #
# 4. edit
# --------------------------------------------------------------------------- #


async def case_edit() -> None:
    with Sandbox() as cwd:
        edit = tool("edit", cwd)
        src = "def greet():\n    return 'hi'\n\n\ndef bye():\n    return 'hi'\n"
        (cwd / "a.py").write_text(src, "utf-8")

        r = await edit.execute(
            {"path": "a.py", "old_text": "def greet():", "new_text": "def hello():"}, ctx()
        )
        check("edit: 精确匹配", "def hello():" in (cwd / "a.py").read_text("utf-8"))
        check("edit: 返回 patch",
              isinstance(r.details, dict) and r.details.get("patch"), str(r.details)[:60])
        check("edit: patch 是 unified diff",
              "@@" in (r.details or {}).get("patch", ""), (r.details or {}).get("patch", "")[:40])
        check("edit: 标记精确匹配", (r.details or {}).get("match") == "exact",
              str((r.details or {}).get("match")))

        r = await edit.execute(
            {"path": "a.py", "old_text": "return 'hi'", "new_text": "return 'x'"}, ctx()
        )
        check("edit: 多处匹配拒绝执行", r.is_error and "2" in text_of(r), text_of(r)[:80])

        r = await edit.execute(
            {"path": "a.py", "old_text": "return 'hi'", "new_text": "return 'x'",
             "replace_all": True}, ctx()
        )
        check("edit: replace_all 生效",
              (cwd / "a.py").read_text("utf-8").count("return 'x'") == 2)

        r = await edit.execute(
            {"path": "a.py", "old_text": "根本没有这段", "new_text": "x"}, ctx()
        )
        check("edit: 找不到时报错", r.is_error, text_of(r)[:60])
        check("edit: 报错提示可操作", "read" in text_of(r).lower() or "exact" in text_of(r).lower()
              or "读" in text_of(r), text_of(r)[:80])

        r = await edit.execute(
            {"path": "a.py", "old_text": "def hello():", "new_text": "def hello():"}, ctx()
        )
        check("edit: 无变化时报错", r.is_error, text_of(r)[:60])

        # 模糊匹配：智能引号 + 行尾空格（缩进必须照抄）
        (cwd / "b.py").write_text("def f():\n    return 'value'\n", "utf-8")
        r = await edit.execute(
            {"path": "b.py", "old_text": "    return \u2018value\u2019   ",
             "new_text": "    return 'other'"}, ctx()
        )
        check("edit: 模糊匹配智能引号", not r.is_error, text_of(r)[:70])
        if not r.is_error:
            check("edit: 标记模糊匹配", (r.details or {}).get("match") == "fuzzy",
                  str((r.details or {}).get("match")))
            check("edit: 模糊匹配后内容正确",
                  "return 'other'" in (cwd / "b.py").read_text("utf-8"))

        # CRLF 文件
        (cwd / "crlf.py").write_bytes(b"a = 1\r\nb = 2\r\n")
        r = await edit.execute({"path": "crlf.py", "old_text": "b = 2", "new_text": "b = 3"}, ctx())
        check("edit: CRLF 文件能改", not r.is_error and "b = 3" in
              (cwd / "crlf.py").read_text("utf-8"), text_of(r)[:60])

        r = await edit.execute({"path": "没有.py", "old_text": "a", "new_text": "b"}, ctx())
        check("edit: 文件不存在报错", r.is_error)

        # fuzzy_find 单元行为
        span = fuzzy_find("x = 1\n    y = 2   \nz = 3", "    y = 2")
        check("edit: fuzzy_find 忽略行尾空白", span is not None, str(span))
        check("edit: fuzzy_find 不忽略缩进", fuzzy_find("    y = 2", "y = 2") is None)

        patch = generate_unified_patch("f.py", "a\nb\n", "a\nc\n")
        check("edit: 生成 patch 含增删",
              "+c" in patch and "-b" in patch, patch[:60])


# --------------------------------------------------------------------------- #
# 5. bash
# --------------------------------------------------------------------------- #


async def case_bash() -> None:
    with Sandbox() as cwd:
        bash = tool("bash", cwd)

        r = await bash.execute({"command": "echo hello"}, ctx())
        check("bash: 基本输出", "hello" in text_of(r) and not r.is_error, text_of(r)[:60])

        r = await bash.execute({"command": "echo out; echo err >&2"}, ctx())
        check("bash: 合并 stderr", "out" in text_of(r) and "err" in text_of(r), text_of(r)[:60])

        r = await bash.execute({"command": "exit 3"}, ctx())
        check("bash: 非零退出码标记错误", r.is_error, text_of(r)[:60])
        check("bash: 退出码写进 details",
              isinstance(r.details, dict) and r.details.get("exit_code") == 3, str(r.details)[:80])

        (cwd / "marker.txt").write_text("在沙箱里", "utf-8")
        r = await bash.execute({"command": "ls"}, ctx())
        check("bash: 在指定 cwd 执行", "marker.txt" in text_of(r), text_of(r)[:60])

        r = await bash.execute(
            {"command": "python3 -c \"print('中文'*3000)\""}, ctx()
        )
        check("bash: 中文不被分块切坏", "\ufffd" not in text_of(r), text_of(r)[:40])

        r = await bash.execute({"command": "seq 1 100000"}, ctx())
        check("bash: 大输出被截断",
              (r.details or {}).get("truncated") is True, str((r.details or {}).get("truncated")))
        full = (r.details or {}).get("full_output_path")
        check("bash: 完整输出落到临时文件", full and Path(full).exists(), str(full))
        if full:
            check("bash: 临时文件路径出现在输出里", full in text_of(r), text_of(r)[-80:])
            Path(full).unlink(missing_ok=True)

        started = time.monotonic()
        r = await bash.execute({"command": "sleep 5", "timeout": 0.4}, ctx())
        elapsed = time.monotonic() - started
        check("bash: 超时被杀掉", elapsed < 3, f"{elapsed:.1f}s")
        check("bash: 超时标记", (r.details or {}).get("timed_out") is True, str(r.details)[:80])

        # 中断：cancel_event 置位后进程应被杀
        c = ctx()
        task = asyncio.create_task(bash.execute({"command": "sleep 20"}, c))
        await asyncio.sleep(0.3)
        started = time.monotonic()
        c.cancel_event.set()
        r = await asyncio.wait_for(task, timeout=5)
        check("bash: 中断能杀掉进程", time.monotonic() - started < 3,
              f"{time.monotonic() - started:.1f}s")

        # 子进程树：父进程被杀后孙子进程不该继续跑
        c2 = ctx()
        pidfile = cwd / "child.pid"
        task = asyncio.create_task(
            bash.execute(
                {"command": f"(sleep 20 & echo $! > {pidfile}); sleep 20"}, c2
            )
        )
        await asyncio.sleep(0.5)
        c2.cancel_event.set()
        await asyncio.wait_for(task, timeout=5)
        await asyncio.sleep(0.2)
        if pidfile.exists():
            child_pid = int(pidfile.read_text().strip())
            alive = process_alive(child_pid)
            check("bash: 子进程也被杀（进程组）", not alive, f"pid {child_pid} 还活着")
            if alive:
                os.kill(child_pid, 9)

        # 流式回调
        updates: list[str] = []
        c3 = ctx()
        c3.on_update = lambda partial: updates.append(text_of(partial))
        await bash.execute(
            {"command": "for i in 1 2 3; do echo line$i; sleep 0.1; done"}, c3
        )
        check("bash: 有流式更新", len(updates) >= 2, f"{len(updates)} 次")
        if updates:
            check("bash: 更新内容递增", len(updates[-1]) >= len(updates[0]))

        r = await bash.execute({"command": "printf 'a\\033[31mred\\033[0m b'"}, ctx())
        check("bash: 剥掉 ANSI 转义", "\033[" not in text_of(r), repr(text_of(r)[:40]))


# --------------------------------------------------------------------------- #
# 6. ls / grep / find
# --------------------------------------------------------------------------- #


async def case_ls() -> None:
    with Sandbox() as cwd:
        (cwd / "a.txt").write_text("x", "utf-8")
        (cwd / "sub").mkdir()
        (cwd / "sub" / "b.txt").write_text("y", "utf-8")
        (cwd / ".hidden").mkdir()
        (cwd / ".hidden" / "secret.txt").write_text("z", "utf-8")
        (cwd / "node_modules").mkdir()
        (cwd / "node_modules" / "pkg.js").write_text("z", "utf-8")
        ls = tool("ls", cwd)

        body = text_of(await ls.execute({}, ctx()))
        check("ls: 列出文件", "a.txt" in body, body[:80])
        check("ls: 目录带斜杠", "sub/" in body, body[:80])

        body = text_of(await ls.execute({"recursive": True}, ctx()))
        check("ls: 递归", "b.txt" in body, body[:80])
        check("ls: 递归跳过隐藏目录", "secret.txt" not in body, body[:120])
        check("ls: 递归跳过 node_modules", "pkg.js" not in body, body[:120])

        r = await ls.execute({"path": "没有这个目录"}, ctx())
        check("ls: 路径不存在报错", r.is_error)


async def case_grep() -> None:
    with Sandbox() as cwd:
        (cwd / "a.py").write_text("def foo():\n    return TODO\n", "utf-8")
        (cwd / "b.js").write_text("// TODO: 修一下\nconst x = 1;\n", "utf-8")
        (cwd / ".git").mkdir()
        (cwd / ".git" / "c.py").write_text("TODO in git\n", "utf-8")
        grep = tool("grep", cwd)

        body = text_of(await grep.execute({"pattern": "TODO"}, ctx()))
        if VERBOSE:
            print("   ", repr(body[:120]))
        check("grep: 找到匹配", "a.py" in body and "b.js" in body, body[:120])
        check("grep: 输出含行号", ":2:" in body or ":1:" in body, body[:120])
        check("grep: 跳过 .git", "c.py" not in body, body[:150])

        body = text_of(await grep.execute({"pattern": "TODO", "glob": "*.py"}, ctx()))
        check("grep: glob 过滤", "a.py" in body and "b.js" not in body, body[:120])

        body = text_of(await grep.execute({"pattern": "todo", "ignore_case": True}, ctx()))
        check("grep: 忽略大小写", "a.py" in body, body[:120])

        body = text_of(await grep.execute({"pattern": "绝不存在的字符串"}, ctx()))
        check("grep: 无匹配时友好提示", "No matches" in body or "没有" in body or "无" in body,
              body[:80])

        r = await grep.execute({"pattern": "[unclosed"}, ctx())
        check("grep: 非法正则不崩", r.is_error or "matches" in text_of(r).lower(),
              text_of(r)[:80])

        body = text_of(await grep.execute({"pattern": "修一下"}, ctx()))
        check("grep: 中文匹配", "b.js" in body, body[:80])


async def case_find() -> None:
    with Sandbox() as cwd:
        (cwd / "src").mkdir()
        (cwd / "src" / "main.py").write_text("x", "utf-8")
        (cwd / "src" / "util.py").write_text("x", "utf-8")
        (cwd / "readme.md").write_text("x", "utf-8")
        (cwd / "__pycache__").mkdir()
        (cwd / "__pycache__" / "cached.py").write_text("x", "utf-8")
        find = tool("find", cwd)

        body = text_of(await find.execute({"pattern": "*.py"}, ctx()))
        check("find: 匹配扩展名", "main.py" in body and "util.py" in body, body[:100])
        check("find: 跳过 __pycache__", "cached.py" not in body, body[:120])
        check("find: 不匹配的不出现", "readme.md" not in body, body[:100])

        body = text_of(await find.execute({"pattern": "readme*"}, ctx()))
        check("find: 前缀匹配", "readme.md" in body, body[:80])

        body = text_of(await find.execute({"pattern": "*.rs"}, ctx()))
        check("find: 无结果时友好提示", "No files" in body or "没有" in body or "无" in body,
              body[:80])


# --------------------------------------------------------------------------- #
# 7. 注册表与 schema
# --------------------------------------------------------------------------- #


def test_registry() -> None:
    with Sandbox() as cwd:
        names = {t.name for t in create_all_tools(cwd)}
        check("注册表: ALL 名单一致", names == set(ALL_TOOL_NAMES), str(names))
        check("注册表: 默认工具集",
              {t.name for t in create_coding_tools(cwd)} == set(DEFAULT_TOOL_NAMES))
        check("注册表: 只读工具集",
              {t.name for t in create_read_only_tools(cwd)} == set(READ_ONLY_TOOL_NAMES))
        check("注册表: 未知名字被忽略而不是崩",
              [t.name for t in create_tools(["read", "不存在"], cwd)] == ["read"])

        for t in create_all_tools(cwd):
            schema = t.parameters
            check(f"schema: {t.name} 是 object",
                  schema.get("type") == "object", str(schema.get("type")))
            check(f"schema: {t.name} 有 properties", isinstance(schema.get("properties"), dict))
            check(f"schema: {t.name} 有描述", bool(t.description.strip()))
            for key in schema.get("required", []):
                check(f"schema: {t.name} 的必填 {key} 已声明",
                      key in schema["properties"], str(schema["properties"].keys()))

        # 跨层验证：pi_agent 的校验器认得这些 schema
        read = tool("read", cwd)
        args = validate_tool_arguments(read.parameters, {"path": "a.py"})
        check("schema: 能通过 pi_agent 校验", args["path"] == "a.py", str(args))

        custom = define_tool(
            "noop", "什么都不做", {"type": "object", "properties": {}},
            lambda a, c: None, label="空"
        )
        check("注册表: define_tool 可用",
              custom.name == "noop" and custom.label == "空")


# --------------------------------------------------------------------------- #
# pytest 兼容层
# --------------------------------------------------------------------------- #


def test_read() -> None:
    asyncio.run(case_read())


def test_write() -> None:
    asyncio.run(case_write())


def test_edit() -> None:
    asyncio.run(case_edit())


def test_bash() -> None:
    asyncio.run(case_bash())


def test_ls() -> None:
    asyncio.run(case_ls())


def test_grep() -> None:
    asyncio.run(case_grep())


def test_find() -> None:
    asyncio.run(case_find())


async def main() -> int:
    print("text.py 编码边界"); test_text_boundary()
    print("truncate");         test_truncate()
    print("read");             await case_read()
    print("write");            await case_write()
    print("edit");             await case_edit()
    print("bash");             await case_bash()
    print("ls");               await case_ls()
    print("grep");             await case_grep()
    print("find");             await case_find()
    print("注册表与 schema");  test_registry()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} 处失败：")
        for f in FAILURES:
            print(f"  - {f}")
        print("\n对照 tests/TOOLS_CHECKLIST.md 定位实现位置")
        return 1
    print("全部通过 —— 工具簇可以交给 session 簇了")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))