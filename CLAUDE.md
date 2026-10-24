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

## 分支与合并

- 每实现一个新功能就从 main 新开一条 `feature/<名称>` 分支（依赖还没合并的分支时可以叠在它上面），不在 main 上直接提交。
- 只有用户明确说"合并进主分支"时才合并：开 PR、以 merge 方式合并；叠加的分支按依赖顺序依次合并。
- **远端分支总数（含 main）最多保留 5 条。** 合并进 main 的分支不马上删，留着方便回看；新开分支前（或推送新分支后）先 `git fetch --prune` 再按时间数一下：
  `git for-each-ref --sort=committerdate --format='%(committerdate:iso8601) %(refname:short)' refs/remotes/origin`
  超过 5 条时，从**最早实现的功能分支**（最后一次提交最早的）开始删，直到剩 5 条：远端 `git push origin --delete <分支>`、本地 `git branch -d <分支>`。
- 只删已经合并进 main 的分支（`git branch -r --merged origin/main` 里有它）；main 和没合并的分支永远不删。如果没合并的分支多到删完已合并的仍超过 5 条，先问用户。

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
  - 推理回传：OpenAI 兼容端点把**调了工具的**回复的思考作为 `reasoning_content` 发回（同 provider 才发）。`Model.reasoning_passback`（models.json `"reasoningPassback"`），None = 自动：DeepSeek / Moonshot(Kimi) 的推理模型开，其余关（严格端点可能拒绝未知字段）。不回传时模型每一步都从头想，长工具链容易跑偏。
  - 用量约定（同 Anthropic）：`usage.input` **不含**命中缓存的部分，上下文大小 = input + cache_read + cache_write。OpenAI 兼容端点的 `prompt_tokens` 包含缓存，必须减掉（`providers/openai.py::apply_openai_usage`，也认 DeepSeek 的 `prompt_cache_hit_tokens`）——曾因此把上下文算成两倍，导致清理/压缩反复误触发。
  - `errors.py`：HTTP 错误经 `describe_llm_error` 把响应体里的原因写进 `error_message`（有 Retry-After 头时追加 `[retry after Ns]`）；`is_context_overflow` 识别各家"上下文超长"（不含限流）；`is_retryable` 识别值得重试的临时失败（408/409/425/429/5xx/529、overloaded、断网超时），余额不足、密钥错、上下文超长不算；`retry_after` 从错误文本取回等待秒数。
  - Anthropic 前缀缓存：`Model.prompt_cache`（models.json `"promptCache"`）为 None 时只对官方 Anthropic 开启；第三方 anthropic-messages 端点可能不认 `cache_control`，需显式开启。断点：system、最后一个工具、最后两条 user 角色消息。
- **pi-agent**：`loop.py::run_agent_loop` 回合状态机 + `execute_tool_calls`（校验 → `before_tool_call` → 并行/串行执行 → `after_tool_call`）；`Agent` 管状态、事件、steering/follow-up 队列、abort。
  - 并行工具的双序语义：`tool_execution_end` 按完成顺序发，结果消息按请求顺序存（写入 `results[index]`，不能用 `gather` 返回值）。
  - steering 在工具批次后注入（打断），follow-up 在模型要停时注入（续接）。
  - 事件约定：Start = 消息开始（可能流式），End = 定稿；生来完整的消息也成对发，订阅方只听 `message_end`。
  - 全程使用 `AgentMessage`，只在 provider 边界 `convert_to_llm`；`CustomMessage.include_in_context` 控制是否给模型看。
  - `default_convert_to_llm` 末尾会 `ensure_tool_results`：没有结果的 tool_call（中途取消、进程在工具执行中退出）补一条"被中断"的结果，否则服务商拒绝整个请求。`Agent.continue_()` 不追加消息、从现有 transcript 续跑。
  - 工具结果 `AgentToolResult(terminate=True)`：这批工具执行完就结束本次运行（reason `terminated`），不再调模型——如提交计划后停下等用户确认。
  - 中断：`_stream_turn` 等下一个流事件时同时等 `cancel_event`（模型还没出字也能立刻停），中断后关流、回复标 `aborted`。
  - `LoopConfig.on_stream_error`：模型调用出错时询问上层，返回 True 则丢弃失败回复（不入 transcript、不发 `message_end`）并重试本轮；连续失败最多 `max_error_retries` 次（成功一次清零）。`Agent.wait_cancelled(timeout)` 供重试前的退避等待用，等待中 abort 立刻返回。
- **pi-coding-agent**：
  - `AgentSession`（`agent_session.py`）是组合点：`AgentSessionServices`（ModelRuntime + SettingsManager + ResourceLoader，按 cwd 解析一次）+ `SessionManager` + `Agent`。持久化靠订阅 agent 事件。会话自己的事件（如压缩）和 agent 事件走同一条 `subscribe` 通道，按 `event.type` 区分。
  - 网络重试：`_on_stream_error` 先看 `is_retryable` → `_retry_after_error`（`retry.baseDelay`·2^(n-1) 秒、封顶 `retry.maxDelay`，有 Retry-After 按它；连续超过 `retry.maxRetries`（默认 3）放弃），发 `RetryEvent`（`retry`）给界面（pi-tui 活动区显示、用时行计数；网页复用 `injected` 通知帧）；否则走 `_recover_overflow`（上下文超长）。
  - 恢复会话：上下文以工具结果或未回复的用户消息结尾 = 上次在一轮中途中断（`can_continue`）；末尾缺结果的工具调用在打开时补上中断结果并写回文件（`interrupted_tool_calls`）。`continue_()` 和 `prompt()` 共用 `_prepare_run()`（模式、钩子、先压后跑），入口：CLI / pi-tui 的 `/continue`、网页 `POST /api/sessions/{id}/continue`。
  - 模式切换用同步的 `switch_mode()`（`set_mode` 是它的 async 包装），发 `mode_changed` 事件、写 `mode_change` 条目，恢复会话时 `_restore_mode()` 还原。扩展经 `session.notify(event)` 发会话事件给界面（`session_events.py`：`mode_changed` / `plan_proposed` / `todo_updated`）。
  - 模式：`_apply_mode()` 每轮先把系统提示和工具集**从 `_base_system_prompt` / `_base_tools` 基线重建**，再调用模式的 `apply`——不要在上一轮结果上叠加。
  - 会话（`session/`）：JSONL **树**（`id`/`parentId`），分支=移动 leaf；存于 `~/.pi/agent/sessions/<项目目录名>/`。磁盘格式与 TS 版 pi 保持一致（camelCase 键、v3 entry 类型），改格式会破坏互操作。compaction entry 写 `firstKeptEntryId`（旧文件里的 `retainedTail` 仍可读），`details.state` 存结构化状态。模型/思考档位从完整分支读取，而非裁剪后的上下文。
  - 工具输出截断：bash 输出超限（2000 行或 50KB，任一）才截：`truncate.py::truncate_middle` 保留开头约 40%、结尾约 60%，按整行截（单行超长才按字符截），中间一行说明省略量和完整输出路径；只要截了就存完整输出到临时文件。
  - 工具（`tools/`）：`read write edit bash grep find ls`。参数是纯 JSON Schema dict，由 `pi_agent.validation` 校验（会把 `"3"` 纠正为 `3`）；非必填字段一律 `args.get()`；`details` 必须可 JSON 序列化（`Path` 先转 `str`）。grep/find 有 rg/fd 时用二进制，否则纯 Python 回退。所有来自 OS 的字符串先过 `text.sanitize`；bash 输出用增量解码器（中文跨块）。
  - 系统提示（`prompt.py::BASE_SYSTEM_PROMPT`，英文，结构参照 Codex）：先判断任务大小、范围以用户请求为准（范围外的只记一句）、隔几步自查够不够回答、多步任务用 `update_plan`、批量工具前一句进度说明、读文件用 `read` 而不是 bash、回答跟随用户语言。`test_resources` 断言了这几条，改提示时别删。
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
  - `LineReader(key_actions={"s-tab": fn})`：只改状态的快捷键（补全菜单打开时不抢），调完立刻重画；`footer` 可多行、每次重画重新取。
  - 常驻输入框：`LineReader(above=, on_escape=, on_interrupt=)`——`above` 画在输入框上方（运行中的活动区）、`invalidate()` 重画；Esc 不用 eager（Alt+Enter 是 Esc+回车），把 `ttimeoutlen` 调到 50ms；ctrl+c 先问 `on_interrupt(text)`。`suspend()` 收起正在读的一行（返回已输入的字，`read()` 返回 `SUSPENDED`），之后 `read(default=)` 接着读；`set_text()` 往输入框里放字。`LiveRegion.host`（`set_live` / `print_above`）有值时活动区交给宿主画、定稿内容交给宿主打印，自己不写光标控制码——两边同时画会互相踩。
  - 命令补全（`input.py`）：`LineReader(commands=...)` 行首输 `/` 弹菜单（`match_commands`：完全相同 > 名字前缀 > 别名前缀 > 包含）。`footer` 参数在输入行正下方插一行（插进 PromptSession 布局，并让输入窗口 `dont_extend_height`，否则会被挤到屏幕最底行）；菜单浮在输入窗口内，窗口高度由 `_input_height` 按**原始输入**的候选数撑开（↑↓ 浏览时输入框文字会变成选中项，按它算菜单会缩成一行）。菜单出来就高亮第一条但**只改下标不改文字**（`go_to_completion` 会把候选写进输入框，接着打字变成 `/helpm`）。回车 / Tab 不依赖异步补全结果，当场匹配（`_accept_command`），否则打字快时 `/mo` 回车会原样提交。ctrl+c 有字清空、空行退出。输入框高度上限半屏（`_input_height` 的 `Dimension(max=)`，至少 `INPUT_MIN_ROWS` 行），粘贴大段时在框内滚动。
  - 代码着色（`highlight.py`）：逐行正则切词，零依赖；python / javascript(ts) / go / rust / bash / json / diff，别名见 `ALIASES`；认不出的语言只给字符串数字上色，没写语言的原样（Markdown 的 # 不是注释）。
  - Markdown（`markdown.py`）：代码块按围栏上的语言着色；`MarkdownStream(table_width=)` 表格可比正文宽。`MarkdownStream` 按整行提交（`feed` 返回收齐的行，`preview` 给半行和没收齐的表格，`finish` 收尾），流式与一次性渲染结果必须一致；`wrap_ansi` 按显示宽度折行、样式跨行补回、句末标点不放行首（会越过连续标点往回带字）；`render_inline` 嵌套样式靠内层 RESET 后补回外层（`Theme.codes`）。不认识的写法原样输出。
  - 选择器（`picker.py`）：`PickerState` 纯状态可离线测，↑↓ 选行、数字键直接选（`select`）、←→ 切次要选项（思考档位）；画成带边框的面板（╭ 上边框 / │ 内容 / ╰ 按键说明，前面空一行），和模型回复明显分开——`pick(header=...)` 给了就写在上边框（如「需要你选择 · 1/2 · 主题」「需要你确认」），标题作为问题折行写在面板里，`detail=` 的几行（可带颜色，超宽截短）缩进写在问题下面；边框文字超宽时截短而不折行。`pick()` 是非全屏 prompt_toolkit 应用，选完擦掉，由调用方打一行结论。
- **pi-app**：把 AgentSession 事件渲染到 pi-tui（`renderer.py`）。同 Codex——过程只占一块位置，结论留下来：运行中问题下面只有活动区（`_live_lines`：第一行转圈 + 本轮用时 + 状态；下面是思考 / 过渡话 / 工具，三者共用一块，后来的顶掉先来的，思考只显示最新 `SLOT_LINES` 行；工具每个一行（`_tool_lines`，最新 `TOOL_ROWS` 个）：名字 + 关键参数（`_tool_target`：路径相对 `renderer.cwd`、bash 命令、搜索词）+ 结果（`_tool_result`：edit `+加 −删`、write `新建 N 行`、read `N 行`、失败的错误第一行，否则用时），在跑的闪烁带用时）。正文开始时还不知道是不是最终回答：先放活动区，这次调用以工具调用收尾就是过渡话（丢掉，记进 `TurnRecord.steps`），否则是回答；超过活动区行数的长文字直接当回答写出。回答经 `MarkdownStream` 排版：定稿的行用 `live.append(lines, live=预览)` 写进 scrollback（必须同时换掉预览，否则旧半行会在下面重复一帧），半行预览留在活动区。L1 清理不单独打行，计数显示在活动区第一行、并进用时行。`update_plan` / `submit_plan` 不算工具：任务清单由 `todo_updated` 事件驱动，存在 `renderer.plan`（跨轮保留，批准计划时在一轮开始前生成），画成「☰ 任务 done/total」+ 带序号的行（项多时围绕正在做的那项显示 `PLAN_ROWS` 项），本轮动过或还没做完就常驻活动区，回答开始前写在用时行下面，一轮结束还没做完时提示；带 explanation 的更新留一行「↻ 调整计划：原因」；回答写出后运行还在继续（`plan_guard` 提醒、插话）时 `_new_segment` 另起一段，最终回答前重新写用时行和最新清单；`plan_proposed` 把计划渲染成 ╭│╰ 框。输入框下沿第二行显示当前模式（`renderer.mode`，`mode_changed` 只改状态不打印——可能正在输入框里）。scrollback 里每轮只留「✻ 用时 · N 次工具 · 改了 N 个文件 +加 −删 · ctrl+o 展开」（回答开始前写，`_commit_summary`；工具名不进这行）、空一行、以 ● 开头的回答（正文限宽 `PROSE_MAX`，表格按全宽）、改动汇总（`_files_lines`：每个文件一行 +加 −删，按 edit 的 patch / write 的内容算，同一文件合并，最多 `FILE_ROWS` 个）、一行 token 统计；输入框下沿右边是状态栏（`_status_text`：模型 · 上下文占用（`context_tokens / context_window`，App 在 attach / 切模型时给）· 费用）；每次提问前空一行（`_blank_tail`）。`ctrl+o` / `/expand` 按发生顺序展开上一轮（`TurnRecord.steps`）。输入框上下各一条不带文字的细线夹住 `❯`（上沿 `input_prompt`、下沿 `input_footer`），提交后擦掉、由 `user_echo` 回显；回显按折行后的行数最多留 `ECHO_ROWS`（终端高度 1/3，夹在 4–8 行）——多的折成「… 还有 N 行 · ctrl+o 看全文」，全文记进 `TurnRecord.prompt`、展开时放最前面。
  - 斜杠命令（`commands.py` 命令表 → 补全菜单和 `/help` 共用；`app.py::App` 分派）：对齐 Claude Code——`/model`（选择器，↑↓ 模型 ←→ 思考档位；`/model <spec>` 直接切）、`/clear`（`/new` `/reset`，沿用模型和档位）、`/resume`（`pi_coding_agent.session.list_sessions`，跳过空会话，切换后回顾最近对话）、`/compact`、`/context`、`/cost`（`/usage`）、`/status`（`/session`）；另有 `/think` `/continue` `/tools` `/expand`。扩展命令经 `session.run_command`，提示模板交给 `prompt()` 展开；像命令名的未知 `/xxx` 报错，`/usr/bin` 这类照常发给模型。换会话用 `App._replace_session`：复用 services、改订阅、`renderer.reset()`。
  - 运行中可输入（同 Claude Code）：交互模式 `App.background=True`，`send()` / `/continue` 经 `spawn()` 放到后台任务（`App.task`），主循环接着读输入；活动区经 `ReaderHost` 画在输入框上方，定稿内容由 patch_stdout 打印到上面。运行中回车 = `steer()`（插话，模型做完当前这步就看到，送到前在活动区显示「↳ 插话」，送到后 `user_echo`），斜杠命令不执行只提示；Esc / 空行 ctrl+c = `interrupt()`（`session.abort()`，留一行「⏹ 已中断」）；一轮结束时没送到的插话放回输入框（`_restore_undelivered`）。选择器要用屏幕走 `exclusive()`：收起输入框（字留着）、用完放回，可重入——同一次交互（几道提问、确认后输原因）整段占着屏幕，否则中间那一瞬间按的键会落进输入框。测试里 `background` 默认 False，`send()` 等跑完才返回。
  - 权限确认：`attach` 时把 `App.confirm_tool` 挂到 `session.permission_handler`：弹「需要你确认 · 执行命令 / 修改文件」面板（`pick(detail=)` 显示命令或 diff 预览），选项 允许 / 允许并且本会话不再询问 / 拒绝 / 拒绝并说明，Esc 拒绝并停止本轮；结论留一行（✗ 拒绝了 … / ✓ 本会话不再询问 …）。`/permissions` 选择器切模式，非默认模式显示在输入框下沿（`renderer.permission`）。朴素 CLI 交互模式用 `cli.confirm_in_terminal`（y / a / n 原因）。
  - 计划 → 执行（同 Claude Code / Codex）：shift+tab（`App.cycle_mode`）在执行 ⇄ 计划之间切；计划模式提问走 `App.ask_user`（`attach` 时挂到 `session.user_input`；先 `renderer.pause()` 收起活动区再弹选择器，Esc 跳过全部），答案由渲染器在 `ask_user` 工具结束时写成「? 主题 → 回答」；`/plan [需求]`、`/build [补充]` 是内置命令。模型提交计划后 `App.send` 弹「执行这个计划？」（执行 / 继续完善 / 先不执行）；执行走 `plan_mode.approve_plan()`（标记 approved、切执行模式、任务变成 update_plan 清单）再 `send(任务说明)`，只回显一行标题。换会话时按 `latest_todo` 恢复清单。
  - 测试里用 `ScriptedApp` 覆盖 `App.pick` 返回预设结果；真终端效果用 pexpect + pyte 驱动（要回应 CPR `\x1b[6n`）。
- **pi-server**：`dto.py`（事件 → SSE 帧，**前后端唯一契约**）、`registry.py`（会话注册 + 事件扇出）、`workspace.py`（历史解析、文件浏览，`resolve_within()` 越界 403）都**不 import fastapi**，`app.py` 只是薄路由层。前端优先用 `pi_server/web/`（Vite 构建产物），不存在时回退 `pi_server/static/index.html`。新增 SSE 帧类型要同时改 `dto.py` 和 `pi-web/src/App.vue` 的 `handle()`。

### 扩展系统（加功能的默认方式）

上游 pi 的立场：MCP、子代理、计划模式、权限弹窗等**不进核心，走扩展**。只有当所有场景都需要、扩展 API 做不到（如改循环控制流）、且不破坏分层时才改核心层。

扩展是导出 `activate(pi)` 的 Python 模块（可选 `NAME`、`DESCRIPTION`），按顺序从以下目录加载（`_` 开头的文件跳过）：
`pi_coding_agent/builtin_extensions/` → `~/.pi/agent/extensions/` → `<cwd>/.pi/extensions/`。

`pi`（`extensions.py::ExtensionAPI`）提供 `register_tool` / `register_command` / `register_mode` / `before_agent_start` / `before_tool_call`（工具执行前 `handler(call, session)`，返回 `BeforeToolCallResult(block=True)` 拦下；AgentSession 经 `Agent(before_tool_call=)` 接上，钩子出错放行并记 `tool_call_errors`）/ `transform_context` / `compaction_context`（压缩后补回给模型的片段）/ `on(event_type)` / `events`（扩展间总线）/ `state` / `session` / `send_message`。加载或钩子异常都会被捕获并记入 `LoadedExtension.error` → `services.diagnostics`，不会让 CLI 起不来。注册项按 `api.owner` 归属到扩展。`settings.disabledExtensions` 中的扩展仍会列出但不执行 `activate`。

内置扩展：`builtin_extensions/permissions.py`（权限确认，同 Claude Code 权限模式：只读工具和只读 bash（`is_read_only`：每段都是只读命令、没有重定向 / 命令替换 / 后台）直接放行；`permissions.deny` / `allow` 规则（`bash(git push*)`、`edit(docs/*)`，fnmatch）；模式 `ask`（默认）/ `acceptEdits`（工作目录内的修改放行）/ `auto`；本会话"不再询问"按 `command_prefixes`（git / npm 等带子命令）记，组合命令每段都允许过才放行；其余交给 `session.permission_handler`（界面回调，返回 allow / always / deny / abort + feedback），回调出错一律拒绝；没有界面按 `permissions.nonInteractive`（默认 allow，print 模式和网页照旧）。状态挂在会话对象上（`_permission_state`）——扩展按文件路径加载，界面 import 包路径是另一份模块，模块全局不共享；`/permissions` 查看 / 切换）；`builtin_extensions/plan_mode.py`（计划 → 执行，参照 Codex Plan Mode：`plan` 模式只挂 read/grep/find/ls + `ask_user` + `submit_plan`（不给 update_plan），提示要求先探索、只问查不到的、计划决策完备；`ask_user`（同 Claude Code AskUserQuestion / Codex request_user_input）把 1-4 道选择题交给 `session.user_input`（界面挂的回调，pi-tui 逐题弹选择器、末项自己输入），答案作为工具结果回来、本轮接着规划；没挂回调（print 模式、网页）时工具报错让模型在回复里问；`submit_plan` 的 steps 是任务对象 `{title, files, done_when}`（完成标准），存 `plan` 条目（`steps` 只有标题、`tasks` 是完整任务；status proposed → approved）、界面画成任务表（`plan_markdown` → Markdown 表格）、发 `plan_proposed`、结果带 terminate 提交即停；`approve_plan()` 切 `chat`（执行模式）并把任务生成 update_plan 清单（`source: "plan"`，每项带 `done_when`）；执行时守住计划：`turn_end` 时模型没调工具（要收尾了）而清单没做完，用 follow-up 插 `plan_guard` 提醒继续或说明原因（每次运行最多 `MAX_NUDGES` 次）；`/plan` `/build` `/show_plan` 命令供网页等用；`compaction_context` 压缩后补回计划），可作为模板；`todo.py`（`update_plan` 工具，同 Codex：步骤 + pending/in_progress/completed，同时只能一个进行中；存为 `todo` custom entry，没做完的计划经 `compaction_context` 在压缩后补回；它经 `register_tool` 进入每个会话的工具基线——断言工具集合的测试要算上它；清单来自批准的计划（`source: "plan"`）时 `check_approved` 拒绝悄悄删改任务、跳过前面没做的项，写了 explanation 才放行，`done_when` 和 `source` 按标题带到下一份清单）；`tool_budget.py`（目标重锚：每 `toolBudget.reanchorEvery` 次工具调用用 steering 插一条 `CustomMessage`，内容是用户最近的请求 + 当前 `update_plan` 计划 + 自查能否回答，`details.quiet` 界面不单独显示（只进 ctrl+o）；到 `softLimit` 次时改为要求收尾，之后每 `every` 次再提醒，显示一行 ⚑ 说明。`update_plan` 不计数；都不打断）。SUMMARY.md 提到的 `subagents.py`、`mcp.py` 尚未在仓库中。

注意 `pi.register_tool` 注册的工具会进入**每个** AgentSession 的工具基线（也会出现在网页的工具选择里）。只属于某个模式的工具（如 `submit_plan`）应直接构造 `AgentTool`，在该模式的 `apply` 里挂上。内置扩展总会被加载（按文件名排序：permissions、plan_mode、todo、tool_budget），断言扩展列表的测试要把它们算进去。
