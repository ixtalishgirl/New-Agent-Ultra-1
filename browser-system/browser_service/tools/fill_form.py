"""Tool: fill_form — fill normal web forms field by field.

Actions: fill / click / select / check / uncheck / press (keyboard key).
SAFETY: password fields are REFUSED unless allow_sensitive=true is passed
explicitly by the operator, and values are never written to history/logs.
This tool never bypasses CAPTCHAs — if the form contains one, the caller
should run detect_blocks first and report it.
"""
from __future__ import annotations

from typing import Any, Dict, List

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from browser_manager import BrowserError
from registry import ToolContext, ToolRegistry

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string"},
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "action": {"type": "string",
                               "enum": ["fill", "click", "select", "check",
                                        "uncheck", "press"],
                               "default": "fill"},
                    "value": {"type": "string",
                              "description": "fill/select text, or key name for press "
                                             "(e.g. 'Enter', 'Tab')."},
                },
                "required": ["selector"],
            },
        },
        "submit": {"type": "boolean", "default": False,
                   "description": "Click the form's submit button after filling."},
        "allow_sensitive": {"type": "boolean", "default": False,
                            "description": "Required to touch input[type=password]."},
    },
    "required": ["task_id", "fields"],
}


async def _handle(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    task = ctx.manager.get_task(args["task_id"])
    page = ctx.manager.active_page(task)
    allow_sensitive = bool(args.get("allow_sensitive", False))
    done: List[Dict[str, str]] = []

    for f in args["fields"]:
        selector, action = f["selector"], f.get("action", "fill")
        value = f.get("value")
        loc = page.locator(selector).first
        if await loc.count() == 0:
            raise BrowserError(f"selector matched nothing: {selector}", code="not_found")

        # Sensitive-field guard. Value is redacted everywhere below.
        is_password = await loc.evaluate(
            "el => el.tagName === 'INPUT' && (el.type || '').toLowerCase() === 'password'")
        if is_password and not allow_sensitive:
            raise BrowserError(
                f"refusing to touch password field ({selector}) without "
                "allow_sensitive=true", code="sensitive_field")

        try:
            if action == "fill":
                await loc.fill(value or "", timeout=ctx.settings.default_timeout_ms)
            elif action == "click":
                await loc.click(timeout=ctx.settings.default_timeout_ms)
            elif action == "select":
                await loc.select_option(value or "", timeout=ctx.settings.default_timeout_ms)
            elif action == "check":
                await loc.check(timeout=ctx.settings.default_timeout_ms)
            elif action == "uncheck":
                await loc.uncheck(timeout=ctx.settings.default_timeout_ms)
            elif action == "press":
                await loc.press(value or "Enter", timeout=ctx.settings.default_timeout_ms)
            else:
                raise BrowserError(f"unknown action: {action}", code="invalid_argument")
        except PlaywrightTimeoutError as e:
            raise BrowserError(f"form action timed out on {selector}: {e}",
                               code="tool_timeout") from e
        done.append({"selector": selector, "action": action,
                     "value": "***redacted***" if is_password else (value or "")})
        await page.wait_for_timeout(300)  # human-like pacing between fields

    submitted = False
    if args.get("submit"):
        submit_btn = page.locator(
            "form button[type='submit'], form input[type='submit']").first
        if await submit_btn.count():
            await task.polite_wait(ctx.settings.min_request_interval_ms)
            await submit_btn.click()
            submitted = True
        else:
            # Fall back to pressing Enter in the last filled field.
            last = page.locator(args["fields"][-1]["selector"]).first
            await last.press("Enter")
            submitted = True
        await page.wait_for_timeout(1500)

    await ctx.emit(task.id, {"event": "fill_form_done",
                             "fields": len(done), "submitted": submitted})
    return {"task_id": task.id, "fields_done": done, "submitted": submitted,
            "url": page.url}


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="fill_form",
        description="Fill normal web forms: fill/click/select/check/uncheck/press per "
                    "field, optional submit. Password fields need allow_sensitive=true. "
                    "Values are redacted from history. Never bypasses CAPTCHAs.",
        schema=SCHEMA,
        handler=_handle,
        tags=["interaction"],
    )
