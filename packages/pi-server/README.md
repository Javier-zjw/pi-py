# pi-server

顶层之一：把 coding agent 暴露成 HTTP + SSE。和 `pi-app`（终端界面）平级，
两者互不认识，共享的只有下面三层和 `~/.pi/agent/` 里的数据。

```
pi-app    ──→ pi-coding-agent ──→ pi-agent ──→ pi-ai
   └────→ pi-tui
pi-server ──→ pi-coding-agent
```

## 安装

```bash
pip install -e packages/pi-server
```

## 启动

```bash
pi-web                          # 当前目录
pi-web -C ~/projects/myapp      # 指定工作目录
pi-web --port 9000
pi-web --host 0.0.0.0           # 局域网，慎用（见下）
pi-web --agent-dir ~/.pi/agent
```

默认 `http://127.0.0.1:8848`。

前端有两种来源，`app.py` 会自动选：

1. `pi_server/web/index.html` 存在 → 用 Vite 构建产物（`packages/pi-web` 里 `npm run build` 的结果）
2. 否则 → 回退到 `pi_server/static/index.html`（免构建的单文件 Vue 页面）

所以**没构建前端也能直接用**。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/meta` | cwd、模型、工具、技能、扩展加载诊断 |
| GET | `/api/sessions` | 活跃会话与最近会话 |
| POST | `/api/sessions` | 新建或恢复会话，body 带 tools/skills 勾选 |
| GET | `/api/sessions/{id}` | 会话快照 |
| DELETE | `/api/sessions/{id}` | 关闭会话 |
| POST | `/api/sessions/{id}/prompt` | 发一轮，立即返回 |
| GET | `/api/sessions/{id}/events` | **SSE 事件流** |
| POST | `/api/sessions/{id}/abort` | 中断 |
| POST | `/api/sessions/{id}/compact` | 压缩上下文 |
| POST | `/api/sessions/{id}/model` `/thinking` | 切模型 / 思考档位 |
| GET | `/api/sessions/{id}/messages` | 当前上下文里的消息 |
| GET | `/api/history` | 历史会话列表（标题、轮数、时间） |
| GET | `/api/history/detail?file=` | 解析某个 .jsonl 成可渲染的轮次 |
| GET | `/api/files?path=` | 列目录 |
| GET | `/api/files/content?path=` | 读文件 |

用 curl 直接观察事件流：

```bash
curl -N http://127.0.0.1:8848/api/sessions/<id>/events
```

## SSE 帧格式

契约定义在 `dto.py`，前端只认这一份：

```
{"type":"turn_start","at":1723...}
{"type":"thinking_delta","text":"..."}
{"type":"text_start"}                          ← 正文开始，前端据此折叠过程
{"type":"text_delta","text":"..."}
{"type":"tool_start","id":..,"name":"read","summary":"a.txt"}
{"type":"tool_update","id":..,"preview":"..."}
{"type":"tool_end","id":..,"ok":true,"preview":"...","patch":"..."}
{"type":"injected","text":"..."}               ← 途中插入的消息
{"type":"message_end","usage":{...},"elapsed":4.2}
{"type":"done","reason":"stop"}
```

## 文件

```
pi_server/
├── dto.py         事件 → 线上 JSON。纯函数，不 import fastapi，可单测
├── registry.py    会话注册表 + 事件扇出。纯 asyncio，不 import fastapi
├── workspace.py   历史会话解析 + 文件浏览（带越界防护）
├── app.py         FastAPI 路由，薄
├── static/        免构建的单文件 Vue 页面（回退用）
└── web/           Vite 构建产物落点（初始为空）
```

前三个刻意不依赖 web 框架，所以没装 fastapi 也能 import 本包做单测——
`tests/test_all.py` 里 14 个 server 断言就是这么跑的。

## 安全提醒

这个服务能执行 bash、读写文件。`--host 0.0.0.0` 等于把一个高权限 agent
挂到网上。需要远程访问就走 SSH 隧道：

```bash
ssh -L 8848:127.0.0.1:8848 你的服务器
```

文件浏览有边界：`workspace.resolve_within()` 先 resolve 再比对，越界一律 403，
不是通用文件系统浏览器。
