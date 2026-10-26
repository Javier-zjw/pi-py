"""MCP 的三种传输，都只管"把 JSON-RPC 消息送过去、把收到的交给 on_message"：

- ``StdioTransport``：起一个子进程，一行一个 JSON（本地服务器，npx / uvx / docker / python ...）；
- ``HttpTransport``：Streamable HTTP（2025 起的远程标准）：每条消息 POST，回复是 JSON 或一段 SSE；
- ``SseTransport``：老的 HTTP+SSE：先 GET 一条长连接拿到 endpoint，消息 POST 到 endpoint，
  回复从长连接里来。还有不少远程服务器只支持它。

HTTP 两种经过可替换的 ``Opener``（默认 httpx 懒加载），测试换成假的，不用起服务器。
"""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import shutil
import signal
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urljoin

OnMessage = Callable[[dict], Awaitable[None]]
OnClose = Callable[[str], None]
STDOUT_LIMIT = 64 * 1024 * 1024     # 一行一条消息：工具结果可能很大，默认 64KB 的行长上限不够
STDERR_LINES = 40


class TransportError(Exception):
    pass


async def deliver(on_message: OnMessage, message: dict) -> None:
    """处理一条消息出错不能让后面的消息丢掉（读循环一停，等着的请求就只能超时）"""
    try:
        await on_message(message)
    except Exception:  # noqa: BLE001, S110
        pass


class Transport(Protocol):
    async def start(self, on_message: OnMessage, on_close: OnClose) -> None: ...
    async def send(self, message: dict) -> None: ...
    async def close(self) -> None: ...


# -- stdio --------------------------------------------------------------- #

_LIVE_PIDS: set[int] = set()


@atexit.register
def _kill_leftovers() -> None:
    """pi 退出时还活着的服务器进程一并结束（事件循环已经关了，只能直接发信号）"""
    for pid in list(_LIVE_PIDS):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def resolve_command(command: str) -> str | None:
    """找可执行文件。Windows 上 npx 实际是 npx.cmd，which 会按 PATHEXT 补全"""
    if os.path.dirname(command):
        return command if os.path.exists(command) else None
    return shutil.which(command)


INSTALL_HINTS = {
    "npx": "先安装 Node.js（https://nodejs.org）", "node": "先安装 Node.js（https://nodejs.org）",
    "uvx": "先安装 uv（https://docs.astral.sh/uv/）", "uv": "先安装 uv（https://docs.astral.sh/uv/）",
    "docker": "先安装并启动 Docker", "bunx": "先安装 Bun（https://bun.sh）",
}


class StdioTransport:
    def __init__(self, command: str, args: list[str], env: dict[str, str] | None = None,
                 cwd: str | None = None) -> None:
        self.command, self.args = command, args
        self.env, self.cwd = env or {}, cwd or None
        self.process: asyncio.subprocess.Process | None = None
        self.stderr: deque[str] = deque(maxlen=STDERR_LINES)
        self._tasks: list[asyncio.Task] = []
        self._closing = False

    async def start(self, on_message: OnMessage, on_close: OnClose) -> None:
        exe = resolve_command(self.command)
        if exe is None:
            hint = INSTALL_HINTS.get(os.path.basename(self.command).lower(), "检查命令是否写对、是否在 PATH 里")
            raise TransportError(f"找不到命令 {self.command}：{hint}")
        try:
            self.process = await asyncio.create_subprocess_exec(
                exe, *self.args, cwd=self.cwd, env={**os.environ, **self.env},
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=STDOUT_LIMIT)
        except OSError as exc:
            raise TransportError(f"启动 {self.command} 失败：{exc}") from exc
        _LIVE_PIDS.add(self.process.pid)
        self._tasks = [asyncio.ensure_future(self._read(on_message, on_close)),
                       asyncio.ensure_future(self._drain_stderr())]

    async def _read(self, on_message: OnMessage, on_close: OnClose) -> None:
        assert self.process and self.process.stdout
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text.startswith("{"):
                    continue        # 有的服务器把日志打到 stdout：不是 JSON 的行跳过
                try:
                    message = json.loads(text)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    await deliver(on_message, message)
        except (asyncio.CancelledError, ValueError):
            pass
        finally:
            if not self._closing:
                code = self.process.returncode if self.process else None
                tail = " | ".join(list(self.stderr)[-3:])
                on_close(f"服务器进程退出了（{code if code is not None else '输出关闭'}）"
                         + (f"：{tail}" if tail else ""))

    async def _drain_stderr(self) -> None:
        """stderr 一定要读走：管道写满了服务器会卡死。留最后几行，出错时给人看"""
        assert self.process and self.process.stderr
        try:
            while True:
                line = await self.process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    self.stderr.append(text)
        except (asyncio.CancelledError, ValueError):
            pass

    async def send(self, message: dict) -> None:
        if not self.process or not self.process.stdin or self.process.returncode is not None:
            raise TransportError("服务器进程已经退出")
        data = json.dumps(message, ensure_ascii=False) + "\n"
        try:
            self.process.stdin.write(data.encode("utf-8"))
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise TransportError("服务器进程已经退出") from exc

    async def close(self) -> None:
        self._closing = True
        process = self.process
        if process is None:
            return
        if process.returncode is None:
            try:
                if process.stdin:
                    process.stdin.close()       # 规范的关法：关 stdin，服务器自己退
                await asyncio.wait_for(process.wait(), 2)
            except (asyncio.TimeoutError, OSError):
                for stop in (process.terminate, process.kill):
                    try:
                        stop()
                        await asyncio.wait_for(process.wait(), 2)
                        break
                    except (asyncio.TimeoutError, ProcessLookupError, OSError):
                        continue
        _LIVE_PIDS.discard(process.pid)
        for task in self._tasks:
            task.cancel()


# -- HTTP：可替换的底层 ------------------------------------------------------ #

@dataclass
class OpenedResponse:
    status: int
    headers: dict[str, str]                 # 键小写
    lines: AsyncIterator[str]               # 按行读响应体（SSE 要边到边读）
    close: Callable[[], Awaitable[None]]
    body_text: Callable[[], Awaitable[str]]  # 一次读完（并关闭）


Opener = Callable[[str, str, dict[str, str], bytes | None], Awaitable[OpenedResponse]]


async def httpx_opener(method: str, url: str, headers: dict[str, str],
                       body: bytes | None) -> OpenedResponse:
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover
        raise TransportError("缺少 httpx：pip install httpx") from exc
    # 读超时不设：SSE 长连接可以很久没有数据；单次调用的超时由客户端按请求控制
    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None), follow_redirects=True)
    try:
        response = await client.send(client.build_request(method, url, headers=headers, content=body),
                                     stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise TransportError(f"连不上 {url}：{type(exc).__name__} {exc}".strip()) from exc

    async def close() -> None:
        await response.aclose()
        await client.aclose()

    async def body_text() -> str:
        try:
            return (await response.aread()).decode("utf-8", "replace")
        finally:
            await close()

    return OpenedResponse(response.status_code, {k.lower(): v for k, v in response.headers.items()},
                          response.aiter_lines(), close, body_text)


async def sse_events(lines: AsyncIterator[str]) -> AsyncIterator[tuple[str, str]]:
    """SSE：event: / data: 行攒到空行为止，产出 (事件名, 数据)"""
    event, data = "message", []
    async for line in lines:
        line = line.rstrip("\r")
        if not line:
            if data:
                yield event, "\n".join(data)
            event, data = "message", []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        yield event, "\n".join(data)


def _http_problem(status: int, text: str) -> str:
    if status in (401, 403):
        return (f"HTTP {status}：服务器要求授权。在配置的 headers 里加上 token，"
                "例如 \"headers\": {\"Authorization\": \"Bearer ${MY_TOKEN}\"}")
    return f"HTTP {status}：{' '.join(text.split())[:200]}"


class HttpTransport:
    """Streamable HTTP：每条消息一次 POST；回复是 JSON 或 SSE 流"""

    def __init__(self, url: str, headers: dict[str, str] | None = None,
                 opener: Opener = httpx_opener) -> None:
        self.url, self.headers, self.opener = url, headers or {}, opener
        self.session_id = ""
        self.protocol_version = ""
        self._on_message: OnMessage | None = None
        self._streams: set[asyncio.Task] = set()

    async def start(self, on_message: OnMessage, on_close: OnClose) -> None:
        self._on_message = on_message

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   **self.headers}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    async def send(self, message: dict) -> None:
        body = json.dumps(message, ensure_ascii=False).encode("utf-8")
        response = await self.opener("POST", self.url, self._headers(), body)
        if response.headers.get("mcp-session-id"):
            self.session_id = response.headers["mcp-session-id"]
        if response.status >= 400:
            text = await response.body_text()
            if response.status == 404 and self.session_id:
                raise TransportError("服务器端的会话过期了：用 /mcp reconnect 重新连接")
            raise TransportError(_http_problem(response.status, text))
        kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if kind == "text/event-stream":
            # 回复在流里，可能夹着服务器的通知 / 请求：后台读，send 先返回
            task = asyncio.ensure_future(self._consume(response))
            self._streams.add(task)
            task.add_done_callback(self._streams.discard)
            return
        text = await response.body_text()
        if kind == "application/json" and text.strip():
            data = json.loads(text)
            for item in data if isinstance(data, list) else [data]:
                if isinstance(item, dict) and self._on_message:
                    await deliver(self._on_message, item)

    async def _consume(self, response: OpenedResponse) -> None:
        try:
            async for _, data in sse_events(response.lines):
                try:
                    message = json.loads(data)
                except ValueError:
                    continue
                if isinstance(message, dict) and self._on_message:
                    await deliver(self._on_message, message)
        except Exception:  # noqa: BLE001, S110 —— 流断了：等着的请求会按超时报错
            pass
        finally:
            await response.close()

    async def close(self) -> None:
        for task in list(self._streams):
            task.cancel()
        if self.session_id:
            # 规范：结束时 DELETE 会话；服务器不支持（405）也无所谓
            try:
                response = await self.opener("DELETE", self.url, self._headers(), None)
                await response.close()
            except Exception:  # noqa: BLE001, S110
                pass


class SseTransport:
    """老的 HTTP+SSE：GET 长连接收消息，第一条 endpoint 事件告诉往哪 POST"""

    def __init__(self, url: str, headers: dict[str, str] | None = None,
                 opener: Opener = httpx_opener, endpoint_timeout: float = 15.0) -> None:
        self.url, self.headers, self.opener = url, headers or {}, opener
        self.endpoint_timeout = endpoint_timeout
        self.endpoint = ""
        self._task: asyncio.Task | None = None
        self._response: OpenedResponse | None = None
        self._closing = False

    async def start(self, on_message: OnMessage, on_close: OnClose) -> None:
        response = await self.opener("GET", self.url, {"Accept": "text/event-stream", **self.headers}, None)
        if response.status >= 400:
            text = await response.body_text()
            raise TransportError(_http_problem(response.status, text))
        self._response = response
        found: asyncio.Future = asyncio.get_running_loop().create_future()

        async def listen() -> None:
            try:
                async for event, data in sse_events(response.lines):
                    if event == "endpoint":
                        if not found.done():
                            found.set_result(urljoin(self.url, data.strip()))
                        continue
                    try:
                        message = json.loads(data)
                    except ValueError:
                        continue
                    if isinstance(message, dict):
                        await deliver(on_message, message)
            except Exception:  # noqa: BLE001, S110
                pass
            finally:
                if not found.done():
                    found.set_exception(TransportError("SSE 连接在给出 endpoint 之前就断了"))
                elif not self._closing:
                    on_close("SSE 连接断开了")

        self._task = asyncio.ensure_future(listen())
        try:
            self.endpoint = await asyncio.wait_for(found, self.endpoint_timeout)
        except asyncio.TimeoutError as exc:
            await self.close()
            raise TransportError("SSE 服务器没有给出 endpoint：可能不是 SSE 服务器，试试 \"type\": \"http\"") \
                from exc

    async def send(self, message: dict) -> None:
        body = json.dumps(message, ensure_ascii=False).encode("utf-8")
        response = await self.opener("POST", self.endpoint,
                                     {"Content-Type": "application/json", **self.headers}, body)
        text = await response.body_text()
        if response.status >= 400:
            raise TransportError(_http_problem(response.status, text))

    async def close(self) -> None:
        self._closing = True
        if self._task:
            self._task.cancel()
        if self._response:
            try:
                await self._response.close()
            except Exception:  # noqa: BLE001, S110
                pass
