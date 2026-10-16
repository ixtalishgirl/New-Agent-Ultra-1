"""Live Browser Control tool module — real Chromium driven over CDP.

Architecture (no live-browser dependency at import time):
  * A persistent daemon (``/tmp/browser_daemon/daemon.py``) launches one
    headless Chromium with a regular ``launch`` (fresh Browser supporting
    multiple isolated contexts), exposes it on a CDP port, and appends
    console messages and network requests to JSONL logs.
  * Each tool runs a small Playwright script in a SUBPROCESS
    (subprocess-isolated, same pattern as agent/browser/playwright_runner.py),
    connects with ``chromium.connect_over_cdp``, performs ONE operation on
    the resolved tab, updates the JSON tab session, and exits. The daemon
    browser is never closed by tool calls.
  * Tab ids (``tab-1``, ``tab-2``, ...) persist across calls in
    ``/tmp/browser_daemon/tabs.json``.
  * If playwright is missing or the daemon cannot start, every tool returns
    ok=False with a clear message — never raises.

Boundaries: no browsing-behavior restriction workarounds, no credential
harvesting helpers; browser_proxy/browser_ua store config applied to new
contexts (documented); destructive ops (cookie/storage clear, download
overwrite) need confirm=True.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# Per-thread browser context routing. The main agent uses the default
# (shared) context; each replicated child agent gets its own isolated
# Playwright browser context (separate cookies, storage, pages) so parallel
# children never leak sessions into each other. replicate_tools sets this
# in the child thread before the child runs.
_thread_context = threading.local()


def _current_browser_context() -> str:
    return getattr(_thread_context, "ctx", "") or ""


BASE_DIR = Path("/tmp/browser_daemon")
PORT_FILE = BASE_DIR / "cdp.port"
HEARTBEAT = BASE_DIR / "heartbeat"
TABS_FILE = BASE_DIR / "tabs.json"
CONFIG_FILE = BASE_DIR / "context_config.json"
CONSOLE_LOG = BASE_DIR / "console.jsonl"
NETWORK_LOG = BASE_DIR / "network.jsonl"
DAEMON_SCRIPT = BASE_DIR / "daemon.py"
DAEMON_LOG = BASE_DIR / "daemon.log"


@dataclass
class BrowserResult:
    """Structured result of a browser operation."""
    ok: bool
    output: str = ""
    parsed: dict = field(default_factory=dict)
    error: str = ""


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

_DAEMON_SRC = r'''
import json, os, socket, sys, time
from pathlib import Path

def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]; s.close(); return p

def main():
    base = Path(sys.argv[1])
    port_file = base / "cdp.port"
    hb = base / "heartbeat"
    profile = base / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    (base / "daemon.pid").write_text(str(os.getpid()))
    cfg_path = base / "context_config.json"
    cfg = {}
    try:
        cfg = json.loads(cfg_path.read_text())
    except Exception:
        pass
    port = _free_port()
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        (base / "daemon.error").write_text(f"playwright import failed: {e}")
        sys.exit(1)
    console_log = base / "console.jsonl"
    net_log = base / "network.jsonl"
    # NOTE: regular launch (not launch_persistent_context) — the persistent
    # variant returns a BrowserContext, not a Browser, which broke
    # browser.contexts / new_context (isolated contexts for replication).
    # The profile dir is ephemeral on Render anyway.
    launch_kwargs = {"headless": True,
                     "args": ["--no-sandbox",
                              f"--remote-debugging-port={port}"]}
    ctx_kwargs: dict = {}
    if cfg.get("user_agent"):
        ctx_kwargs["user_agent"] = cfg["user_agent"]
    if cfg.get("proxy"):
        ctx_kwargs["proxy"] = cfg["proxy"]
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(**launch_kwargs)
            ctx = browser.new_context(**ctx_kwargs)
            def on_console(msg):
                try:
                    with open(console_log, "a") as f:
                        f.write(json.dumps({
                            "t": time.time(), "type": msg.type,
                            "text": (msg.text or "")[:2000]}) + "\n")
                except Exception:
                    pass
            def on_request(req):
                try:
                    with open(net_log, "a") as f:
                        f.write(json.dumps({
                            "t": time.time(), "method": req.method,
                            "url": (req.url or "")[:2000],
                            "resource": req.resource_type}) + "\n")
                except Exception:
                    pass
            if ctx:
                try:
                    ctx.on("console", on_console)
                    ctx.on("request", on_request)
                except Exception:
                    pass
            port_file.write_text(str(port))
            hb.write_text(str(time.time()))
            while True:
                time.sleep(5)
                try:
                    hb.write_text(str(time.time()))
                except Exception:
                    pass
    except Exception as e:
        (base / "daemon.error").write_text(str(e)[:1000])

main()
'''

_PW_OK_CHECK = ("import importlib.util; "
                "print(bool(importlib.util.find_spec('playwright')))")


def _playwright_available() -> bool:
    try:
        proc = subprocess.run([sys.executable, "-c", _PW_OK_CHECK],
                              capture_output=True, text=True, timeout=15)
        return proc.stdout.strip() == "True"
    except Exception:
        return False


def _daemon_alive() -> Optional[int]:
    try:
        if not PORT_FILE.exists():
            return None
        port = int(PORT_FILE.read_text().strip())
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
        if HEARTBEAT.exists():
            age = time.time() - float(HEARTBEAT.read_text().strip())
            if age > 90:
                return None  # stale daemon
        return port
    except Exception:
        return None


def _ensure_daemon() -> tuple[Optional[int], str]:
    """Return (cdp_port, error). Starts the daemon if needed."""
    port = _daemon_alive()
    if port:
        return port, ""
    if not _playwright_available():
        return None, ("playwright not installed "
                      "(pip install playwright && playwright install chromium)")
    # Self-heal: download the browser binary at runtime when the build-time
    # install never landed (wrong HOME, silent `|| true` failure, ...).
    try:
        try:
            from agent.browser.ensure_browser import ensure_browser_installed
        except ImportError:
            from ..browser.ensure_browser import ensure_browser_installed
        _ok, _msg = ensure_browser_installed()
        if not _ok:
            return None, _msg
    except Exception as e:
        return None, f"browser install check failed: {e}"
    try:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        DAEMON_SCRIPT.write_text(_DAEMON_SRC)
        for stale in (PORT_FILE, BASE_DIR / "daemon.error"):
            try:
                stale.unlink()
            except OSError:
                pass
        with open(DAEMON_LOG, "a") as log:
            subprocess.Popen([sys.executable, str(DAEMON_SCRIPT), str(BASE_DIR)],
                             stdout=log, stderr=subprocess.STDOUT,
                             start_new_session=True,
                             cwd=str(BASE_DIR))
        deadline = time.time() + 45
        while time.time() < deadline:
            err = BASE_DIR / "daemon.error"
            if err.exists():
                return None, f"daemon failed: {err.read_text()[:400]}"
            port = _daemon_alive()
            if port:
                # Fresh daemon start: best-effort restore of saved logins
                # so accounts stay logged in across Render restarts.
                try:
                    browser_login_restore({})
                except Exception:
                    pass
                return port, ""
            time.sleep(1)
        return None, "daemon did not come up within 45s (see daemon.log)"
    except Exception as e:
        return None, f"daemon start failed: {e}"


# ---------------------------------------------------------------------------
# Generic one-shot CDP operation script
# ---------------------------------------------------------------------------

_OP_SCRIPT = r'''
import json, sys, time
from pathlib import Path

BASE = Path(sys.argv[1])

def load(p, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default

def save(p, obj):
    p.write_text(json.dumps(obj))

def main():
    args = json.loads(sys.argv[2])
    op = args["op"]
    out = {}
    # Isolated browser context per child agent: separate tabs file and a
    # dedicated Playwright context (own cookies/storage/pages). Empty ctx_id
    # = the default shared context (main agent behavior, unchanged).
    ctx_id = str(args.get("browser_context") or "")
    tabs_file = (BASE / "tabs.json" if not ctx_id
                 else BASE / ("tabs_ctx_" + "".join(
                     c if c.isalnum() or c in "-_" else "_"
                     for c in ctx_id)[:48] + ".json"))
    try:
        from playwright.sync_api import sync_playwright
        sess = load(tabs_file,
                    {"seq": 0, "active": None, "tabs": {}})
        tabs = sess.setdefault("tabs", {})

        def _op_log_console(msg):
            try:
                with open(BASE / "console.jsonl", "a") as f:
                    f.write(json.dumps({
                        "t": time.time(), "type": msg.type,
                        "text": (msg.text or "")[:2000],
                        "ctx": ctx_id}) + "\n")
            except Exception:
                pass

        def _op_log_request(req):
            try:
                with open(BASE / "network.jsonl", "a") as f:
                    f.write(json.dumps({
                        "t": time.time(), "method": req.method,
                        "url": (req.url or "")[:2000],
                        "resource": req.resource_type,
                        "ctx": ctx_id}) + "\n")
            except Exception:
                pass

        def _pick_context(browser, ctx_id):
            """Return the isolated context for ctx_id, creating it on demand."""
            ctxs = list(browser.contexts)
            if not ctx_id:
                return ctxs[0] if ctxs else browser.new_context()
            cmap = load(BASE / "contexts.json", {})
            for i, c in enumerate(ctxs):
                if cmap.get(ctx_id) == i:
                    return c
            new_ctx = browser.new_context()
            try:
                new_ctx.on("console", _op_log_console)
                new_ctx.on("request", _op_log_request)
            except Exception:
                pass
            ctxs = list(browser.contexts)
            try:
                cmap[ctx_id] = ctxs.index(new_ctx)
            except ValueError:
                cmap[ctx_id] = len(ctxs) - 1
            save(BASE / "contexts.json", cmap)
            return new_ctx

        def sync_session(ctx):
            pages = ctx.pages
            by_index = {}
            for tid, info in tabs.items():
                by_index[info.get("index")] = tid
            new_tabs = {}
            for i, pg in enumerate(pages):
                tid = by_index.get(i)
                if not tid:
                    sess["seq"] = sess.get("seq", 0) + 1
                    tid = f"tab-{sess['seq']}"
                new_tabs[tid] = {"index": i, "url": pg.url,
                                 "title": pg.title() if i < len(pages) else ""}
            sess["tabs"] = new_tabs
            tabs.clear(); tabs.update(new_tabs)
            if sess.get("active") not in tabs and tabs:
                sess["active"] = next(iter(tabs))

        def resolve(ctx, tab_id):
            pages = ctx.pages
            if tab_id and tab_id in tabs:
                idx = tabs[tab_id].get("index", 0)
                if 0 <= idx < len(pages):
                    return tab_id, pages[idx]
                url = tabs[tab_id].get("url", "")
                for i, pg in enumerate(pages):
                    if pg.url == url:
                        tabs[tab_id]["index"] = i
                        return tab_id, pg
            act = sess.get("active")
            if act in tabs:
                idx = tabs[act].get("index", 0)
                if 0 <= idx < len(pages):
                    return act, pages[idx]
            if pages:
                return (tab_id or next(iter(tabs), "tab-?")), pages[0]
            return None, None

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(
                f"http://127.0.0.1:{args['port']}")
            ctx = _pick_context(browser, ctx_id)
            sync_session(ctx)

            if op in ("back", "forward", "refresh"):
                tid, page = resolve(ctx, args.get("tab_id"))
                if not page:
                    out = {"error": "no pages open"}
                else:
                    {"back": page.go_back, "forward": page.go_forward,
                     "refresh": page.reload}[op](timeout=30000,
                                                 wait_until="domcontentloaded")
                    out = {"tab_id": tid, "url": page.url,
                           "title": page.title()}
            elif op == "new_tab":
                page = ctx.new_page()
                url = args.get("url")
                if url:
                    page.goto(url, timeout=30000,
                              wait_until="domcontentloaded")
                sync_session(ctx)
                # find the id of the last page
                tid = [t for t, i in tabs.items()
                       if i["index"] == len(ctx.pages) - 1][0]
                sess["active"] = tid
                out = {"tab_id": tid, "url": page.url}
            elif op == "close_tab":
                tid, page = resolve(ctx, args.get("tab_id"))
                if not page:
                    out = {"error": "tab not found"}
                else:
                    page.close()
                    sync_session(ctx)
                    out = {"closed": tid, "tabs": list(tabs)}
            elif op == "switch_tab":
                tid, page = resolve(ctx, args.get("tab_id"))
                if not page:
                    out = {"error": "tab not found"}
                else:
                    page.bring_to_front()
                    sess["active"] = tid
                    out = {"tab_id": tid, "url": page.url}
            elif op == "tab_list":
                out = {"tabs": [
                    {"tab_id": t, "url": i.get("url", ""),
                     "title": i.get("title", ""),
                     "active": t == sess.get("active")}
                    for t, i in tabs.items()]}
            elif op == "tab_screenshot":
                tid, page = resolve(ctx, args.get("tab_id"))
                if not page:
                    out = {"error": "tab not found"}
                else:
                    path = args["output"]
                    page.screenshot(path=path,
                                    full_page=bool(args.get("full_page")))
                    out = {"tab_id": tid, "path": path}
            elif op == "tab_html":
                tid, page = resolve(ctx, args.get("tab_id"))
                out = {"tab_id": tid, "html": page.content()[:200000]} \
                    if page else {"error": "tab not found"}
            elif op == "tab_text":
                tid, page = resolve(ctx, args.get("tab_id"))
                out = {"tab_id": tid,
                       "text": page.inner_text("body")[:100000]} \
                    if page else {"error": "tab not found"}
            elif op == "click":
                tid, page = resolve(ctx, args.get("tab_id"))
                if not page:
                    out = {"error": "no pages open"}
                else:
                    page.click(args["selector"], timeout=args.get("timeout_ms", 10000))
                    page.wait_for_timeout(500)
                    out = {"tab_id": tid, "url": page.url}
            elif op == "fill":
                tid, page = resolve(ctx, args.get("tab_id"))
                if not page:
                    out = {"error": "no pages open"}
                else:
                    page.fill(args["selector"], args.get("value", ""),
                              timeout=args.get("timeout_ms", 10000))
                    out = {"tab_id": tid, "filled": args["selector"]}
            elif op == "select":
                tid, page = resolve(ctx, args.get("tab_id"))
                vals = page.select_option(args["selector"], args["value"],
                                          timeout=args.get("timeout_ms", 10000))
                out = {"tab_id": tid, "selected": vals}
            elif op == "form_submit":
                tid, page = resolve(ctx, args.get("tab_id"))
                page.eval_on_selector(
                    args["selector"],
                    "el => { const f = el.closest('form') || el; "
                    "if (f.requestSubmit) f.requestSubmit(); else f.submit(); }")
                page.wait_for_timeout(1000)
                out = {"tab_id": tid, "url": page.url}
            elif op == "wait":
                tid, page = resolve(ctx, args.get("tab_id"))
                ms = int(args.get("timeout_ms", 10000))
                sel = args.get("selector")
                if sel:
                    page.wait_for_selector(sel, timeout=ms)
                    out = {"tab_id": tid, "waited_for": sel}
                else:
                    page.wait_for_timeout(ms)
                    out = {"tab_id": tid, "waited_ms": ms}
            elif op == "scroll":
                tid, page = resolve(ctx, args.get("tab_id"))
                direction = (args.get("direction") or "down").lower()
                pixels = int(args.get("pixels", 600))
                sel = args.get("selector")
                if direction == "top":
                    js = "window.scrollTo(0, 0)"
                elif direction == "bottom":
                    js = "window.scrollTo(0, document.body.scrollHeight)"
                else:
                    dx, dy = 0, 0
                    if direction == "down":
                        dy = pixels
                    elif direction == "up":
                        dy = -pixels
                    elif direction == "right":
                        dx = pixels
                    elif direction == "left":
                        dx = -pixels
                    else:
                        raise ValueError(f"bad direction: {direction}")
                    if sel:
                        js = ("{ const el = document.querySelector(" +
                              json.dumps(sel) + "); if (el) el.scrollBy(" +
                              str(dx) + ", " + str(dy) +
                              "); else window.scrollBy(" + str(dx) + ", " +
                              str(dy) + "); }")
                    else:
                        js = f"window.scrollBy({dx}, {dy})"
                page.evaluate(js)
                page.wait_for_timeout(400)
                pos = page.evaluate("() => ({x: window.scrollX, y: window.scrollY})")
                out = {"tab_id": tid, "direction": direction, "pixels": pixels,
                       "scroll": pos, "url": page.url}
            elif op == "js":
                tid, page = resolve(ctx, args.get("tab_id"))
                res = page.evaluate(args["script"])
                out = {"tab_id": tid,
                       "result": json.loads(json.dumps(res, default=str))}
            elif op == "cookies":
                action = args.get("action", "get")
                if action == "get":
                    out = {"cookies": ctx.cookies()}
                elif action == "set":
                    tid, page = resolve(ctx, args.get("tab_id"))
                    url = page.url if page else "https://example.com"
                    ctx.add_cookies([{"name": args["name"],
                                      "value": args["value"], "url": url}])
                    out = {"set": args["name"]}
                elif action == "clear":
                    ctx.clear_cookies()
                    out = {"cleared": True}
            elif op == "cookies_restore":
                # Batch restore cookies (with domain/path) e.g. from Turso backup.
                raw = args.get("cookies") or []
                cleaned = []
                for c in raw:
                    if not isinstance(c, dict):
                        continue
                    name = c.get("name")
                    value = c.get("value")
                    if not name:
                        continue
                    entry = {"name": name, "value": value if value is not None else ""}
                    # Prefer domain/path over url for cross-site restore.
                    if c.get("domain"):
                        entry["domain"] = c["domain"]
                    if c.get("path"):
                        entry["path"] = c["path"]
                    if not entry.get("domain") and c.get("url"):
                        entry["url"] = c["url"]
                    if "secure" in c:
                        entry["secure"] = bool(c["secure"])
                    if "httpOnly" in c:
                        entry["httpOnly"] = bool(c["httpOnly"])
                    if "sameSite" in c and c["sameSite"] in ("Strict", "Lax", "None"):
                        entry["sameSite"] = c["sameSite"]
                    cleaned.append(entry)
                if cleaned:
                    ctx.add_cookies(cleaned)
                out = {"restored": len(cleaned)}
            elif op == "storage":
                tid, page = resolve(ctx, args.get("tab_id"))
                stype = args.get("type", "local")
                action = args.get("action", "get")
                store = ("localStorage" if stype == "local"
                         else "sessionStorage")
                if action == "get":
                    out = {"storage": page.evaluate(
                        f"() => {{ const o = {{}}; for (let i = 0; i < "
                        f"{store}.length; i++) {{ const k = {store}.key(i); "
                        f"o[k] = {store}.getItem(k); }} return o; }}")()}
                elif action == "set":
                    page.evaluate(
                        f"({store}).setItem({json.dumps(args['key'])}, "
                        f"{json.dumps(args['value'])})")
                    out = {"set": args["key"]}
                elif action == "clear":
                    page.evaluate(f"{store}.clear()")
                    out = {"cleared": True}
            elif op == "upload":
                tid, page = resolve(ctx, args.get("tab_id"))
                page.set_input_files(args["selector"], args["file_path"],
                                     timeout=args.get("timeout_ms", 15000))
                out = {"tab_id": tid, "uploaded": args["file_path"]}
            elif op == "viewport":
                tid, page = resolve(ctx, args.get("tab_id"))
                page.set_viewport_size({"width": int(args["width"]),
                                        "height": int(args["height"])})
                out = {"tab_id": tid, "viewport": [args["width"],
                                                  args["height"]]}
            elif op == "inspect_element":
                tid, page = resolve(ctx, args.get("tab_id"))
                sel = args.get("selector", "")
                info = page.evaluate("""(sel) => {
                  const el = document.querySelector(sel);
                  if (!el) return {found: false};
                  const cs = getComputedStyle(el);
                  const r = el.getBoundingClientRect();
                  const pick = {};
                  ["display","visibility","color","backgroundColor",
                   "fontSize","fontFamily","width","height","position",
                   "zIndex","opacity","cursor"].forEach(k => pick[k] = cs[k]);
                  const attrs = {};
                  for (const a of el.attributes) attrs[a.name] = a.value;
                  return {found: true, tag: el.tagName.toLowerCase(),
                    outerHTML: el.outerHTML.slice(0, 20000),
                    text: (el.innerText || "").slice(0, 2000),
                    attrs: attrs, computed: pick,
                    box: {x: r.x, y: r.y, width: r.width, height: r.height}};
                }""", sel)
                out = {"tab_id": tid, "selector": sel, **info}
            elif op == "login":
                # Full login flow in one op: navigate -> detect/fill fields ->
                # optional TOTP -> submit -> verify. The password is never
                # included in `out` (never logged, never persisted).
                tid, page = resolve(ctx, args.get("tab_id"))
                url = args.get("url", "")
                if url and (not page or not page.url.startswith(url[:24])):
                    page = ctx.new_page()
                    page.goto(url, timeout=45000,
                              wait_until="domcontentloaded")
                    page.wait_for_timeout(1500)
                    sync_session(ctx)
                    tid = [t for t, i in tabs.items()
                           if i["index"] == len(ctx.pages) - 1][0]
                    sess["active"] = tid
                username = args.get("username", "")
                password = args.get("password", "")
                user_sel = args.get("username_selector") or ""
                pass_sel = args.get("password_selector") or ""
                submit_sel = args.get("submit_selector") or ""
                success_sel = args.get("success_selector") or ""
                start_url = page.url
                find_input = """(kind) => {
                  const sels = kind === "user"
                    ? ['input[type="email"]','input[name*="user" i]',
                       'input[name*="email" i]','input[name*="login" i]',
                       'input[type="text"]','input:not([type])']
                    : ['input[type="password"]'];
                  for (const s of sels) {
                    try { const el = document.querySelector(s);
                      if (el && el.offsetParent !== null) return s;
                    } catch (e) {}
                  }
                  return null; }"""
                if not user_sel:
                    user_sel = page.evaluate(f"({find_input})('user')")
                if user_sel and username:
                    page.fill(user_sel, username, timeout=15000)
                    page.wait_for_timeout(500)
                if not pass_sel:
                    pass_sel = page.evaluate(f"({find_input})('pass')")
                if not pass_sel:
                    # multi-step login (e.g. Google): advance once, retry
                    nxt = submit_sel or page.evaluate("""() => {
                      const cands = ['button[type="submit"]',
                        'input[type="submit"]', 'button'];
                      for (const s of cands) {
                        try { const el = document.querySelector(s);
                          if (el && el.offsetParent !== null &&
                              /next|continue|sign in|log in/i.test(
                                el.innerText || el.value || "")) return s;
                        } catch (e) {}
                      }
                      try { const b = document.querySelector(
                        'button[type="submit"]');
                        return (b && b.offsetParent !== null)
                          ? 'button[type="submit"]' : null;
                      } catch (e) { return null; } }""")
                    if nxt:
                        page.click(nxt, timeout=10000)
                        page.wait_for_timeout(2000)
                    pass_sel = page.evaluate(f"({find_input})('pass')")
                if pass_sel and password:
                    page.fill(pass_sel, password, timeout=15000)
                    page.wait_for_timeout(500)
                totp_secret = (args.get("totp_secret") or "").strip()
                if totp_secret:
                    import hmac as _hmac, hashlib as _hl, struct as _st
                    import base64 as _b64, time as _tm
                    try:
                        key = _b64.b32decode(
                            totp_secret.replace(" ", "").upper())
                        ctr = int(_tm.time()) // 30
                        hs = _hmac.new(
                            key, _st.pack(">Q", ctr), _hl.sha1).digest()
                        off = hs[-1] & 0x0F
                        code = str((_st.unpack(
                            ">I", hs[off:off + 4])[0] & 0x7FFFFFFF)
                            % 1000000).zfill(6)
                        tsel = page.evaluate("""() => {
                          const sels = ['input[name*="totp" i]',
                            'input[name*="otp" i]','input[name*="code" i]',
                            'input[inputmode="numeric"]'];
                          for (const s of sels) {
                            try { const el = document.querySelector(s);
                              if (el && el.offsetParent !== null) return s;
                            } catch (e) {}
                          }
                          return null; }""")
                        if tsel:
                            page.fill(tsel, code, timeout=10000)
                            page.wait_for_timeout(500)
                    except Exception:
                        pass
                sub = submit_sel or page.evaluate("""() => {
                  try { const b = document.querySelector(
                    'button[type="submit"]');
                    return (b && b.offsetParent !== null)
                      ? 'button[type="submit"]' : null;
                  } catch (e) { return null; } }""")
                if sub:
                    page.click(sub, timeout=15000)
                else:
                    page.keyboard.press("Enter")
                page.wait_for_timeout(3000)
                verified, detail = False, ""
                if success_sel:
                    try:
                        page.wait_for_selector(success_sel, timeout=10000)
                        verified = True
                        detail = f"success indicator present: {success_sel}"
                    except Exception:
                        detail = ("login not confirmed "
                                  f"(no {success_sel}); url={page.url}")
                else:
                    still_pw = page.evaluate(
                        "() => !!document.querySelector("
                        "'input[type=\"password\"]')")
                    url_changed = page.url != start_url
                    verified = (not still_pw) or url_changed
                    detail = (f"url_changed={url_changed} "
                              f"password_field_gone={not still_pw} "
                              f"url={page.url}")
                sync_session(ctx)
                out = {"tab_id": tid, "url": page.url,
                       "logged_in": verified, "detail": detail}
            save(tabs_file, sess)
    except Exception as e:
        out = {"error": str(e)[:800]}
    print(json.dumps(out, default=str))

main()
'''


def _run_op(op: str, params: dict, timeout: int = 90) -> dict:
    port, err = _ensure_daemon()
    if port is None:
        return {"error": err}
    with tempfile.TemporaryDirectory() as tmpdir:
        script = Path(tmpdir) / "pw_op.py"
        script.write_text(_OP_SCRIPT)
        try:
            proc = subprocess.run(
                [sys.executable, str(script), str(BASE_DIR),
                 json.dumps({"op": op, "port": port, **params})],
                capture_output=True, text=True, timeout=timeout, cwd=tmpdir)
        except subprocess.TimeoutExpired:
            return {"error": f"browser op '{op}' timed out after {timeout}s"}
        if proc.returncode != 0:
            return {"error": (proc.stderr or "browser op failed")[:600]}
        try:
            return json.loads(proc.stdout.strip().split("\n")[-1])
        except Exception:
            return {"error": "bad browser op output",
                    "raw": proc.stdout[:400]}


def _res(op: str, params: dict, timeout: int = 90) -> BrowserResult:
    try:
        # Route to this thread's isolated browser context (child agents).
        params = dict(params)
        params.setdefault("browser_context", _current_browser_context())
        r = _run_op(op, params, timeout)
        if r.get("error"):
            return BrowserResult(ok=False, error=r["error"])
        return BrowserResult(ok=True, output=json.dumps(r)[:2000],
                             parsed=r)
    except Exception as e:
        return BrowserResult(ok=False, error=str(e)[:600])


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


def _save_config(cfg: dict) -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def browser_back(args: dict) -> BrowserResult:
    r = _res("back", {"tab_id": args.get("tab_id")})
    if r.ok:
        r.output = f"went back -> {r.parsed.get('url')}"
    return r


def browser_forward(args: dict) -> BrowserResult:
    r = _res("forward", {"tab_id": args.get("tab_id")})
    if r.ok:
        r.output = f"went forward -> {r.parsed.get('url')}"
    return r


def browser_refresh(args: dict) -> BrowserResult:
    r = _res("refresh", {"tab_id": args.get("tab_id")})
    if r.ok:
        r.output = f"refreshed -> {r.parsed.get('url')}"
    return r


def browser_new_tab(args: dict) -> BrowserResult:
    r = _res("new_tab", {"url": args.get("url")})
    if r.ok:
        r.output = (f"opened {r.parsed.get('tab_id')} -> "
                    f"{r.parsed.get('url')}")
    return r


def browser_close_tab(args: dict) -> BrowserResult:
    tab_id = args.get("tab_id")
    if not tab_id:
        return BrowserResult(ok=False, error="tab_id is required")
    r = _res("close_tab", {"tab_id": tab_id})
    if r.ok:
        r.output = f"closed {r.parsed.get('closed')}"
    return r


def browser_switch_tab(args: dict) -> BrowserResult:
    tab_id = args.get("tab_id")
    if not tab_id:
        return BrowserResult(ok=False, error="tab_id is required")
    r = _res("switch_tab", {"tab_id": tab_id})
    if r.ok:
        r.output = (f"active tab is now {r.parsed.get('tab_id')} -> "
                    f"{r.parsed.get('url')}")
    return r


def browser_tab_list(args: dict) -> BrowserResult:
    r = _res("tab_list", {})
    if r.ok:
        tabs = r.parsed.get("tabs", [])
        lines = [f"{len(tabs)} tab(s):"]
        for t in tabs:
            mark = "*" if t.get("active") else " "
            lines.append(f" {mark} {t['tab_id']}: {t.get('title','')[:60]} "
                         f"<{t.get('url','')[:80]}>")
        r.output = "\n".join(lines)
    return r


def browser_tab_screenshot(args: dict) -> BrowserResult:
    try:
        tab_id = args.get("tab_id")
        out = args.get("output") or \
            f"/tmp/tabshot_{tab_id or 'active'}_{int(time.time())}.png"
        r = _res("tab_screenshot", {"tab_id": tab_id, "output": out})
        if r.ok:
            path = r.parsed.get("path") or out
            data_uri = ""
            try:
                import base64
                with open(path, "rb") as f:
                    data_uri = ("data:image/png;base64," +
                                base64.b64encode(f.read()).decode("ascii"))
            except Exception:
                pass
            r.parsed["data_uri"] = data_uri
            r.output = f"screenshot -> {path}"
            if data_uri:
                r.output += f"\n![screenshot]({data_uri})"
        return r
    except Exception as e:
        return BrowserResult(ok=False, error=str(e)[:600])


def browser_tab_html(args: dict) -> BrowserResult:
    r = _res("tab_html", {"tab_id": args.get("tab_id")})
    if r.ok:
        r.output = f"HTML ({len(r.parsed.get('html',''))} chars) from " \
                   f"{r.parsed.get('tab_id')}"
    return r


def browser_tab_text(args: dict) -> BrowserResult:
    r = _res("tab_text", {"tab_id": args.get("tab_id")})
    if r.ok:
        r.output = r.parsed.get("text", "")[:4000]
    return r


def browser_click(args: dict) -> BrowserResult:
    sel = args.get("selector")
    if not sel:
        return BrowserResult(ok=False, error="selector is required")
    r = _res("click", {"selector": sel, "tab_id": args.get("tab_id"),
                       "timeout_ms": int(args.get("timeout_ms", 10000))})
    if r.ok:
        r.output = f"clicked '{sel}' -> {r.parsed.get('url')}"
    return r


def browser_fill(args: dict) -> BrowserResult:
    sel = args.get("selector")
    if not sel:
        return BrowserResult(ok=False, error="selector is required")
    # If a url is given, open it in a new tab first so there is always
    # a page to fill (previously failed with a confusing NoneType error
    # when the daemon browser had no tabs open).
    tab_id = args.get("tab_id")
    url = args.get("url")
    if url and not tab_id:
        nav = _res("new_tab", {"url": url})
        if not nav.ok:
            return BrowserResult(
                ok=False,
                error=f"could not open {url}: {nav.error}")
        tab_id = (nav.parsed or {}).get("tab_id")
    r = _res("fill", {"selector": sel, "value": str(args.get("value", "")),
                      "tab_id": tab_id})
    if r.ok:
        r.output = f"filled '{sel}'"
    return r


def browser_select(args: dict) -> BrowserResult:
    sel = args.get("selector")
    if not sel:
        return BrowserResult(ok=False, error="selector is required")
    if "value" not in args:
        return BrowserResult(ok=False, error="value is required")
    r = _res("select", {"selector": sel, "value": args["value"],
                        "tab_id": args.get("tab_id")})
    if r.ok:
        r.output = f"selected {r.parsed.get('selected')} in '{sel}'"
    return r


def browser_form_submit(args: dict) -> BrowserResult:
    sel = args.get("selector")
    if not sel:
        return BrowserResult(ok=False, error="selector is required")
    r = _res("form_submit", {"selector": sel, "tab_id": args.get("tab_id")})
    if r.ok:
        r.output = f"submitted form at '{sel}' -> {r.parsed.get('url')}"
    return r


def browser_wait(args: dict) -> BrowserResult:
    r = _res("wait", {"selector": args.get("selector"),
                      "timeout_ms": int(args.get("timeout_ms", 10000)),
                      "tab_id": args.get("tab_id")},
             timeout=int(args.get("timeout_ms", 10000)) // 1000 + 60)
    if r.ok:
        r.output = (f"waited for '{r.parsed.get('waited_for')}'"
                    if r.parsed.get("waited_for")
                    else f"waited {r.parsed.get('waited_ms')}ms")
    return r


def _tail_jsonl(path: Path, n: int) -> list[dict]:
    try:
        lines = path.read_text().splitlines()[-n:]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out


def _tab_url(tab_id: Optional[str]) -> str:
    try:
        sess = json.loads(TABS_FILE.read_text())
        tabs = sess.get("tabs", {})
        if tab_id and tab_id in tabs:
            return tabs[tab_id].get("url", "")
        act = sess.get("active")
        if act in tabs:
            return tabs[act].get("url", "")
    except Exception:
        pass
    return ""


def _scrub_secret(text: str, *secrets: str) -> str:
    """Redact secret values from any text (outputs, errors)."""
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


def browser_login(args: dict) -> BrowserResult:
    """Log in to a website like an assistant would: navigate, auto-detect the
    username/password fields (or use the given selectors), handle multi-step
    logins (e.g. Google: username -> Next -> password), optional TOTP, submit,
    and verify via success_selector or URL/field heuristics.

    The password is NEVER logged or persisted: it lives only in the call args
    in memory and is scrubbed from every output and error. The login session
    (cookies) persists in the daemon browser for later tool calls.

    Google/Kaggle are best-effort: bot detection (CAPTCHA, device checks) may
    block automated login; the result reports honestly what happened.
    """
    url = str(args.get("url", "")).strip()
    username = str(args.get("username", ""))
    password = str(args.get("password", ""))
    if not url or not username or not password:
        return BrowserResult(
            ok=False, error="url, username and password are required")
    params = {
        "url": url,
        "username": username,
        "password": password,  # in-memory only; never echoed in results
        "username_selector": args.get("username_selector") or "",
        "password_selector": args.get("password_selector") or "",
        "submit_selector": args.get("submit_selector") or "",
        "success_selector": args.get("success_selector") or "",
        "totp_secret": args.get("totp_secret") or "",
        "tab_id": args.get("tab_id"),
    }
    r = _res("login", params, timeout=int(args.get("timeout", 120)))
    # Scrub secrets from everything that leaves this function.
    r.output = _scrub_secret(r.output, password,
                             str(args.get("totp_secret") or ""))
    r.error = _scrub_secret(r.error, password,
                            str(args.get("totp_secret") or ""))
    if r.parsed:
        for k in ("detail",):
            if isinstance(r.parsed.get(k), str):
                r.parsed[k] = _scrub_secret(r.parsed[k], password)
    if r.ok:
        logged_in = bool(r.parsed.get("logged_in"))
        detail = r.parsed.get("detail", "")
        r.output = (f"login {'VERIFIED' if logged_in else 'NOT confirmed'}: "
                    f"{detail}")
        if not logged_in:
            r.output += ("\nNote: Google/Kaggle may block bots (CAPTCHA / "
                         "device verification). Try success_selector or "
                         "manual approval.")
    return r


def browser_inspect_element(args: dict) -> BrowserResult:
    """Inspect a DOM element: outer HTML, attributes, computed styles,
    text, and bounding box. Complements browser_tab_html (full DOM),
    browser_console (logs) and browser_network (requests)."""
    sel = str(args.get("selector", "")).strip()
    if not sel:
        return BrowserResult(ok=False, error="selector is required")
    r = _res("inspect_element", {"selector": sel,
                                 "tab_id": args.get("tab_id")})
    if r.ok:
        if not r.parsed.get("found"):
            r.ok = False
            r.error = f"no element matches: {sel}"
            r.output = r.error
        else:
            info = r.parsed
            r.output = (
                f"<{info.get('tag')}> {sel}\n"
                f"box={info.get('box')}\n"
                f"text: {(info.get('text') or '')[:300]}\n"
                f"attrs: {json.dumps(info.get('attrs', {}))[:500]}\n"
                f"computed: {json.dumps(info.get('computed', {}))[:500]}\n"
                f"html[{len(info.get('outerHTML', ''))} chars] "
                f"(full HTML in parsed.outerHTML)")
    return r


def browser_console(args: dict) -> BrowserResult:
    try:
        port, err = _ensure_daemon()
        if port is None:
            return BrowserResult(ok=False, error=err)
        n = int(args.get("tail", 100))
        msgs = _tail_jsonl(CONSOLE_LOG, n)
        url = _tab_url(args.get("tab_id"))
        if url:
            host = urllib.request.urlparse(url).netloc
            msgs = [m for m in msgs if host and host in
                    (m.get("url", "") or "")]
        lines = [f"[{m.get('type')}] {m.get('text','')[:200]}" for m in msgs]
        return BrowserResult(ok=True,
            output="\n".join(lines) or "(no console messages captured)",
            parsed={"messages": msgs, "count": len(msgs)})
    except Exception as e:
        return BrowserResult(ok=False, error=str(e)[:600])


def browser_network(args: dict) -> BrowserResult:
    try:
        port, err = _ensure_daemon()
        if port is None:
            return BrowserResult(ok=False, error=err)
        n = int(args.get("tail", 100))
        reqs = _tail_jsonl(NETWORK_LOG, n)
        url = _tab_url(args.get("tab_id"))
        if url:
            host = urllib.request.urlparse(url).netloc
            reqs = [r for r in reqs if host and host in (r.get("url", "") or "")]
        lines = [f"{r.get('method')} {r.get('url','')[:120]}" for r in reqs]
        return BrowserResult(ok=True,
            output="\n".join(lines) or "(no network requests captured)",
            parsed={"requests": reqs, "count": len(reqs)})
    except Exception as e:
        return BrowserResult(ok=False, error=str(e)[:600])


def browser_download(args: dict) -> BrowserResult:
    try:
        url = args.get("url")
        if not url:
            return BrowserResult(ok=False, error="url is required")
        dest = args.get("dest")
        if not dest:
            name = urllib.request.urlparse(url).path.rsplit("/", 1)[-1] \
                or "download.bin"
            dest = str(Path("/tmp/downloads") / name)
        if Path(dest).exists() and not args.get("confirm", False):
            return BrowserResult(ok=False, error=
                f"{dest} exists — pass confirm=True to overwrite "
                "(never hard-deleted, just refused)")
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0"})
        total = 0
        with urllib.request.urlopen(req, timeout=60) as resp, \
                open(dest, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                total += len(chunk)
        return BrowserResult(ok=True,
            output=f"downloaded {total} bytes -> {dest}",
            parsed={"path": dest, "bytes": total, "url": url})
    except Exception as e:
        return BrowserResult(ok=False, error=str(e)[:600])


def browser_upload(args: dict) -> BrowserResult:
    try:
        sel = args.get("selector")
        fpath = args.get("file_path")
        if not sel:
            return BrowserResult(ok=False, error="selector is required")
        if not fpath or not Path(fpath).exists():
            return BrowserResult(ok=False, error="file_path not found")
        r = _res("upload", {"selector": sel, "file_path": fpath,
                            "tab_id": args.get("tab_id")})
        if r.ok:
            r.output = f"uploaded {fpath} into '{sel}'"
        return r
    except Exception as e:
        return BrowserResult(ok=False, error=str(e)[:600])


def browser_js(args: dict) -> BrowserResult:
    script = args.get("script")
    if not script:
        return BrowserResult(ok=False, error="script is required")
    r = _res("js", {"script": script, "tab_id": args.get("tab_id")})
    if r.ok:
        r.output = f"js result: {json.dumps(r.parsed.get('result'))[:2000]}"
    return r


def _turso_exec(sql: str, params: list | None = None) -> tuple[bool, str]:
    """Minimal Turso pipeline call (no import of turso_tools to avoid cycles)."""
    url = os.environ.get("TURSO_URL", "").strip().rstrip("/")
    token = os.environ.get("TURSO_TOKEN", "").strip()
    if not url or not token:
        return False, "TURSO_URL / TURSO_TOKEN not set"
    def _arg(v):
        if v is None:
            return {"type": "null"}
        if isinstance(v, bool):
            return {"type": "integer", "value": str(int(v))}
        if isinstance(v, int):
            return {"type": "integer", "value": str(v)}
        if isinstance(v, float):
            return {"type": "float", "value": repr(v)}
        return {"type": "text", "value": str(v)}
    body = json.dumps({"requests": [
        {"type": "execute", "stmt": {"sql": sql, "args": [_arg(p) for p in (params or [])]}},
        {"type": "close"},
    ]}).encode()
    req = urllib.request.Request(url + "/v2/pipeline", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return True, resp.read().decode()
    except Exception as e:
        return False, f"turso error: {e}"[:300]


def browser_login_save(args: dict) -> BrowserResult:
    """Save current browser cookies to Turso so logins survive restarts.
    Call after logging into a site. No passwords are stored — only cookies."""
    try:
        r = _res("cookies", {"action": "get"})
        if not r.ok:
            return BrowserResult(ok=False, error="could not read cookies: " + r.error)
        cookies = (r.parsed or {}).get("cookies") or []
        if not cookies:
            return BrowserResult(ok=False, error="no cookies to save (browser empty?)")
        ok, _ = _turso_exec(
            "CREATE TABLE IF NOT EXISTS browser_logins "
            "(id INTEGER PRIMARY KEY CHECK (id = 1), cookies TEXT NOT NULL, updated_at TEXT NOT NULL)")
        if not ok:
            return BrowserResult(ok=False, error="turso unavailable")
        import datetime
        now = datetime.datetime.utcnow().isoformat() + "Z"
        ok, _ = _turso_exec(
            "INSERT INTO browser_logins (id, cookies, updated_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET cookies=excluded.cookies, updated_at=excluded.updated_at",
            [json.dumps(cookies), now])
        if not ok:
            return BrowserResult(ok=False, error="turso write failed")
        domains = sorted({(c.get("domain") or "").lstrip(".") for c in cookies if c.get("domain")})
        return BrowserResult(ok=True,
                             output=f"saved {len(cookies)} cookies ({len(domains)} sites) to cloud backup",
                             parsed={"cookies": len(cookies), "domains": domains[:20]})
    except Exception as e:
        return BrowserResult(ok=False, error=str(e)[:300])


def browser_login_restore(args: dict) -> BrowserResult:
    """Restore browser cookies from Turso cloud backup (logins survive restarts)."""
    try:
        ok, payload = _turso_exec("SELECT cookies FROM browser_logins WHERE id = 1")
        if not ok:
            return BrowserResult(ok=False, error="turso unavailable")
        try:
            data = json.loads(payload)
            rows = ((data.get("results") or [{}])[0].get("response", {}).get("result", {}).get("rows") or [])
        except Exception:
            rows = []
        if not rows:
            return BrowserResult(ok=False, error="no saved login found (save one first)")
        try:
            cookies = json.loads(rows[0][0].get("value") if isinstance(rows[0][0], dict) else rows[0][0])
        except Exception:
            return BrowserResult(ok=False, error="saved login data corrupted")
        r = _res("cookies_restore", {"cookies": cookies})
        if not r.ok:
            return BrowserResult(ok=False, error="restore failed: " + r.error)
        n = (r.parsed or {}).get("restored", 0)
        return BrowserResult(ok=True, output=f"restored {n} cookies — logins active",
                             parsed={"restored": n})
    except Exception as e:
        return BrowserResult(ok=False, error=str(e)[:300])


def browser_cookies(args: dict) -> BrowserResult:
    try:
        action = str(args.get("action", "get")).lower()
        if action not in ("get", "set", "clear"):
            return BrowserResult(ok=False, error="action must be get|set|clear")
        if action == "clear" and not args.get("confirm", False):
            return BrowserResult(ok=False, error=
                "clearing cookies is destructive — pass confirm=True")
        if action == "set" and ("name" not in args or "value" not in args):
            return BrowserResult(ok=False,
                                 error="set needs name and value")
        r = _res("cookies", {"action": action, "name": args.get("name"),
                             "value": args.get("value"),
                             "tab_id": args.get("tab_id")})
        if r.ok:
            r.output = f"cookies {action}: " + json.dumps(r.parsed)[:1500]
        return r
    except Exception as e:
        return BrowserResult(ok=False, error=str(e)[:600])


def browser_storage(args: dict) -> BrowserResult:
    try:
        action = str(args.get("action", "get")).lower()
        stype = str(args.get("type", "local")).lower()
        if action not in ("get", "set", "clear"):
            return BrowserResult(ok=False, error="action must be get|set|clear")
        if stype not in ("local", "session"):
            return BrowserResult(ok=False, error="type must be local|session")
        if action == "clear" and not args.get("confirm", False):
            return BrowserResult(ok=False, error=
                "clearing storage is destructive — pass confirm=True")
        if action == "set" and ("key" not in args or "value" not in args):
            return BrowserResult(ok=False, error="set needs key and value")
        r = _res("storage", {"action": action, "type": stype,
                             "key": args.get("key"),
                             "value": args.get("value"),
                             "tab_id": args.get("tab_id")})
        if r.ok:
            r.output = f"{stype} storage {action}: " + \
                       json.dumps(r.parsed)[:1500]
        return r
    except Exception as e:
        return BrowserResult(ok=False, error=str(e)[:600])


def browser_viewport(args: dict) -> BrowserResult:
    try:
        w, h = int(args["width"]), int(args["height"])
        if w < 100 or h < 100 or w > 7680 or h > 4320:
            return BrowserResult(ok=False, error="viewport out of range")
        r = _res("viewport", {"width": w, "height": h,
                              "tab_id": args.get("tab_id")})
        if r.ok:
            cfg = _load_config(); cfg["viewport"] = [w, h]; _save_config(cfg)
            r.output = f"viewport set to {w}x{h}"
        return r
    except Exception as e:
        return BrowserResult(ok=False, error=str(e)[:600])


def browser_ua(args: dict) -> BrowserResult:
    try:
        ua = args.get("user_agent")
        if not ua:
            return BrowserResult(ok=False, error="user_agent is required")
        cfg = _load_config(); cfg["user_agent"] = ua; _save_config(cfg)
        return BrowserResult(ok=True,
            output="user-agent stored; applies to new browser contexts "
                   "(restart the browser daemon to take effect on existing tabs)",
            parsed={"user_agent": ua, "applies_to": "new_contexts"})
    except Exception as e:
        return BrowserResult(ok=False, error=str(e)[:600])


def browser_proxy(args: dict) -> BrowserResult:
    try:
        proxy_url = args.get("proxy_url")
        if not proxy_url:
            return BrowserResult(ok=False, error="proxy_url is required")
        cfg = _load_config()
        cfg["proxy"] = {"server": proxy_url}
        _save_config(cfg)
        return BrowserResult(ok=True,
            output="proxy stored; applies to NEW contexts only "
                   "(restart the browser daemon for existing tabs to use it)",
            parsed={"proxy": proxy_url, "applies_to": "new_contexts"})
    except Exception as e:
        return BrowserResult(ok=False, error=str(e)[:600])


# ---------------------------------------------------------------------------


def browser_launch(args: dict) -> BrowserResult:
    """Start the managed Playwright browser daemon (idempotent); return CDP port."""
    try:
        port, err = _ensure_daemon()
        if port is None:
            return BrowserResult(ok=False, error=err)
        return BrowserResult(ok=True,
            output=f"browser daemon running on CDP port {port}",
            parsed={"port": port, "cdp_url": f"http://127.0.0.1:{port}",
                    "running": True})
    except Exception as e:
        return BrowserResult(ok=False, error=str(e)[:600])


def _tor_mgr():
    try:
        from agent.browser.tor_manager import ensure_tor, tor_status, tor_running
        return ensure_tor, tor_status, tor_running
    except ImportError:
        from browser.tor_manager import ensure_tor, tor_status, tor_running  # type: ignore
        return ensure_tor, tor_status, tor_running


def tor_status_tool(args: dict) -> BrowserResult:
    """Check Tor SOCKS proxy status."""
    try:
        _, tor_status, _ = _tor_mgr()
        st = tor_status()
        return BrowserResult(ok=True, output=json.dumps(st),
                             parsed=st)
    except Exception as e:
        return BrowserResult(ok=False, error=str(e)[:300])


def tor_start(args: dict) -> BrowserResult:
    """Start Tor SOCKS proxy on 127.0.0.1:9050 (self-healing download if needed)."""
    try:
        ensure_tor, tor_status, _ = _tor_mgr()
        ok, msg = ensure_tor()
        st = tor_status()
        return BrowserResult(ok=ok, output=msg, parsed=st)
    except Exception as e:
        return BrowserResult(ok=False, error=str(e)[:300])


def tor_browse(args: dict) -> BrowserResult:
    """Browse a .onion URL through Tor. Returns screenshot + page text.
    args: {url: str}. Tor is auto-started if needed."""
    url = (args.get("url") or "").strip()
    if not url:
        return BrowserResult(ok=False, error="url is required")
    if ".onion" not in url:
        return BrowserResult(ok=False, error="tor_browse is for .onion URLs only")
    try:
        ensure_tor, _, tor_running = _tor_mgr()
        ok, msg = ensure_tor()
        if not ok:
            return BrowserResult(ok=False, error="tor not available: " + msg)
        # Use the existing browser daemon via CDP, with a Tor-proxied context.
        from playwright.sync_api import sync_playwright
        port, err = _ensure_daemon()
        if port is None:
            return BrowserResult(ok=False, error="browser daemon: " + str(err))
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            try:
                # New isolated context routed through Tor SOCKS proxy.
                ctx = browser.new_context(
                    proxy={"server": "socks5://127.0.0.1:9050"})
                try:
                    page = ctx.new_page()
                    page.goto(url, timeout=60000,
                              wait_until="domcontentloaded")
                    page.wait_for_timeout(3000)
                    title = page.title()
                    text = page.inner_text("body")[:5000]
                    shot = page.screenshot()
                    import base64
                    data_uri = ("data:image/png;base64," +
                                base64.b64encode(shot).decode())
                finally:
                    ctx.close()
            finally:
                browser.close()
        return BrowserResult(ok=True,
                             output=f"tor browse OK: {title[:100]}",
                             parsed={"url": url, "title": title,
                                     "text": text, "data_uri": data_uri})
    except Exception as e:
        return BrowserResult(ok=False, error=str(e)[:500])


def browser_close(args: dict) -> BrowserResult:
    """Stop the managed browser daemon (SIGTERM, SIGKILL fallback); clean state files."""
    try:
        import signal
        pid_file = BASE_DIR / "daemon.pid"
        killed = False
        try:
            pid = int(pid_file.read_text().strip())
            try:
                os.kill(pid, signal.SIGTERM)
                for _ in range(20):
                    time.sleep(0.25)
                    try:
                        os.kill(pid, 0)
                    except OSError:
                        killed = True
                        break
                if not killed:
                    os.kill(pid, signal.SIGKILL)
                    killed = True
            except ProcessLookupError:
                pass
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            return BrowserResult(ok=False, error=f"could not stop daemon: {e}")
        for stale in (PORT_FILE, pid_file, HEARTBEAT):
            try:
                stale.unlink()
            except OSError:
                pass
        return BrowserResult(ok=True,
            output="browser daemon stopped" if killed else "browser daemon was not running",
            parsed={"stopped": True, "was_running": killed})
    except Exception as e:
        return BrowserResult(ok=False, error=str(e)[:600])


def browser_scroll(args: dict) -> BrowserResult:
    """Scroll the page or an element: direction up|down|left|right|top|bottom."""
    r = _res("scroll", {"tab_id": args.get("tab_id"),
                        "direction": args.get("direction", "down"),
                        "pixels": args.get("pixels", 600),
                        "selector": args.get("selector")})
    if r.ok:
        r.output = (f"scrolled {r.parsed.get('direction')} "
                    f"-> scroll={r.parsed.get('scroll')}")
    return r


def cross_browser_note(args: dict) -> BrowserResult:
    """Honest cross-browser validation guidance."""
    return BrowserResult(ok=True,
        output=("Cross-browser validation: this agent drives Chromium via "
                "Playwright. For Firefox/WebKit, install them "
                "(`playwright install firefox webkit`) and rerun the same "
                "browser_* flows; compare screenshots with visual_verify. "
                "No multi-engine grid is running here."),
        parsed={"guidance": True})


# Module contract
# ---------------------------------------------------------------------------

def _S(arg_type: str, required: bool, desc: str) -> dict:
    return {"type": arg_type, "required": required, "description": desc}


_TAB = {"tab_id": _S("string", False,
                     "Tab id (default: active tab)")}

TOOL_DEFS: list[tuple] = [
    ("browser_back", "Go back in the active tab's history",
     dict(_TAB), "computer", 60),
    ("browser_forward", "Go forward in the active tab's history",
     dict(_TAB), "computer", 60),
    ("browser_refresh", "Reload the active tab",
     dict(_TAB), "computer", 60),
    ("browser_new_tab", "Open a new persistent tab (optional URL)",
     {"url": _S("string", False, "URL to open")}, "computer", 90),
    ("browser_close_tab", "Close a tab",
     {"tab_id": _S("string", True, "Tab id")}, "computer", 60),
    ("browser_switch_tab", "Make a tab the active tab",
     {"tab_id": _S("string", True, "Tab id")}, "computer", 60),
    ("browser_tab_list", "List all persistent tabs",
     {}, "computer", 60),
    ("browser_tab_screenshot", "Screenshot a tab to PNG",
     {**_TAB, "output": _S("string", False, "Output path"),
      "full_page": _S("boolean", False, "Full-page capture")},
     "computer", 60),
    ("browser_tab_html", "Get a tab's full HTML",
     dict(_TAB), "computer", 60),
    ("browser_tab_text", "Get a tab's visible text",
     dict(_TAB), "computer", 60),
    ("browser_click", "Click a selector in a tab",
     {**_TAB, "selector": _S("string", True, "CSS selector"),
      "timeout_ms": _S("integer", False, "Wait timeout ms (default 10000)")},
     "computer", 90),
    ("browser_fill", "Fill an input/textarea",
     {**_TAB, "selector": _S("string", True, "CSS selector"),
      "value": _S("string", False, "Value")}, "computer", 90),
    ("browser_select", "Select a dropdown option",
     {**_TAB, "selector": _S("string", True, "CSS selector"),
      "value": _S("string", True, "Option value")}, "computer", 90),
    ("browser_form_submit", "Submit the form containing a selector",
     {**_TAB, "selector": _S("string", True, "CSS selector")},
     "computer", 90),
    ("browser_wait", "Wait for a selector or a fixed timeout",
     {**_TAB, "selector": _S("string", False, "CSS selector to wait for"),
      "timeout_ms": _S("integer", False, "Timeout ms (default 10000)")},
     "computer", 180),
    ("browser_console", "Read captured console messages (daemon log)",
     {**_TAB, "tail": _S("integer", False, "Last N messages (default 100)")},
     "computer", 60),
    ("browser_network", "Read captured network requests (daemon log)",
     {**_TAB, "tail": _S("integer", False, "Last N requests (default 100)")},
     "computer", 60),
    ("browser_login", "Log in to a website like an assistant: auto-detect fields, multi-step + TOTP support, verifies login. Google/Kaggle best-effort (bot detection may block). Password never logged/persisted.",
     {"url": _S("string", True, "Login page URL"),
      "username": _S("string", True, "Username / email"),
      "password": _S("string", True, "Password (in-memory only, never logged)"),
      "username_selector": _S("string", False, "CSS selector override"),
      "password_selector": _S("string", False, "CSS selector override"),
      "submit_selector": _S("string", False, "CSS selector override"),
      "success_selector": _S("string", False, "Selector proving login worked"),
      "totp_secret": _S("string", False, "Base32 TOTP secret (optional)"),
      "tab_id": _S("string", False, "Tab id (default: active tab)")},
     "computer", 180),
    ("browser_inspect_element", "Inspect a DOM element: outer HTML, attributes, computed styles, text, bounding box",
     {**_TAB, "selector": _S("string", True, "CSS selector")},
     "computer", 60),
    ("browser_download", "Download a URL to a file (refuses overwrite without confirm)",
     {"url": _S("string", True, "File URL"),
      "dest": _S("string", False, "Destination path"),
      "confirm": _S("boolean", False, "Confirm overwrite")},
     "computer", 300),
    ("browser_upload", "Set files on a file input",
     {**_TAB, "selector": _S("string", True, "CSS selector"),
      "file_path": _S("string", True, "Local file to upload")},
     "computer", 120),
    ("browser_js", "Evaluate JavaScript in a tab; return JSON-serializable result",
     {**_TAB, "script": _S("string", True, "JS expression")},
     "computer", 90),
    ("browser_cookies", "Get/set/clear cookies (clear needs confirm)",
     {**_TAB, "action": _S("string", False, "get|set|clear (default get)"),
      "name": _S("string", False, "Cookie name (set)"),
      "value": _S("string", False, "Cookie value (set)"),
      "confirm": _S("boolean", False, "Confirm clear")}, "computer", 60),
    ("browser_login_save", "Save current logins (cookies) to cloud so they survive restarts. Call after logging in.",
     {}, "computer", 60),
    ("browser_login_restore", "Restore saved logins (cookies) from cloud backup.",
     {}, "computer", 60),
    ("tor_status", "Check Tor SOCKS proxy status.",
     {}, "network", 30),
    ("tor_start", "Start Tor SOCKS proxy on 127.0.0.1:9050.",
     {}, "network", 60),
    ("tor_browse", "Browse a .onion URL through Tor. Returns screenshot + text.",
     {"url": _S("string", True, ".onion URL")}, "network", 120),
    ("browser_storage", "Get/set/clear local|session storage (clear needs confirm)",
     {**_TAB, "action": _S("string", False, "get|set|clear (default get)"),
      "type": _S("string", False, "local|session (default local)"),
      "key": _S("string", False, "Key (set)"),
      "value": _S("string", False, "Value (set)"),
      "confirm": _S("boolean", False, "Confirm clear")}, "computer", 60),
    ("browser_viewport", "Set the active tab's viewport size",
     {**_TAB, "width": _S("integer", True, "Width"),
      "height": _S("integer", True, "Height")}, "computer", 60),
    ("browser_ua", "Set user-agent for new contexts (stored config)",
     {"user_agent": _S("string", True, "User-Agent string")},
     "computer", 60),
    ("browser_proxy", "Set proxy for new contexts (stored config; new tabs only)",
     {"proxy_url": _S("string", True, "Proxy URL, e.g. http://host:port")},
     "computer", 60),
    ("browser_launch", "Start the managed Playwright browser daemon (idempotent)",
     {}, "computer", 90),
    ("browser_close", "Stop the managed browser daemon gracefully",
     {}, "computer", 60),
    ("browser_scroll", "Scroll page or element (up|down|left|right|top|bottom)",
     {**_TAB, "direction": _S("string", False, "Direction (default down)"),
      "pixels": _S("integer", False, "Pixels for directional scroll (default 600)"),
      "selector": _S("string", False, "CSS selector to scroll instead of page")},
     "computer", 60),

    ("cross_browser_note", "Honest cross-browser validation guidance",
     {}, "computer", 10),

]

HANDLERS: dict[str, Any] = {
    "browser_back": browser_back,
    "browser_forward": browser_forward,
    "browser_refresh": browser_refresh,
    "browser_new_tab": browser_new_tab,
    "browser_close_tab": browser_close_tab,
    "browser_switch_tab": browser_switch_tab,
    "browser_tab_list": browser_tab_list,
    "browser_tab_screenshot": browser_tab_screenshot,
    "browser_tab_html": browser_tab_html,
    "browser_tab_text": browser_tab_text,
    "browser_click": browser_click,
    "browser_fill": browser_fill,
    "browser_select": browser_select,
    "browser_form_submit": browser_form_submit,
    "browser_wait": browser_wait,
    "browser_console": browser_console,
    "browser_network": browser_network,
    "browser_login": browser_login,
    "browser_inspect_element": browser_inspect_element,
    "browser_download": browser_download,
    "browser_upload": browser_upload,
    "browser_js": browser_js,
    "browser_cookies": browser_cookies,
    "browser_login_save": browser_login_save,
    "browser_login_restore": browser_login_restore,
    "tor_status": tor_status_tool,
    "tor_start": tor_start,
    "tor_browse": tor_browse,
    "browser_storage": browser_storage,
    "browser_viewport": browser_viewport,
    "browser_ua": browser_ua,
    "browser_proxy": browser_proxy,
    "browser_launch": browser_launch,
    "browser_close": browser_close,
    "browser_scroll": browser_scroll,
    "cross_browser_note": cross_browser_note,

}

__all__ = ["BrowserResult", "TOOL_DEFS", "HANDLERS"]
