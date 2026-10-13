"""Tool: page_state — snapshot of the current page (URL, title, timestamp...)."""
from __future__ import annotations

import time
from typing import Any, Dict

from registry import ToolContext, ToolRegistry

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {"task_id": {"type": "string"}},
    "required": ["task_id"],
}


async def _handle(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    task = ctx.manager.get_task(args["task_id"])
    page = ctx.manager.active_page(task)
    metrics = await page.evaluate(
        """({scrollX: window.scrollX, scrollY: window.scrollY,
             innerWidth: window.innerWidth, innerHeight: window.innerHeight,
             scrollHeight: document.body ? document.body.scrollHeight : 0})""")
    return {
        "task_id": task.id,
        "tab_id": task.active_tab,
        "url": page.url,
        "title": await page.title(),
        "timestamp": time.time(),
        "viewport": metrics,
        "tabs": sorted(task.pages.keys()),
    }


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="page_state",
        description="Snapshot of the current page: URL, title, timestamp, "
                    "viewport/scroll metrics, and the task's tab list.",
        schema=SCHEMA,
        handler=_handle,
        tags=["inspection"],
    )
