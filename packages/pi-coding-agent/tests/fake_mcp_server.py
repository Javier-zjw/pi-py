"""测试用的假 MCP 服务器（stdio）：一行一个 JSON-RPC 消息。test_mcp.py 用子进程起它。

工具：echo（回文字）、add（只有 structuredContent）、image（回一张图）、fail（isError）、
slow（睡很久，测中断和超时）、roots（反过来问客户端 roots/list）、crash（进程直接退出）。
tools/list 分两页返回，测翻页。启动时故意往 stdout 打一行非 JSON 的日志、往 stderr 打一行。
"""

import json
import os
import sys
import threading
import time

LOCK = threading.Lock()
PENDING: dict = {}          # 服务器发出去的请求 id → (Event, 结果)
CANCELLED: set = set()

TOOLS = [
    {"name": "echo", "description": "Echo the text back", "annotations": {"readOnlyHint": True},
     "inputSchema": {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
                     "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "add", "description": "Add two numbers",
     "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}}},
    {"name": "image", "description": "Return a tiny image", "inputSchema": {"type": "object"}},
    {"name": "fail", "description": "Always fails", "inputSchema": {"type": "object"}},
    {"name": "slow", "description": "Sleeps", "inputSchema": {"type": "object"}},
    {"name": "roots", "description": "Ask the client for roots", "inputSchema": {"type": "object"}},
    {"name": "crash", "description": "Exit the process", "inputSchema": {"type": "object"}},
]


def send(message: dict) -> None:
    with LOCK:
        sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def reply(request_id, result=None, error=None) -> None:
    message = {"jsonrpc": "2.0", "id": request_id}
    if error:
        message["error"] = error
    else:
        message["result"] = result
    send(message)


def ask_client(method: str) -> dict:
    request_id = f"srv-{time.monotonic_ns()}"
    event = threading.Event()
    PENDING[request_id] = [event, None]
    send({"jsonrpc": "2.0", "id": request_id, "method": method})
    event.wait(10)
    return PENDING.pop(request_id)[1] or {}


def call_tool(request_id, name: str, args: dict) -> None:
    text = lambda t, err=False: reply(request_id, {"content": [{"type": "text", "text": t}],
                                                   "isError": err})
    if name == "echo":
        text(f"echo: {args.get('text', '')}")
    elif name == "add":
        reply(request_id, {"content": [], "structuredContent": {"sum": args.get("a", 0) + args.get("b", 0)}})
    elif name == "image":
        reply(request_id, {"content": [{"type": "text", "text": "an image"},
                                       {"type": "image", "data": "iVBORw0KGgo=", "mimeType": "image/png"}]})
    elif name == "fail":
        text("something went wrong", err=True)
    elif name == "slow":
        for _ in range(300):
            if request_id in CANCELLED:
                return      # 收到取消：不再回复（规范要求）
            time.sleep(0.1)
        text("slow done")
    elif name == "roots":
        roots = ask_client("roots/list").get("roots") or []
        text("roots: " + ", ".join(r["uri"] for r in roots))
    elif name == "crash":
        sys.stderr.write("fatal: crashing on purpose\n")
        sys.stderr.flush()
        sys.stdout.flush()
        import os
        os._exit(3)
    else:
        reply(request_id, error={"code": -32602, "message": f"unknown tool {name}"})


def handle(message: dict) -> None:
    method = message.get("method")
    request_id = message.get("id")
    if method is None and request_id in PENDING:        # 客户端回答我们的 roots/list
        PENDING[request_id][1] = message.get("result")
        PENDING[request_id][0].set()
        return
    if method == "initialize":
        time.sleep(float(os.environ.get("FAKE_MCP_SLOW_INIT", "0")))     # 模拟 npx 下载 / 启动慢
        reply(request_id, {"protocolVersion": "2025-03-26", "capabilities": {"tools": {"listChanged": True}},
                           "serverInfo": {"name": "fake", "version": "1.0"},
                           "instructions": "Use echo to repeat things."})
    elif method == "tools/list":
        cursor = (message.get("params") or {}).get("cursor")
        if cursor == "page2":
            reply(request_id, {"tools": TOOLS[4:]})
        else:
            reply(request_id, {"tools": TOOLS[:4], "nextCursor": "page2"})
    elif method == "tools/call":
        params = message.get("params") or {}
        threading.Thread(target=call_tool, args=(request_id, params.get("name"), params.get("arguments") or {}),
                         daemon=True).start()
    elif method == "notifications/cancelled":
        CANCELLED.add((message.get("params") or {}).get("requestId"))
    elif method == "ping":
        reply(request_id, {})
    elif request_id is not None:
        reply(request_id, error={"code": -32601, "message": f"no method {method}"})


def main() -> None:
    sys.stdout.write("fake server starting (not JSON)\n")
    sys.stdout.flush()
    sys.stderr.write("fake server log line\n")
    sys.stderr.flush()
    for line in sys.stdin:
        line = line.strip()
        if line:
            handle(json.loads(line))


if __name__ == "__main__":
    main()
