#!/usr/bin/env python3
"""Offline unit tests for the flat REST shim (flat_shim.py).

Runs WITHOUT a browser: the tool registry, browser manager and settings
are faked, so no Playwright Chromium or network is needed. Verifies that
the Side A flat contract (see CONTRACT_CHECK.md, Option A) is served with
the exact field shapes the Node integration layer expects.

Usage:
    <venv>/bin/python -m pytest tests/test_flat_shim.py -q
    # or simply:
    <venv>/bin/python tests/test_flat_shim.py        (runs a tiny runner)
"""
from __future__ import annotations

import base64
import sys
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.testclient import TestClient

import flat_shim
from browser_manager import BrowserError

_TINY_PNG_B64 = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d49444154789c626001000000ffff030000060005"
        "0b0d0a2db40000000049454e44ae426082"
    )
).decode("ascii")


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

class FakeTool:
    def __init__(self, handler):
        self.handler = handler


class FakeRegistry:
    def __init__(self, handlers):
        self._handlers = handlers

    def get(self, name):
        try:
            return FakeTool(self._handlers[name])
        except KeyError:
            raise KeyError(f"unknown tool: {name}") from None


class FakePage:
    def __init__(self, url="https://example.com", title="Example"):
        self.url = url
        self._title = title

    async def title(self):
        return self._title


class FakeTask:
    def __init__(self, tid):
        self.id = tid
        self.pages = {"tab-1": FakePage()}
        self.active_tab = "tab-1"


class FakeManager:
    """Mimics the BrowserManager surface the shim uses."""

    def __init__(self):
        self._tasks: Dict[str, FakeTask] = {}
        self._browser = object()  # pretend chromium launched
        self.closed: list[str] = []
        self._n = 0

    async def get_or_create_task(self, task_id=None):
        if task_id and task_id in self._tasks:
            return self._tasks[task_id]
        self._n += 1
        tid = task_id or f"task-{self._n}"
        self._tasks[tid] = FakeTask(tid)
        return self._tasks[tid]

    def get_task(self, task_id):
        return self._tasks[task_id]

    async def close_task(self, task_id):
        self.closed.append(task_id)
        self._tasks.pop(task_id, None)

    def task_ids(self):
        return sorted(self._tasks.keys())


class FakeSettings:
    https_proxy = None
    http_proxy = None


def _ctx():
    return object()  # handlers below ignore ctx


def make_handlers(block_type=None):
    async def h_navigate(ctx, args):
        return {"task_id": "task-9", "url": args["url"], "title": "Example Domain",
                "http_status": 200, "tab_id": "tab-9"}

    async def h_detect_blocks(ctx, args):
        return {"task_id": args["task_id"], "blocked": block_type is not None,
                "block_type": block_type, "signals": [], "url": "https://example.com"}

    async def h_search(ctx, args):
        return {"engine": "duckduckgo", "query": args["query"], "count": 1,
                "results": [{"title": "T", "url": "https://x.example",
                             "snippet": "S"}]}

    async def h_extract(ctx, args):
        return {"task_id": args["task_id"], "url": "https://example.com",
                "title": "Example Domain", "text": "hello world",
                "headings": ["H1", "H2"], "links": [], "tables": [], "forms": []}

    async def h_screenshot(ctx, args):
        return {"task_id": args["task_id"], "url": "https://example.com",
                "mime": "image/png", "bytes": 120, "data_base64": _TINY_PNG_B64}

    async def h_download_file(ctx, args):
        return {"task_id": args["task_id"], "filename": "f.txt",
                "path": "/tmp/dl/f.txt", "bytes": 10, "sha256": "abc",
                "warning": None}

    return {"navigate": h_navigate, "detect_blocks": h_detect_blocks,
            "search": h_search, "extract": h_extract,
            "screenshot": h_screenshot, "download_file": h_download_file}


def build_client(handlers, history=None):
    app = FastAPI()
    manager = FakeManager()
    flat_shim.register_flat_routes(
        app, registry=FakeRegistry(handlers), make_ctx=_ctx,
        manager=manager, settings=FakeSettings(),
        history_fn=(lambda: history),
    )
    return TestClient(app), manager


# --------------------------------------------------------------------------
# tiny test runner (no pytest needed)
# --------------------------------------------------------------------------

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" +
          (f" — {detail}" if detail and not cond else ""))


def main() -> int:
    print("== flat shim offline tests ==\n")

    # --- GET /status ---
    c, m = build_client(make_handlers())
    r = c.get("/status")
    b = r.json()
    check("GET /status -> 200", r.status_code == 200, r.text[:200])
    check("status shape {ok, service, playwright, egress_ip}",
          b.get("ok") is True and b.get("service") == "browser-service"
          and b.get("playwright") is True and "egress_ip" in b, str(b)[:200])

    # --- POST /navigate (clean) ---
    r = c.post("/navigate", json={"url": "https://example.com"})
    b = r.json()
    check("POST /navigate -> 200", r.status_code == 200, r.text[:200])
    check("navigate flat shape",
          b.get("ok") is True and b.get("url") == "https://example.com"
          and b.get("title") == "Example Domain"
          and b.get("blocked") is False and b.get("block_kind") is None,
          str(b)[:200])
    check("navigate closed its task", m.closed == ["task-9"], str(m.closed))

    # --- POST /navigate (blocked -> block_kind mapping) ---
    c2, _ = build_client(make_handlers(block_type="bot_check"))
    b = c2.post("/navigate", json={"url": "https://example.com"}).json()
    check("navigate maps bot_check -> cloudflare",
          b.get("blocked") is True and b.get("block_kind") == "cloudflare",
          str(b)[:200])

    c3, _ = build_client(make_handlers(block_type="login_wall"))
    b = c3.post("/navigate", json={"url": "https://example.com"}).json()
    check("navigate maps login_wall -> login", b.get("block_kind") == "login")

    c4, _ = build_client(make_handlers(block_type="captcha"))
    b = c4.post("/navigate", json={"url": "https://example.com"}).json()
    check("navigate maps captcha -> captcha", b.get("block_kind") == "captcha")

    # --- POST /search ---
    r = c.post("/search", json={"query": "german jobs", "max_results": 5})
    b = r.json()
    check("POST /search -> flat results",
          r.status_code == 200 and b.get("ok") is True
          and b.get("results") == [{"title": "T", "url": "https://x.example",
                                    "snippet": "S"}], str(b)[:200])

    # --- POST /extract ---
    r = c.post("/extract", json={"url": "https://example.com"})
    b = r.json()
    check("POST /extract -> flat shape",
          r.status_code == 200 and b.get("ok") is True
          and b.get("text") == "hello world"
          and {"label": "heading", "value": "H1"} in b.get("facts", [])
          and b.get("accessed_at"), str(b)[:300])

    # --- POST /screenshot ---
    r = c.post("/screenshot", json={"url": "https://example.com"})
    b = r.json()
    check("POST /screenshot -> screenshot_base64 (not data_base64)",
          r.status_code == 200 and b.get("ok") is True
          and b.get("screenshot_base64") == _TINY_PNG_B64
          and b.get("mime") == "image/png"
          and "data_base64" not in b, str(b)[:200])
    png = base64.b64decode(b.get("screenshot_base64") or "")
    check("screenshot decodes to PNG", png[:8] == b"\x89PNG\r\n\x1a\n")

    # --- POST /detect_blocks ---
    r = c.post("/detect_blocks", json={"url": "https://example.com"})
    b = r.json()
    check("POST /detect_blocks -> {ok, blocked, block_kind}",
          r.status_code == 200 and b == {"ok": True, "blocked": False,
                                         "block_kind": None}, str(b)[:200])

    # --- GET /tabs ---
    c5, m5 = build_client(make_handlers())
    import asyncio
    asyncio.run(m5.get_or_create_task(None))
    r = c5.get("/tabs")
    b = r.json()
    check("GET /tabs -> {ok, tabs}",
          r.status_code == 200 and b.get("ok") is True
          and len(b.get("tabs", [])) == 1
          and b["tabs"][0]["tab_id"] == "tab-1", str(b)[:200])

    # --- POST /download ---
    r = c.post("/download", json={"url": "https://example.com/f.txt"})
    b = r.json()
    check("POST /download -> {ok, path, note}",
          r.status_code == 200 and b.get("ok") is True
          and b.get("path") == "/tmp/dl/f.txt"
          and "f.txt" in (b.get("note") or ""), str(b)[:200])

    # --- GET /history (disabled) ---
    r = c.get("/history")
    b = r.json()
    check("GET /history disabled -> {ok, entries: []}",
          r.status_code == 200 and b.get("ok") is True
          and b.get("entries") == [], str(b)[:200])

    # --- GET /history (enabled) ---
    class FakeHistory:
        def list(self, task_id=None, limit=100):
            return [{"task_id": "t", "url": "https://example.com",
                     "title": "T", "summary": "s"}]

    c6, _ = build_client(make_handlers(), history=FakeHistory())
    b = c6.get("/history").json()
    check("GET /history enabled -> {ok, entries}",
          b.get("ok") is True and len(b.get("entries", [])) == 1
          and "observations" not in b, str(b)[:200])

    # --- error path: robots_disallowed -> flat 403, task cleaned up ---
    async def h_nav_robots(ctx, args):
        raise BrowserError("robots.txt disallows this URL", code="robots_disallowed",
                           details={"robots_txt": "https://example.com/robots.txt"})

    handlers = make_handlers()
    handlers["navigate"] = h_nav_robots
    c7, m7 = build_client(handlers)
    r = c7.post("/extract", json={"url": "https://example.com"})
    b = r.json()
    check("robots block -> flat 403 {ok:false, error_code}",
          r.status_code == 403 and b.get("ok") is False
          and b.get("error_code") == "robots_disallowed", r.text[:200])

    print(f"\n== {len(PASS)} passed, {len(FAIL)} failed ==")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
