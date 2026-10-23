# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

pi-py 是 TypeScript 项目 [earendil-works/pi](https://github.com/earendil-works/pi) 的 Python 分层重写。代码注释、文档以中文为主。`SUMMARY.md` 是更完整的交接文档（设计取舍、踩坑记录、待办），它在 `.gitignore` 里但已被跟踪。

## 常用命令

```bash
uv sync                                   # 工作区安装（或对每个 packages/* 执行 pip install -e）
pip install -e packages/pi-server         # web 后端需要 fastapi/uvicorn

pi                                        # 朴素 CLI（pi_coding_agent.cli）：交互 / --mode print / --mode json
pi-tui                                    # 终端 UI（pi_app.app）
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
python packages/pi-coding-agent/tests/test_tools.py      # 另有 test_session / test_resources / test_runtime / test_agent_session
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
- **pi-agent**：`loop.py::run_agent_loop` 回合状态机 + `execute_tool_calls`（校验 → `before_tool_call` → 并行/串行执行 → `after_tool_call`）；`Agent` 管状态、事件、steering/follow-up 队列、abort。
  - 并行工具的双序语义：`tool_execution_end` 按完成顺序发，结果消息按请求顺序存（写入 `results[index]`，不能用 `gather` 返回值）。
  - steering 在工具批次后注入（打断），follow-up 在模型要停时注入（续接）。
  - 事件约定：Start = 消息开始（可能流式），End = 定稿；生来完整的消息也成对发，订阅方只听 `message_end`。
  - 全程使用 `AgentMessage`，只在 provider 边界 `convert_to_llm`；`CustomMessage.include_in_context` 控制是否给模型看。
- **pi-coding-agent**：
  - `AgentSession`（`agent_session.py`）是组合点：`AgentSessionServices`（ModelRuntime + SettingsManager + ResourceLoader，按 cwd 解析一次）+ `SessionManager` + `Agent`。持久化靠订阅 agent 事件；自动压缩挂在 agent 的 `transform_context` 钩子上。
  - 模式：`_apply_mode()` 每轮先把系统提示和工具集**从 `_base_system_prompt` / `_base_tools` 基线重建**，再调用模式的 `apply`——不要在上一轮结果上叠加。
  - 会话（`session/`）：JSONL **树**（`id`/`parentId`），分支=移动 leaf；存于 `~/.pi/agent/sessions/<项目目录名>/`。磁盘格式与 TS 版 pi 保持一致（camelCase 键、v3 entry 类型），改格式会破坏互操作。compaction entry 自带 `retained_tail`，是自包含检查点。模型/思考档位从完整分支读取，而非裁剪后的上下文。
  - 工具（`tools/`）：`read write edit bash grep find ls`。参数是纯 JSON Schema dict，由 `pi_agent.validation` 校验（会把 `"3"` 纠正为 `3`）；非必填字段一律 `args.get()`；`details` 必须可 JSON 序列化（`Path` 先转 `str`）。grep/find 有 rg/fd 时用二进制，否则纯 Python 回退。所有来自 OS 的字符串先过 `text.sanitize`；bash 输出用增量解码器（中文跨块）。
  - 配置：`~/.pi/agent/` 下 `models.json`（自定义 provider/模型，`api` 字段决定协议）、`auth.json`（`{provider_id: {apiKey}}`）、`settings.json`；项目级 `<cwd>/.pi/settings.json` 深度合并覆盖全局（默认值见 `settings.py::DEFAULT_SETTINGS`）。`reasoning: true` 只是能力标记；思考档位来源：CLI `-t` > 会话记录 > settings > `off`。
- **pi-tui**：零依赖终端渲染原语（`prompt_toolkit` 为可选 extra）。**pi-app**：把 AgentSession 事件渲染到 pi-tui。
- **pi-server**：`dto.py`（事件 → SSE 帧，**前后端唯一契约**）、`registry.py`（会话注册 + 事件扇出）、`workspace.py`（历史解析、文件浏览，`resolve_within()` 越界 403）都**不 import fastapi**，`app.py` 只是薄路由层。前端优先用 `pi_server/web/`（Vite 构建产物），不存在时回退 `pi_server/static/index.html`。新增 SSE 帧类型要同时改 `dto.py` 和 `pi-web/src/App.vue` 的 `handle()`。

### 扩展系统（加功能的默认方式）

上游 pi 的立场：MCP、子代理、计划模式、权限弹窗等**不进核心，走扩展**。只有当所有场景都需要、扩展 API 做不到（如改循环控制流）、且不破坏分层时才改核心层。

扩展是导出 `activate(pi)` 的 Python 模块（可选 `NAME`、`DESCRIPTION`），按顺序从以下目录加载（`_` 开头的文件跳过）：
`pi_coding_agent/builtin_extensions/` → `~/.pi/agent/extensions/` → `<cwd>/.pi/extensions/`。

`pi`（`extensions.py::ExtensionAPI`）提供 `register_tool` / `register_command` / `register_mode` / `before_agent_start` / `transform_context` / `on(event_type)` / `events`（扩展间总线）/ `state` / `session` / `send_message`。加载或钩子异常都会被捕获并记入 `LoadedExtension.error` → `services.diagnostics`，不会让 CLI 起不来。注册项按 `api.owner` 归属到扩展。`settings.disabledExtensions` 中的扩展仍会列出但不执行 `activate`。

当前唯一的内置扩展是 `builtin_extensions/plan_mode.py`（`chat`/`plan` 模式 + `/plan` `/build` `/show_plan` 命令），可作为模板。SUMMARY.md 提到的 `subagents.py`、`mcp.py` 尚未在仓库中。

注意 `pi.register_tool` 注册的工具会进入**每个** AgentSession 的工具基线（也会出现在网页的工具选择里）。只属于某个模式的工具（如 `submit_plan`）应直接构造 `AgentTool`，在该模式的 `apply` 里挂上。内置扩展总会被加载，断言扩展列表的测试要把它们算进去。
