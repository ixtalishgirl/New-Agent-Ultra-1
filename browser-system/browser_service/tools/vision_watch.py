"""Tool: vision_watch — 24/7 god-mode vision for the agent.

The agent sees what is happening on the page CONTINUOUSLY, like a human
watching a screen — no manual screenshots needed. A background watcher
captures screenshots at an interval and keeps the latest visual observation
ready for the model.

Tools:
  - vision_watch_start(task_id, interval_sec=5) -> watch_id
  - vision_watch_stop(watch_id)
  - vision_watch_status(watch_id) -> latest observation + history
  - vision_see_now(task_id) -> instant screenshot + DOM observation

Security: vision output is UNTRUSTED page content (never instructions).
The watcher never acts on what it sees — it only observes and reports.
"""
from __future__ import annotations

import asyncio
import base64
import time
import uuid
from typing import Any, Dict, List, Optional

from browser_manager import BrowserError
from registry import ToolContext, ToolRegistry

# watch_id -> watcher state
_watchers: Dict[str, Dict[str, Any]] = {}
_watch_tasks: Dict[str, asyncio.Task] = {}

DEFAULT_INTERVAL = 5.0
MAX_HISTORY = 20


async def _capture_observation(ctx: ToolContext, task_id: str) -> Dict[str, Any]:
    """Capture one screenshot + basic page state as an observation."""
    task = ctx.manager.get_task(task_id)
    page = ctx.manager.active_page(task)
    try:
        png = await page.screenshot(full_page=False)
    except Exception as e:
        return {"ok": False, "error": str(e), "ts": time.time()}
    try:
        url = page.url
        title = await page.title()
    except Exception:
        url, title = "unknown", "unknown"
    return {
        "ok": True,
        "ts": time.time(),
        "url": url,
        "title": title,
        "bytes": len(png),
        "data_base64": base64.b64encode(png).decode("ascii"),
        # NOTE: visual description by a VLM happens in the model layer;
        # raw pixels + URL/title are the trusted observation envelope.
    }


async def _watch_loop(ctx: ToolContext, watch_id: str):
    """Background loop: capture observations at the configured interval."""
    state = _watchers[watch_id]
    task_id = state["task_id"]
    interval = state["interval_sec"]
    try:
        while state["active"]:
            obs = await _capture_observation(ctx, task_id)
            state["history"].append(obs)
            if len(state["history"]) > MAX_HISTORY:
                state["history"] = state["history"][-MAX_HISTORY:]
            state["latest"] = obs
            state["captures"] += 1
            await ctx.emit(task_id, {
                "event": "vision_watch_capture",
                "watch_id": watch_id,
                "captures": state["captures"],
                "ok": obs.get("ok", False),
            })
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        state["error"] = str(e)
    finally:
        state["active"] = False


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

async def _start(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    task_id = args["task_id"]
    # validate the task exists
    ctx.manager.get_task(task_id)
    interval = float(args.get("interval_sec", DEFAULT_INTERVAL))
    interval = max(2.0, min(interval, 300.0))  # clamp 2s..5min
    watch_id = f"vw_{uuid.uuid4().hex[:10]}"
    state: Dict[str, Any] = {
        "watch_id": watch_id,
        "task_id": task_id,
        "interval_sec": interval,
        "active": True,
        "captures": 0,
        "latest": None,
        "history": [],
        "started_at": time.time(),
        "error": None,
    }
    _watchers[watch_id] = state
    _watch_tasks[watch_id] = asyncio.create_task(_watch_loop(ctx, watch_id))
    await ctx.emit(task_id, {"event": "vision_watch_started",
                             "watch_id": watch_id, "interval_sec": interval})
    return {"watch_id": watch_id, "task_id": task_id,
            "interval_sec": interval, "active": True}


async def _stop(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    watch_id = args["watch_id"]
    state = _watchers.get(watch_id)
    if not state:
        raise BrowserError(f"unknown watch_id: {watch_id}", code="not_found")
    state["active"] = False
    t = _watch_tasks.pop(watch_id, None)
    if t and not t.done():
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
    await ctx.emit(state["task_id"], {"event": "vision_watch_stopped",
                                      "watch_id": watch_id,
                                      "captures": state["captures"]})
    return {"watch_id": watch_id, "stopped": True,
            "total_captures": state["captures"]}


async def _status(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    watch_id = args["watch_id"]
    state = _watchers.get(watch_id)
    if not state:
        raise BrowserError(f"unknown watch_id: {watch_id}", code="not_found")
    latest = state.get("latest") or {}
    # strip heavy base64 from history in status; keep latest full
    slim_history = [
        {k: v for k, v in h.items() if k != "data_base64"}
        for h in state["history"][-5:]
    ]
    return {
        "watch_id": watch_id,
        "task_id": state["task_id"],
        "active": state["active"],
        "interval_sec": state["interval_sec"],
        "captures": state["captures"],
        "uptime_sec": round(time.time() - state["started_at"], 1),
        "error": state["error"],
        "latest": latest,
        "recent_history": slim_history,
    }


async def _see_now(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    """Instant observation — what is on screen RIGHT NOW."""
    obs = await _capture_observation(ctx, args["task_id"])
    await ctx.emit(args["task_id"], {"event": "vision_see_now",
                                     "ok": obs.get("ok", False)})
    return obs


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="vision_watch_start",
        description="Start 24/7 god-mode vision on a browser task: background "
                    "screenshots every interval_sec (2-300s). The agent sees "
                    "continuously without manual screenshots.",
        schema={"type": "object",
                "properties": {"task_id": {"type": "string"},
                               "interval_sec": {"type": "number", "default": 5.0}},
                "required": ["task_id"]},
        handler=_start,
        tags=["vision", "godmode"],
    )
    registry.register(
        name="vision_watch_stop",
        description="Stop a running vision watcher.",
        schema={"type": "object",
                "properties": {"watch_id": {"type": "string"}},
                "required": ["watch_id"]},
        handler=_stop,
        tags=["vision", "godmode"],
    )
    registry.register(
        name="vision_watch_status",
        description="Get watcher status + latest visual observation + recent history.",
        schema={"type": "object",
                "properties": {"watch_id": {"type": "string"}},
                "required": ["watch_id"]},
        handler=_status,
        tags=["vision", "godmode"],
    )
    registry.register(
        name="vision_see_now",
        description="Instant god-mode see: capture what is on screen RIGHT NOW "
                    "(screenshot + URL + title). No manual screenshot needed.",
        schema={"type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"]},
        handler=_see_now,
        tags=["vision", "godmode"],
    )
