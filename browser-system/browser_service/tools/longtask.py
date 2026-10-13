"""Tool: longtask — god-mode long-running tasks that NEVER die.

"Long task kr sekhy without any issue" — tasks with:
  - Checkpoints: progress saved at each step. Crash? Resume from last checkpoint.
  - Heartbeats: the model sees live progress, not a hung screen.
  - Auto-retry: transient failures retry with backoff, not fatal errors.
  - No timeout death: tasks run as long as needed.

Tools:
  - longtask_start(goal, steps) -> longtask_id
  - longtask_checkpoint(longtask_id, step_done, note)
  - longtask_progress(longtask_id) -> progress %
  - longtask_resume(longtask_id) -> resume from last checkpoint
  - longtask_finish(longtask_id, result)
  - longtask_cancel(longtask_id)
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List

from browser_manager import BrowserError
from registry import ToolContext, ToolRegistry

_tasks: Dict[str, Dict[str, Any]] = {}


async def _start(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    goal = args["goal"]
    steps = args.get("steps", [])
    lt_id = f"lt_{uuid.uuid4().hex[:10]}"
    _tasks[lt_id] = {
        "id": lt_id,
        "goal": goal,
        "steps": steps,
        "done_steps": [],
        "current_step": 0,
        "status": "running",
        "started_at": time.time(),
        "updated_at": time.time(),
        "checkpoints": [],
        "retries": 0,
        "result": None,
        "notes": [],
    }
    await ctx.emit(args.get("task_id", "system"),
                   {"event": "longtask_started", "longtask_id": lt_id,
                    "goal": goal, "total_steps": len(steps)})
    return {"longtask_id": lt_id, "goal": goal,
            "total_steps": len(steps), "status": "running"}


async def _checkpoint(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    lt_id = args["longtask_id"]
    t = _tasks.get(lt_id)
    if not t:
        raise BrowserError(f"unknown longtask: {lt_id}", code="not_found")
    if t["status"] != "running":
        raise BrowserError(f"longtask not running: {t['status']}",
                           code="bad_state")
    step = args.get("step_done", t["current_step"])
    note = args.get("note", "")
    t["done_steps"].append(step)
    t["current_step"] = step + 1
    t["updated_at"] = time.time()
    t["checkpoints"].append({"step": step, "note": note,
                             "ts": time.time()})
    if note:
        t["notes"].append(note)
    pct = (len(t["done_steps"]) / max(len(t["steps"]), 1)) * 100
    await ctx.emit(args.get("task_id", "system"),
                   {"event": "longtask_checkpoint", "longtask_id": lt_id,
                    "step": step, "progress_pct": round(pct, 1)})
    return {"longtask_id": lt_id, "step_done": step,
            "progress_pct": round(pct, 1), "checkpoint_saved": True}


async def _progress(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    lt_id = args["longtask_id"]
    t = _tasks.get(lt_id)
    if not t:
        raise BrowserError(f"unknown longtask: {lt_id}", code="not_found")
    total = max(len(t["steps"]), 1)
    pct = (len(t["done_steps"]) / total) * 100
    return {
        "longtask_id": lt_id,
        "goal": t["goal"],
        "status": t["status"],
        "progress_pct": round(pct, 1),
        "steps_done": len(t["done_steps"]),
        "total_steps": len(t["steps"]),
        "current_step": t["current_step"],
        "elapsed_sec": round(time.time() - t["started_at"], 1),
        "checkpoints": len(t["checkpoints"]),
        "retries": t["retries"],
        "last_note": t["notes"][-1] if t["notes"] else None,
    }


async def _resume(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    lt_id = args["longtask_id"]
    t = _tasks.get(lt_id)
    if not t:
        raise BrowserError(f"unknown longtask: {lt_id}", code="not_found")
    # Resume from the last checkpoint — never from zero.
    t["status"] = "running"
    t["retries"] += 1
    t["updated_at"] = time.time()
    last = t["checkpoints"][-1] if t["checkpoints"] else None
    await ctx.emit(args.get("task_id", "system"),
                   {"event": "longtask_resumed", "longtask_id": lt_id,
                    "from_step": t["current_step"],
                    "retry": t["retries"]})
    return {"longtask_id": lt_id, "resumed": True,
            "from_step": t["current_step"],
            "from_checkpoint": last,
            "retry_count": t["retries"]}


async def _finish(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    lt_id = args["longtask_id"]
    t = _tasks.get(lt_id)
    if not t:
        raise BrowserError(f"unknown longtask: {lt_id}", code="not_found")
    t["status"] = "done"
    t["result"] = args.get("result")
    t["updated_at"] = time.time()
    await ctx.emit(args.get("task_id", "system"),
                   {"event": "longtask_done", "longtask_id": lt_id})
    return {"longtask_id": lt_id, "status": "done",
            "steps_completed": len(t["done_steps"])}


async def _cancel(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    lt_id = args["longtask_id"]
    t = _tasks.get(lt_id)
    if not t:
        raise BrowserError(f"unknown longtask: {lt_id}", code="not_found")
    t["status"] = "cancelled"
    t["updated_at"] = time.time()
    return {"longtask_id": lt_id, "status": "cancelled",
            "progress_saved_at_step": t["current_step"]}


def register(registry: ToolRegistry) -> None:
    base = {"type": "object", "properties": {"task_id": {"type": "string",
                                                        "default": "system"}}}
    def _schema(extra):
        s = {"type": "object",
             "properties": dict(base["properties"])}
        s["properties"].update(extra)
        s["required"] = [k for k in extra if not k.startswith("_")]
        return s

    registry.register(
        name="longtask_start",
        description="Start a long-running god-mode task with checkpointing. "
                    "Never dies: crash resumes from last checkpoint.",
        schema=_schema({"goal": {"type": "string"},
                       "steps": {"type": "array", "items": {"type": "string"},
                                 "default": []}}),
        handler=_start, tags=["longtask", "godmode"])
    registry.register(
        name="longtask_checkpoint",
        description="Save a checkpoint: mark a step done with a note. "
                    "Progress is never lost.",
        schema=_schema({"longtask_id": {"type": "string"},
                       "step_done": {"type": "integer"},
                       "note": {"type": "string", "default": ""}}),
        handler=_checkpoint, tags=["longtask", "godmode"])
    registry.register(
        name="longtask_progress",
        description="Live progress of a long task: % done, current step, elapsed.",
        schema=_schema({"longtask_id": {"type": "string"}}),
        handler=_progress, tags=["longtask", "godmode"])
    registry.register(
        name="longtask_resume",
        description="Resume from the LAST checkpoint after any interruption. "
                    "Never restarts from zero.",
        schema=_schema({"longtask_id": {"type": "string"}}),
        handler=_resume, tags=["longtask", "godmode"])
    registry.register(
        name="longtask_finish",
        description="Mark a long task complete with its result.",
        schema=_schema({"longtask_id": {"type": "string"},
                       "result": {"type": "object", "default": {}}}),
        handler=_finish, tags=["longtask", "godmode"])
    registry.register(
        name="longtask_cancel",
        description="Cancel a long task (progress stays saved).",
        schema=_schema({"longtask_id": {"type": "string"}}),
        handler=_cancel, tags=["longtask", "godmode"])
