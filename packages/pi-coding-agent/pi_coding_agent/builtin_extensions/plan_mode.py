"""
计划模式（plan → execute）：先只读调研、出一份带任务清单的计划，用户批准后切到执行模式，
按清单逐项完成，界面实时打勾。参照 Claude Code（shift+tab 切模式、提交计划后选择是否执行）
和 Codex（Plan Mode 协作模式：先探索再提问，计划要"决策完备"，批准后切回 Default 执行）。

为什么稳：

1. **工具层面强制只读** —— 不是靠提示词请求模型"别改文件"，而是计划模式下只挂 read / grep /
   find / ls（web 扩展在时加上 web_search / web_fetch，同样只读）、ask_user 和 submit_plan。模型想改也调不到。update_plan（执行进度单）在计划模式下也不给，
   避免和"计划"混淆（Codex 同样禁止）。
2. **提交即停** —— submit_plan 的结果带 terminate，本轮立刻结束、不再调模型，
   等用户决定：执行 / 继续完善 / 放弃。
3. **计划落进会话记录** —— custom entry "plan"（status: proposed → approved），
   刷新页面、换终端、resume 都还在；压缩后经 compaction_context 补回。
4. **提问走选择器** —— 代码回答不了、又会改变计划的决定，用 ask_user 一次问完（同 Claude Code 的
   AskUserQuestion、Codex 的 request_user_input）：界面弹选项，答案作为工具结果回来，本轮接着规划。
   没有可交互界面（print 模式、网页）时工具报错，模型退回到在回复里提问。
5. **任务表** —— 每项任务带「完成标准」（怎么验证做完了：命令、测试、可观察的结果）和涉及文件，
   界面画成一张表。批准后任务直接变成 update_plan 的清单（第一项 in_progress），执行时每完成
   一项先按完成标准验证再打勾，界面实时显示；目标重锚提醒里带着清单和当前这项的完成标准。
6. **执行时守住计划**（todo.py 校验 + 这里的收尾检查）——
   - 已批准的任务不能悄悄删掉或改写，不能跳过前面没做的去做后面的；确需调整要写 explanation，
     界面显示调整原因；
   - 模型想结束回答时清单还没做完：用 follow-up 提醒它继续下一项或说明卡在哪（每次运行最多
     MAX_NUDGES 次，不会死循环）。

模式的注册和生效走 ExtensionAPI 的 register_mode，核心层不认识"计划"这个概念。
界面（pi-tui / 网页）调 approve_plan() 或 /build 来执行计划。
"""

from __future__ import annotations

from pi_agent import AgentTool, AgentToolResult, CustomMessage, ToolContext

NAME = "plan_mode"
DESCRIPTION = "计划模式：只读调研、提交带任务清单的计划，批准后切到执行模式按清单逐项完成"

ENTRY_TYPE = "plan"
READ_ONLY = ("read", "grep", "find", "ls")
# 调研用的只读工具：web 扩展的上网工具；task 在计划模式里只能派只读的 explore 子智能体
RESEARCH = ("web_search", "web_fetch", "task")
KEEP = ("memory",)      # 计划模式里也能记东西：写的是记忆文件，不是项目
ASK_TOOL = "ask_user"
GUARD_TYPE = "plan_guard"   # 清单没做完就想收尾时的提醒（CustomMessage）
MAX_NUDGES = 2              # 每次运行最多提醒几次：确实做不下去时让模型能停下来

# 结构参照 Codex 的 Plan Mode 协作模式提示（collaboration-mode-templates/templates/plan.md）
PLAN_PROMPT = """

<plan_mode>
You are in PLAN MODE. Your job is to produce a plan, not to do the work.

Mode rules:
- You stay in plan mode until the user switches modes. If the user asks you to implement \
something while in plan mode, treat it as a request to plan the implementation.
- Only read-only tools are available (read, grep, find, ls, and web_search / web_fetch / task \
(read-only sub-agents for broad research) when present), plus `ask_user` and `submit_plan`. \
Do not try to edit files or run commands.

Work in three phases:
1. Explore first. Ground yourself in the code: read the relevant files, entry points and \
configs. Answer every question that the repository can answer by looking, not by asking.
2. Clarify only what you cannot discover. If a decision would materially change the plan \
(scope, behavior, trade-offs) and the code cannot settle it, call `ask_user` once with all such \
questions (1-4), each with 2-4 options and the recommended option marked. The user picks \
answers in the UI and they come back as the tool result; then continue in the same turn. \
Don't ask about things with an obvious default: take the default and list it under questions \
in the plan as an assumption. Don't write questions as plain text in your reply when \
`ask_user` is available, and don't repeat them in your reply before calling it.
3. Submit. When the plan is decision complete (an implementer would not need to make any \
decisions), call `submit_plan` exactly once:
   - title: a short name for the change
   - summary: Markdown, concise: approach, key changes by area, how it will be tested, \
assumptions
   - steps: 3-10 ordered tasks, shown to the user as a task table and used as the execution \
checklist. Each task has a title (one concrete unit of work: "Add X to Y"), the files it \
touches, and done_when: how to verify it is done (a command to run, a test that passes, an \
observable result). Make done_when specific enough that the implementer can check it.
   - files: the main files to be changed or created
   - questions: remaining open questions or assumptions (may be empty)
After calling `submit_plan`, stop. The user will approve it, ask for changes, or drop it. \
If they ask for changes, submit a complete replacement plan.

Language: everything the user sees (progress notes, questions and options, the plan) must be \
in the language the user writes in. If they write Chinese, write Chinese.
</plan_mode>"""

SUBMIT_DESCRIPTION = (
    "Submit the finished implementation plan for the user's approval. Call it once the plan is "
    "decision complete; the turn ends right after, waiting for the user. `steps` becomes the "
    "execution checklist, so make each step concrete and verifiable."
)

ASK_DESCRIPTION = (
    "Ask the user 1-4 multiple-choice questions and wait for the answers. Use it only for "
    "decisions that materially change the plan and that the repository cannot answer. Each "
    "question has 2-4 short options; mark the one you recommend. The UI always adds an option "
    "for the user to type their own answer. Write questions and options in the user's language. "
    "Don't repeat the questions or options in your reply text: the UI shows them."
)

ASK_PARAMETERS = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "description": "1-4 questions, asked one after another",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The full question"},
                    "header": {"type": "string",
                               "description": "Very short topic label, a few words"},
                    "options": {
                        "type": "array",
                        "description": "2-4 mutually exclusive choices",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string", "description": "Short choice text"},
                                "description": {"type": "string",
                                                "description": "What choosing it means"},
                                "recommended": {"type": "boolean"},
                            },
                            "required": ["label"],
                        },
                    },
                },
                "required": ["question", "options"],
            },
        },
    },
    "required": ["questions"],
}

SUBMIT_PARAMETERS = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Short name for the change"},
        "summary": {"type": "string",
                    "description": "Markdown: approach, key changes, test plan, assumptions"},
        "steps": {
            "type": "array",
            "description": "3-10 ordered tasks: the task table and the execution checklist",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "One concrete unit of work"},
                    "files": {"type": "array", "items": {"type": "string"},
                              "description": "Files this task changes or creates"},
                    "done_when": {"type": "string",
                                  "description": "How to verify it is done: a command, a test, "
                                                 "an observable result"},
                },
                "required": ["title", "done_when"],
            },
        },
        "files": {"type": "array", "items": {"type": "string"},
                  "description": "Main files to change or create"},
        "questions": {"type": "array", "items": {"type": "string"},
                      "description": "Open questions or assumptions; empty if none"},
    },
    "required": ["summary", "steps"],
}


def activate(pi) -> None:

    async def apply_plan_mode(session) -> None:
        """每轮开始前生效。session 已经把系统提示和工具重置回基线了"""
        from pi_coding_agent.tools import create_tools

        session.agent.state.system_prompt = (session.agent.state.system_prompt or "") + PLAN_PROMPT
        excluded = set(session.settings.get("excludeTools") or [])
        tools = create_tools([name for name in READ_ONLY if name not in excluded], session.cwd)
        tools += [t for t in session.agent.state.tools if t.name in RESEARCH + KEEP]
        tools += [ask_tool, submit_tool]
        session.agent.set_tools(tools)

    async def ask_user(args: dict, ctx: ToolContext) -> AgentToolResult:
        questions = normalize_questions(args.get("questions"))
        if not questions:
            return AgentToolResult.text(
                "questions is empty: give 1-4 questions, each with 2-4 options", is_error=True)
        session = pi.session
        if session is None or session.user_input is None:
            # print 模式、网页等没有可交互界面：退回到在回复里提问
            return AgentToolResult.text(
                "No interactive UI is available to show these questions. Ask them in your reply "
                "instead (each with its options and your recommendation), then stop and wait for "
                "the user's answer. Do not submit a plan yet.",
                is_error=True)
        answers = await session.ask_user(questions)
        if not answers:
            return AgentToolResult.text(
                "The user skipped the questions. Use your recommended options, list them as "
                "assumptions in the plan's questions field, and continue.",
                details={"questions": questions, "answers": []})
        lines = ["The user answered:"]
        for answer in answers:
            how = " (typed by the user)" if answer.get("custom") else ""
            lines.append(f"- {answer['question']} -> {answer['answer']}{how}")
        lines.append("Continue planning with these decisions; don't ask about them again.")
        return AgentToolResult.text("\n".join(lines),
                                    details={"questions": questions, "answers": answers})

    async def submit_plan(args: dict, ctx: ToolContext) -> AgentToolResult:
        tasks = normalize_tasks(args.get("steps"))
        if not tasks:
            return AgentToolResult.text("steps is empty: give the ordered tasks of the plan",
                                        is_error=True)
        steps = [task["title"] for task in tasks]
        summary = str(args.get("summary") or "").strip()
        plan = {
            "title": str(args.get("title") or "").strip() or _first_line(summary) or "实施计划",
            "summary": summary,
            "steps": steps,         # 只有标题：旧版本和别处按字符串列表读
            "tasks": tasks,         # 完整任务：标题 + 涉及文件 + 完成标准
            "files": [str(f) for f in args.get("files") or []],
            "questions": [str(q) for q in args.get("questions") or []],
            "status": "proposed",
        }
        pi.state["plan"] = plan
        session = pi.session
        if session is not None:
            from pi_coding_agent.session_events import PlanProposedEvent

            # 存进会话记录：刷新页面、换终端、下次 resume 都还在
            session.session.append_custom_entry(ENTRY_TYPE, plan)
            session.notify(PlanProposedEvent(plan=plan))
            pi.events.emit("plan-submitted", plan)
        return AgentToolResult.text(
            f"Plan submitted ({len(steps)} steps). Waiting for the user to approve, revise or drop "
            "it. Do not continue.",
            details={"plan": plan},
            terminate=True,     # 提交即停：本轮结束，不再调模型
        )

    # 不走 pi.register_tool：那会把它加进每个会话的工具基线，执行模式下模型也能调。
    # 它只属于计划模式，由 apply_plan_mode 按需挂上
    submit_tool = AgentTool(
        name="submit_plan",
        label="提交计划",
        description=SUBMIT_DESCRIPTION,
        parameters=SUBMIT_PARAMETERS,
        execute=submit_plan,
    )

    ask_tool = AgentTool(
        name=ASK_TOOL,
        label="提问",
        description=ASK_DESCRIPTION,
        parameters=ASK_PARAMETERS,
        execute=ask_user,
    )

    pi.register_mode(
        id="plan",
        label="计划模式",
        description="只读调研，提交带任务清单的计划，批准后再执行",
        apply=apply_plan_mode,
        badge="计划",
    )
    pi.register_mode(id="chat", label="执行模式", description="全部工具可用，按计划逐项执行", badge="")

    async def cmd_plan(rest: str) -> str | None:
        session = pi.session
        if session is None:
            return "还没有活跃会话"
        session.switch_mode("plan")
        if rest.strip():
            await session.prompt(rest.strip())
            return None
        return "已进入计划模式：只读调研，先出带任务清单的计划。描述你的需求开始。"

    async def cmd_build(rest: str) -> str | None:
        """批准最近一份计划：切到执行模式、生成进度单、开始执行"""
        session = pi.session
        if session is None:
            return "还没有活跃会话"
        try:
            task = approve_plan(session, rest.strip())
        except LookupError as exc:
            return str(exc)
        await session.prompt(task)
        return None

    async def cmd_show_plan(rest: str) -> str:
        session = pi.session
        plan = (latest_plan(session) if session is not None else None) or pi.state.get("plan")
        return plan_markdown(plan) if plan else "当前没有计划"

    async def plan_context(session) -> str | None:
        """上下文压缩后把当前计划补回给模型，否则执行到一半压缩就忘了计划"""
        plan = latest_plan(session) or pi.state.get("plan")
        if not plan:
            return None
        if session.mode == "plan":
            note = "当前仍在计划模式：只读调研；计划提交后等用户确认。"
        elif plan.get("status") == "approved":
            note = "这是用户已批准、正在执行的计划，执行时以它为准（进度见任务计划）。"
        else:
            note = "这是最近一次提交的计划，执行时以它为准。"
        return render_plan_context(plan, note)

    pi.compaction_context(plan_context)

    nudges = {"count": 0}

    def on_agent_start(event) -> None:
        nudges["count"] = 0

    def on_turn_end(event) -> None:
        """模型这次调用没调工具（要结束回答了），而批准的清单还没做完：提醒它继续或说明原因。
        turn_end 在循环取 follow-up 之前同步发出，这里塞进去的消息当场就会被取走"""
        session = pi.session
        message = getattr(event, "message", None)
        if session is None or message is None or getattr(event, "tool_results", None):
            return
        if getattr(message, "stop_reason", "") in ("error", "aborted") or session.mode == "plan":
            return
        left = unfinished_plan_steps(session)
        if not left or nudges["count"] >= MAX_NUDGES:
            return
        nudges["count"] += 1
        session.agent.follow_up(CustomMessage(
            custom_type=GUARD_TYPE,
            content=render_guard(left),
            details={"notice": f"计划还有 {len(left)} 项没完成，提醒模型继续"
                               f"（{nudges['count']}/{MAX_NUDGES}）", "left": len(left)},
        ))

    pi.on("agent_start", on_agent_start)
    pi.on("turn_end", on_turn_end)

    pi.register_command("plan", "进入计划模式（可直接带上需求）", cmd_plan, usage="/plan [需求]")
    pi.register_command("build", "批准最近的计划并开始执行", cmd_build, usage="/build [补充说明]")
    pi.register_command("show_plan", "查看当前计划", cmd_show_plan)


def approve_plan(session, extra: str = "") -> str:
    """批准最近一份计划：标记 approved、切到执行模式、把任务生成进度单（第一项 in_progress）。
    返回交给模型执行的任务说明，由调用方 prompt 出去（界面可以只回显一行标题）。
    没有计划时抛 LookupError"""
    from pi_coding_agent.builtin_extensions.todo import save_todo

    plan = latest_plan(session)
    if not plan:
        raise LookupError("还没有可执行的计划。先进入计划模式让模型提交一份。")
    approved = {**plan, "status": "approved"}
    session.session.append_custom_entry(ENTRY_TYPE, approved)
    session.switch_mode("chat")
    steps = [{"step": task["title"], "status": "in_progress" if i == 0 else "pending",
              "done_when": task["done_when"]}
             for i, task in enumerate(plan_tasks(plan))]
    save_todo(session, {"plan": steps, "source": "plan"})
    return render_plan_as_task(approved, extra)


def normalize_tasks(raw) -> list[dict]:
    """模型给的任务整理干净：{title, files, done_when}；去掉空标题。也接受纯字符串（旧格式）"""
    tasks = []
    for item in raw or []:
        if isinstance(item, str):
            item = {"title": item}
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or "").split())
        if not title:
            continue
        tasks.append({
            "title": title,
            "files": [str(f).strip() for f in item.get("files") or [] if str(f).strip()],
            "done_when": " ".join(str(item.get("done_when") or "").split()),
        })
    return tasks


def plan_tasks(plan: dict) -> list[dict]:
    """计划的任务列表。旧计划只有 steps（字符串），补成没有完成标准的任务"""
    tasks = plan.get("tasks")
    if isinstance(tasks, list) and tasks:
        return normalize_tasks(tasks)
    return normalize_tasks(plan.get("steps"))


def unfinished_plan_steps(session) -> list[dict]:
    """正在执行的已批准计划里还没做完的项；没有在执行的计划返回空"""
    from pi_coding_agent.builtin_extensions.todo import latest_todo

    record = latest_todo(session)
    if not record or record.get("source") != "plan":
        return []
    return [s for s in record["plan"] if s.get("status") != "completed"]


def render_guard(left: list[dict]) -> str:
    from pi_coding_agent.builtin_extensions.todo import MARKS

    lines = [f"[系统提醒] 用户批准的计划还有 {len(left)} 项没完成：", ""]
    for step in left:
        lines.append(f"- {MARKS.get(step.get('status'), '[ ]')} {step['step']}")
        if step.get("done_when"):
            lines.append(f"  完成标准：{step['done_when']}")
    lines += [
        "",
        "接着做下一项：先用 update_plan 把它标为 in_progress，做完按完成标准验证，再标 completed。",
        ("如果确实做不下去（缺信息、被阻塞、需要用户决定），用 update_plan 写 explanation 调整清单，"
         "然后告诉用户卡在哪里、需要什么。"),
    ]
    return "\n".join(lines)


def normalize_questions(raw) -> list[dict]:
    """模型给的问题整理干净：去掉空问题、空选项，选项最多 4 个，问题最多 4 个"""
    questions = []
    for item in raw or []:
        if not isinstance(item, dict) or not str(item.get("question") or "").strip():
            continue
        options = []
        for option in item.get("options") or []:
            if isinstance(option, str):
                option = {"label": option}
            if isinstance(option, dict) and str(option.get("label") or "").strip():
                options.append({"label": str(option["label"]).strip(),
                                "description": str(option.get("description") or "").strip(),
                                "recommended": bool(option.get("recommended"))})
        if not options:
            continue
        questions.append({"question": str(item["question"]).strip(),
                          "header": str(item.get("header") or "").strip(),
                          "options": options[:4]})
    return questions[:4]


def latest_plan(session) -> dict | None:
    """当前分支上最近一份计划（提交或批准的）"""
    for entry in reversed(session.session.get_branch()):
        if getattr(entry, "type", "") == "custom" and getattr(entry, "custom_type", "") == ENTRY_TYPE:
            return entry.data if isinstance(entry.data, dict) and entry.data.get("steps") else None
    return None


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:60]
    return ""


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "/") or "—"


def task_table(tasks: list[dict]) -> list[str]:
    """任务表（Markdown 表格）：# | 任务 | 完成标准 | 涉及文件；没有的列不画"""
    with_done = any(t["done_when"] for t in tasks)
    with_files = any(t["files"] for t in tasks)
    header = ["#", "任务"] + (["完成标准"] if with_done else []) + (["涉及文件"] if with_files else [])
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for i, task in enumerate(tasks, 1):
        row = [str(i), _cell(task["title"])]
        if with_done:
            row.append(_cell(task["done_when"]))
        if with_files:
            row.append(_cell("、".join(task["files"])))
        lines.append("| " + " | ".join(row) + " |")
    return lines


def plan_markdown(plan: dict) -> str:
    """给人看的计划（界面渲染成 Markdown）：概述 + 任务表"""
    lines = []
    if plan.get("summary"):
        lines += [plan["summary"].strip(), ""]
    tasks = plan_tasks(plan)
    lines.append(f"**任务（{len(tasks)} 项）**")
    lines += ["", *task_table(tasks)]
    if plan.get("files") and not any(t["files"] for t in tasks):
        lines += ["", "**涉及文件**：" + "、".join(f"`{f}`" for f in plan["files"])]
    if plan.get("questions"):
        lines += ["", "**待确认**", *[f"- {q}" for q in plan["questions"]]]
    return "\n".join(lines)


def render_plan_context(plan: dict, note: str) -> str:
    lines = ["## 当前计划", note, "", f"目标：{plan.get('title') or plan.get('summary', '')}", ""]
    if plan.get("summary") and plan.get("title"):
        lines += [plan["summary"], ""]
    lines.append("任务：")
    lines += _task_lines(plan_tasks(plan))
    if plan.get("files"):
        lines += ["", "涉及文件：" + "、".join(plan["files"])]
    if plan.get("questions"):
        lines += ["", "待确认：", *[f"- {q}" for q in plan["questions"]]]
    return "\n".join(lines)


def _task_lines(tasks: list[dict]) -> list[str]:
    lines = []
    for i, task in enumerate(tasks, 1):
        lines.append(f"{i}. {task['title']}")
        if task["done_when"]:
            lines.append(f"   完成标准：{task['done_when']}")
        if task["files"]:
            lines.append(f"   涉及文件：{'、'.join(task['files'])}")
    return lines


def render_plan_as_task(plan: dict, extra: str = "") -> str:
    """批准后交给模型执行的任务说明"""
    lines = [f"执行已批准的计划：{plan.get('title') or ''}".rstrip("："), ""]
    if plan.get("summary"):
        lines += [plan["summary"], ""]
    lines.append("任务清单（已写进 update_plan，第 1 项已设为 in_progress）：")
    lines += _task_lines(plan_tasks(plan))
    if plan.get("files"):
        lines += ["", "涉及文件：" + "、".join(plan["files"])]
    if extra:
        lines += ["", f"用户补充：{extra}"]
    lines += [
        "",
        "执行要求：",
        ("- 按清单顺序逐项完成。每做完一项，先按它的「完成标准」实际验证（运行命令、跑测试），"
         "验证通过再用 update_plan 标为 completed，同时把下一项设为 in_progress。"),
        ("- 清单是用户批准的：不要删除、改写或跳过任务。确需调整（拆分、跳过、新增）时，"
         "在 update_plan 里写 explanation 说明原因。"),
        "- 不做清单以外的改动。",
        "- 全部完成后汇报：每项改了什么、怎么验证的、结果如何。",
    ]
    return "\n".join(lines)
