"""@文件 / 图片测试：不联网、不装东西。

    python tests/test_mentions.py
    pytest tests/test_mentions.py

覆盖 mentions.py（文件、行范围、目录、图片、二进制、截断、总量上限、不是文件的 @、
拖进来的图片路径、带空格的路径），以及接进会话后：用户消息分块、发给模型的内容、
附件事件、压缩只取原话、会话标题、models.json 的 input。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))                     # packages/pi-coding-agent
sys.path.insert(0, str(HERE.parents[2] / "pi-agent"))
sys.path.insert(0, str(HERE.parents[2] / "pi-ai"))
sys.path.insert(0, str(HERE.parent))

from fakes import FAKE_MODEL, Sandbox, fake_models
from pi_ai import ImageContent, TextContent, UserMessage
from pi_coding_agent import (
    AgentSession,
    ModelRuntime,
    SessionManager,
    create_agent_session_services,
)
from pi_coding_agent.mentions import MAX_IMAGE_BYTES, expand_mentions, user_words

FAILURES: list[str] = []
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


def under_pytest() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILURES.append(f"{name}: {detail}")
    print(f"  FAIL {name} {detail}")
    if under_pytest():
        raise AssertionError(f"{name}: {detail}")


def make_project(root: Path) -> None:
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("import os\n\ndef main():\n    print('hi')\n")
    (root / "src" / "util.py").write_text("x = 1\n")
    (root / "my notes.txt").write_text("带空格的文件\n")
    (root / "shot.png").write_bytes(PNG)
    (root / "doc.pdf").write_bytes(b"%PDF-1.4\x00\xff\xfe binary")
    (root / "big.py").write_text("\n".join(f"line {i}" for i in range(1, 3001)))


def test_expand() -> None:
    with Sandbox() as root:
        make_project(root)
        cwd = str(root)
        r = expand_mentions("看看 @src/app.py 有什么问题", cwd)
        check("文件: 原话不变", r.text == "看看 @src/app.py 有什么问题")
        check("文件: 内容带行号放进 <file> 块", '<file path="src/app.py">' in r.context
              and "     3\tdef main():" in r.context and r.context.startswith("<attachments>"), r.context)
        check("文件: 记下附了什么", [(a.label, a.kind, a.note) for a in r.attachments]
              == [("src/app.py", "file", "4 行")], str(r.attachments))

        r = expand_mentions("@src/app.py:3-4 这段", cwd)
        check("行范围: 只附这几行", '<file path="src/app.py" lines="3-4 of 4">' in r.context
              and "def main" in r.context and "import os" not in r.context, r.context)

        r = expand_mentions("@src/ 里有哪些文件", cwd)
        check("目录: 附文件列表", '<directory path="src/">' in r.context and "app.py" in r.context
              and r.attachments[0].note == "目录 2 项", r.context)

        r = expand_mentions("这张图 @shot.png 是什么", cwd)
        check("图片: 能看图的模型收到图片块", len(r.images) == 1 and isinstance(r.images[0], ImageContent)
              and r.images[0].mime_type == "image/png" and '<image path="shot.png" index="1"/>' in r.context,
              r.context)
        r = expand_mentions("这张图 @shot.png 是什么", cwd, vision=False)
        check("图片: 看不了图的模型不发图、提醒怎么开", not r.images and "cannot view images" in r.context
              and "models.json" in r.warnings[0], str(r.warnings))

        (root / "huge.png").write_bytes(PNG + b"\x00" * MAX_IMAGE_BYTES)
        r = expand_mentions("@huge.png", cwd)
        check("图片: 超过 5MB 不发并说明", not r.images and "5MB" in r.warnings[0], str(r.warnings))

        r = expand_mentions("@doc.pdf 总结一下", cwd)
        check("二进制: 不附内容、提醒", "binary file" in r.context and "二进制" in r.warnings[0], r.context)

        r = expand_mentions("@big.py", cwd)
        check("长文件: 按 2000 行截断、告诉模型用 read 接着看", "line 2000" in r.context
              and "line 2001" not in r.context and "offset=2001" in r.context, r.context[-200:])

        r = expand_mentions("用 @property 装饰器，发邮件给 a@b.com，@某人 看下", cwd)
        check("不是文件的 @ 原样留着、什么都不附", not r.context and not r.attachments and not r.warnings)

        r = expand_mentions("看 @src/app.py，还有 @src/util.py。", cwd)
        check("句末标点不算路径", [a.label for a in r.attachments] == ["src/app.py", "src/util.py"],
              str(r.attachments))
        r = expand_mentions("看看@src/app.py帮我改一下，还有＠src/util.py", cwd)
        check("紧挨中文的 @、后面直接接中文、全角 ＠", [a.label for a in r.attachments]
              == ["src/app.py", "src/util.py"], str(r.attachments))
        (root / "笔记.md").write_text("中文名\n")
        r = expand_mentions("看看@笔记.md里写了什么", cwd)
        check("中文文件名后面接中文", [a.label for a in r.attachments] == ["笔记.md"], str(r.attachments))
        r = expand_mentions('@"my notes.txt" 和 @my\\ notes.txt', cwd)
        check("带空格的路径（引号 / 转义）、同一个只附一次", [a.label for a in r.attachments] == ["my notes.txt"],
              str(r.attachments))

        dropped = str(root / "screen shot.png")
        Path(dropped).write_bytes(PNG)
        r = expand_mentions(f"看看这个 {dropped.replace(' ', chr(92) + ' ')} 报错", cwd)
        check("拖进来的图片路径（空格转义）也当附图", len(r.images) == 1 and r.attachments[0].label == "screen shot.png",
              str(r.attachments))
        r = expand_mentions(f"文件在 {root / 'src' / 'app.py'}", cwd)
        check("拖进来的不是图片：路径原样给模型，不自动附", not r.attachments)

        many = " ".join(f"@big.py:{i * 2000 + 1}-{i * 2000 + 2000}" for i in range(2)) + " " + \
            " ".join(f"@f{i}.txt" for i in range(8))
        for i in range(8):
            (root / f"f{i}.txt").write_text("x" * 40_000)
        r = expand_mentions(many, cwd)
        check("总量上限: 附太多时后面的只附路径", any(a.kind == "skipped" for a in r.attachments)
              and len(r.context) < 260_000, str([(a.label, a.kind) for a in r.attachments]))

    message = UserMessage(content=[TextContent(text="原话"), TextContent(text="<attachments>…")])
    check("user_words: 只取原话", user_words(message) == "原话" and user_words(UserMessage(content="纯文字")) == "纯文字")


async def case_session() -> None:
    from pi_coding_agent.compaction.state import extract_facts
    from pi_coding_agent.session.listing import describe_session

    with Sandbox() as root:
        make_project(root)
        models, provider = fake_models([[TextContent(text="看完了")]])
        vision = FAKE_MODEL.__class__(**{**FAKE_MODEL.__dict__, "input_modalities": ("text", "image")})
        models.models = [vision] if hasattr(models, "models") else models
        runtime = ModelRuntime(models, root / "agent")
        services = create_agent_session_services(cwd=root, agent_dir=root / "agent", model_runtime=runtime)
        session = AgentSession(services, SessionManager.create(root, root / "agent"), model=vision, tools=["read"])
        events: list = []
        session.subscribe(lambda e: events.append(e) if e.type == "attachments" else None)
        await session.prompt("看看 @src/app.py 和 @shot.png")

        sent = provider.calls[0]["context"].messages[-1]
        kinds = [getattr(block, "type", "") for block in sent.content]
        check("会话: 用户消息 = 原话 + 附件文字 + 图片", kinds == ["text", "text", "image"]
              and sent.content[0].text == "看看 @src/app.py 和 @shot.png"
              and '<file path="src/app.py">' in sent.content[1].text, str(kinds))
        check("会话: 界面收到附件事件", len(events) == 1
              and [i["label"] for i in events[0].items] == ["src/app.py", "shot.png"], str(events))
        facts = extract_facts(session.messages, str(root))
        check("压缩: 用户原话不含文件内容", [m["text"] for m in facts["userMessages"]]
              == ["看看 @src/app.py 和 @shot.png"], str(facts["userMessages"]))
        summary = describe_session(session.session.get_session_file())
        check("会话列表: 标题只用原话", summary is not None and summary.title == "看看 @src/app.py 和 @shot.png",
              summary.title if summary else "")

        models2, provider2 = fake_models([[TextContent(text="好")]])
        runtime2 = ModelRuntime(models2, root / "agent")
        services2 = create_agent_session_services(cwd=root, agent_dir=root / "agent", model_runtime=runtime2)
        plain = AgentSession(services2, SessionManager.in_memory(root), model=FAKE_MODEL, tools=[])
        await plain.prompt("没有附件的普通问题")
        check("会话: 没有 @ 时消息照旧是纯文字", provider2.calls[0]["context"].messages[-1].content
              == "没有附件的普通问题")
        await plain.prompt("@shot.png 这是什么")
        last = provider2.calls[-1]["context"].messages[-1]
        check("会话: 看不了图的模型（默认只收文字）不发图片", not any(getattr(b, "type", "") == "image"
                                                           for b in last.content), str(last.content))

        plain.steer("再看 @src/util.py")
        plain.steer("不带附件的插话")
        queued = plain.agent.steering_queue.take()
        check("插话: @文件 同样附上内容", len(queued) == 2 and user_words(queued[0]) == "再看 @src/util.py"
              and "x = 1" in queued[0].content[1].text and user_words(queued[1]) == "不带附件的插话", str(queued))


def test_models_json_input() -> None:
    with Sandbox() as root:
        agent = root / "agent"
        agent.mkdir()
        (agent / "models.json").write_text(json.dumps({
            "providers": [{"id": "zhipu", "api": "openai-completions", "baseUrl": "https://example.com/v1"}],
            "models": [{"id": "glm-4v", "provider": "zhipu", "input": ["text", "image"]},
                       {"id": "glm-text", "provider": "zhipu"}]}))
        runtime = ModelRuntime.create(agent_dir=agent)
        vision = runtime.get_model("zhipu", "glm-4v")
        text = runtime.get_model("zhipu", "glm-text")
        check("models.json: input 写了 image 就能看图", vision is not None
              and "image" in vision.input_modalities, str(vision))
        check("models.json: 不写默认只收文字", text is not None and text.input_modalities == ("text",), str(text))


def test_session() -> None:
    asyncio.run(case_session())


async def main() -> int:
    print("展开");         test_expand()
    print("接进会话");     await case_session()
    print("models.json"); test_models_json_input()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} 处失败：")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("全部通过 —— @文件可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
