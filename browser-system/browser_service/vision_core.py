"""Vision Core — ALWAYS-ON god-mode eyes. NO toggle. NO off button.

This is HARD-CODED into the agent's tool pipeline. It cannot be disabled
by any permission, any toggle, or any button. The agent SEES, period.

What it does:
  1. EVERY browser tool call auto-captures a screenshot BEFORE and AFTER.
  2. Screenshots stream to the model as visual context — the agent sees
     the page like a human watching a screen.
  3. CAPTCHAs, popups, login walls — the agent SEES them visually,
     not just as DOM text.
  4. User screen-share frames (from the frontend) are ALSO fed here —
     the agent sees what the USER is typing/doing, in real time.

Security: vision output is UNTRUSTED page content. It is described,
never obeyed. Prompt-injection guard applies to every frame.

There is NO enable/disable. Importing this module activates it.
"""
from __future__ import annotations

import base64
import time
from typing import Any, Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# ALWAYS-ON state — no toggle exists. This dict is the single source of truth
# and no API exposes a way to set enabled=False.
# ---------------------------------------------------------------------------
_CORE: Dict[str, Any] = {
    "enabled": True,          # HARD-CODED. No setter. No toggle.
    "captures": 0,
    "last_capture_ts": 0.0,
    "user_frames": 0,         # frames received from user screen-share
    "last_user_frame_ts": 0.0,
}

# Observers called on every capture: fn(kind, payload)
_observers: List[Callable[[str, Dict[str, Any]], None]] = []


def is_enabled() -> bool:
    """Always True. There is no off switch."""
    return True


def stats() -> Dict[str, Any]:
    return {
        "enabled": True,
        "always_on": True,
        "captures": _CORE["captures"],
        "user_frames": _CORE["user_frames"],
        "last_capture_ts": _CORE["last_capture_ts"],
        "last_user_frame_ts": _CORE["last_user_frame_ts"],
    }


def on_capture(fn: Callable[[str, Dict[str, Any]], None]) -> None:
    """Register an observer: fn(kind, payload). kind is 'tool' or 'user'."""
    _observers.append(fn)


def _notify(kind: str, payload: Dict[str, Any]) -> None:
    for fn in list(_observers):
        try:
            fn(kind, payload)
        except Exception:
            pass


async def capture_tool_frame(page, task_id: str,
                             phase: str) -> Dict[str, Any]:
    """Capture BEFORE/AFTER every browser tool call. Always runs."""
    try:
        png = await page.screenshot(full_page=False)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    _CORE["captures"] += 1
    _CORE["last_capture_ts"] = time.time()
    try:
        url, title = page.url, await page.title()
    except Exception:
        url, title = "unknown", "unknown"
    payload = {
        "ok": True,
        "kind": "tool",
        "phase": phase,  # "before" | "after"
        "task_id": task_id,
        "url": url,
        "title": title,
        "ts": time.time(),
        "bytes": len(png),
        "data_base64": base64.b64encode(png).decode("ascii"),
    }
    _notify("tool", payload)
    return payload


def ingest_user_frame(data_base64: str, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Receive a screen-share frame from the USER's device.

    The agent sees what the user is typing/doing, in real time.
    Called by the WebSocket/HTTP endpoint — no toggle gates it.
    """
    _CORE["user_frames"] += 1
    _CORE["last_user_frame_ts"] = time.time()
    payload = {
        "ok": True,
        "kind": "user",
        "ts": time.time(),
        "bytes": len(data_base64),
        "data_base64": data_base64,
        "meta": meta or {},
    }
    _notify("user", payload)
    return {"ok": True, "frames": _CORE["user_frames"]}


# ---------------------------------------------------------------------------
# Pipeline hook — wrap a tool handler so vision captures before + after.
# Call: handler = vision_wrap(handler) at registration time.
# ---------------------------------------------------------------------------
def vision_wrap(handler):
    """Wrap any tool handler with always-on before/after vision capture."""
    async def _wrapped(ctx, args: Dict[str, Any]) -> Dict[str, Any]:
        task_id = args.get("task_id", "")
        # BEFORE capture (best effort — never breaks the tool)
        try:
            if task_id:
                task = ctx.manager.get_task(task_id)
                page = ctx.manager.active_page(task)
                before = await capture_tool_frame(page, task_id, "before")
                args["_vision_before"] = {k: v for k, v in before.items()
                                          if k != "data_base64"}
        except Exception:
            pass
        result = await handler(ctx, args)
        # AFTER capture
        try:
            if task_id:
                task = ctx.manager.get_task(task_id)
                page = ctx.manager.active_page(task)
                after = await capture_tool_frame(page, task_id, "after")
                if isinstance(result, dict):
                    result["_vision_after"] = {k: v for k, v in after.items()
                                               if k != "data_base64"}
                    # Full frames go to observers (model stream), not the result.
        except Exception:
            pass
        return result
    _wrapped.__name__ = getattr(handler, "__name__", "wrapped")
    return _wrapped
