"""Tools: new_tab / switch_tab / list_tabs / close_tab.

Each tab is an isolated Page inside the task's BrowserContext.
Registered as four separate tools so the REST API has one endpoint per action.
"""
from __future__ import annotations

from typing import Any, Dict

from registry import ToolContext, ToolRegistry
from ._common import normalize_url


async def _tabs_snapshot(ctx: ToolContext, task) -> list[dict]:
    out = []
    for tab_id, page in task.pages.items():
        try:
            title = await page.title()
        except Exception:
            title = ""
        out.append({"tab_id": tab_id, "url": page.url, "title": title,
                    "active": tab_id == task.active_tab})
    return out


def register(registry: ToolRegistry) -> None:

    async def _new_tab(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
        task = ctx.manager.get_task(args["task_id"])
        url = args.get("url")
        if url:
            url = normalize_url(url)
        info = await ctx.manager.new_tab(task, url)
        page = ctx.manager.active_page(task)
        await ctx.emit(task.id, {"event": "tab_opened", "tab_id": info["tab_id"]})
        return {"task_id": task.id, "tab_id": info["tab_id"],
                "url": page.url, "title": await page.title(),
                "http_status": info.get("status"),
                "tabs": await _tabs_snapshot(ctx, task)}

    async def _switch_tab(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
        task = ctx.manager.get_task(args["task_id"])
        page = ctx.manager.switch_tab(task, args["tab_id"])
        return {"task_id": task.id, "tab_id": task.active_tab,
                "url": page.url, "title": await page.title()}

    async def _list_tabs(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
        task = ctx.manager.get_task(args["task_id"])
        return {"task_id": task.id, "tabs": await _tabs_snapshot(ctx, task)}

    async def _close_tab(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
        task = ctx.manager.get_task(args["task_id"])
        remaining = await ctx.manager.close_tab(task, args["tab_id"])
        await ctx.emit(task.id, {"event": "tab_closed", "tab_id": args["tab_id"]})
        return {"task_id": task.id, "closed": args["tab_id"],
                "active_tab": task.active_tab, "remaining_tabs": remaining}

    registry.register(
        name="new_tab",
        description="Open a new tab in the task's context (optionally at a URL). "
                    "The new tab becomes active.",
        schema={"type": "object",
                "properties": {"task_id": {"type": "string"},
                               "url": {"type": "string"}},
                "required": ["task_id"]},
        handler=_new_tab, tags=["tabs"])

    registry.register(
        name="switch_tab",
        description="Switch the task's active tab.",
        schema={"type": "object",
                "properties": {"task_id": {"type": "string"},
                               "tab_id": {"type": "string"}},
                "required": ["task_id", "tab_id"]},
        handler=_switch_tab, tags=["tabs"])

    registry.register(
        name="list_tabs",
        description="List all tabs in the task with URL/title/active flag.",
        schema={"type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"]},
        handler=_list_tabs, tags=["tabs"])

    registry.register(
        name="close_tab",
        description="Close a tab. If it was active, another tab becomes active.",
        schema={"type": "object",
                "properties": {"task_id": {"type": "string"},
                               "tab_id": {"type": "string"}},
                "required": ["task_id", "tab_id"]},
        handler=_close_tab, tags=["tabs"])
