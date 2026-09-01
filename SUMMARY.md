# pi-py 项目工作摘要

> 用于在新对话中继续工作的上下文交接文档。

---

## 一、项目定位

将 TypeScript 项目 [earendil-works/pi](https://github.com/earendil-works/pi)（原 badlogic/pi-mono）用 Python 重构。核心约束：**严格分层，下层不能访问上层，每层是独立可发布单元**。

---

## 二、架构

```
pi-app    ──→ pi-coding-agent ──→ pi-agent ──→ pi-ai
   └────→ pi-tui
pi-server ──→ pi-coding-agent
pi-web（Vue 3 + Vite，纯前端，HTTP/SSE 通信）
```

双根 DAG，两个零依赖的根：`pi-ai` 和 `pi-tui`。

| 包 | 职责 | 依赖 |
|---|---|---|
| **pi-ai** | 原子层：一次 LLM 调用。类型、流式事件、provider、凭证、成本 | 无 |
| **pi-agent** | 分子层：循环、工具管线、钩子、事件、队列 | pi-ai |
| **pi-coding-agent** | 合成层：工具、会话树、压缩、技能、扩展、设置、CLI | pi-agent |
| **pi-tui** | 终端渲染原语（零依赖，谁都不认识） | 无 |
| **pi-app** | 终端界面：事件 → 屏幕 | pi-coding-agent + pi-tui |
| **pi-server** | HTTP + SSE API | pi-coding-agent |
| **pi-web** | Vue 前端 | 无 Python 依赖 |

三个入口：`pi`（朴素 CLI）、`pi-tui`（终端 UI）、`pi-web`（网页）。

### 分层的四道锁

1. **包边界** —— `pi-ai` 的 `dependencies = []`，单独安装必须能跑通
2. **AST 检查** —— `tests/test_all.py::test_layering` 解析所有 `.py` 的 import 节点
3. **import-linter 契约**（`.importlinter`）—— layers 契约 + `pi_tui` 不认识任何 `pi_*` + `pi_coding_agent` 不许依赖 `pi_tui`/`pi_app`/`pi_server`
4. **依赖倒置** —— `StreamFn`、`Provider`、`HttpTransport`、`CredentialStore`、`AgentTool.execute`、`ResourceLoader`

`StreamFn` 是最关键的倒置点：测试用 20 行假 generator 就能替换整个网络栈。

---

## 三、⭐ 扩展开发指南（后续加功能主要看这里）

### 决策树：新功能放哪一层

```
需要发 HTTP 给模型？                     → pi-ai（新 provider）
只是循环行为的变化（钩子、队列、终止条件）？ → pi-agent
要碰文件系统/进程/配置目录？               → pi-coding-agent
只是渲染方式？                           → pi-tui / pi-app / pi-web
用户可选、非核心、可能被别人复用？          → **扩展**（默认答案）
```

上游 pi 明确表态：MCP、子智能体、计划模式、权限弹窗、待办列表、后台 bash **都不内置**，全靠扩展。我们照搬这个决定。

### 什么时候才该改核心层

必须同时满足三条：

1. 所有使用场景都需要它（不是可选功能）
2. 用扩展 API 做不到（比如要改循环的控制流）
3. 改动不会让某一层认识到它不该认识的东西

### 扩展 API 全貌

扩展是一个 Python 模块，放在下列任一目录，导出 `activate(pi)`：

```
pi_coding_agent/builtin_extensions/   随包分发的内置扩展
~/.pi/agent/extensions/               全局扩展
<项目>/.pi/extensions/                项目扩展（同名时覆盖前面的）
```

```python
NAME = "my-extension"
DESCRIPTION = "一句话说明"     # 会显示在网页的扩展面板

def activate(pi):
    # 注册工具（JSON Schema 描述参数）
    pi.register_tool(name, description, parameters, execute, label=None)

    # 注册斜杠命令（终端和网页共用）
    pi.register_command(name, description, handler, usage="")

    # 注册工作模式（界面会显示成可切换的选项）
    pi.register_mode(id, label, description="", apply=None, badge="")

    # 每轮 prompt 开始前调用，入参是 AgentSession
    # 可改系统提示、换工具集、注入消息
    pi.before_agent_start(handler)

    # 每次 LLM 调用前改上下文消息列表
    pi.transform_context(handler)

    # 订阅 agent 事件（按 event.type）
    pi.on("agent_end", handler)

    # 扩展之间通信
    pi.events.on("topic", handler)
    pi.events.emit("topic", data)

    # 跨调用状态、上下文
    pi.state["key"] = ...
    pi.cwd, pi.session
    await pi.send_message(text)     # 驱动 agent
```

**加载失败不会让 CLI 起不来** —— 无 `activate`、语法错误、初始化抛异常都会被记进 `LoadedExtension.error`，汇总到 `services.diagnostics` 展示给用户。

**注册项归属追踪** —— 加载时 `api.owner` 记录当前扩展名，注册的每一项都归属到它，所以界面能显示"这个扩展提供了 3 个工具 2 个命令"，停用时也能精确移除。

### 已实现的三个内置扩展（可作模板）

**`builtin_extensions/plan_mode.py`** —— 计划模式，真正落地的三件事：
- 工具层面**强制只读**（把 write/edit/bash 从工具集移除，不是靠提示词请求）
- 计划落进会话记录（custom entry），刷新页面、换终端都还在
- `/build` 一键执行：切回聊天模式 + 把计划渲染成任务发出去

关键实现：`register_mode(apply=...)` + `AgentSession._apply_mode()` 每轮从基线重建（不叠加，否则来回切几次系统提示会越滚越长）。

**`builtin_extensions/subagents.py`** —— 子代理，价值在**上下文隔离**：
- 复用父会话的 `stream_fn`，造一个独立 `AgentState` 的 `Agent`
- 并发上限 3、单个 15 轮 / 300 秒超时、父会话中断时子代理跟着停
- 只回传结论，中间过程留在子上下文里

**`builtin_extensions/mcp.py`** —— MCP 客户端（JSON-RPC over stdio）：
- MCP 的工具定义就是 JSON Schema，和 `AgentTool.parameters` 完全同构，转换零成本
- 懒启动、自动重连（上限 3 次）、单次调用 60 秒超时、会话结束清理子进程
- 配置在 `<项目>/.pi/mcp.json` 或 `~/.pi/agent/mcp.json`

### 扩展的启用/停用

设置项 `disabledExtensions`（存 `~/.pi/agent/settings.json`）。停用的扩展仍会被列出（界面要显示它存在），但**不执行 `activate`**，所以它注册的一切都不生效。切换后活跃会话的工具集会一起重建，不用新建会话。

### 加功能后的自检

```bash
python tests/test_all.py     # test_layering 会抓到反向 import
lint-imports                 # 五个包的契约
```

---

## 四、各层关键设计

### pi-ai

- `Context` 刻意可序列化 → 会话能存、能换 provider 重放
- `stream`/`complete`（provider 原生）vs `stream_simple`/`complete_simple`（统一 `reasoning` 字段）
- `HttpTransport` 是 Protocol，httpx 在函数内延迟 import → 整层可脱离网络栈测试
- 凭证链优先级：运行时 override > `auth.json` > 环境变量

### pi-agent

- 全程用 `AgentMessage`，只在 provider 边界 `convert_to_llm`
- `CustomMessage.include_in_context` 是上层扩展点
- **并行工具的双序语义**：`tool_execution_end` 按完成顺序发，结果消息按 assistant 请求顺序存（靠 `results[index]`，**不能用 `gather` 返回值**）
- 两个队列语义不同：steering 在工具批次后注入（打断）、follow-up 在模型要停时注入（续接）
- 事件约定：**Start = 消息开始（可能有流式），End = 定稿**。生来完整的消息（用户输入、steering）也成对发，订阅方只监听 `message_end`

### pi-coding-agent

- 会话是 JSONL **树**（`id`/`parentId`），分支即移动 leaf，两条路共存一个文件
- compaction entry 自带 `retained_tail` → 自包含检查点，重建上下文不用回头翻
- **模型/思考档位从完整分支读，不是从裁剪后的上下文读**
- 技能机制：描述进系统提示、正文按需读（省上下文）
- `text.py` 编码边界：所有 OS 来的字符串先过 `sanitize`
- `AgentSession` 持有 `_base_system_prompt` 和 `_base_tools` 基线，模式在基线上做增减

---

## 五、踩过的坑（复现时注意）

| 现象 | 根因 |
|---|---|
| `list[X] \| None` 类型报错 | `try` 里 `return` + `finally`，PyCharm 推断盲区。把 `return` 挪到 `try` 外 |
| 第二轮模型不输出 | `Agent.prompt` 的 `finally` 写成 `self._cancel.set()`，应是 `self._idle.set()` |
| 未知工具时崩溃 | `execute_tool_calls` 的 `tool is None` 分支漏了 `continue` |
| `except {A, B}` 报 TypeError | 花括号是集合，必须用元组 `(A, B)` |
| 坏 JSON 没被 catch | `UnicodeDecodeError` 是 `ValueError` 子类，**不是** `JSONDecodeError` 子类 |
| usage 永远是 0 | `anthropic.py` 里 `content_block_stop`/`message_delta`/`error` 三个 `elif` 多缩进一级，永远进不去 |
| `ls` 缺参数 KeyError | schema 里非必填的字段一律用 `args.get()` |
| `details` 无法序列化 | 里面不能有 `Path` 对象，写入前 `str(path)` |
| 中文每 4KB 烂一个字 | bash 逐块 `decode()`，需跨块保留状态的增量解码器 |
| 中文退格要按两下 | `input()` 不懂宽字符，用 prompt_toolkit |
| 用户输入打两遍 | 输入层已回显，渲染器又画了一次；网页端是 `message_end` 的 user 消息被当成"插话" |
| 整页滚动 / 文件预览不能滚 | flex 子元素缺 `min-height: 0`，`overflow` 不生效 |
| 网络错误静默失败 | httpx 异常没包成 `LLMError`；前端错误挂在 meta 的 `v-if` 下被隐藏 |
| 思考档位/工具改了不生效 | 只在新建会话时用了一次，需要 watch + 调对应接口 |
| 压缩后 token 不降 | 前端要用 `contextBase` 标记压缩检查点，忽略之前的轮次 |

---

## 六、测试体系

每层一套离线测试，零依赖零网络，**同时支持 `python xxx.py` 和 pytest**（async 用例套同步壳，`check()` 在 pytest 下抛 `AssertionError`）。

| 层 | 文件 | 断言 |
|---|---|---|
| pi-ai | `tests/test_offline.py` + `tests/manual/{smoke,tap,chat}.py` | 41 |
| pi-agent | `tests/test_offline.py` + `tests/manual/live_agent.py` | 76 |
| coding-agent | `test_tools.py` / `test_session.py` / `test_resources.py` / `test_runtime.py` / `test_agent_session.py` | 125/123/96/60/78 |
| 全局 | `tests/test_all.py`（含 `test_layering`） | 81 |

**每套都做过变异测试**（故意改坏实现验证测试会红）。两次因此发现盲区并补齐：压缩的 `retained_tail` 优先级、`reload()` 的状态重建。

调试工具：`tests/manual/tap.py record|replay|show` —— 联网录一次 SSE，之后离线回放随便打断点。

---

## 七、配置格式

`~/.pi/agent/models.json`：
```json
{
  "providers": [{"id": "ark-planing", "name": "方舟",
                 "baseUrl": "https://ark.cn-beijing.volces.com/api/plan"}],
  "models": [{"id": "glm-5.2", "provider": "ark-planing", "api": "anthropic-messages",
              "contextWindow": 200000, "maxTokens": 16384, "reasoning": true,
              "baseUrl": "https://ark.cn-beijing.volces.com/api/plan"}]
}
```

`~/.pi/agent/auth.json`：`{"ark-planing": {"apiKey": "..."}}`

`<项目>/.pi/mcp.json`：
```json
{"servers": {"filesystem": {"command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path"], "enabled": true}}}
```

⚠️ `ModelRuntime.create` 需按 `api` 字段选协议（原版写死了 `openai_compatible_provider`），且 `AnthropicProvider.id` 硬编码为 `"anthropic"`，自定义 provider 必须改名。

⚠️ `reasoning: true` 是**能力标记**，不是开关；档位来源：CLI `-t` > 会话记录 > settings > `off`。默认会思考的模型（GLM）必须显式发 `thinking: {"type": "disabled"}`。

---

## 八、Web 端

**后端** `pi-server`：28 个路由。`dto.py`/`registry.py`/`workspace.py` 不 import fastapi（可脱离框架单测）。SSE 帧契约定义在 `dto.py`。

**前端** `pi-web`：Vite + Vue 3 SFC。开发 `npm run dev`（5173 代理到 8848），`npm run build` 产物落进 `pi_server/web/`，之后**只要 Python**。

已实现：会话历史浏览、文件浏览与预览（文本可缩放、图片/PDF 内联、Office 提示下载、越界 403）、工作目录弹窗选择器、模型/技能/工具中途可切、历史隐藏（不删磁盘文件）、配置持久化 localStorage、上下文用量进度条、**扩展面板（开关启停）**、**模式切换（聊天/计划）**、**斜杠命令补全（↑↓ 选择、Tab 补全）**、**计划执行入口**。

SSE 帧类型：`turn_start` `thinking_delta` `text_start` `text_delta` `tool_start` `tool_update` `tool_end` `injected` `message_end` `done` `error` `aborted` `compacted` `mode` `notice`

---

## 九、待办候选

1. Office 文档预览（需 python-docx / openpyxl）
2. skills 安装器 → 吃 pi 生态 5000+ 包里的 skill 部分（pi 的 skills/prompts 格式和我们完全一致，可直接拷用）
3. Google provider、`/tree` 分支导航、主题系统
4. 扩展的 `session_start` / `session_shutdown` 钩子
5. provider 层钩子（`before_provider_request` / `after_provider_response`）

**打包**：PyInstaller 需用干净 venv（非 conda），macOS 加 `target_arch="arm64"`、`upx=False`、ad-hoc 签名；产物 16–22MB。目标机有 Python 的话用 `shiv` 只需 2–3MB。

---

## 十、与官方 pi 的关系

**格式完全对齐**（可互操作）：会话 JSONL、消息结构、SKILL.md、提示模板、AGENTS.md、目录约定。**第三方 pi 包里的 skills/prompts 可以直接拷进 `.pi/skills/` 使用。**

**结构性差异**：官方把会话/压缩放在 agent 包（AgentHarness），我们放在 coding-agent —— 让 `pi_agent` 保持纯循环，可被非编码 agent 复用。

**未实现**：provider 只有 2 个（官方 15+）、订阅登录、模型目录自动生成、主题系统、RPC 模式、`pi install` 包管理、HTML 导出。
