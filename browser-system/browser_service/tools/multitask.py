"""Tool: multitask — god-mode parallel task execution.

Run MANY browser tasks at once, like a team working in parallel.
The model can spawn sub-tasks, check on them, collect results, and cancel.

Tools:
  - mtask_spawn(url, goal) -> child task_id (new browser task, runs parallel)
  - mtask_list() -> all tasks with status
  - mtask_result(task_id, timeout_sec=60) -> wait for + return result
  - mtask_cancel(task_id)

Each child task is an independent browser task in the BrowserManager,
so they run truly in parallel (separate pages/contexts).
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Dict, List

from browser_manager import BrowserError
from registry import ToolContext, ToolRegistry

# child task metadata (task_id -> info)
_children: Dict[str, Dict[str, Any]] = {}


async def _spawn(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    url = args["url"]
    goal = args.get("goal", "")
    child_id = f"mt_{uuid.uuid4().hex[:10]}"
    # Create an independent browser task via the manager.
    # BrowserManager.new_task signature: (task_id, url) — adapt if needed.
    try:
        task = ctx.manager.new_task(child_id, url)
    except TypeError:
        # fallback: manager may use create_task
        task = ctx.manager.create_task(child_id, url)
    info = {
        "task_id": child_id,
        "url": url,
        "goal": goal,
        "spawned_at": time.time(),
        "parent_emit": True,
    }
    _children[child_id] = info
    # Emit on the PARENT task so the model sees the spawn event.
    parent_id = args.get("_parent_task_id") or getattr(ctx, "task_id", None)
    if parent_id:
        try:
            await ctx.emit(parent_id, {"event": "mtask_spawned",
                                       "child_id": child_id, "url": url,
                                       "goal": goal})
        except Exception:
            pass
    return {"task_id": child_id, "url": url, "goal": goal, "status": "spawned"}


async def _list(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    for tid, info in _children.items():
        try:
            task = ctx.manager.get_task(tid)
            status = getattr(task, "status", "unknown")
        except Exception:
            status = "gone"
        items.append({
            "task_id": tid,
            "url": info["url"],
            "goal": info["goal"],
            "status": str(status),
            "age_sec": round(time.time() - info["spawned_at"], 1),
        })
    return {"count": len(items), "tasks": items}


async def _result(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    tid = args["task_id"]
    timeout = float(args.get("timeout_sec", 60))
    timeout = max(1.0, min(timeout, 600.0))
    info = _children.get(tid)
    if not info:
        raise BrowserError(f"unknown child task: {tid}", code="not_found")
    # Poll the child task's page state until timeout.
    deadline = time.time() + timeout
    last_state: Dict[str, Any] = {}
    while time.time() < deadline:
        try:
            task = ctx.manager.get_task(tid)
            page = ctx.manager.active_page(task)
            last_state = {
                "url": page.url,
                "title": await page.title(),
                "ready": True,
            }
            break
        except Exception as e:
            last_state = {"ready": False, "error": str(e)}
            await asyncio.sleep(2.0)
    return {"task_id": tid, "goal": info["goal"], "state": last_state}


async def _cancel(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    tid = args["task_id"]
    info = _children.pop(tid, None)
    if not info:
        raise BrowserError(f"unknown child task: {tid}", code="not_found")
    try:
        ctx.manager.close_task(tid)
    except Exception:
        pass
    return {"task_id": tid, "cancelled": True}


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="mtask_spawn",
        description="God-mode multi-task: spawn a PARALLEL browser sub-task on a "
                    "URL with a goal. Returns child task_id. Run many at once.",
        schema={"type": "object",
                "properties": {"url": {"type": "string"},
                               "goal": {"type": "string", "default": ""}},
                "required": ["url"]},
        handler=_spawn,
        tags=["multitask", "godmode"],
    )
    registry.register(
        name="mtask_list",
        description="List all parallel sub-tasks with status.",
        schema={"type": "object", "properties": {}},
        handler=_list,
        tags=["multitask", "godmode"],
    )
    registry.register(
        name="mtask_result",
        description="Wait for (up to timeout_sec) and return a sub-task's state.",
        schema={"type": "object",
                "properties": {"task_id": {"type": "string"},
                               "timeout_sec": {"type": "number", "default": 60}},
                "required": ["task_id"]},
        handler=_result,
        tags=["multitask", "godmode"],
    )
    registry.register(
        name="mtask_cancel",
        description="Cancel a parallel sub-task.",
        schema={"type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"]},
        handler=_cancel,
        tags=["multitask", "godmode"],
    )
