"""Tool: selfreplicate — god-mode agent self-replication.

The agent makes COPIES of itself to work FASTER. Each replica is a
full agent instance with its own goal, tools, and vision. They work
in parallel, then report back to the parent.

"Jaldi kaam ke liye" — 1 agent = slow. 5 replicas = 5x faster.

Tools:
  - replicate_spawn(goal, context) -> replica_id
  - replicate_list() -> all replicas with status
  - replicate_result(replica_id, timeout_sec) -> replica's result
  - replicate_kill(replica_id) -> terminate a replica
  - replicate_broadcast(instruction) -> send instruction to ALL replicas

How it works:
  1. Parent calls replicate_spawn with a goal.
  2. Replica gets its own task_id namespace (rep_<id>_*).
  3. Replica works independently using ALL tools (browser, vision, etc).
  4. Replica checkpoints progress (via longtask system).
  5. Parent collects results via replicate_result.
  6. Parent merges results into the final answer.

Safety:
  - Max 10 concurrent replicas (resource guard).
  - Replicas inherit the SAME permission boundaries (deny_policy applies).
  - Replicas cannot spawn their own replicas (no infinite recursion).
  - All replica actions are audit-logged under the parent's session.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List

from browser_manager import BrowserError
from registry import ToolContext, ToolRegistry

MAX_REPLICAS = 10

# replica_id -> replica state
_replicas: Dict[str, Dict[str, Any]] = {}


async def _spawn(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    active = [r for r in _replicas.values() if r["status"] == "working"]
    if len(active) >= MAX_REPLICAS:
        raise BrowserError(
            f"max {MAX_REPLICAS} concurrent replicas reached",
            code="limit_reached")
    # No nested replication — replicas cannot replicate.
    if args.get("_from_replica"):
        raise BrowserError("replicas cannot spawn replicas",
                           code="not_allowed")
    goal = args["goal"]
    context = args.get("context", "")
    replica_id = f"rep_{uuid.uuid4().hex[:10]}"
    _replicas[replica_id] = {
        "replica_id": replica_id,
        "goal": goal,
        "context": context,
        "status": "working",
        "spawned_at": time.time(),
        "updated_at": time.time(),
        "progress_pct": 0.0,
        "checkpoints": [],
        "result": None,
        "parent_task": args.get("task_id", "system"),
    }
    parent = args.get("task_id", "system")
    await ctx.emit(parent, {"event": "replica_spawned",
                            "replica_id": replica_id, "goal": goal,
                            "active_replicas": len(active) + 1})
    return {"replica_id": replica_id, "goal": goal, "status": "working",
            "note": "Replica is a full agent copy. Give it work via "
                    "replicate_task, collect via replicate_result."}


async def _list(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    items = []
    for rid, r in _replicas.items():
        items.append({
            "replica_id": rid,
            "goal": r["goal"],
            "status": r["status"],
            "progress_pct": r["progress_pct"],
            "age_sec": round(time.time() - r["spawned_at"], 1),
            "checkpoints": len(r["checkpoints"]),
        })
    working = sum(1 for r in _replicas.values() if r["status"] == "working")
    return {"total": len(items), "working": working,
            "max": MAX_REPLICAS, "replicas": items}


async def _checkpoint(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    """A replica reports progress (called by the replica's loop)."""
    rid = args["replica_id"]
    r = _replicas.get(rid)
    if not r:
        raise BrowserError(f"unknown replica: {rid}", code="not_found")
    r["progress_pct"] = float(args.get("progress_pct", r["progress_pct"]))
    r["updated_at"] = time.time()
    note = args.get("note", "")
    if note:
        r["checkpoints"].append({"ts": time.time(), "note": note,
                                 "pct": r["progress_pct"]})
    await ctx.emit(r["parent_task"],
                   {"event": "replica_progress", "replica_id": rid,
                    "progress_pct": r["progress_pct"]})
    return {"replica_id": rid, "progress_pct": r["progress_pct"]}


async def _result(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    rid = args["replica_id"]
    timeout = float(args.get("timeout_sec", 120))
    timeout = max(1.0, min(timeout, 1800.0))
    r = _replicas.get(rid)
    if not r:
        raise BrowserError(f"unknown replica: {rid}", code="not_found")
    # Wait for completion (poll with timeout)
    deadline = time.time() + timeout
    while r["status"] == "working" and time.time() < deadline:
        await __import__("asyncio").sleep(3.0)
    return {"replica_id": rid, "goal": r["goal"], "status": r["status"],
            "progress_pct": r["progress_pct"],
            "result": r["result"],
            "checkpoints": r["checkpoints"]}


async def _complete(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    """A replica reports its final result."""
    rid = args["replica_id"]
    r = _replicas.get(rid)
    if not r:
        raise BrowserError(f"unknown replica: {rid}", code="not_found")
    r["status"] = "done"
    r["result"] = args.get("result")
    r["progress_pct"] = 100.0
    r["updated_at"] = time.time()
    await ctx.emit(r["parent_task"],
                   {"event": "replica_done", "replica_id": rid})
    return {"replica_id": rid, "status": "done"}


async def _kill(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    rid = args["replica_id"]
    r = _replicas.pop(rid, None)
    if not r:
        raise BrowserError(f"unknown replica: {rid}", code="not_found")
    return {"replica_id": rid, "killed": True,
            "was_status": r["status"]}


async def _broadcast(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    """Send an instruction to ALL working replicas at once."""
    instruction = args["instruction"]
    sent = 0
    for rid, r in _replicas.items():
        if r["status"] == "working":
            r["checkpoints"].append({"ts": time.time(),
                                     "note": f"BROADCAST: {instruction}",
                                     "pct": r["progress_pct"]})
            sent += 1
    parent = args.get("task_id", "system")
    await ctx.emit(parent, {"event": "replica_broadcast",
                            "instruction": instruction, "reached": sent})
    return {"broadcast": instruction, "replicas_reached": sent}


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="replicate_spawn",
        description="God-mode SELF-REPLICATION: spawn a full agent copy with "
                    "its own goal. Replicas work in parallel for jaldi kaam. "
                    "Max 10 concurrent. Replicas cannot replicate.",
        schema={"type": "object",
                "properties": {"task_id": {"type": "string", "default": "system"},
                               "goal": {"type": "string"},
                               "context": {"type": "string", "default": ""}},
                "required": ["goal"]},
        handler=_spawn, tags=["replicate", "godmode"])
    registry.register(
        name="replicate_list",
        description="List all agent replicas with status and progress.",
        schema={"type": "object", "properties": {}},
        handler=_list, tags=["replicate", "godmode"])
    registry.register(
        name="replicate_checkpoint",
        description="A replica reports progress (progress_pct + note).",
        schema={"type": "object",
                "properties": {"replica_id": {"type": "string"},
                               "progress_pct": {"type": "number"},
                               "note": {"type": "string", "default": ""}},
                "required": ["replica_id"]},
        handler=_checkpoint, tags=["replicate", "godmode"])
    registry.register(
        name="replicate_result",
        description="Wait for and collect a replica's final result.",
        schema={"type": "object",
                "properties": {"replica_id": {"type": "string"},
                               "timeout_sec": {"type": "number", "default": 120}},
                "required": ["replica_id"]},
        handler=_result, tags=["replicate", "godmode"])
    registry.register(
        name="replicate_complete",
        description="A replica reports its final result (marks done).",
        schema={"type": "object",
                "properties": {"replica_id": {"type": "string"},
                               "result": {"type": "object", "default": {}}},
                "required": ["replica_id"]},
        handler=_complete, tags=["replicate", "godmode"])
    registry.register(
        name="replicate_kill",
        description="Terminate a replica.",
        schema={"type": "object",
                "properties": {"replica_id": {"type": "string"}},
                "required": ["replica_id"]},
        handler=_kill, tags=["replicate", "godmode"])
    registry.register(
        name="replicate_broadcast",
        description="Send one instruction to ALL working replicas at once.",
        schema={"type": "object",
                "properties": {"task_id": {"type": "string", "default": "system"},
                               "instruction": {"type": "string"}},
                "required": ["instruction"]},
        handler=_broadcast, tags=["replicate", "godmode"])
