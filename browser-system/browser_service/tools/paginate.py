"""Tool: paginate — walk "next page" links/buttons, collecting each page's URL.

Auto-detects common next-page controls (rel=next, text like "Next"/"Load more")
or uses an explicit selector. Returns the list of visited page URLs so the
caller can extract() each one. Rate-limited per task.
"""
from __future__ import annotations

from typing import Any, Dict, List

from browser_manager import BrowserError
from registry import ToolContext, ToolRegistry

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string"},
        "selector": {"type": "string",
                     "description": "CSS selector of the next-page control. "
                                    "Auto-detected when omitted."},
        "max_pages": {"type": "integer", "minimum": 1, "maximum": 20, "default": 3},
    },
    "required": ["task_id"],
}

_NEXT_CANDIDATES = (
    "a[rel='next'], link[rel='next'], a:has-text('Next'), a:has-text('next'), "
    "button:has-text('Next'), button:has-text('Load more'), a:has-text('Load more'), "
    "a:has-text('>'), a.pagination-next, .pagination .next a"
)


async def _handle(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    task = ctx.manager.get_task(args["task_id"])
    page = ctx.manager.active_page(task)
    max_pages = int(args.get("max_pages", 3))
    selector = args.get("selector") or _NEXT_CANDIDATES

    visited: List[str] = [page.url]
    steps = 0
    for _ in range(max_pages - 1):
        loc = page.locator(selector).first
        if await loc.count() == 0:
            break
        await task.polite_wait(ctx.settings.min_request_interval_ms)
        before = page.url
        try:
            await loc.click(timeout=ctx.settings.default_timeout_ms)
        except Exception as e:
            raise BrowserError(f"next-page click failed: {e}", code="tool_timeout") from e
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        await page.wait_for_timeout(800)
        if page.url == before and not await loc.count():
            break
        if page.url not in visited:
            visited.append(page.url)
        steps += 1
        await ctx.emit(task.id, {"event": "paginate_step", "step": steps, "url": page.url})
        if ctx.history:
            ctx.history.record(task.id, page.url, await page.title(), "paginated page")

    return {"task_id": task.id, "pages_visited": steps + 1, "urls": visited,
            "more_available": steps == max_pages - 1}


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="paginate",
        description="Click through next-page / load-more controls up to max_pages, "
                    "returning every visited URL. Auto-detects common controls.",
        schema=SCHEMA,
        handler=_handle,
        tags=["navigation"],
    )
