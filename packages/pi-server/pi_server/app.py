"""FastAPI 应用。

薄薄一层：路由只做参数校验和调用 registry，没有业务逻辑。事件流走 SSE，
不用 WebSocket——单向推送用 SSE 更简单，断线浏览器自动重连，还能被 curl
直接观察。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .dto import sse_frame
from .registry import SessionRegistry
from .workspace import (
    AccessDenied,
    hide_session,
    list_directory,
    list_session_files,
    list_subdirectories,
    list_workspaces,
    quick_locations,
    raw_file,
    read_file,
    read_session_file,
    unhide_all,
)

STATIC_DIR = Path(__file__).parent / "static"


class CreateSessionRequest(BaseModel):
    model: str | None = None
    thinking: str = "off"
    tools: list[str] | None = None
    skills: list[str] | None = None
    resume: str | None = None
    persist: bool = True
    cwd: str | None = None


class PromptRequest(BaseModel):
    text: str = Field(min_length=1)


class ToolsRequest(BaseModel):
    tools: list[str] | None = None
    skills: list[str] | None = None


class CommandRequest(BaseModel):
    name: str
    rest: str = ""


def create_app(cwd: str | Path = ".", agent_dir: str | Path | None = None) -> FastAPI:
    registry = SessionRegistry(cwd=cwd, agent_dir=agent_dir)
    app = FastAPI(title="pi web", version="0.1.0")

    # 本地工具，前后端分离开发时前端跑在 5173
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def require(session_id: str):
        live = registry.get(session_id)
        if live is None:
            raise HTTPException(404, "会话不存在")
        return live

    # -- 元信息 ---------------------------------------------------- #

    @app.get("/api/meta")
    def meta(cwd: str | None = None) -> dict[str, Any]:
        target = cwd or registry.cwd
        return {
            "cwd": target,
            "models": registry.list_models(),
            "tools": registry.list_tools(target),
            "skills": registry.list_skills(target),
            "diagnostics": registry.services_for(target).diagnostics,
            "extensions": registry.list_extensions(target),
            "modes": registry.list_modes(target),
            "commands": registry.list_commands(target),
        }

    # -- 扩展 ------------------------------------------------------ #

    @app.get("/api/extensions")
    def extensions(cwd: str | None = None) -> dict[str, Any]:
        return {"extensions": registry.list_extensions(cwd)}

    @app.post("/api/extensions/{key}")
    def toggle_extension(key: str, enabled: bool = Body(..., embed=True),
                         cwd: str | None = None) -> dict[str, Any]:
        return {"extensions": registry.toggle_extension(key, enabled, cwd)}

    # -- 工作目录 -------------------------------------------------- #

    @app.get("/api/workspaces")
    def workspaces() -> dict[str, Any]:
        """用过的工作目录，用于快速切换。"""
        return {"current": registry.cwd, "workspaces": list_workspaces(registry.agent_dir)}

    @app.get("/api/workspaces/browse")
    def browse_dirs(path: str = Query("~")) -> dict[str, Any]:
        """只列子目录，供选择工作目录用。附带面包屑和常用入口。"""
        result = list_subdirectories(path)
        parts, walk = [], Path(result["path"])
        while True:
            parts.append({"name": walk.name or "/", "path": str(walk)})
            if walk.parent == walk:
                break
            walk = walk.parent
        result["breadcrumb"] = list(reversed(parts))
        result["quick"] = quick_locations()
        return result

    @app.post("/api/workspaces")
    def set_workspace(cwd: str = Body(..., embed=True)) -> dict[str, Any]:
        try:
            current = registry.set_cwd(cwd)
        except NotADirectoryError as exc:
            raise HTTPException(400, f"不是一个目录：{cwd}") from exc
        return {"cwd": current}

    @app.get("/api/sessions")
    def list_sessions(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
        return {"recent": registry.list_recent(limit)}

    # -- 会话 ------------------------------------------------------ #

    @app.post("/api/sessions")
    def create_session(body: CreateSessionRequest = Body(...)) -> dict[str, Any]:
        try:
            live = registry.create(
                model=body.model,
                thinking_level=body.thinking,
                tools=body.tools,
                skills=body.skills,
                resume=body.resume,
                persist=body.persist,
                cwd=body.cwd,
            )
        except Exception as exc:
            raise HTTPException(400, f"{type(exc).__name__}: {exc}") from exc
        if live.session.model is None:
            registry.close(live.id)
            raise HTTPException(400, "没有可用模型：检查 auth.json 或环境变量")
        return registry.snapshot(live)

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str) -> dict[str, Any]:
        return registry.snapshot(require(session_id))

    @app.delete("/api/sessions/{session_id}")
    def delete_session(session_id: str) -> dict[str, Any]:
        return {"closed": registry.close(session_id)}

    @app.post("/api/sessions/{session_id}/prompt")
    async def prompt(session_id: str, body: PromptRequest = Body(...)) -> dict[str, Any]:
        live = require(session_id)
        try:
            await registry.prompt(live, body.text)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/abort")
    def abort(session_id: str) -> dict[str, Any]:
        registry.abort(require(session_id))
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/compact")
    async def compact(session_id: str) -> dict[str, Any]:
        live = require(session_id)
        result = await live.session.compact()
        if result:
            # 关键：告诉前端"上下文已经重置"，否则页面上的占用还按旧的累计
            live.publish({
                "type": "compacted",
                "tokensBefore": result.tokens_before,
                "summary": result.summary[:200],
            })
        return {"ok": bool(result), "tokensBefore": result.tokens_before if result else 0}

    # -- 模式与命令 ------------------------------------------------ #

    @app.post("/api/sessions/{session_id}/mode")
    async def set_mode(session_id: str, mode: str = Body(..., embed=True)) -> dict[str, Any]:
        live = require(session_id)
        available = {m["id"] for m in registry.list_modes(live.session.cwd)}
        if mode not in available:
            raise HTTPException(404, f"未知模式 {mode}")
        await live.session.set_mode(mode)
        live.publish({"type": "mode", "mode": mode})
        return registry.snapshot(live)

    @app.post("/api/sessions/{session_id}/command")
    async def run_command(session_id: str, body: CommandRequest = Body(...)) -> dict[str, Any]:
        """执行斜杠命令。和终端走同一条路径，行为一致。"""
        live = require(session_id)
        try:
            output = await live.session.run_command(body.name, body.rest)
        except KeyError:
            raise HTTPException(404, f"未知命令 /{body.name}") from None
        except Exception as exc:
            raise HTTPException(500, f"{type(exc).__name__}: {exc}") from exc
        if output:
            live.publish({"type": "notice", "text": output})
        return {"ok": True, "output": output, "mode": live.session.mode}

    @app.post("/api/sessions/{session_id}/model")
    def set_model(session_id: str, spec: str = Body(..., embed=True)) -> dict[str, Any]:
        live = require(session_id)
        model = registry.services.model_runtime.resolve(spec)
        if model is None:
            raise HTTPException(404, f"未知模型 {spec}")
        live.session.set_model(model)
        return registry.snapshot(live)

    @app.post("/api/sessions/{session_id}/tools")
    def set_tools(session_id: str, body: ToolsRequest = Body(...)) -> dict[str, Any]:
        live = require(session_id)
        return registry.update_tools(live, body.tools, body.skills)

    @app.post("/api/sessions/{session_id}/thinking")
    def set_thinking(session_id: str, level: str = Body(..., embed=True)) -> dict[str, Any]:
        live = require(session_id)
        live.session.set_thinking_level(level)
        return registry.snapshot(live)

    # -- 事件流 ---------------------------------------------------- #

    @app.get("/api/sessions/{session_id}/events")
    async def events(session_id: str, replay: bool = Query(True)) -> StreamingResponse:
        live = require(session_id)
        queue = live.subscribe()
        backlog = list(live.frames) if replay else []

        async def stream():
            try:
                for frame in backlog:  # 刷新页面后补齐已发生的内容
                    yield sse_frame(frame)
                yield sse_frame({"type": "ready"})
                while True:
                    try:
                        frame = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"  # 防代理掐连接
                        continue
                    yield sse_frame(frame)
            except asyncio.CancelledError:
                raise
            finally:
                live.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/sessions/{session_id}/messages")
    def messages(session_id: str) -> dict[str, Any]:
        live = require(session_id)
        out = []
        for message in live.session.messages:
            role = getattr(message, "role", "")
            out.append(
                {
                    "role": role,
                    "text": message.text() if hasattr(message, "text") else "",
                    "isError": getattr(message, "is_error", False),
                    "toolName": getattr(message, "tool_name", None),
                }
            )
        return {"messages": out}

    # -- 工作区：文件浏览 ------------------------------------------ #

    def workspace_root(session_id: str | None) -> str:
        """文件访问的根。给了会话就用会话的 cwd，否则用服务启动目录。"""
        if session_id:
            live = registry.get(session_id)
            if live is not None:
                return live.session.cwd
        return registry.cwd

    @app.get("/api/files")
    def files(path: str = Query("."), session: str | None = None) -> dict[str, Any]:
        try:
            return list_directory(workspace_root(session), path)
        except AccessDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/files/content")
    def file_content(path: str = Query(...), session: str | None = None) -> dict[str, Any]:
        try:
            return read_file(workspace_root(session), path)
        except AccessDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/files/raw")
    def file_raw(path: str = Query(...), session: str | None = None) -> FileResponse:
        """原始字节。图片和 PDF 直接交给浏览器渲染。"""
        try:
            target, mime = raw_file(workspace_root(session), path)
        except AccessDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        return FileResponse(target, media_type=mime, filename=target.name)

    # -- 工作区：历史会话 ------------------------------------------ #

    @app.get("/api/history")
    def history(limit: int = Query(30, ge=1, le=200), cwd: str | None = None) -> dict[str, Any]:
        return {"sessions": list_session_files(cwd or registry.cwd, registry.agent_dir, limit)}

    @app.delete("/api/history")
    def hide_history(file: str = Query(...)) -> dict[str, Any]:
        """从列表里移除，但**不删磁盘文件**。

        会话记录是排查问题的依据，也能从终端 --resume 找回来，
        所以网页端的"删除"只是隐藏。
        """
        return {"hidden": len(hide_session(file, registry.agent_dir))}

    @app.post("/api/history/restore")
    def restore_history() -> dict[str, Any]:
        unhide_all(registry.agent_dir)
        return {"ok": True}

    @app.get("/api/history/detail")
    def history_detail(file: str = Query(...)) -> dict[str, Any]:
        try:
            return read_session_file(file)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    # -- 静态资源 -------------------------------------------------- #

    # Vite 构建产物优先；没构建过就回退到内置的单文件页面
    dist = Path(__file__).parent / "web"
    if (dist / "index.html").exists():
        if (dist / "assets").exists():
            app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(dist / "index.html")

    elif STATIC_DIR.exists():
        app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")

        @app.get("/")
        def index_fallback() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    return app


def main() -> int:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(prog="pi-web", description="pi 的网页界面")
    parser.add_argument("-C", "--cwd", default=".")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8848)
    parser.add_argument("--agent-dir")
    args = parser.parse_args()

    app = create_app(cwd=args.cwd, agent_dir=args.agent_dir)
    print(f"  pi web  →  http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
