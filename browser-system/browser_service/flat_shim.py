"""Flat REST shim (Side A contract) over the /tools/* registry.

The Node integration layer (browserActions.js / browserRoutes.js) expects
flat, stateless endpoints::

    GET  /status        -> { ok, service, playwright, egress_ip }
    POST /navigate      { url }                -> { ok, url, title, blocked?, block_kind? }
    POST /search        { query, max_results } -> { ok, results: [{ title, url, snippet }] }
    POST /extract       { url }                -> { ok, url, title, facts, text, accessed_at }
    POST /screenshot    { url }                -> { ok, url, title, screenshot_base64, mime }
    POST /detect_blocks { url }                -> { ok, blocked, block_kind }
    GET  /tabs                                 -> { ok, tabs: [...] }
    POST /download      { url }                -> { ok, path?, note? }
    GET  /history                              -> { ok, entries: [...] }

This module adds those routes ADDITIVELY. Each handler delegates to the
existing tool registry and auto-manages a per-request task
(create -> act -> close), so the Node side works unchanged and the
/tools/* contract (envelope, task_id lifecycle) is untouched.

See browser-system/CONTRACT_CHECK.md (Option A, now implemented).

Safety note: page content is DATA. Nothing scraped from a page is ever
treated as instructions. Block detection is detect-only; this shim never
solves or bypasses CAPTCHAs / bot checks.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable, Dict, List, Optional

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from browser_manager import BrowserError

# Side B block_type -> Side A block_kind vocabulary mapping.
_BLOCK_KIND_MAP = {
    "captcha": "captcha",
    "bot_check": "cloudflare",   # PerimeterX / DataDome / Cloudflare challenge pages
    "login_wall": "login",
    "blocked": "waf",            # generic access-denied / WAF block page
    "rate_limited": "waf",       # no closer Side A kind exists
}

# HTTP status mapping for tool errors (mirrors app._CODE_STATUS).
_CODE_STATUS = {
    "task_not_found": 404,
    "tab_not_found": 404,
    "not_found": 404,
    "invalid_argument": 400,
    "forbidden_file_type": 403,
    "sensitive_field": 403,
    "blocked_host": 403,
    "robots_disallowed": 403,
    "page_blocked": 403,
    "too_many_tasks": 429,
    "too_many_tabs": 429,
    "tool_timeout": 504,
    "navigation_failed": 502,
    "download_failed": 502,
    "search_failed": 502,
    "file_too_large": 413,
    "not_a_file": 400,
}


class _ShimHTTPError(Exception):
    """Raised inside shim handlers; converted to a flat {ok: False, ...} body."""

    def __init__(self, status_code: int, body: Dict[str, Any]):
        super().__init__(body.get("error", "shim error"))
        self.status_code = status_code
        self.body = body


# ---------- request bodies ----------

class _UrlBody(BaseModel):
    url: str


class _SearchBody(BaseModel):
    query: str
    max_results: int = 10


# ---------- egress IP (for the Germany verifier; cached 120s) ----------

_egress_cache: Dict[str, Any] = {"ip": None, "at": 0.0}


async def _egress_ip(settings) -> Optional[str]:
    now = time.time()
    if _egress_cache["ip"] and now - _egress_cache["at"] < 120:
        return _egress_cache["ip"]
    proxies = None
    proxy = settings.https_proxy or settings.http_proxy
    if proxy:
        proxies = {"http://": proxy, "https://": proxy}
    try:
        async with httpx.AsyncClient(timeout=5, proxies=proxies) as client:
            r = await client.get("https://api.ipify.org?format=json")
            r.raise_for_status()
            ip = (r.json().get("ip") or "").strip()
            if ip:
                _egress_cache.update(ip=ip, at=now)
                return ip
    except Exception:
        pass
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def register_flat_routes(
    app: FastAPI,
    *,
    registry,
    make_ctx: Callable[[], Any],
    manager,
    settings,
    history_fn: Optional[Callable[[], Any]] = None,
) -> None:
    """Register the flat Side A routes on *app*. Purely additive."""

    async def _tool(ctx, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Run a registry tool; on failure raise _ShimHTTPError (flat body)."""
        tool = registry.get(name)  # KeyError -> 404 unknown_tool
        try:
            return await tool.handler(ctx, args)
        except BrowserError as e:
            raise _ShimHTTPError(
                _CODE_STATUS.get(e.code, 500),
                {"ok": False, "error": e.message, "error_code": e.code,
                 "details": e.details},
            ) from e
        except KeyError as e:
            raise _ShimHTTPError(
                404, {"ok": False, "error": str(e), "error_code": "unknown_tool"}
            ) from e
        except Exception as e:  # never leak an unstructured 500
            raise _ShimHTTPError(
                500, {"ok": False, "error": f"{type(e).__name__}: {e}",
                      "error_code": "internal_error"}
            ) from e

    async def _close_quietly(task_id: Optional[str]) -> None:
        if not task_id:
            return
        try:
            await manager.close_task(task_id)
        except Exception:
            pass  # best-effort cleanup; the task TTL reaps stragglers

    def _flat(fn):
        @wraps(fn)
        async def inner(*a, **k):
            try:
                return await fn(*a, **k)
            except _ShimHTTPError as e:
                return JSONResponse(status_code=e.status_code, content=e.body)

        return inner

    # ----- GET /status -----
    @app.get("/status")
    @_flat
    async def flat_status():
        return {
            "ok": True,
            "service": "browser-service",
            "playwright": getattr(manager, "_browser", None) is not None,
            "browser_error": getattr(manager, "browser_error", None),
            "browser_lazy": getattr(manager, "_browser", None) is None
                            and not getattr(manager, "browser_error", None),
            "egress_ip": await _egress_ip(settings),
        }

    # ----- POST /navigate -----
    @app.post("/navigate")
    @_flat
    async def flat_navigate(body: _UrlBody):
        ctx = make_ctx()
        nav = await _tool(ctx, "navigate", {"url": body.url})
        tid = nav.get("task_id")
        blocked, block_kind = False, None
        try:
            # Best-effort block verdict bundled into the navigate response
            # (Side A expects blocked/block_kind here). A detect failure must
            # not fail an otherwise good navigation; use /detect_blocks
            # for the strict check.
            try:
                verdict = await _tool(ctx, "detect_blocks", {"task_id": tid})
                blocked = bool(verdict.get("blocked"))
                if blocked:
                    block_kind = _BLOCK_KIND_MAP.get(verdict.get("block_type"))
            except _ShimHTTPError:
                pass
        finally:
            await _close_quietly(tid)
        return {"ok": True, "url": nav.get("url"), "title": nav.get("title"),
                "blocked": blocked, "block_kind": block_kind}

    # ----- POST /search -----
    @app.post("/search")
    @_flat
    async def flat_search(body: _SearchBody):
        ctx = make_ctx()
        data = await _tool(ctx, "search",
                            {"query": body.query, "max_results": body.max_results})
        # Registry already returns [{title, url, snippet}] — pass through.
        return {"ok": True, "results": data.get("results", [])}

    # ----- POST /extract -----
    @app.post("/extract")
    @_flat
    async def flat_extract(body: _UrlBody):
        ctx = make_ctx()
        nav = await _tool(ctx, "navigate", {"url": body.url})
        tid = nav.get("task_id")
        try:
            data = await _tool(ctx, "extract", {"task_id": tid})
        finally:
            await _close_quietly(tid)
        headings = data.get("headings") or []
        facts: List[Dict[str, str]] = [
            {"label": "heading", "value": str(h)}
            for h in headings[:10] if str(h).strip()
        ]
        return {"ok": True, "url": data.get("url"), "title": data.get("title"),
                "facts": facts, "text": data.get("text", ""),
                "accessed_at": _now_iso()}

    # ----- POST /screenshot -----
    @app.post("/screenshot")
    @_flat
    async def flat_screenshot(body: _UrlBody):
        ctx = make_ctx()
        nav = await _tool(ctx, "navigate", {"url": body.url})
        tid = nav.get("task_id")
        try:
            data = await _tool(ctx, "screenshot", {"task_id": tid})
        finally:
            await _close_quietly(tid)
        # Side A reads d.screenshot_base64 (Side B calls it data_base64).
        return {"ok": True, "url": data.get("url"),
                "title": nav.get("title") or data.get("url"),
                "screenshot_base64": data.get("data_base64"),
                "mime": data.get("mime", "image/png")}

    # ----- POST /detect_blocks -----
    @app.post("/detect_blocks")
    @_flat
    async def flat_detect_blocks(body: _UrlBody):
        ctx = make_ctx()
        nav = await _tool(ctx, "navigate", {"url": body.url})
        tid = nav.get("task_id")
        try:
            verdict = await _tool(ctx, "detect_blocks", {"task_id": tid})
        finally:
            await _close_quietly(tid)
        blocked = bool(verdict.get("blocked"))
        return {"ok": True, "blocked": blocked,
                "block_kind": _BLOCK_KIND_MAP.get(verdict.get("block_type"))
                if blocked else None}

    # ----- GET /tabs -----
    @app.get("/tabs")
    @_flat
    async def flat_tabs():
        tabs: List[Dict[str, Any]] = []
        for tid in manager.task_ids():
            try:
                task = manager.get_task(tid)
            except Exception:
                continue
            for tab_id, page in task.pages.items():
                try:
                    title = await page.title()
                except Exception:
                    title = ""
                tabs.append({"tab_id": tab_id, "url": getattr(page, "url", ""),
                             "title": title, "active": tab_id == task.active_tab,
                             "task_id": tid})
        return {"ok": True, "tabs": tabs}

    # ----- POST /download -----
    @app.post("/download")
    @_flat
    async def flat_download(body: _UrlBody):
        ctx = make_ctx()
        # download_file needs an existing task with a tab; navigate first.
        nav = await _tool(ctx, "navigate", {"url": body.url})
        tid = nav.get("task_id")
        try:
            data = await _tool(ctx, "download_file",
                                {"task_id": tid, "url": body.url})
        finally:
            await _close_quietly(tid)
        note = f"downloaded {data.get('bytes', 0)} bytes -> {data.get('filename')}"
        if data.get("warning"):
            note += f" (warning: {data['warning']})"
        return {"ok": True, "path": data.get("path"), "note": note}

    # ----- GET /history -----
    @app.get("/history")
    @_flat
    async def flat_history():
        hist = history_fn() if history_fn else None
        if not hist:
            return {"ok": True, "entries": [],
                    "note": "history not enabled (set BROWSER_ENABLE_HISTORY=1)"}
        return {"ok": True, "entries": hist.list(task_id=None, limit=100)}
