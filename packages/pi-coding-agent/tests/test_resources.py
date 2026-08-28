"""
簇 C · 配置与资源测试：prompt / skills / extensions / settings / resources

    python tests/test_resources.py
    python tests/test_resources.py -v
    pytest tests/test_resources.py        # PyCharm 默认走这个

全部同步用例。所有 .pi 目录、AGENTS.md、扩展文件都造在临时目录里。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))                  # packages/pi-coding-agent
sys.path.insert(0, str(HERE.parents[2] / "pi-agent"))
sys.path.insert(0, str(HERE.parents[2] / "pi-ai"))

from pi_coding_agent.extensions import (  # noqa: E402
    ExtensionAPI,
    create_event_bus,
    load_extension_file,
    load_extensions,
)
from pi_coding_agent.prompt import (  # noqa: E402
    BASE_SYSTEM_PROMPT,
    build_system_prompt,
    environment_block,
    find_context_files,
)
from pi_coding_agent.resources import DefaultResourceLoader  # noqa: E402
from pi_coding_agent.settings import DEFAULT_SETTINGS, SettingsManager  # noqa: E402
from pi_coding_agent.skills import (  # noqa: E402
    discover_prompts,
    discover_skills,
    load_skill,
    parse_front_matter,
    skills_block,
)
from pi_coding_agent.text import read_text_lenient  # noqa: E402

VERBOSE = "-v" in sys.argv
UNDER_PYTEST = "PYTEST_CURRENT_TEST" in os.environ
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILURES.append(f"{name}: {detail}")
    print(f"  FAIL {name} {detail}")
    if UNDER_PYTEST:
        raise AssertionError(f"{name}: {detail}")


class Sandbox:
    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix="pi-res-"))
        return self.path

    def __exit__(self, *exc) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")
    return path


SKILL_MD = """---
name: commit-message
description: 按本仓库约定写提交信息
---

# 提交信息

1. 先看 `git diff --cached`
2. 主题行祈使句，60 字符以内
"""


# --------------------------------------------------------------------------- #
# 1. prompt.py
# --------------------------------------------------------------------------- #


def test_environment_block() -> None:
    with Sandbox() as cwd:
        block = environment_block(cwd)
        check("环境: 包裹在 tag 里",
              block.startswith("<environment>") and block.endswith("</environment>"),
              block[:30])
        check("环境: 含 cwd", str(cwd.resolve()) in block, block[:120])
        for field in ("platform", "date"):
            check(f"环境: 含 {field}", field in block, block[:120])


def test_context_files() -> None:
    with Sandbox() as root:
        # 模拟 git 仓库：根目录一个 AGENTS.md，子目录再一个
        (root / ".git").mkdir()
        write(root / "AGENTS.md", "# 仓库约定\n小改动优先")
        sub = root / "src" / "core"
        write(sub / "AGENTS.md", "# 本目录约定\n这里全是异步代码")
        global_dir = root / "agentdir"
        write(global_dir / "AGENTS.md", "# 全局约定\n永远写中文注释")

        files = find_context_files(sub, global_dir)
        contents = [f.content for f in files]
        check("上下文文件: 找到三份", len(files) == 3, str(len(files)))
        check("上下文文件: 全局在最前", "全局约定" in contents[0], contents[0][:20])
        check("上下文文件: 外层在内层之前",
              contents.index(next(c for c in contents if "仓库约定" in c))
              < contents.index(next(c for c in contents if "本目录约定" in c)),
              str([c[:8] for c in contents]))
        check("上下文文件: 到 .git 就停",
              all("上一级" not in c for c in contents))

    with Sandbox() as root:
        (root / ".git").mkdir()
        files = find_context_files(root, root / "nowhere")
        check("上下文文件: 没有时返回空", files == [], str(files))

    with Sandbox() as root:
        (root / ".git").mkdir()
        (root / "AGENTS.md").write_bytes("中文约定".encode("gbk"))
        files = find_context_files(root, None)
        check("上下文文件: 非 UTF-8 不让启动失败", len(files) == 1, str(files))
        check("上下文文件: 降级解码出内容", bool(files[0].content.strip()), repr(files[0].content))


def test_read_text_lenient() -> None:
    with Sandbox() as cwd:
        utf8 = write(cwd / "a.md", "中文内容")
        check("宽容读: UTF-8", read_text_lenient(utf8) == "中文内容")
        gbk = cwd / "b.md"
        gbk.write_bytes("中文内容".encode("gbk"))
        check("宽容读: GBK 降级", read_text_lenient(gbk) == "中文内容", repr(read_text_lenient(gbk)))
        check("宽容读: 文件不存在返回 None", read_text_lenient(cwd / "没有.md") is None)


def test_build_system_prompt() -> None:
    with Sandbox() as cwd:
        (cwd / ".git").mkdir()
        write(cwd / "AGENTS.md", "# 项目约定\n必须写测试")
        files = find_context_files(cwd, None)
        prompt = build_system_prompt(cwd, context_files=files, skills_block="<skills>x</skills>")

        check("系统提示: 含基础提示", BASE_SYSTEM_PROMPT[:30] in prompt)
        check("系统提示: 含环境块", "<environment>" in prompt)
        check("系统提示: 含 AGENTS.md 内容", "必须写测试" in prompt)
        check("系统提示: 含技能块", "<skills>" in prompt)
        check("系统提示: 上下文文件带路径属性", 'path="' in prompt, prompt[-200:])

        bare = build_system_prompt(cwd)
        check("系统提示: 无附加内容时也能用",
              "<environment>" in bare and "<skills>" not in bare)

        custom = build_system_prompt(cwd, base="你是一个只会说是的助手。")
        check("系统提示: 可替换基础提示",
              "只会说是" in custom and BASE_SYSTEM_PROMPT[:30] not in custom)


# --------------------------------------------------------------------------- #
# 2. skills.py
# --------------------------------------------------------------------------- #


def test_front_matter() -> None:
    meta, body = parse_front_matter(SKILL_MD)
    check("前置元数据: 解析 name", meta.get("name") == "commit-message", str(meta))
    check("前置元数据: 解析 description", "提交信息" in meta.get("description", ""), str(meta))
    check("前置元数据: 正文不含分隔符", not body.startswith("---") and "# 提交信息" in body,
          body[:40])

    meta, body = parse_front_matter("# 没有元数据\n正文")
    check("前置元数据: 没有时返回空 dict", meta == {} and body.startswith("# 没有元数据"))

    meta, _ = parse_front_matter("---\nname: 'quoted'\ndesc: \"双引号\"\n---\n正文")
    check("前置元数据: 去掉引号",
          meta.get("name") == "quoted" and meta.get("desc") == "双引号", str(meta))

    meta, body = parse_front_matter("---\nname: 没闭合\n正文")
    check("前置元数据: 未闭合时不崩", isinstance(meta, dict))


def test_skill_discovery() -> None:
    with Sandbox() as cwd:
        write(cwd / ".pi" / "skills" / "commit-message" / "SKILL.md", SKILL_MD)
        write(cwd / ".pi" / "skills" / "no-meta" / "SKILL.md", "这是第一行描述\n后面是正文")
        write(cwd / "global" / "skills" / "commit-message" / "SKILL.md",
              "---\nname: commit-message\ndescription: 全局版本\n---\n内容")
        write(cwd / "global" / "skills" / "only-global" / "SKILL.md",
              "---\nname: only-global\ndescription: 只有全局有\n---\n内容")

        skills = discover_skills([
            (cwd / ".pi" / "skills", "project"),
            (cwd / "global" / "skills", "global"),
        ])
        names = [s.name for s in skills]
        check("技能发现: 找到项目技能", "commit-message" in names, str(names))
        check("技能发现: 找到全局技能", "only-global" in names, str(names))
        check("技能发现: 同名去重", names.count("commit-message") == 1, str(names))

        cm = next(s for s in skills if s.name == "commit-message")
        check("技能发现: 项目版本优先",
              "本仓库约定" in cm.description and cm.source == "project", cm.description)
        check("技能发现: 记录文件路径", cm.file_path.endswith("SKILL.md"), cm.file_path)
        check("技能发现: 记录基目录", cm.base_dir.endswith("commit-message"), cm.base_dir)

        no_meta = next(s for s in skills if s.name == "no-meta")
        check("技能发现: 无 name 时用目录名", no_meta.name == "no-meta")
        check("技能发现: 无 description 时取首行",
              "第一行描述" in no_meta.description, no_meta.description)

        check("技能发现: 目录不存在不崩",
              discover_skills([(cwd / "根本没有", "project")]) == [])

        block = skills_block(skills)
        check("技能块: 含标签", block.startswith("<skills>") and block.endswith("</skills>"))
        check("技能块: 含名字与描述", "commit-message" in block and "本仓库约定" in block)
        check("技能块: 含读取路径（按需加载的关键）", "SKILL.md" in block, block[:200])
        check("技能块: 不含正文（省上下文）", "git diff --cached" not in block, block[:300])
        check("技能块: 空列表返回空串", skills_block([]) == "")


def test_prompt_templates() -> None:
    with Sandbox() as cwd:
        write(cwd / ".pi" / "prompts" / "tidy.md",
              "---\nname: tidy\ndescription: 清理文件\n---\n请整理我指定的文件。")
        write(cwd / ".pi" / "prompts" / "review.md", "直接是正文，没有元数据")
        write(cwd / "global" / "prompts" / "tidy.md",
              "---\nname: tidy\ndescription: 全局版\n---\n全局正文")

        prompts = discover_prompts([
            (cwd / ".pi" / "prompts", "project"),
            (cwd / "global" / "prompts", "global"),
        ])
        names = [p.name for p in prompts]
        check("提示模板: 找到两个", set(names) == {"tidy", "review"}, str(names))
        tidy = next(p for p in prompts if p.name == "tidy")
        check("提示模板: 项目版优先", tidy.description == "清理文件", tidy.description)
        check("提示模板: 正文不含元数据", not tidy.content.startswith("---"), tidy.content[:20])
        review = next(p for p in prompts if p.name == "review")
        check("提示模板: 无元数据时用文件名", review.name == "review")


# --------------------------------------------------------------------------- #
# 3. extensions.py
# --------------------------------------------------------------------------- #


GOOD_EXT = '''
NAME = "demo-ext"

def activate(pi):
    async def hello(args, ctx):
        from pi_agent import AgentToolResult
        return AgentToolResult.text("hi " + args.get("who", "world"))

    pi.register_tool(
        name="hello",
        description="打个招呼",
        parameters={"type": "object", "properties": {"who": {"type": "string"}}},
        execute=hello,
    )

    async def handler(rest):
        return "命令跑了 " + rest

    pi.register_command("greet", "打招呼命令", handler)
    pi.on("agent_end", lambda e: pi.state.setdefault("ends", []).append(e))
'''

NO_ACTIVATE_EXT = "NAME = 'broken'\n# 忘了写 activate\n"
SYNTAX_ERROR_EXT = "def activate(pi)\n    pass\n"
RAISING_EXT = "def activate(pi):\n    raise RuntimeError('扩展初始化失败')\n"


def test_event_bus() -> None:
    bus = create_event_bus()
    seen: list = []
    off = bus.on("ping", seen.append)
    bus.emit("ping", 1)
    bus.emit("other", 2)
    check("事件总线: 只收订阅的事件", seen == [1], str(seen))
    off()
    bus.emit("ping", 3)
    check("事件总线: 退订生效", seen == [1], str(seen))

    def broken(_):
        raise RuntimeError("监听器炸了")

    bus.on("ping", broken)
    bus.on("ping", seen.append)
    bus.emit("ping", 4)
    check("事件总线: 一个监听器炸了不影响别的", 4 in seen, str(seen))


def test_extension_api() -> None:
    api = ExtensionAPI(cwd="/tmp", events=create_event_bus())

    async def noop(args, ctx):
        return None

    api.register_tool("t1", "描述", {"type": "object", "properties": {}}, noop)
    check("扩展 API: 注册工具", [t.name for t in api.tools] == ["t1"])
    api.register_tool("t1", "新描述", {"type": "object", "properties": {}}, noop)
    check("扩展 API: 同名工具替换而不是重复",
          len(api.tools) == 1 and api.tools[0].description == "新描述", str(api.tools))

    async def handler(rest):
        return rest

    api.register_command("cmd", "描述", handler)
    check("扩展 API: 注册命令", "cmd" in api.commands)

    got: list = []
    api.on("agent_end", got.append)
    api.on("turn_end", got.append)

    class E:
        type = "agent_end"

    api.dispatch(E())
    check("扩展 API: 按事件类型分派", len(got) == 1, str(len(got)))

    api.on("agent_end", lambda e: (_ for _ in ()).throw(RuntimeError("炸")))
    api.dispatch(E())
    check("扩展 API: 处理器异常被隔离", len(got) == 2, str(len(got)))


def test_extension_loading() -> None:
    with Sandbox() as cwd:
        ext_dir = cwd / ".pi" / "extensions"
        write(ext_dir / "demo.py", GOOD_EXT)
        write(ext_dir / "broken_no_activate.py", NO_ACTIVATE_EXT)
        write(ext_dir / "broken_syntax.py", SYNTAX_ERROR_EXT)
        write(ext_dir / "broken_raise.py", RAISING_EXT)
        write(ext_dir / "_private.py", "raise RuntimeError('不该被加载')")

        api = ExtensionAPI(cwd=str(cwd), events=create_event_bus())
        loaded = load_extensions([ext_dir], api)
        by_name = {e.name: e for e in loaded}

        check("扩展加载: 加载成功的用 NAME", "demo-ext" in by_name, str(list(by_name)))
        check("扩展加载: 成功的没有 error", by_name["demo-ext"].error is None)
        check("扩展加载: 注册的工具可见", [t.name for t in api.tools] == ["hello"],
              str([t.name for t in api.tools]))
        check("扩展加载: 注册的命令可见", "greet" in api.commands)

        check("扩展加载: 跳过下划线开头",
              not any("private" in n for n in by_name), str(list(by_name)))
        for stem, why in [("broken_no_activate", "没有 activate"),
                          ("broken_syntax", "语法错误"),
                          ("broken_raise", "初始化抛异常")]:
            entry = by_name.get(stem)
            check(f"扩展加载: {why} 被记录而不是崩",
                  entry is not None and entry.error, str(entry))

        check("扩展加载: 目录不存在不崩", load_extensions([cwd / "没有"], api) == [])

        single = load_extension_file(write(cwd / "solo.py", GOOD_EXT), api)
        check("扩展加载: 单文件加载", single.error is None and single.name == "demo-ext",
              str(single))


# --------------------------------------------------------------------------- #
# 4. settings.py
# --------------------------------------------------------------------------- #


def test_settings_defaults_and_get() -> None:
    s = SettingsManager.in_memory()
    check("设置: 有默认值", s.get("thinkingLevel") == DEFAULT_SETTINGS["thinkingLevel"],
          str(s.get("thinkingLevel")))
    check("设置: 点号取嵌套值", s.get("compaction.threshold") == 0.85,
          str(s.get("compaction.threshold")))
    check("设置: 缺失返回默认", s.get("不存在的键", "兜底") == "兜底")
    check("设置: 缺失的嵌套也返回默认", s.get("a.b.c", 42) == 42)

    s.set("compaction.threshold", 0.5)
    check("设置: 点号写入嵌套", s.get("compaction.threshold") == 0.5)
    s.set("新建.深层.键", "值")
    check("设置: 自动建中间层", s.get("新建.深层.键") == "值")

    s.apply_overrides({"model": "x/y", "compaction": {"enabled": False}})
    check("设置: 覆盖生效", s.get("model") == "x/y")
    check("设置: 覆盖是深合并，不清空同级",
          s.get("compaction.threshold") == 0.5 and s.get("compaction.enabled") is False,
          str(s.get("compaction")))


def test_settings_merge_and_files() -> None:
    with Sandbox() as cwd:
        agent_dir = cwd / "agent"
        write(agent_dir / "settings.json", json.dumps({
            "model": "global/model",
            "thinkingLevel": "low",
            "compaction": {"threshold": 0.7, "keepLastTurns": 8},
        }))
        write(cwd / ".pi" / "settings.json", json.dumps({
            "model": "project/model",
            "compaction": {"threshold": 0.9},
        }))

        s = SettingsManager.create(cwd, agent_dir)
        check("设置: 项目覆盖全局", s.get("model") == "project/model", str(s.get("model")))
        check("设置: 全局未被覆盖的保留", s.get("thinkingLevel") == "low")
        check("设置: 嵌套逐键合并",
              s.get("compaction.threshold") == 0.9 and s.get("compaction.keepLastTurns") == 8,
              str(s.get("compaction")))
        check("设置: 默认值仍在", s.get("retry.enabled") is True, str(s.get("retry")))

    with Sandbox() as cwd:
        agent_dir = cwd / "agent"
        write(agent_dir / "settings.json", "{ 这不是合法 json ")
        s = SettingsManager.create(cwd, agent_dir)
        check("设置: 坏 JSON 被忽略而不是崩", s.get("thinkingLevel") == "off")

        (agent_dir / "settings.json").write_bytes("中文".encode("gbk"))
        s = SettingsManager.create(cwd, agent_dir)
        check("设置: 非 UTF-8 也被忽略（UnicodeDecodeError 不是 JSONDecodeError）",
              s.get("thinkingLevel") == "off")

    with Sandbox() as cwd:
        agent_dir = cwd / "agent"
        s = SettingsManager.create(cwd, agent_dir)
        s.set("model", "写盘测试")
        s.flush()
        check("设置: flush 写出文件", (agent_dir / "settings.json").exists())
        again = SettingsManager.create(cwd, agent_dir)
        check("设置: 写出的能读回", again.get("model") == "写盘测试")


# --------------------------------------------------------------------------- #
# 5. resources.py（把上面四个粘起来的门面）
# --------------------------------------------------------------------------- #


def test_resource_loader() -> None:
    with Sandbox() as cwd:
        (cwd / ".git").mkdir()
        write(cwd / "AGENTS.md", "# 项目约定\n全部用中文注释")
        write(cwd / ".pi" / "skills" / "commit-message" / "SKILL.md", SKILL_MD)
        write(cwd / ".pi" / "prompts" / "tidy.md",
              "---\nname: tidy\ndescription: 清理\n---\n请整理文件。")
        write(cwd / ".pi" / "extensions" / "demo.py", GOOD_EXT)
        agent_dir = cwd / "agentdir"
        write(agent_dir / "skills" / "global-skill" / "SKILL.md",
              "---\nname: global-skill\ndescription: 全局技能\n---\n内容")

        loader = DefaultResourceLoader(cwd=cwd, agent_dir=agent_dir)
        loader.reload()

        check("资源: 发现技能",
              {s.name for s in loader.get_skills()} == {"commit-message", "global-skill"},
              str([s.name for s in loader.get_skills()]))
        check("资源: 发现提示模板", [p.name for p in loader.get_prompts()] == ["tidy"])
        check("资源: 发现上下文文件",
              any("项目约定" in f.content for f in loader.get_context_files()))
        check("资源: 加载扩展",
              [e.name for e in loader.get_extensions()] == ["demo-ext"],
              str([(e.name, e.error) for e in loader.get_extensions()]))
        check("资源: 扩展注册的工具可取",
              [t.name for t in loader.get_extension_api().tools] == ["hello"])

        prompt = loader.get_system_prompt()
        check("资源: 系统提示含技能块", "<skills>" in prompt and "commit-message" in prompt)
        check("资源: 系统提示含 AGENTS.md", "全部用中文注释" in prompt)
        check("资源: 系统提示含环境", "<environment>" in prompt)
        if VERBOSE:
            print("   系统提示长度:", len(prompt))

        # reload 必须重建扩展状态：删掉扩展文件后，它注册的东西也该消失
        loader.reload()
        check("资源: reload 后工具不重复",
              len(loader.get_extension_api().tools) == 1,
              str([t.name for t in loader.get_extension_api().tools]))
        (cwd / ".pi" / "extensions" / "demo.py").unlink()
        loader.reload()
        check("资源: reload 丢弃已删除扩展的工具",
              loader.get_extension_api().tools == [],
              str([t.name for t in loader.get_extension_api().tools]))
        check("资源: reload 丢弃已删除扩展的命令",
              "greet" not in loader.get_extension_api().commands,
              str(list(loader.get_extension_api().commands)))

    with Sandbox() as cwd:
        loader = DefaultResourceLoader(cwd=cwd, agent_dir=cwd / "空目录")
        loader.reload()
        check("资源: 什么都没有时也能工作",
              loader.get_skills() == [] and loader.get_prompts() == [])
        check("资源: 空项目的系统提示仍可用", "<environment>" in loader.get_system_prompt())

    with Sandbox() as cwd:
        write(cwd / ".pi" / "extensions" / "bad.py", RAISING_EXT)
        loader = DefaultResourceLoader(cwd=cwd, agent_dir=cwd / "agent")
        loader.reload()
        errors = [e for e in loader.get_extensions() if e.error]
        check("资源: 扩展报错被记录而不是崩", len(errors) == 1, str(loader.get_extensions()))

    with Sandbox() as cwd:
        extra = write(cwd / "外部扩展.py", GOOD_EXT)
        loader = DefaultResourceLoader(
            cwd=cwd, agent_dir=cwd / "agent", additional_extension_paths=[extra]
        )
        loader.reload()
        check("资源: 支持额外扩展路径",
              "hello" in [t.name for t in loader.get_extension_api().tools],
              str([t.name for t in loader.get_extension_api().tools]))

    with Sandbox() as cwd:
        def factory(api) -> None:
            api.register_command("inline", "内联注册", lambda rest: None)

        loader = DefaultResourceLoader(
            cwd=cwd, agent_dir=cwd / "agent", extension_factories=[factory]
        )
        loader.reload()
        check("资源: 支持代码内注册扩展",
              "inline" in loader.get_extension_api().commands,
              str(list(loader.get_extension_api().commands)))

    with Sandbox() as cwd:
        loader = DefaultResourceLoader(
            cwd=cwd, agent_dir=cwd / "agent",
            system_prompt_override=lambda: "自定义基础提示",
        )
        loader.reload()
        check("资源: 可替换基础系统提示",
              "自定义基础提示" in loader.get_system_prompt())


# --------------------------------------------------------------------------- #


def main() -> int:
    print("prompt.py")
    test_environment_block()
    test_context_files()
    test_read_text_lenient()
    test_build_system_prompt()
    print("skills.py")
    test_front_matter()
    test_skill_discovery()
    test_prompt_templates()
    print("extensions.py")
    test_event_bus()
    test_extension_api()
    test_extension_loading()
    print("settings.py")
    test_settings_defaults_and_get()
    test_settings_merge_and_files()
    print("resources.py")
    test_resource_loader()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} 处失败：")
        for f in FAILURES:
            print(f"  - {f}")
        print("\n对照 tests/SESSION_CHECKLIST.md 定位实现位置")
        return 1
    print("全部通过 —— 配置与资源簇可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())