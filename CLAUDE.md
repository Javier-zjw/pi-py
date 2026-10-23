# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

pi-py 是 TypeScript 项目 [earendil-works/pi](https://github.com/earendil-works/pi) 的 Python 分层重写。代码注释、文档以中文为主。`SUMMARY.md` 是更完整的交接文档（设计取舍、踩坑记录、待办），它在 `.gitignore` 里但已被跟踪。

## 常用命令

```bash
uv sync                                   # 工作区安装（或对每个 packages/* 执行 pip install -e）
pip install -e packages/pi-server         # web 后端需要 fastapi/uvicorn

pi                                        # 朴素 CLI（pi_coding_agent.cli）：交互 / --mode print / --mode json
pi-tui                                    # 终端 UI（pi_app.app）；--continue / --resume <jsonl> 恢复会话
pi-web [-C DIR] [--port 8848]             # HTTP+SSE 后端（pi_server.app），默认 127.0.0.1:8848

cd packages/pi-web && npm install
npm run dev                               # Vite 5173，/api 代理到 8848
npm run build                             # 产物写入 packages/pi-server/pi_server/web/

ruff check packages                       # line-length 100, py310
```

### 测试

测试全部离线、无需安装：每个测试文件自己往 `sys.path` 塞入 `packages/pi-ai`、`pi-agent`、`pi-coding-agent`。

以脚本方式直接运行会跑完全部断言并一次性汇总失败（脚本模式下 `check()` 失败不中断）：

```bash
python packages/pi-ai/tests/test_offline.py
python packages/pi-agent/tests/test_offline.py [-v]
python packages/pi-coding-agent/tests/test_tools.py      # 另有 test_session / test_resources / test_runtime / test_compaction / test_agent_session
python packages/pi-app/tests/test_renderer.py            # 终端渲染：真会话 + 假模型，检查实际输出
python packages/pi-app/tests/test_commands.py            # 斜杠命令：命令表、/model 选择器、/clear、/resume 等
python packages/pi-tui/tests/test_widgets.py             # 命令补全候选、选择器状态与渲染
python packages/pi-tui/tests/test_markdown.py            # 流式 Markdown：块、表格、行内、折行
```

pytest 下 `check()` 失败会抛 AssertionError（`under_pytest()` 必须在调用时判断环境变量，不能在 import 时求值，否则 pytest 会假绿）。pi-ai 和 pi-agent 的测试文件都叫 `test_offline.py` 且没有 `__init__.py`，必须**按包分别运行**，一次性收集会报 import file mismatch：

```bash
cd packages/pi-agent && pytest tests
cd packages/pi-coding-agent && pytest tests/test_session.py::test_persistence   # 单个用例
```

新增用例时沿用现有写法：异步逻辑写成 `async def case_xxx()`，在文件末尾的 pytest 兼容层加同步壳 `def test_xxx(): asyncio.run(case_xxx())`，并加进 `main()`。

`tests/manual/*.py` 需要真实模型和密钥，不属于离线测试。`packages/pi-agent/tests/CHECKLIST.md` 把每条断言映射到实现位置，断言变红时先查它。

## 架构

### 严格分层（核心约束）

```
pi-app    ──→ pi-coding-agent ──→ pi-agent ──→ pi-ai
   └────→ pi-tui
pi-server ──→ pi-coding-agent
pi-web（Vue 3 + Vite，只通过 HTTP/SSE 与 pi-server 通信）
```

- 下层绝不 import 上层；每个包可独立安装、独立测试。`pi-ai` 与 `pi-tui` 的 `dependencies = []` 必须保持为空（`pi-ai` 对 httpx 是函数内懒加载）。
- `pi_coding_agent` 不能依赖 `pi_tui` / `pi_app` / `pi_server`；`pi_tui` 不认识任何 `pi_*`。`pi_agent` 不碰磁盘、不起进程。
- README/SUMMARY 提到的 `ARCHITECTURE.md`、`.importlinter`、`tests/test_all.py`（含 `test_layering`）目前**不在仓库中**，分层只能靠人工遵守——新增 import 时自行检查方向。
- 依赖倒置点：`StreamFn`（pi-agent 只认它，测试用几十行假 generator 替换整个网络栈，见 `pi-coding-agent/tests/fakes.py`）、`Provider`、`HttpTransport`、`CredentialStore`、`AgentTool.execute`、`ResourceLoader`。

### 各层职责

- **pi-ai**：单次 LLM 调用。`Context` 可序列化；`Models` 注册表提供 `stream`/`complete`（provider 原生）与 `stream_simple`/`complete_simple`（统一 `reasoning`）。provider 只有 `anthropic-messages` 和 `openai-completions`（兼容端点）。凭证优先级：运行时 override > `auth.json` > 环境变量。
  - 用量约定（同 Anthropic）：`usage.input` **不含**命中缓存的部分，上下文大小 = input + cache_read + cache_write。OpenAI 兼容端点的 `prompt_tokens` 包含缓存，必须减掉（`providers/openai.py::apply_openai_usage`，也认 DeepSeek 的 `prompt_cache_hit_tokens`）——曾因此把上下文算成两倍，导致清理/压缩反复误触发。
  - `errors.py`：HTTP 错误经 `describe_llm_error` 把响应体里的原因写进 `error_message`；`is_context_overflow` 识别各家"上下文超长"（不含限流）。
  - Anthropic 前缀缓存：`Model.prompt_cache`（models.json `"promptCache"`）为 None 时只对官方 Anthropic 开启；第三方 anthropic-messages 端点可能不认 `cache_control`，需显式开启。断点：system、最后一个工具、最后两条 user 角色消息。
- **pi-agent**：`loop.py::run_agent_loop` 回合状态机 + `execute_tool_calls`（校验 → `before_tool_call` → 并行/串行执行 → `after_tool_call`）；`Agent` 管状态、事件、steering/follow-up 队列、abort。
  - 并行工具的双序语义：`tool_execution_end` 按完成顺序发，结果消息按请求顺序存（写入 `results[index]`，不能用 `gather` 返回值）。
  - steering 在工具批次后注入（打断），follow-up 在模型要停时注入（续接）。
  - 事件约定：Start = 消息开始（可能流式），End = 定稿；生来完整的消息也成对发，订阅方只听 `message_end`。
  - 全程使用 `AgentMessage`，只在 provider 边界 `convert_to_llm`；`CustomMessage.include_in_context` 控制是否给模型看。
  - `default_convert_to_llm` 末尾会 `ensure_tool_results`：没有结果的 tool_call（中途取消、进程在工具执行中退出）补一条"被中断"的结果，否则服务商拒绝整个请求。`Agent.continue_()` 不追加消息、从现有 transcript 续跑。
  - `LoopConfig.on_stream_error`：模型调用出错时询问上层，返回 True 则丢弃失败回复（不入 transcript、不发 `message_end`）并重试本轮，每次运行最多 `MAX_ERROR_RETRIES` 次。
- **pi-coding-agent**：
  - `AgentSession`（`agent_session.py`）是组合点：`AgentSessionServices`（ModelRuntime + SettingsManager + ResourceLoader，按 cwd 解析一次）+ `SessionManager` + `Agent`。持久化靠订阅 agent 事件。会话自己的事件（如压缩）和 agent 事件走同一条 `subscribe` 通道，按 `event.type` 区分。
  - 恢复会话：上下文以工具结果或未回复的用户消息结尾 = 上次在一轮中途中断（`can_continue`）；末尾缺结果的工具调用在打开时补上中断结果并写回文件（`interrupted_tool_calls`）。`continue_()` 和 `prompt()` 共用 `_prepare_run()`（模式、钩子、先压后跑），入口：CLI / pi-tui 的 `/continue`、网页 `POST /api/sessions/{id}/continue`。
  - 模式：`_apply_mode()` 每轮先把系统提示和工具集**从 `_base_system_prompt` / `_base_tools` 基线重建**，再调用模式的 `apply`——不要在上一轮结果上叠加。
  - 会话（`session/`）：JSONL **树**（`id`/`parentId`），分支=移动 leaf；存于 `~/.pi/agent/sessions/<项目目录名>/`。磁盘格式与 TS 版 pi 保持一致（camelCase 键、v3 entry 类型），改格式会破坏互操作。compaction entry 写 `firstKeptEntryId`（旧文件里的 `retainedTail` 仍可读），`details.state` 存结构化状态。模型/思考档位从完整分支读取，而非裁剪后的上下文。
  - 工具（`tools/`）：`read write edit bash grep find ls`。参数是纯 JSON Schema dict，由 `pi_agent.validation` 校验（会把 `"3"` 纠正为 `3`）；非必填字段一律 `args.get()`；`details` 必须可 JSON 序列化（`Path` 先转 `str`）。grep/find 有 rg/fd 时用二进制，否则纯 Python 回退。所有来自 OS 的字符串先过 `text.sanitize`；bash 输出用增量解码器（中文跨块）。
  - 配置：`~/.pi/agent/` 下 `models.json`（自定义 provider/模型，`api` 字段决定协议）、`auth.json`（`{provider_id: {apiKey}}`）、`settings.json`；项目级 `<cwd>/.pi/settings.json` 深度合并覆盖全局（默认值见 `settings.py::DEFAULT_SETTINGS`）。`reasoning: true` 只是能力标记；思考档位来源：CLI `-t` > 会话记录 > settings > `off`。
- **上下文压缩**（`pi_coding_agent/compaction/`，设计取舍见各模块 docstring）：
  - 分工：`budget`（CJK 感知计量，以实报 usage 为锚）→ `cut`（按 token 预算保留尾部，不拆工具调用/结果）→ `engine`（只计算结果、不改会话）→ `AgentSession._apply_compaction`（落盘 + 替换消息，中间无 await）。
  - 压缩请求复用正文的 system prompt + tools + 消息前缀、`tool_choice="none"`，指令追加在末尾（`prompt.py`），为了命中前缀缓存；改动请求构造时保持前缀一致。
  - 状态磁盘存 JSON、给模型渲染成 Markdown（`state.py`）。用户原话、文件记录、命令由代码提取，模型只写需要理解的字段；`constraints/decisions/knowledge` 被模型漏掉时由代码补回。
  - 分层：L1 `prune`（到 60% 时把受保护区间外的旧工具输出换成占位，清到 40%，不调模型；一次省不到可用窗口的 `minBatchRatio`（10%）就不清——清理会让前缀缓存整段失效，小批量清理得不偿失，超长兜底不受此限）→ 仍超 85% 才 L2 摘要 → L3 `rehydrate`（摘要后把最近改过的文件当前内容和扩展片段附进状态消息）→ L4 超长兜底（服务商报超长时经 `on_stream_error`：L1 全清 + 保留尾部减半的 L2，重试本轮；每次 prompt 只救一次）。
  - 触发点：`prompt()` 先压后跑（新请求不进压缩）、`transform_context` 每次调模型前检查。服务商出错、取消、没变小都保持原历史；格式两次不合格才降级。压缩期间到来的 `prompt()` 会等待它结束。
  - L1 只把内存里的消息换成**副本**（会话条目里的原对象不动，文件保留原文），清理集合存为 `context_prune` 条目、恢复时重放；清理前报的 usage 不能当锚（`AgentSession._anchor_floor` / `context_tokens(min_anchor=)`）。
  - 保留尾部里 assistant 的 usage 是压缩前的，状态消息 `details.tailLength` 标出这段，计量时不能当锚——否则会反复压缩。
  - 前端契约：`compaction_start/progress/end`、`context_pruned` 帧（`pi_server/dto.py::compaction_dto`，历史回放共用）。网页里压缩卡片必须插在进行中的那一轮之前——`handle()` 总把帧写进最后一个 turn。
- **pi-tui**：零依赖终端渲染原语（`prompt_toolkit` 为可选 extra）。`LineReader` 支持额外快捷键（在输入行上方打印、不丢正在输入的字）和 `erase_when_done`；不要用 prompt_toolkit 的 `bottom_toolbar`——它固定在屏幕最底行，会在输入行下方留出大片空白。
  - 命令补全（`input.py`）：`LineReader(commands=...)` 行首输 `/` 弹菜单（`match_commands`：完全相同 > 名字前缀 > 别名前缀 > 包含）。`footer` 参数在输入行正下方插一行（插进 PromptSession 布局，并让输入窗口 `dont_extend_height`，否则会被挤到屏幕最底行）；菜单浮在输入窗口内，窗口高度由 `_input_height` 按**原始输入**的候选数撑开（↑↓ 浏览时输入框文字会变成选中项，按它算菜单会缩成一行）。菜单出来就高亮第一条但**只改下标不改文字**（`go_to_completion` 会把候选写进输入框，接着打字变成 `/helpm`）。回车 / Tab 不依赖异步补全结果，当场匹配（`_accept_command`），否则打字快时 `/mo` 回车会原样提交。ctrl+c 有字清空、空行退出。
  - Markdown（`markdown.py`）：`MarkdownStream` 按整行提交（`feed` 返回收齐的行，`preview` 给半行和没收齐的表格，`finish` 收尾），流式与一次性渲染结果必须一致；`wrap_ansi` 按显示宽度折行、样式跨行补回、句末标点不放行首（会越过连续标点往回带字）；`render_inline` 嵌套样式靠内层 RESET 后补回外层（`Theme.codes`）。不认识的写法原样输出。
  - 选择器（`picker.py`）：`PickerState` 纯状态可离线测，↑↓ 选行、←→ 切次要选项（思考档位）；`pick()` 是非全屏 prompt_toolkit 应用，选完擦掉，由调用方打一行结论。
- **pi-app**：把 AgentSession 事件渲染到 pi-tui（`renderer.py`）。同 Codex——过程只占一块位置，结论留下来：运行中问题下面只有活动区（`_live_lines`：第一行转圈 + 本轮用时 + 状态；下面是思考 / 过渡话 / 工具，三者共用一块，后来的顶掉先来的，思考只显示最新 `SLOT_LINES` 行，工具只显示名字（闪烁）不带参数）。正文开始时还不知道是不是最终回答：先放活动区，这次调用以工具调用收尾就是过渡话（丢掉，记进 `TurnRecord.steps`），否则是回答；超过活动区行数的长文字直接当回答写出。回答经 `MarkdownStream` 排版：定稿的行用 `live.append(lines, live=预览)` 写进 scrollback（必须同时换掉预览，否则旧半行会在下面重复一帧），半行预览留在活动区。L1 清理不单独打行，计数显示在活动区第一行、并进用时行。scrollback 里每轮只留「✻ 用时 · 工具 · ctrl+o 展开」（回答开始前写，`_commit_summary`）、空一行、以 ● 开头的回答、一行 token 统计；每次提问前空一行（`_blank_tail`）。`ctrl+o` / `/expand` 按发生顺序展开上一轮（`TurnRecord.steps`）。输入框上下各一条不带文字的细线夹住 `❯`（上沿 `input_prompt`、下沿 `input_footer`），提交后擦掉、由 `user_echo` 回显。
  - 斜杠命令（`commands.py` 命令表 → 补全菜单和 `/help` 共用；`app.py::App` 分派）：对齐 Claude Code——`/model`（选择器，↑↓ 模型 ←→ 思考档位；`/model <spec>` 直接切）、`/clear`（`/new` `/reset`，沿用模型和档位）、`/resume`（`pi_coding_agent.session.list_sessions`，跳过空会话，切换后回顾最近对话）、`/compact`、`/context`、`/cost`（`/usage`）、`/status`（`/session`）；另有 `/think` `/continue` `/tools` `/expand`。扩展命令经 `session.run_command`，提示模板交给 `prompt()` 展开；像命令名的未知 `/xxx` 报错，`/usr/bin` 这类照常发给模型。换会话用 `App._replace_session`：复用 services、改订阅、`renderer.reset()`。
  - 测试里用 `ScriptedApp` 覆盖 `App.pick` 返回预设结果；真终端效果用 pexpect + pyte 驱动（要回应 CPR `\x1b[6n`）。
- **pi-server**：`dto.py`（事件 → SSE 帧，**前后端唯一契约**）、`registry.py`（会话注册 + 事件扇出）、`workspace.py`（历史解析、文件浏览，`resolve_within()` 越界 403）都**不 import fastapi**，`app.py` 只是薄路由层。前端优先用 `pi_server/web/`（Vite 构建产物），不存在时回退 `pi_server/static/index.html`。新增 SSE 帧类型要同时改 `dto.py` 和 `pi-web/src/App.vue` 的 `handle()`。

### 扩展系统（加功能的默认方式）

上游 pi 的立场：MCP、子代理、计划模式、权限弹窗等**不进核心，走扩展**。只有当所有场景都需要、扩展 API 做不到（如改循环控制流）、且不破坏分层时才改核心层。

扩展是导出 `activate(pi)` 的 Python 模块（可选 `NAME`、`DESCRIPTION`），按顺序从以下目录加载（`_` 开头的文件跳过）：
`pi_coding_agent/builtin_extensions/` → `~/.pi/agent/extensions/` → `<cwd>/.pi/extensions/`。

`pi`（`extensions.py::ExtensionAPI`）提供 `register_tool` / `register_command` / `register_mode` / `before_agent_start` / `transform_context` / `compaction_context`（压缩后补回给模型的片段）/ `on(event_type)` / `events`（扩展间总线）/ `state` / `session` / `send_message`。加载或钩子异常都会被捕获并记入 `LoadedExtension.error` → `services.diagnostics`，不会让 CLI 起不来。注册项按 `api.owner` 归属到扩展。`settings.disabledExtensions` 中的扩展仍会列出但不执行 `activate`。

内置扩展：`builtin_extensions/plan_mode.py`（`chat`/`plan` 模式 + `/plan` `/build` `/show_plan` 命令，并通过 `compaction_context` 在压缩后补回当前计划），可作为模板；`tool_budget.py`（一次请求工具调用到 `toolBudget.softLimit` 次时用 steering 插一条 `CustomMessage` 提醒模型收尾，之后每 `every` 次再提醒，不打断；`details.notice` 供界面显示一行说明）。SUMMARY.md 提到的 `subagents.py`、`mcp.py` 尚未在仓库中。

注意 `pi.register_tool` 注册的工具会进入**每个** AgentSession 的工具基线（也会出现在网页的工具选择里）。只属于某个模式的工具（如 `submit_plan`）应直接构造 `AgentTool`，在该模式的 `apply` 里挂上。内置扩展总会被加载，断言扩展列表的测试要把它们算进去。
