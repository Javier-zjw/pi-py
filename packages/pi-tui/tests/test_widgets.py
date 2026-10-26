"""pi-tui 输入组件测试：命令补全、选择器

    python tests/test_widgets.py
    pytest tests/test_widgets.py

只测纯逻辑（候选、选择器状态和渲染）；prompt_toolkit 没装时跳过补全器那一条。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pi_tui import (
    HAS_PROMPT_TOOLKIT,
    PickerItem,
    PickerState,
    SlashCommand,
    Theme,
    display_width,
    match_commands,
    strip_ansi,
)
from pi_tui.theme import ColorMode

FAILURES: list[str] = []


def under_pytest() -> bool:
    # 必须在调用时判断：import（收集）阶段 pytest 还没设置这个环境变量
    return "PYTEST_CURRENT_TEST" in os.environ


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILURES.append(f"{name}: {detail}")
    print(f"  FAIL {name} {detail}")
    if under_pytest():
        raise AssertionError(f"{name}: {detail}")


COMMANDS = [
    SlashCommand("model", "切换模型"),
    SlashCommand("compact", "压缩上下文"),
    SlashCommand("cost", "用量", aliases=("usage",)),
    SlashCommand("context", "上下文"),
    SlashCommand("status", "会话信息", aliases=("session",)),
]


def names(found: list[SlashCommand]) -> list[str]:
    return [c.name for c in found]


def test_match_commands() -> None:
    check("补全: 只输 / 列出全部", names(match_commands("/", COMMANDS)) == names(COMMANDS))
    check("补全: 前缀匹配", names(match_commands("/co", COMMANDS)) == ["compact", "cost", "context"])
    check("补全: 大小写不敏感", names(match_commands("/MO", COMMANDS)) == ["model"])
    check("补全: 别名排在名字前缀之后",
          names(match_commands("/s", COMMANDS)) == ["status", "cost"],
          str(names(match_commands("/s", COMMANDS))))
    check("补全: 名字中间包含也算", names(match_commands("/tex", COMMANDS)) == ["context"])
    check("补全: 别名能找到命令", names(match_commands("/usa", COMMANDS)) == ["cost"])
    check("补全: 敲了空格（在输参数）不再补", match_commands("/model ", COMMANDS) == [])
    check("补全: 不是 / 开头不补", match_commands("model", COMMANDS) == [])
    check("补全: 无匹配为空", match_commands("/usr/bin", COMMANDS) == [])
    extra = [SlashCommand("planner"), SlashCommand("plan")]
    check("补全: 名字完全相同的排最前", names(match_commands("/plan", extra)) == ["plan", "planner"])
    check("补全: 别名完全相同的也排最前",
          names(match_commands("/session", [SlashCommand("sessions"), *COMMANDS]))[0] == "status")


def test_completer() -> None:
    if not HAS_PROMPT_TOOLKIT:
        print("  skip 没装 prompt_toolkit")
        return
    from pi_tui import SlashCompleter
    from prompt_toolkit.document import Document

    dynamic = list(COMMANDS)
    completer = SlashCompleter(lambda: dynamic)
    found = list(completer.get_completions(Document("/co"), None))
    check("补全器: 替换整段输入", [c.text for c in found] == ["/compact", "/cost", "/context"]
          and all(c.start_position == -3 for c in found))
    check("补全器: 说明放在 meta", found[0].display_meta_text == "压缩上下文")
    dynamic.append(SlashCommand("plan", "计划模式"))
    check("补全器: 命令表现取（扩展后加的也能补）",
          [c.text for c in completer.get_completions(Document("/pl"), None)] == ["/plan"])


def items() -> list[PickerItem]:
    levels = ["off", "low", "high"]
    return [
        PickerItem("claude", "Anthropic", choices=levels, choice=1, current=True),
        PickerItem("deepseek", "兼容端点", note="不支持思考"),
        PickerItem("gpt", "OpenAI", choices=levels),
    ]


def test_picker_state() -> None:
    state = PickerState(items(), index=0, choice_label="思考")
    state.up()
    check("选择器: ↑ 在顶部绕到底部", state.index == 2)
    state.down()
    check("选择器: ↓ 在底部绕回顶部", state.index == 0)
    state.right()
    state.right()
    check("选择器: → 切到下一档，到头停住", state.item.choices[state.item.choice] == "high")
    state.left()
    state.left()
    state.left()
    check("选择器: ← 到头停住", state.item.choice == 0)
    state.down()
    state.right()
    result = state.result()
    check("选择器: 没有次要选项的行 ←→ 无效，结果 choice 为 None",
          result.item.label == "deepseek" and result.choice is None)
    state.up()
    result = state.result()
    check("选择器: 结果带上次要选项", result.index == 0 and result.choice == "off")
    check("选择器: 初始下标越界会夹住", PickerState(items(), index=9).index == 2)


def test_picker_render() -> None:
    plain = Theme(ColorMode.NONE)
    state = PickerState(items(), index=0, choice_label="思考")
    lines = [strip_ansi(line) for line in state.render("选择模型", 100, plain)]
    rows = [line for line in lines if line.startswith(("│ ❯", "│  "))]
    check("面板: 和上面的内容空一行", lines[0] == "", str(lines))
    check("面板: 上边框写标题", lines[1].startswith("╭─ 选择模型 ─"), lines[1])
    check("面板: 下边框是按键说明（数字直接选、←→ 次要选项）", lines[-1].startswith("╰─ ↑↓ 选择")
          and "1-3 直接选" in lines[-1] and "←→ 思考" in lines[-1], lines[-1])
    check("面板: 每项一行、带编号", len(lines) == 6 and len(rows) == 3
          and "1. claude" in rows[0] and "3. gpt" in rows[2], str(lines))
    check("面板: 不超过屏宽", all(display_width(line) <= 100 for line in lines),
          str([display_width(line) for line in lines]))
    check("渲染: 选中行有 ❯ 和 ◀ ▶", rows[0].startswith("│ ❯") and "◀ low ▶" in rows[0], rows[0])
    check("渲染: 当前项标 ✓", "✓ 当前" in rows[0])
    check("渲染: 不支持的行显示说明", "不支持思考" in rows[1], rows[1])
    check("渲染: 未选中行只显示档位", "思考 off" in rows[2] and "◀" not in rows[2], rows[2])
    narrow = [strip_ansi(line) for line in state.render("选择模型", 40, plain)]
    check("渲染: 窄屏时省掉说明", "Anthropic" not in narrow[2], narrow[2])
    no_choices = PickerState([PickerItem("a"), PickerItem("b")])
    footer = strip_ansi(no_choices.render("会话", 80, plain)[-1])
    check("渲染: 没有次要选项时不提示 ←→", "←→" not in footer, footer)

    question = PickerState([PickerItem("统一改（推荐）", "list 也显示 10.00"), PickerItem("其他")])
    lines = [strip_ansi(line) for line in question.render(
        "list 命令的金额也统一显示两位小数吗？这一句故意写得很长，窄屏时要折行显示", 40, plain,
        header="需要你选择 · 1/2")]
    check("提问面板: 上边框写明需要选择和进度", lines[1].startswith("╭─ 需要你选择 · 1/2 ─"), lines[1])
    body = [line for line in lines[2:] if line.startswith("│ ") and "1." not in line and "2." not in line]
    check("提问面板: 问题在面板里，长问题折行", len(body) >= 2 and body[0].startswith("│ list 命令")
          and all(display_width(line) <= 40 for line in lines), str(lines))

    lines = [strip_ansi(line) for line in question.render("允许执行这条命令吗？", 40, plain,
                                                         header="需要你确认 · 执行命令",
                                                         detail=["rm -rf build " + "x" * 60])]
    check("细节: 问题下面缩进显示，超宽截短", lines[3].startswith("│   rm -rf build") and lines[4] == "│"
          and all(display_width(line) <= 40 for line in lines), str(lines))
    check("数字键: 选中对应项", question.select(2) and question.index == 1)
    check("数字键: 超出范围不动", not question.select(3) and not question.select(0) and question.index == 1)


def test_live_region_host() -> None:
    """有宿主（常驻输入框）时活动区交给宿主画、定稿内容交给宿主打印，自己不往终端写光标控制码"""
    import io

    from pi_tui.live import LiveRegion

    class Host:
        def __init__(self) -> None:
            self.live: list[str] = []
            self.printed: list[str] = []

        def set_live(self, lines: list[str]) -> None:
            self.live = list(lines)

        def print_above(self, text: str) -> None:
            self.printed.append(text)

    stream = io.StringIO()
    region = LiveRegion(stream=stream)
    region.host = host = Host()
    region.update(["⠋ 思考中"])
    check("宿主: 活动区交给宿主", host.live == ["⠋ 思考中"] and region.lines == ["⠋ 思考中"])
    region.append(["● 第一行"], live=["预览"])
    check("宿主: 定稿内容打印、活动区换成预览", host.printed == ["● 第一行"] and host.live == ["预览"])
    region.finalize(["收尾"])
    check("宿主: 收起活动区再打印", host.live == [] and host.printed[-1] == "收尾")
    check("宿主: 自己不写终端", stream.getvalue() == "", repr(stream.getvalue()))


def test_viewer_state() -> None:
    """全屏查看器的状态：选段落、展开收起、翻页、搜索（命中正文时自动展开）、渲染正好占满屏"""
    from pi_tui import Section, ViewerState

    plain = Theme(ColorMode.NONE)
    sections = [Section(f"第 {i} 段", [f"正文 {i}-{j}" for j in range(5)]) for i in range(8)]
    sections.append(Section("没有正文的段"))
    state = ViewerState(sections, "上一轮的过程")
    lines = [strip_ansi(line) for line in state.render(60, 10, plain)]
    check("查看器: 正好占满屏（标题 + 内容 + 状态栏）", len(lines) == 10 and lines[0].startswith("上一轮的过程  9 段")
          and lines[-1].startswith("第 1/9 段"), str(lines))
    check("查看器: 选中行有 ❯，收起的有 ▸", lines[1] == "❯ ▸ 第 0 段" and lines[2] == "  ▸ 第 1 段", str(lines))
    state.down()
    state.toggle()
    lines = [strip_ansi(line) for line in state.render(60, 10, plain)]
    check("查看器: 展开后正文缩进显示", "❯ ▾ 第 1 段" in lines and "    │ 正文 1-0" in lines, str(lines))
    state.cursor = 8
    state.toggle()
    check("查看器: 没有正文的段不能展开", not sections[8].expanded)
    lines = [strip_ansi(line) for line in state.render(60, 10, plain)]
    check("查看器: 光标走到下面时跟着滚动", "❯   没有正文的段" in lines and "❯ ▸ 第 0 段" not in lines, str(lines))
    state.home()
    state.page(1, 10)
    check("查看器: 翻页后光标跟到屏幕里", state.cursor > 0 and state.offset > 0, f"{state.cursor} {state.offset}")
    state.expand_all(False)
    state.search("正文 6-3")
    check("查看器: 搜索命中正文时跳过去并展开", state.cursor == 6 and sections[6].expanded and state.matches == [6])
    state.search("第")
    first = state.cursor
    state.next_match()
    check("查看器: n 下一个、绕回", state.cursor == first + 1 and len(state.matches) == 8)
    state.next_match(-1)
    state.next_match(-1)
    check("查看器: N 上一个、绕回", state.cursor == 7, str(state.cursor))
    state.search("没有这个词")
    footer = strip_ansi(state.render(60, 10, plain)[-1])
    check("查看器: 搜不到时状态栏说明", "没找到" in footer, footer)
    state.expand_all(True)
    check("查看器: e 全部展开（没正文的不算）", all(s.expanded for s in sections[:8]) and not sections[8].expanded)


def test_quiet_cpr() -> None:
    """终端回答光标位置慢了一下时，prompt_toolkit 会误报 "your terminal doesn't support ... (CPR)"：
    去掉这句提示，但不改 2 秒的判定时间（改长了真不支持的终端每次回车都要多等）"""
    from types import SimpleNamespace

    from pi_tui.terminal import quiet_cpr

    renderer = SimpleNamespace(cpr_not_supported_callback=print, CPR_TIMEOUT=2)
    quiet_cpr(SimpleNamespace(renderer=renderer))
    check("CPR: 不再打印不支持的警告", renderer.cpr_not_supported_callback is None)
    check("CPR: 判定时间不动", renderer.CPR_TIMEOUT == 2)
    quiet_cpr(SimpleNamespace())
    check("CPR: 没有 renderer 的对象不报错", True)
    if HAS_PROMPT_TOOLKIT:
        from pi_tui.input import LineReader
        reader = LineReader(commands=[])
        check("CPR: 输入框装上了", reader.session.app.renderer.cpr_not_supported_callback is None)


def test_background_and_notify() -> None:
    """浅色背景换一套压暗的颜色；背景深浅的探测顺序；提醒只写真正的终端"""
    import io
    import sys

    from pi_tui import notify
    from pi_tui.theme import (
        ACCENT,
        HIGHLIGHT,
        background_from_colorfgbg,
        background_from_rgb,
        detect_background,
        parse_osc11,
    )

    dark, light = Theme(ColorMode.TRUECOLOR), Theme(ColorMode.TRUECOLOR, background="light")
    check("浅色: 同一个颜色换成压暗的版本", dark.paint("x", ACCENT) != light.paint("x", ACCENT)
          and "38;2;30;90;190" in light.paint("x", ACCENT), repr(light.paint("x", ACCENT)))
    check("浅色: 亮金在白底上也换掉", "236;196;106" not in light.paint("x", HIGHLIGHT))
    check("浅色: 没颜色的样式不受影响", light.paint("x", None, bold=True) == dark.paint("x", None, bold=True))
    check("COLORFGBG: 背景 0 是深色、15 / 7 是浅色", background_from_colorfgbg("15;0") == "dark"
          and background_from_colorfgbg("0;15") == "light" and background_from_colorfgbg("0;7") == "light"
          and background_from_colorfgbg("0;default;15") == "light" and background_from_colorfgbg("") is None)
    check("OSC 11: 解析终端回答（BEL / ST 结尾、2 位或 4 位十六进制）",
          parse_osc11("\x1b]11;rgb:ffff/ffff/ffff\x1b\\") == (1.0, 1.0, 1.0)
          and parse_osc11("\x1b]11;rgb:1e/1f/22\x07") is not None and parse_osc11("乱码") is None)
    check("亮度: 白底浅色、深灰底深色", background_from_rgb((1, 1, 1)) == "light"
          and background_from_rgb((0.12, 0.12, 0.13)) == "dark")
    saved = {k: os.environ.pop(k, None) for k in ("PI_THEME", "COLORFGBG")}
    try:
        check("探测: 设置优先", detect_background("light") == "light" and detect_background("dark") == "dark")
        os.environ["PI_THEME"] = "light"
        check("探测: 其次 PI_THEME", detect_background("auto") == "light")
        del os.environ["PI_THEME"]
        os.environ["COLORFGBG"] = "0;15"
        check("探测: 再看 COLORFGBG", detect_background(None) == "light")
        del os.environ["COLORFGBG"]
        check("探测: 不是终端、问不到时默认深色", detect_background(None) == "dark")
    finally:
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value

    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    real = sys.__stdout__
    try:
        sys.__stdout__ = fake = Tty()
        notify("做完了")
        check("提醒: 默认只响铃", fake.getvalue() == "\a", repr(fake.getvalue()))
        sys.__stdout__ = fake = Tty()
        notify("做完了", desktop=True)
        check("提醒: desktop 时再发 OSC 9 通知", fake.getvalue() == "\x1b]9;做完了\x07\a", repr(fake.getvalue()))
        sys.__stdout__ = fake = io.StringIO()
        notify("做完了", desktop=True)
        check("提醒: 不是终端（重定向）时什么都不写", fake.getvalue() == "")
    finally:
        sys.__stdout__ = real


def make_tree(root: Path) -> None:
    (root / "src" / "pi_app").mkdir(parents=True)
    (root / "src" / "pi_app" / "renderer.py").write_text("")
    (root / "src" / "app.py").write_text("")
    (root / "README.md").write_text("")
    (root / "my notes.txt").write_text("")
    (root / ".env").write_text("")
    (root / "node_modules" / "left-pad").mkdir(parents=True)
    (root / "node_modules" / "left-pad" / "index.js").write_text("")


def test_mention_paths() -> None:
    import tempfile

    from pi_tui.paths import ProjectIndex, mention_token, path_candidates

    check("@词: 行首和空白后才算", mention_token("看 @src/a") == ("src/a", 5)
          and mention_token("@") == ("", 0) and mention_token("a@b") is None
          and mention_token("@src/app.py 后面") is None)
    check("@词: 引号和转义空格", mention_token('@"my no') == ("my no", 6)
          and mention_token("@my\\ no") == ("my no", 6))
    check("@词: 中文标点结束路径", mention_token("@app.py，") is None)
    check("@词: 紧挨中文也算（中文句子里 @ 前面不打空格）", mention_token("看看@") == ("", 0)
          and mention_token("帮我看下@src") == ("src", 3) and mention_token("（@a") == ("a", 1))
    check("@词: 全角 ＠ 也算", mention_token("＠") == ("", 0) and mention_token("看看＠src") == ("src", 3))
    check("@词: 紧挨英文字母数字的不算（邮箱）", mention_token("me@ex") is None and mention_token("x1@") is None)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_tree(root)
        index = ProjectIndex()
        found = [c.display for c in path_candidates(tmp, "", index)]
        check("候选: 空 @ 列顶层，目录在前，跳过隐藏和 node_modules",
              found == ["src/", "my notes.txt", "README.md"], str(found))
        found = [c.display for c in path_candidates(tmp, "ren", index)]
        check("候选: 一个词在整个项目里模糊找", found == ["src/pi_app/renderer.py"], str(found))
        found = [c.display for c in path_candidates(tmp, "app", index)]
        check("候选: 文件名开头的排前面", found[:2] == ["src/app.py", "src/pi_app/"], str(found))
        found = [(c.insert, c.is_dir) for c in path_candidates(tmp, "src/", index)]
        check("候选: 带 / 按目录逐级补", found == [("src/pi_app/", True), ("src/app.py", False)], str(found))
        found = [c.insert for c in path_candidates(tmp, "my", index)]
        check("候选: 带空格的路径加引号", found == ['"my notes.txt"'], str(found))
        found = [c.display for c in path_candidates(tmp, "./.e", index)]
        check("候选: 明确输 . 才列隐藏文件", found == ["./.env"], str(found))

    from unittest import mock

    from pi_tui.paths import empty_reason
    with tempfile.TemporaryDirectory() as tmp:
        check("没有候选: 空目录说明一句", "还没有文件" in empty_reason(tmp, ""))
        make_tree(Path(tmp))
        check("没有候选: 没匹配 / 目录里没有 / 没这个目录", empty_reason(tmp, "zzz") == "没有匹配「zzz」的文件"
              and empty_reason(tmp, "src/zz") == "src/ 里没有以「zz」开头的文件"
              and "这个目录" in empty_reason(tmp, "nope/x"))
        index = ProjectIndex()
        with mock.patch("os.scandir", side_effect=PermissionError(1, "Operation not permitted")), \
                mock.patch("sys.platform", "darwin"):
            reason = (path_candidates(tmp, "", index), empty_reason(tmp, "", index))
        check("没有候选: 读不了项目目录说清楚（macOS 指到隐私设置）", reason[0] == []
              and "没有权限" in reason[1] and "隐私与安全性" in reason[1], str(reason))


def test_mention_completer() -> None:
    if not HAS_PROMPT_TOOLKIT:
        print("  skip 没装 prompt_toolkit")
        return
    import tempfile

    from pi_tui import SlashCompleter
    from pi_tui.input import MentionCompleter, _Completers
    from prompt_toolkit.document import Document

    with tempfile.TemporaryDirectory() as tmp:
        make_tree(Path(tmp))
        both = _Completers(SlashCompleter(COMMANDS), MentionCompleter(lambda: tmp))
        found = list(both.get_completions(Document("看看 @src/a"), None))
        check("补全器: @ 里补路径、替换已输入的部分", [(c.text, c.start_position) for c in found]
              == [("src/app.py", -5)], str([(c.text, c.start_position) for c in found]))
        found = list(both.get_completions(Document("看看@ap"), None))
        check("补全器: 紧挨中文的 @ 也补", [c.text for c in found][:1] == ["src/app.py"]
              and found[0].start_position == -2, str(found))

        async def collect(text):
            return [c.text async for c in both.get_completions_async(Document(text), None)]
        import asyncio
        check("补全器: 异步取（路径在线程里找）", asyncio.run(collect("看看@src/a")) == ["src/app.py"]
              and asyncio.run(collect("/cos")) == ["/cost"])
        hint = list(both.get_completions(Document("看看@zzz"), None))
        check("补全器: 没有候选时一行说明、选它不改输入", len(hint) == 1 and hint[0].text == "zzz "
              and hint[0].start_position == -3 and "没有匹配" in hint[0].display_text, str(hint))
        found = list(both.get_completions(Document("/co"), None))
        check("补全器: 不在 @ 里照旧补命令", [c.text for c in found] == ["/compact", "/cost", "/context"])
        check("补全器: 普通文字没有候选", not list(both.get_completions(Document("你好"), None)))


def test_clipboard() -> None:
    import subprocess
    import tempfile

    from pi_tui import clipboard

    calls: list[list[str]] = []

    def fake_run(args, png=b"\x89PNG fake", types="image/png\ntext/plain"):
        def run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[0] == "osascript":
                target = cmd[2].split('POSIX file "')[1].split('"')[0]
                if png:
                    Path(target).write_bytes(png)
                return subprocess.CompletedProcess(cmd, 0, b"", b"")
            if "--list-types" in cmd or "TARGETS" in cmd:
                return subprocess.CompletedProcess(cmd, 0, types, "")
            return subprocess.CompletedProcess(cmd, 0, png, b"")
        return run

    real_which = clipboard.shutil.which
    with tempfile.TemporaryDirectory() as tmp:
        saved = clipboard.save_clipboard_image(tmp, run=fake_run(None), platform="darwin")
        check("剪贴板 mac: 有图存成 PNG", saved is not None and saved.read_bytes() == b"\x89PNG fake"
              and saved.suffix == ".png", str(saved))
        saved = clipboard.save_clipboard_image(tmp, run=fake_run(None, png=b""), platform="darwin")
        check("剪贴板 mac: 不是图片返回 None、不留空文件", saved is None
              and len(list(Path(tmp).iterdir())) == 1)
        try:
            clipboard.shutil.which = lambda name: "/usr/bin/xclip" if name == "xclip" else None
            calls.clear()
            saved = clipboard.save_clipboard_image(tmp, run=fake_run(None), platform="linux")
            check("剪贴板 linux: xclip 取 image/png", saved is not None and calls[-1][-3:]
                  == ["-t", "image/png", "-o"], str(calls))
            saved = clipboard.save_clipboard_image(tmp, run=fake_run(None, types="text/plain"),
                                                   platform="linux")
            check("剪贴板 linux: 剪贴板里是文字返回 None", saved is None)
            clipboard.shutil.which = lambda name: None
            check("剪贴板 linux: 没装 xclip / wl-paste 返回 None",
                  clipboard.save_clipboard_image(tmp, run=fake_run(None), platform="linux") is None)
        finally:
            clipboard.shutil.which = real_which

        def broken(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 5)
        check("剪贴板: 命令超时不抛错", clipboard.save_clipboard_image(tmp, run=broken, platform="darwin") is None)


def main() -> int:
    print("命令补全")
    test_match_commands()
    test_completer()
    print("@文件")
    test_mention_paths()
    test_mention_completer()
    test_clipboard()
    print("选择器")
    test_picker_state()
    test_picker_render()
    test_live_region_host()
    test_viewer_state()
    test_background_and_notify()
    test_quiet_cpr()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} 处失败：")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
