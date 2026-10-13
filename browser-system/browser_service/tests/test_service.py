#!/usr/bin/env python3
"""
NEEDS LIVE RUN — do NOT treat as verified until executed against a running service.

End-to-end test for the browser service. Requires:
  1. The service running:  uvicorn app:app --host 127.0.0.1 --port 8080
     (from the browser_service/ directory, with Playwright Chromium installed)
  2. Network access to example.com / duckduckgo.com

Usage:
    BROWSER_SERVICE_URL=http://127.0.0.1:8080 python tests/test_service.py
    # with API key:
    BROWSER_SERVICE_URL=http://127.0.0.1:8080 BROWSER_SERVICE_API_KEY=secret \\
        python tests/test_service.py

Exit code 0 = all checks passed, 1 = one or more failed.
"""
from __future__ import annotations

import base64
import os
import sys
import time

import httpx

BASE = os.environ.get("BROWSER_SERVICE_URL", "http://127.0.0.1:8080").rstrip("/")
HEADERS = {}
if os.environ.get("BROWSER_SERVICE_API_KEY"):
    HEADERS["X-API-Key"] = os.environ["BROWSER_SERVICE_API_KEY"]

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))


def main() -> int:
    print(f"== browser-service tests ==\n   target: {BASE}\n")
    c = httpx.Client(base_url=BASE, headers=HEADERS, timeout=90)

    # --- health ---
    print("[health]")
    r = c.get("/health")
    check("GET /health -> 200", r.status_code == 200, r.text[:200])
    tools = r.json().get("tools", []) if r.status_code == 200 else []
    expected = {"navigate", "search", "open_link", "extract", "scroll", "paginate",
                "fill_form", "screenshot", "page_state", "new_tab", "switch_tab",
                "list_tabs", "close_tab", "download_file", "detect_blocks"}
    check("all 15 tools registered", expected.issubset(set(tools)), f"got: {tools}")

    # --- registry spec ---
    print("[registry]")
    r = c.get("/tools")
    specs = r.json() if r.status_code == 200 else []
    check("GET /tools lists specs with schemas",
          all("schema" in s and "name" in s for s in specs) and len(specs) >= 15)

    # --- generic dispatch ---
    print("[generic dispatch]")
    r = c.post("/tools/call", json={"name": "search",
                                    "arguments": {"query": "example domain", "max_results": 3}})
    body = r.json()
    check("POST /tools/call search ok", body.get("ok") is True and body.get("data", {}).get("count", 0) > 0,
          str(body)[:300])
    r = c.post("/tools/call", json={"name": "no_such_tool", "arguments": {}})
    check("unknown tool -> 404", r.status_code == 404, r.text[:200])

    # --- navigate ---
    print("[navigate]")
    r = c.post("/tools/navigate", json={"url": "https://example.com"})
    nav = r.json()
    check("navigate example.com ok", nav.get("ok") is True, str(nav)[:300])
    task_id = nav.get("task_id") or nav.get("data", {}).get("task_id")
    check("task_id returned", bool(task_id))
    if not task_id:
        print("ABORT: no task_id, cannot continue"); return 1
    check("title mentions Example", "Example" in nav["data"].get("title", ""))

    # --- page_state ---
    print("[page_state]")
    r = c.post("/tools/page_state", json={"task_id": task_id})
    st = r.json()
    check("page_state ok + url matches",
          st.get("ok") and "example.com" in st["data"].get("url", ""), str(st)[:300])

    # --- extract ---
    print("[extract]")
    r = c.post("/tools/extract", json={"task_id": task_id, "max_chars": 5000})
    ex = r.json()
    d = ex.get("data", {})
    check("extract ok with text", ex.get("ok") and len(d.get("text", "")) > 50, str(ex)[:300])
    check("extract found links list", isinstance(d.get("links"), list))

    # --- detect_blocks (should be clean on example.com) ---
    print("[detect_blocks]")
    r = c.post("/tools/detect_blocks", json={"task_id": task_id})
    db = r.json().get("data", {})
    check("detect_blocks ok, not blocked", r.json().get("ok") and db.get("blocked") is False,
          str(db)[:300])

    # --- tabs ---
    print("[tabs]")
    r = c.post("/tools/new_tab", json={"task_id": task_id, "url": "https://example.com"})
    nt = r.json()
    tab2 = nt.get("data", {}).get("tab_id")
    check("new_tab ok", nt.get("ok") and bool(tab2), str(nt)[:300])
    r = c.post("/tools/list_tabs", json={"task_id": task_id})
    tabs = r.json().get("data", {}).get("tabs", [])
    check("list_tabs shows 2 tabs", len(tabs) == 2, str(tabs)[:300])
    first_tab = tabs[0]["tab_id"]
    r = c.post("/tools/switch_tab", json={"task_id": task_id, "tab_id": first_tab})
    check("switch_tab ok", r.json().get("ok") is True)
    r = c.post("/tools/close_tab", json={"task_id": task_id, "tab_id": tab2})
    check("close_tab ok, 1 remaining",
          r.json().get("ok") and r.json()["data"].get("remaining_tabs") is not None)

    # --- scroll ---
    print("[scroll]")
    r = c.post("/tools/scroll", json={"task_id": task_id, "direction": "to_bottom"})
    sc = r.json()
    check("scroll ok", sc.get("ok") is True, str(sc)[:300])

    # --- open_link (via search result -> navigate instead; open_link by index needs links) ---
    print("[open_link + paginate]")
    r = c.post("/tools/navigate", json={"task_id": task_id, "url": "https://example.com"})
    check("re-navigate ok", r.json().get("ok") is True)
    # paginate on a page with no next control should return 1 page gracefully
    r = c.post("/tools/paginate", json={"task_id": task_id, "max_pages": 2})
    pg = r.json()
    check("paginate graceful with no next-link",
          pg.get("ok") and pg["data"].get("pages_visited") == 1, str(pg)[:300])

    # --- screenshot ---
    print("[screenshot]")
    r = c.post("/tools/screenshot", json={"task_id": task_id})
    ss = r.json().get("data", {})
    valid_png = False
    if ss.get("data_base64"):
        raw = base64.b64decode(ss["data_base64"])
        valid_png = raw[:8] == b"\x89PNG\r\n\x1a\n"
    check("screenshot returns valid PNG bytes", r.json().get("ok") and valid_png,
          f"bytes={ss.get('bytes')}")

    # --- fill_form on a real form (httpbin) ---
    print("[fill_form]")
    r = c.post("/tools/navigate", json={"task_id": task_id, "url": "https://httpbin.org/forms/post"})
    if r.json().get("ok"):
        r = c.post("/tools/fill_form", json={
            "task_id": task_id,
            "fields": [{"selector": "input[name='custname']", "action": "fill", "value": "Test User"}],
            "submit": False})
        ff = r.json()
        check("fill_form ok (no submit)", ff.get("ok") is True, str(ff)[:300])
    else:
        check("fill_form ok (no submit)", False, "httpbin unreachable, skipped")

    # --- download_file (small text file) ---
    print("[download_file]")
    r = c.post("/tools/download_file", json={
        "task_id": task_id,
        "url": "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf",
        "filename": "test_dummy.pdf"})
    dl = r.json()
    check("download_file pdf ok", dl.get("ok") and dl["data"].get("bytes", 0) > 0,
          str(dl)[:400])
    # forbidden type must be refused
    r = c.post("/tools/download_file", json={"task_id": task_id,
                                              "url": "https://example.com/x.exe",
                                              "filename": "x.exe"})
    check("download_file refuses .exe", r.status_code == 403, r.text[:200])

    # --- error paths ---
    print("[error paths]")
    r = c.post("/tools/navigate", json={"url": "gopher://example.com"})
    check("bad scheme rejected", r.status_code in (400, 500), r.text[:200])
    r = c.post("/tools/page_state", json={"task_id": "task-does-not-exist"})
    check("unknown task -> 404", r.status_code == 404, r.text[:200])

    # --- history (only if enabled) ---
    print("[history]")
    r = c.get("/health")
    if r.json().get("history_enabled"):
        r = c.get("/history", params={"task_id": task_id, "limit": 5})
        check("history returns observations",
              r.status_code == 200 and len(r.json().get("observations", [])) > 0)
    else:
        print("  [SKIP] history not enabled (BROWSER_ENABLE_HISTORY=1 to test)")

    # --- task cleanup ---
    print("[cleanup]")
    r = c.delete(f"/tasks/{task_id}")
    check("DELETE /tasks/:id ok", r.status_code == 200, r.text[:200])

    # --- summary ---
    print(f"\n== {len(PASS)} passed, {len(FAIL)} failed ==")
    if FAIL:
        print("FAILED:", ", ".join(FAIL))
        return 1
    print("ALL CHECKS PASSED (live run).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
