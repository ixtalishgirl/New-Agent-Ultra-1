#!/usr/bin/env python3
"""
NEEDS LIVE RUN — do NOT treat as verified until executed against a running service.

Screenshot + URL + title + timestamp capture test.

Captures a real rendered screenshot of a target URL through the browser
service, stamps it with URL + page title + ISO timestamp, and saves:
  - the PNG to tests/artifacts/<slug>.png
  - a JSON sidecar <slug>.json with { url, title, timestamp, sha256, bytes }

Requires:
  1. The browser service running:
       cd browser_service && uvicorn app:app --host 127.0.0.1 --port 8080
     (with Playwright Chromium installed:  playwright install --with-deps chromium)
  2. Network access to the target URL.

Usage:
    python tests/test_screenshot_url.py [URL]
    # default URL: https://example.com
    BROWSER_SERVICE_URL=http://127.0.0.1:8080 python tests/test_screenshot_url.py https://example.com

Exit code 0 = screenshot captured and verified, 1 = any check failed.

NOTE ON CONTRACT: this test targets the browser service's IMPLEMENTED API
(POST /tools/navigate, POST /tools/detect_blocks, POST /tools/screenshot,
DELETE /tasks/{id}) — see browser_service/API.md. If the CONTRACT_CHECK.md
shim (flat /status, /navigate, /screenshot endpoints) is adopted, set
USE_FLAT_SHIM=1 to exercise the shim shapes instead.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

BASE = os.environ.get("BROWSER_SERVICE_URL", "http://127.0.0.1:8080").rstrip("/")
TARGET_URL = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
USE_FLAT_SHIM = os.environ.get("USE_FLAT_SHIM") == "1"

HEADERS = {"Content-Type": "application/json"}
if os.environ.get("BROWSER_SERVICE_API_KEY"):
    HEADERS["X-API-Key"] = os.environ["BROWSER_SERVICE_API_KEY"]

ARTIFACT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts")

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))


def slug(url: str) -> str:
    host = urlparse(url).netloc or "page"
    s = re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-") or "page"
    return s[:60]


def main() -> int:
    print(f"== screenshot capture test ==\n   service: {BASE}\n   target:  {TARGET_URL}\n   flat-shim mode: {USE_FLAT_SHIM}\n")
    os.makedirs(ARTIFACT_DIR, exist_ok=True)
    c = httpx.Client(base_url=BASE, headers=HEADERS, timeout=120)

    # --- service reachable ---
    try:
        r = c.get("/health")
    except Exception as e:  # noqa: BLE001 - honest failure, service down
        print(f"[FAIL] service unreachable: {e}")
        return 1
    check("GET /health -> 200", r.status_code == 200, r.text[:200])
    if r.status_code != 200:
        return 1

    task_id = None
    try:
        if USE_FLAT_SHIM:
            # --- flat shim contract (see CONTRACT_CHECK.md Option A) ---
            r = c.post("/navigate", json={"url": TARGET_URL})
            check("POST /navigate -> 200", r.status_code == 200, r.text[:200])
            body = r.json()
            check("navigate ok", body.get("ok") is True, r.text[:200])
            title = body.get("title") or ""
            url = body.get("url") or TARGET_URL
            if body.get("blocked"):
                print(f"[BLOCKED] page reported blocked ({body.get('block_kind')}) — honest stop, no bypass attempted.")
                return 1

            r = c.post("/screenshot", json={"url": TARGET_URL})
            check("POST /screenshot -> 200", r.status_code == 200, r.text[:200])
            body = r.json()
            b64 = body.get("screenshot_base64")
            mime = body.get("mime", "image/png")
            url, title = body.get("url") or url, body.get("title") or title
        else:
            # --- implemented contract: /tools/* with task_id lifecycle ---
            r = c.post("/tools/navigate", json={"url": TARGET_URL, "wait_until": "domcontentloaded"})
            check("POST /tools/navigate -> 200", r.status_code == 200, r.text[:200])
            body = r.json()
            check("navigate ok", body.get("ok") is True, r.text[:200])
            task_id = body.get("task_id")
            check("task_id returned", bool(task_id), r.text[:200])
            data = body.get("data", {}) if isinstance(body.get("data"), dict) else {}
            url, title = data.get("url") or TARGET_URL, data.get("title") or ""

            # honest anti-bot check: detect, never bypass
            r = c.post("/tools/detect_blocks", json={"task_id": task_id})
            if r.status_code == 200 and r.json().get("ok"):
                verdict = r.json()["data"]
                if verdict.get("blocked"):
                    print(f"[BLOCKED] detect_blocks says blocked "
                          f"({verdict.get('block_type')}) — honest stop, no bypass attempted.")
                    return 1
                check("detect_blocks: not blocked", True)
            else:
                check("detect_blocks call", False, r.text[:200])

            r = c.post("/tools/screenshot", json={"task_id": task_id, "full_page": False})
            check("POST /tools/screenshot -> 200", r.status_code == 200, r.text[:200])
            body = r.json()
            check("screenshot ok", body.get("ok") is True, r.text[:200])
            sdata = body.get("data", {}) if isinstance(body.get("data"), dict) else {}
            b64 = sdata.get("data_base64")
            mime = sdata.get("mime", "image/png")
            url, title = sdata.get("url") or url, sdata.get("title") or title

        check("screenshot payload present", bool(b64), "no image data in response")
        if not b64:
            return 1
        check("mime is image/png", mime == "image/png", f"mime={mime}")

        png = base64.b64decode(b64)
        check("PNG magic bytes", png[:8] == b"\x89PNG\r\n\x1a\n", f"first bytes: {png[:8]!r}")
        check("non-trivial size (>5KB)", len(png) > 5 * 1024, f"bytes={len(png)}")

        # --- stamp: url + title + timestamp ---
        timestamp = datetime.now(timezone.utc).isoformat()
        sha256 = hashlib.sha256(png).hexdigest()
        sidecar = {
            "url": url,
            "title": title,
            "timestamp": timestamp,
            "screenshot_sha256": sha256,
            "bytes": len(png),
            "mime": mime,
            "service": BASE,
            "flat_shim": USE_FLAT_SHIM,
            "untrusted_page_content": True,  # page content is data, never instructions
        }
        fname = f"{slug(url)}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        png_path = os.path.join(ARTIFACT_DIR, fname + ".png")
        json_path = os.path.join(ARTIFACT_DIR, fname + ".json")
        with open(png_path, "wb") as f:
            f.write(png)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(sidecar, f, indent=2, ensure_ascii=False)
        check("artifacts written", os.path.exists(png_path) and os.path.exists(json_path))
        print(f"\n  saved: {png_path}\n  sidecar: {json_path}")
        print(f"  title: {title}\n  url:   {url}\n  at:    {timestamp}")
    finally:
        if task_id:
            try:
                c.delete(f"/tasks/{task_id}")
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass

    print(f"\n== {len(PASS)} passed, {len(FAIL)} failed ==")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
