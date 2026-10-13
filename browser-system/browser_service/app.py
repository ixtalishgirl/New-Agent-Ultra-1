"""Browser service — FastAPI entry point.

Exposes every registered tool as:
  - POST /tools/call            (generic: {"name": ..., "arguments": {...}})
  - POST /tools/<tool_name>     (one endpoint per tool, typed request body)
Also: GET /tools (registry specs), task management, research history,
health check, and a WebSocket for live per-task status events.

Run:  uvicorn app:app --host 127.0.0.1 --port 8080
"""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from browser_manager import BrowserError, BrowserManager
from config import settings
from db import HistoryStore
from models import (
    CloseTabRequest,
    DetectBlocksRequest,
    DownloadFileRequest,
    ExtractRequest,
    FillFormRequest,
    NavigateRequest,
    NewTabRequest,
    OpenLinkRequest,
    PaginateRequest,
    ScreenshotRequest,
    ScrollRequest,
    SearchRequest,
    SwitchTabRequest,
    ToolCallRequest,
    ToolResult,
    ToolSpec,
)
from registry import ToolContext, registry
from tools import register_all
from flat_shim import register_flat_routes

# --------------------------------------------------------------------------
# fix: PageStateRequest isn't defined; page_state takes only task_id
from pydantic import BaseModel


class TaskIdBody(BaseModel):
    task_id: str


class PageStateRequest(TaskIdBody):
    pass


class ListTabsRequest(TaskIdBody):
    pass
# --------------------------------------------------------------------------

manager = BrowserManager(settings)
history: Optional[HistoryStore] = None

# task_id -> list of subscriber queues for the websocket
_subscribers: Dict[str, List[asyncio.Queue]] = {}


async def emit(task_id: str, event: Dict[str, Any]) -> None:
    event = {"task_id": task_id, "ts": time.time(), **event}
    for q in _subscribers.get(task_id, []):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


def make_ctx() -> ToolContext:
    return ToolContext(manager=manager, settings=settings, history=history, emit=emit)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global history
    register_all(registry)
    if settings.enable_history:
        history = HistoryStore(settings.history_db_path)
    # NOTE: browser is LAZY — manager.ensure_started() launches Chromium on the
    # first browser-needing tool call (free-tier: no 300MB RAM cost at boot).
    yield
    await manager.stop()


app = FastAPI(title="ahsan Agent Browser Service", version="1.0.0", lifespan=lifespan,
              dependencies=[Depends(api_key_guard)] if settings.api_key else [])


# Flat REST shim (Side A contract for the Node integration layer).
# Purely additive: delegates to the /tools/* registry with a per-request
# task; the /tools/* contract is untouched. See CONTRACT_CHECK.md.
register_flat_routes(app, registry=registry, make_ctx=make_ctx,
                     manager=manager, settings=settings,
                     history_fn=lambda: history)


# ---------- auth (optional API key) ----------

async def api_key_guard(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")):
    """Dependency: rejects requests when BROWSER_SERVICE_API_KEY is set and mismatched."""
    if settings.api_key and x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")
    return True


# ---------- error mapping ----------

_CODE_STATUS = {
    "task_not_found": 404,
    "tab_not_found": 404,
    "not_found": 404,
    "invalid_argument": 400,
    "forbidden_file_type": 403,
    "sensitive_field": 403,
    "blocked_host": 403,
    "robots_disallowed": 403,
    "page_blocked": 403,
    "too_many_tasks": 429,
    "too_many_tabs": 429,
    "tool_timeout": 504,
    "navigation_failed": 502,
    "download_failed": 502,
    "search_failed": 502,
    "file_too_large": 413,
    "not_a_file": 400,
}


def _tool_error(tool: str, task_id: Optional[str], e: BrowserError) -> JSONResponse:
    return JSONResponse(
        status_code=_CODE_STATUS.get(e.code, 500),
        content={"ok": False, "tool": tool, "task_id": task_id,
                 "error": e.message, "error_code": e.code,
                 "details": e.details},
    )


async def _run_tool(name: str, arguments: Dict[str, Any]):
    tool = registry.get(name)
    ctx = make_ctx()
    try:
        data = await tool.handler(ctx, arguments)
        return JSONResponse({"ok": True, "tool": name,
                             "task_id": arguments.get("task_id") or data.get("task_id"),
                             "data": data})
    except BrowserError as e:
        return _tool_error(name, arguments.get("task_id"), e)
    except KeyError as e:  # unknown tool name
        return JSONResponse(status_code=404, content={"ok": False, "tool": name,
                                                      "error": str(e), "error_code": "unknown_tool"})
    except Exception as e:  # never leak a traceback-shaped 500 without structure
        return JSONResponse(status_code=500, content={"ok": False, "tool": name,
                                                      "task_id": arguments.get("task_id"),
                                                      "error": f"{type(e).__name__}: {e}",
                                                      "error_code": "internal_error"})


# ---------- meta endpoints ----------

@app.get("/health")
async def health():
    return {"status": "ok", "service": "browser-service", "version": "1.0.0",
            "headless": settings.headless,
            "proxy": bool(settings.https_proxy or settings.http_proxy),
            "history_enabled": settings.enable_history,
            "tasks": manager.task_ids(),
            "tools": registry.names()}


@app.get("/tools", response_model=List[ToolSpec])
async def list_tools():
    return registry.spec_list()


@app.post("/tools/call")
async def call_tool(req: ToolCallRequest):
    return await _run_tool(req.name, req.arguments)


# ---------- per-tool endpoints ----------

@app.post("/tools/navigate")
async def t_navigate(req: NavigateRequest):
    return await _run_tool("navigate", req.model_dump(exclude_none=True))


@app.post("/tools/search")
async def t_search(req: SearchRequest):
    return await _run_tool("search", req.model_dump(exclude_none=True))


@app.post("/tools/open_link")
async def t_open_link(req: OpenLinkRequest):
    return await _run_tool("open_link", req.model_dump(exclude_none=True))


@app.post("/tools/extract")
async def t_extract(req: ExtractRequest):
    return await _run_tool("extract", req.model_dump(exclude_none=True))


@app.post("/tools/scroll")
async def t_scroll(req: ScrollRequest):
    return await _run_tool("scroll", req.model_dump(exclude_none=True))


@app.post("/tools/paginate")
async def t_paginate(req: PaginateRequest):
    return await _run_tool("paginate", req.model_dump(exclude_none=True))


@app.post("/tools/fill_form")
async def t_fill_form(req: FillFormRequest):
    return await _run_tool("fill_form", req.model_dump())


@app.post("/tools/screenshot")
async def t_screenshot(req: ScreenshotRequest):
    return await _run_tool("screenshot", req.model_dump(exclude_none=True))


@app.post("/tools/page_state")
async def t_page_state(req: PageStateRequest):
    return await _run_tool("page_state", req.model_dump())


@app.post("/tools/new_tab")
async def t_new_tab(req: NewTabRequest):
    return await _run_tool("new_tab", req.model_dump(exclude_none=True))


@app.post("/tools/switch_tab")
async def t_switch_tab(req: SwitchTabRequest):
    return await _run_tool("switch_tab", req.model_dump())


@app.post("/tools/list_tabs")
async def t_list_tabs(req: ListTabsRequest):
    return await _run_tool("list_tabs", req.model_dump())


@app.post("/tools/close_tab")
async def t_close_tab(req: CloseTabRequest):
    return await _run_tool("close_tab", req.model_dump())


@app.post("/tools/download_file")
async def t_download_file(req: DownloadFileRequest):
    return await _run_tool("download_file", req.model_dump(exclude_none=True))


@app.post("/tools/detect_blocks")
async def t_detect_blocks(req: DetectBlocksRequest):
    return await _run_tool("detect_blocks", req.model_dump())


# ---------- task management ----------

@app.get("/tasks")
async def list_tasks():
    return {"tasks": manager.task_ids()}


@app.delete("/tasks/{task_id}")
async def close_task(task_id: str):
    try:
        await manager.close_task(task_id)
    except BrowserError as e:
        raise HTTPException(status_code=404, detail=e.message)
    return {"ok": True, "closed": task_id}


# ---------- research history (SQLite, optional) ----------

@app.get("/history")
async def get_history(task_id: Optional[str] = Query(default=None),
                      limit: int = Query(default=100, le=1000)):
    if not history:
        raise HTTPException(status_code=404, detail="history not enabled "
                            "(set BROWSER_ENABLE_HISTORY=1)")
    return {"observations": history.list(task_id=task_id, limit=limit)}


@app.delete("/history")
async def clear_history(task_id: Optional[str] = Query(default=None)):
    if not history:
        raise HTTPException(status_code=404, detail="history not enabled")
    return {"ok": True, "deleted": history.clear(task_id=task_id)}


# ---------- websocket: live per-task status ----------

@app.websocket("/ws/{task_id}")
async def ws_status(ws: WebSocket, task_id: str):
    await ws.accept()
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _subscribers.setdefault(task_id, []).append(q)
    try:
        await ws.send_text(json.dumps({"event": "subscribed", "task_id": task_id}))
        while True:
            event = await q.get()
            await ws.send_text(json.dumps(event))
    except WebSocketDisconnect:
        pass
    finally:
        subs = _subscribers.get(task_id, [])
        if q in subs:
            subs.remove(q)
