"""Tool: open_link — click a link on the current page.

Address a link by the index from the last extract() call, by href/text
match, or by CSS selector. Optionally opens in a new tab.
"""
from __future__ import annotations

from typing import Any, Dict

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from browser_manager import BrowserError, NavigationError, Task
from registry import ToolContext, ToolRegistry

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string"},
        "index": {"type": "integer",
                  "description": "Index into the links list returned by the last extract()."},
        "href": {"type": "string", "description": "Substring matched against link href."},
        "text": {"type": "string", "description": "Substring matched against link text."},
        "selector": {"type": "string", "description": "CSS selector of the <a> element."},
        "new_tab": {"type": "boolean", "default": False},
    },
    "required": ["task_id"],
}


async def _resolve_target(task: Task, args: Dict[str, Any]) -> str:
    """Return an href to navigate to."""
    if args.get("index") is not None:
        idx = int(args["index"])
        try:
            return task.last_extract_links[idx]["href"]
        except IndexError:
            raise BrowserError(
                f"link index {idx} out of range "
                f"(last extract found {len(task.last_extract_links)} links)",
                code="invalid_argument") from None
    # href/text/selector need the live page
    return ""


async def _handle(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    task: Task = ctx.manager.get_task(args["task_id"])
    href = await _resolve_target(task, args)

    if not href and not any(args.get(k) for k in ("href", "text", "selector")):
        raise BrowserError(
            "specify one of: index, href, text, selector", code="invalid_argument")

    await task.polite_wait(ctx.settings.min_request_interval_ms)
    await ctx.emit(task.id, {"event": "open_link_start", "args": {k: v for k, v in args.items() if k != "task_id"}})

    if href:
        # Fast path: direct navigation to a known href (same tab or new tab).
        ctx.manager._check_host(href)
        if args.get("new_tab"):
            info = await ctx.manager.new_tab(task, href)
            page = ctx.manager.active_page(task)
            result = {"tab_id": info["tab_id"], "url": page.url,
                      "title": await page.title(), "http_status": info.get("status")}
        else:
            page = ctx.manager.active_page(task)
            try:
                resp = await page.goto(href, wait_until="domcontentloaded")
            except PlaywrightTimeoutError as e:
                raise NavigationError(f"open_link timed out: {href}") from e
            except Exception as e:
                raise NavigationError(f"open_link failed: {e}") from e
            result = {"tab_id": task.active_tab, "url": page.url,
                      "title": await page.title(),
                      "http_status": resp.status if resp else None}
    else:
        # Click path: find the element and click it.
        page = ctx.manager.active_page(task)
        locator = None
        if args.get("selector"):
            locator = page.locator(args["selector"]).first
        else:
            # Find by href/text via JS to keep selector logic in one place.
            handle = await page.evaluate(
                """({href, text}) => {
                    const as = Array.from(document.querySelectorAll('a[href]'));
                    const h = (href || '').toLowerCase(), t = (text || '').toLowerCase();
                    const el = as.find(a =>
                        (!h || (a.getAttribute('href') || '').toLowerCase().includes(h)) &&
                        (!t || (a.innerText || '').toLowerCase().includes(t)));
                    return el ? el.getAttribute('href') : null;
                }""",
                {"href": args.get("href") or "", "text": args.get("text") or ""},
            )
            if not handle:
                raise BrowserError("no link matched href/text criteria", code="not_found")
            ctx.manager._check_host(handle)
            if args.get("new_tab"):
                info = await ctx.manager.new_tab(task, handle)
                page = ctx.manager.active_page(task)
                result = {"tab_id": info["tab_id"], "url": page.url,
                          "title": await page.title(), "http_status": info.get("status")}
                await ctx.emit(task.id, {"event": "open_link_done", **result})
                return {"task_id": task.id, **result}
            locator = page.locator(f'a[href="{handle}"]').first

        try:
            if args.get("new_tab"):
                async with task.context.expect_page() as new_page_info:
                    await locator.click(timeout=ctx.settings.default_timeout_ms)
                new_page = await new_page_info.value
                tab_id = f"tab-{id(new_page):x}"[-12:]
                # normalize through manager registry
                created = await ctx.manager.new_tab(task)
                # replace the blank page with the real one
                blank = ctx.manager.get_page(task, created["tab_id"])
                await blank.close()
                task.pages[created["tab_id"]] = new_page
                task.active_tab = created["tab_id"]
                await new_page.wait_for_load_state("domcontentloaded")
                result = {"tab_id": created["tab_id"], "url": new_page.url,
                          "title": await new_page.title(), "http_status": None}
            else:
                await locator.click(timeout=ctx.settings.default_timeout_ms)
                await page.wait_for_load_state("domcontentloaded")
                result = {"tab_id": task.active_tab, "url": page.url,
                          "title": await page.title(), "http_status": None}
        except PlaywrightTimeoutError as e:
            raise BrowserError(f"click timed out: {e}", code="tool_timeout") from e

    await ctx.emit(task.id, {"event": "open_link_done", **result})
    if ctx.history:
        ctx.history.record(task.id, result["url"], result.get("title", ""), "opened link")
    return {"task_id": task.id, **result}


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="open_link",
        description="Open a link from the current page: by extract() index, href/text "
                    "match, or CSS selector. Same tab by default, new_tab=true to keep "
                    "the current page open.",
        schema=SCHEMA,
        handler=_handle,
        tags=["navigation"],
    )
