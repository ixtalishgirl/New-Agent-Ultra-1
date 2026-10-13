"""Tool: screenshot — capture the current page as PNG.

Returns PNG bytes info + base64 data (capped by BROWSER_SCREENSHOT_MAX_KB).
Optionally full-page or a single element.
"""
from __future__ import annotations

import base64
from typing import Any, Dict

from browser_manager import BrowserError
from registry import ToolContext, ToolRegistry

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string"},
        "full_page": {"type": "boolean", "default": False},
        "selector": {"type": "string",
                     "description": "Screenshot a single element instead of the viewport."},
    },
    "required": ["task_id"],
}


async def _handle(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    task = ctx.manager.get_task(args["task_id"])
    page = ctx.manager.active_page(task)
    max_bytes = ctx.settings.screenshot_max_kb * 1024

    if args.get("selector"):
        loc = page.locator(args["selector"]).first
        if await loc.count() == 0:
            raise BrowserError(f"selector matched nothing: {args['selector']}",
                               code="not_found")
        png = await loc.screenshot()
        scope = f"element:{args['selector']}"
    else:
        png = await page.screenshot(full_page=bool(args.get("full_page", False)))
        scope = "full_page" if args.get("full_page") else "viewport"

    truncated = False
    if len(png) > max_bytes:
        # Downscale: re-screenshot viewport-only as a cheap cap. Full fidelity
        # isn't critical for the agent's vision pass; size cap protects RAM.
        if args.get("full_page") or args.get("selector"):
            png = await page.screenshot(full_page=False)
            scope = "viewport(fallback: size cap)"
        if len(png) > max_bytes:
            png = png[:max_bytes]
            truncated = True

    await ctx.emit(task.id, {"event": "screenshot_taken",
                             "bytes": len(png), "scope": scope})
    return {
        "task_id": task.id,
        "url": page.url,
        "scope": scope,
        "mime": "image/png",
        "bytes": len(png),
        "truncated": truncated,
        "data_base64": base64.b64encode(png).decode("ascii"),
    }


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="screenshot",
        description="Capture the current page as PNG (viewport, full_page, or one "
                    "element). Returns base64 data, size-capped for RAM safety.",
        schema=SCHEMA,
        handler=_handle,
        tags=["capture"],
    )
