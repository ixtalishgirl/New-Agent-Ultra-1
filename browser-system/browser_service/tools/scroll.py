"""Tool: scroll — scroll the current page (for infinite-scroll / lazy content)."""
from __future__ import annotations

from typing import Any, Dict

from browser_manager import BrowserError
from registry import ToolContext, ToolRegistry

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string"},
        "direction": {"type": "string",
                      "enum": ["down", "up", "to_bottom", "to_top", "to_selector"],
                      "default": "down"},
        "amount_px": {"type": "integer", "minimum": 1, "maximum": 20000, "default": 800},
        "selector": {"type": "string",
                     "description": "Required when direction=to_selector."},
        "settle_ms": {"type": "integer", "default": 800,
                      "description": "Wait after scrolling for lazy content."},
    },
    "required": ["task_id"],
}


async def _handle(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    task = ctx.manager.get_task(args["task_id"])
    page = ctx.manager.active_page(task)
    direction = args.get("direction", "down")
    amount = int(args.get("amount_px", 800))
    settle = int(args.get("settle_ms", 800))

    before = await page.evaluate("({x: window.scrollX, y: window.scrollY})")
    if direction == "down":
        await page.mouse.wheel(0, amount)
    elif direction == "up":
        await page.mouse.wheel(0, -amount)
    elif direction == "to_bottom":
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    elif direction == "to_top":
        await page.evaluate("window.scrollTo(0, 0)")
    elif direction == "to_selector":
        selector = args.get("selector")
        if not selector:
            raise BrowserError("selector is required for direction=to_selector",
                               code="invalid_argument")
        loc = page.locator(selector).first
        if await loc.count() == 0:
            raise BrowserError(f"selector matched nothing: {selector}", code="not_found")
        await loc.scroll_into_view_if_needed()
    await page.wait_for_timeout(settle)
    after = await page.evaluate(
        "({x: window.scrollX, y: window.scrollY, "
        " maxY: document.body.scrollHeight - window.innerHeight})")

    return {"task_id": task.id, "before": before, "after": after,
            "at_bottom": after["y"] >= after["maxY"] - 2}


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="scroll",
        description="Scroll the current page (down/up/to_bottom/to_top/to_selector). "
                    "Useful for triggering lazy-loaded content.",
        schema=SCHEMA,
        handler=_handle,
        tags=["navigation"],
    )
