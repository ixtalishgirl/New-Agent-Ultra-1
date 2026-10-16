"""
Playwright browser runner — real Chromium automation.
Read-only by default; consequential actions need explicit approval flag.
"""
from __future__ import annotations
import json
import subprocess
import sys
import tempfile
from pathlib import Path


BROWSER_SCRIPT = r'''
import json, sys
from playwright.sync_api import sync_playwright

def main():
    args = json.loads(sys.argv[1])
    action = args.get("action", "navigate")
    url = args.get("url", "")
    out = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        )
        page = ctx.new_page()
        try:
            if action == "navigate":
                page.goto(url, timeout=30000)
                out = {"title": page.title(), "url": page.url,
                       "text": page.inner_text("body")[:10000]}
            elif action == "screenshot":
                page.goto(url, timeout=30000)
                shot = sys.argv[2] if len(sys.argv) > 2 else "/tmp/shot.png"
                page.screenshot(path=shot, full_page=False)
                out = {"screenshot": shot, "title": page.title()}
            elif action == "links":
                page.goto(url, timeout=30000)
                out = {"links": page.eval_on_selector_all(
                    "a[href]", "els => els.map(e => e.href).slice(0,50)")}
            elif action == "click":
                page.goto(url, timeout=30000)
                page.click(args["selector"], timeout=10000)
                page.wait_for_timeout(2000)
                out = {"title": page.title(), "url": page.url}
            elif action == "form":
                # Fill a form and submit it in one session:
                # {url, fields: [{selector, value}], submit: selector}
                page.goto(url, timeout=30000)
                filled = []
                for f in args.get("fields", []):
                    sel = f.get("selector")
                    if sel:
                        page.fill(sel, str(f.get("value", "")),
                                  timeout=10000)
                        filled.append(sel)
                page.click(args["submit"], timeout=10000)
                page.wait_for_timeout(3000)
                out = {"title": page.title(), "url": page.url,
                       "filled": filled,
                       "text": page.inner_text("body")[:4000]}
            else:
                out = {"error": f"unknown action {action}"}
        except Exception as e:
            out = {"error": str(e)[:500]}
        finally:
            browser.close()
    print(json.dumps(out))

main()
'''


class PlaywrightRunner:
    """Chromium automation via Playwright (subprocess-isolated)."""

    def __init__(self, timeout: int = 60):
        self.timeout = timeout

    @staticmethod
    def _ensure() -> dict | None:
        """Make sure a browser binary exists; return error dict or None."""
        try:
            from .ensure_browser import ensure_browser_installed
        except Exception:
            try:
                from agent.browser.ensure_browser import ensure_browser_installed
            except Exception as e:
                return {"error": f"browser ensure helper unavailable: {e}"}
        ok, msg = ensure_browser_installed()
        if not ok:
            return {"error": msg}
        return None

    def _run(self, args: dict) -> dict:
        err = self._ensure()
        if err:
            return err
        with tempfile.TemporaryDirectory() as tmpdir:
            script = Path(tmpdir) / "pw.py"
            script.write_text(BROWSER_SCRIPT)
            proc = subprocess.run(
                [sys.executable, str(script), json.dumps(args)],
                capture_output=True, text=True,
                timeout=self.timeout, cwd=tmpdir,
            )
            if proc.returncode != 0:
                return {"error": proc.stderr[:500] or "playwright failed"}
            try:
                return json.loads(proc.stdout.strip().split("\n")[-1])
            except Exception:
                return {"error": "bad playwright output", "raw": proc.stdout[:300]}

    def navigate(self, url: str) -> dict:
        """Go to URL, return title/url/text."""
        return self._run({"action": "navigate", "url": url})

    def fill_form(self, url: str, fields: list, submit: str) -> dict:
        """Fill form fields and click submit in one session."""
        return self._run({"action": "form", "url": url,
                          "fields": fields, "submit": submit})

    def click(self, url: str, selector: str) -> dict:
        """Go to URL and click a selector."""
        return self._run({"action": "click", "url": url,
                          "selector": selector})

    def screenshot(self, url: str, path: str = "/tmp/shot.png") -> dict:
        """Screenshot a page."""
        err = self._ensure()
        if err:
            return err
        with tempfile.TemporaryDirectory() as tmpdir:
            script = Path(tmpdir) / "pw.py"
            script.write_text(BROWSER_SCRIPT)
            proc = subprocess.run(
                [sys.executable, str(script),
                 json.dumps({"action": "screenshot", "url": url}), path],
                capture_output=True, text=True,
                timeout=self.timeout, cwd=tmpdir,
            )
            try:
                return json.loads(proc.stdout.strip().split("\n")[-1])
            except Exception:
                return {"error": "screenshot failed"}

    def links(self, url: str) -> dict:
        """Collect links from a page."""
        return self._run({"action": "links", "url": url})
