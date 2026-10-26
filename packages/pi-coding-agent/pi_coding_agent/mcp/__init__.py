"""MCP（Model Context Protocol）客户端：把别人写好的 MCP 服务器接进来当 pi 的工具。

- config：配置格式（和 Claude Desktop / Cursor / Claude Code 一样的 mcpServers）、从别的客户端导入；
- transport：stdio / Streamable HTTP / 老的 SSE 三种传输；
- client：JSON-RPC 握手、列工具、调工具；
- manager：一组服务器的连接和状态，把工具变成 AgentTool（名字 mcp__服务器__工具）。

工具在内置扩展 ``builtin_extensions/mcp.py`` 里挂进会话。纯 Python（httpx + 标准库），不依赖官方 SDK。
"""
