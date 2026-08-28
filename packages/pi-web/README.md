# pi-web

Vue 3 + Vite 前端，通过 HTTP + SSE 和 `pi-server`（FastAPI）通信。
不含任何 Python 代码，也不被任何 Python 包依赖——纯粹的表现层。

## 开发

两个进程，各跑各的：

```bash
# 终端 1：后端
pi-web-server            # 或 python -m pi_server.app
# 默认 http://127.0.0.1:8848

# 终端 2：前端
cd packages/pi-web
npm install
npm run dev              # http://localhost:5173
```

Vite 已配好 `/api` 转发到 8848，改前端不用重启后端。

## 构建

```bash
npm run build
```

产物直接落到 `packages/pi-server/pi_server/web/`，之后单独跑后端即可：

```bash
pi-web-server            # 打开 http://127.0.0.1:8848
```

也就是说**发布时只需要 Python**，Node 只在开发和构建阶段用。

## 结构

```
src/
├── api.js                 全部 HTTP/SSE 调用收在这里，组件不碰 fetch
├── App.vue                状态与 SSE 帧处理
├── styles/tokens.css      设计令牌，深浅主题
└── components/
    ├── SideBar.vue        会话历史 / 文件浏览 / 模型技能工具（三个标签页）
    ├── TurnBlock.vue      一轮：用户 → 思考 → 工具 → 正文 → 统计
    ├── ToolItem.vue       单个工具调用，可展开看 diff 或输出
    ├── Composer.vue       输入框
    └── FileViewer.vue     文件预览面板
```

## 与后端的契约

SSE 帧的形状定义在 `pi_server/dto.py`，前端只认那一份。加新帧类型时
两边一起改，`App.vue` 的 `handle()` 里加一个 case 即可。
