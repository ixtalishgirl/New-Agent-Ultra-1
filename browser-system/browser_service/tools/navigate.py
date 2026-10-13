"""Tool: navigate — go to a URL in the task's active tab (or a fresh tab)."""
from __future__ import annotations

from typing import Any, Dict

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from browser_manager import BrowserError, NavigationError, Task
from registry import ToolContext, ToolRegistry
from ._common import normalize_url, robots_allowed


SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string", "description": "Existing task, or omit to start a new one."},
        "url": {"type": "string", "description": "http(s) URL to visit."},
        "wait_until": {
            "type": "string", "enum": ["load", "domcontentloaded", "networkidle"],
            "default": "domcontentloaded",
        },
        "timeout_ms": {"type": "integer", "description": "Override navigation timeout."},
        "force_robots": {
            "type": "boolean", "default": False,
            "description": "Visit even if robots.txt disallows it. Only the human operator "
                           "(ahsan) may set this, never automatically.",
        },
    },
    "required": ["url"],
}


async def _handle(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    settings = ctx.settings
    url = normalize_url(args["url"])
    wait_until = args.get("wait_until", "domcontentloaded")
    timeout_ms = args.get("timeout_ms") or settings.navigation_timeout_ms

    # robots.txt politeness
    if settings.respect_robots and not args.get("force_robots"):
        allowed, robots_src = robots_allowed(url, user_agent="*")
        if not allowed:
            raise BrowserError(
                f"robots.txt disallows this URL ({robots_src}). "
                "Set force_robots=true only on ahsan's explicit instruction.",
                code="robots_disallowed",
                details={"robots_txt": robots_src},
            )

    task: Task = await ctx.manager.get_or_create_task(args.get("task_id"))
    ctx.manager._check_host(url)
    await task.polite_wait(settings.min_request_interval_ms)

    await ctx.emit(task.id, {"event": "navigate_start", "url": url})
    page = ctx.manager.active_page(task) if task.pages else None
    if page is None:
        info = await ctx.manager.new_tab(task)
        page = ctx.manager.active_page(task)
    else:
        info = {}
    try:
        resp = await page.goto(url, wait_until=wait_until, timeout=timeout_ms)  # type: ignore[arg-type]
    except PlaywrightTimeoutError as e:
        raise NavigationError(f"navigation timed out after {timeout_ms}ms: {url}") from e
    except Exception as e:
        raise NavigationError(f"navigation failed: {e}") from e

    status = resp.status if resp else None
    title = await page.title()
    final_url = page.url
    await ctx.emit(task.id, {"event": "navigate_done", "url": final_url, "status": status})

    if ctx.history:
        ctx.history.record(task.id, final_url, title, f"navigated (HTTP {status})")

    return {
        "task_id": task.id,
        "url": final_url,
        "title": title,
        "http_status": status,
        "tab_id": task.active_tab,
    }


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="navigate",
        description="Navigate the task's active tab to a URL (creates the task/tab if needed). "
                    "Respects robots.txt unless force_robots is set by the operator. "
                    "Rate-limited per task.",
        schema=SCHEMA,
        handler=_handle,
        tags=["navigation"],
    )
