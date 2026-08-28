# pi-ai

## 定位：原子层（atomic layer）

核心职责：一问一答 —— **给定一份 `Context`，获取模型输出内容。**

本层不包含：调用循环、工具执行逻辑、文件系统业务、会话管理。
依赖约束：无强制依赖（`httpx` 采用懒加载导入，仅在真正发起网络请求时才加载）。

## 最简示例代码
```python
import asyncio
from pi_ai import Context, Models, UserMessage, anthropic_provider, get_model

async def main():
    models = Models()
    models.set_provider(anthropic_provider())
    model = get_model("anthropic", "claude-sonnet-4-5")

    context = Context(system_prompt="Be terse.", messages=[UserMessage(content="hi")])
    async for event in models.stream_simple(model, context):
        if event.type == "text_delta":
            print(event.delta, end="", flush=True)

asyncio.run(main())
```
## 内部模块清单

- **types**
内容块、消息结构、Token 用量与费用、`Context` 请求上下文、模型信息、工具定义等全部原子类型。
- **events**
定义 12 种标准化流式事件类型。
- **providers**
  - `anthropic-messages`：Anthropic 服务商适配器
  - `openai-completions`：OpenAI 适配器，兼容所有遵循 OpenAI 协议的端点
- **models**
运行时注册表；对外提供 `stream` / `stream_simple` / `complete` 调用入口。
- **auth**
凭证存储 `CredentialStore`；支持环境变量、本地文件、内存存储、链式多层存储组合。
- **cost**
Token 计价、成本估算相关逻辑。
- **serde**
JSON 序列化与反序列化，遵循 pi 磁盘存储格式（传输 JSON 使用小驼峰键名）。
- **transport**`HttpTransport` HTTP 请求抽象协议 + SSE 流式解析器。

## 架构边界要点

pi-ai 作为底层原子内核，仅负责单次独立 LLM 请求；
智能体循环、会话持久化、多轮工具调用等复杂业务逻辑，全部在上层包实现，不在本层范围内。