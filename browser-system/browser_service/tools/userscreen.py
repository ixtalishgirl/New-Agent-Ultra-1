"""Tool: userscreen — the agent SEES the user's screen.

The user shares their screen from the app (one tap). Frames stream here.
The agent sees what the user is typing/doing — REAL eyes, not fake.

Tools:
  - userscreen_frame(data_base64, meta) -> ingest one frame
  - userscreen_status() -> frame count, last frame time, always-on state

This is ALWAYS ON at the backend — the endpoint accepts frames whenever
the user shares. There is no backend toggle. The user controls sharing
from their own device (browser permission prompt).
"""
from __future__ import annotations

from typing import Any, Dict

from registry import ToolContext, ToolRegistry

try:
    from vision_core import ingest_user_frame, stats as core_stats
except ImportError:
    from browser_service.vision_core import ingest_user_frame, stats as core_stats


async def _frame(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    data = args["data_base64"]
    # sanity cap: 2MB per frame
    if len(data) > 2 * 1024 * 1024:
        return {"ok": False, "error": "frame too large (max 2MB base64)"}
    result = ingest_user_frame(data, args.get("meta"))
    await ctx.emit(args.get("task_id", "system"),
                   {"event": "userscreen_frame", **result})
    return result


async def _status(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    s = core_stats()
    return {
        "vision": "ALWAYS_ON",
        "toggle": "NONE — hard-coded, cannot be disabled",
        **s,
    }


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="userscreen_frame",
        description="Receive one screen-share frame from the USER's device. "
                    "The agent sees what the user is typing/doing in real time.",
        schema={"type": "object",
                "properties": {
                    "task_id": {"type": "string", "default": "system"},
                    "data_base64": {"type": "string",
                                    "description": "JPEG/PNG frame as base64"},
                    "meta": {"type": "object", "default": {}}},
                "required": ["data_base64"]},
        handler=_frame,
        tags=["vision", "godmode", "userscreen"],
    )
    registry.register(
        name="userscreen_status",
        description="Always-on vision status: frame counts, always-on proof.",
        schema={"type": "object", "properties": {}},
        handler=_status,
        tags=["vision", "godmode", "userscreen"],
    )
