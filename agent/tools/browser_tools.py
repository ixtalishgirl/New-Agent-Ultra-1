"""Live Browser Control tool module — real Chromium driven over CDP.

Architecture (no live-browser dependency at import time):
  * A persistent daemon (``/tmp/browser_daemon/daemon.py``) launches one
    headless Chromium with ``launch_persistent_context`` on a fixed
    user-data-dir, exposes it on a CDP port, and appends console messages
    and network requests to JSONL logs.
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
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

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
import json, socket, sys, time
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
    kwargs = {"user_data_dir": str(profile), "headless": True,
              "args": ["--no-sandbox", f"--remote-debugging-port={port}"]}
    if cfg.get("user_agent"):
        kwargs["user_agent"] = cfg["user_agent"]
    if cfg.get("proxy"):
        kwargs["proxy"] = cfg["proxy"]
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch_persistent_context(**kwargs)
            ctx = browser.contexts[0] if browser.contexts else None
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
    try:
        from playwright.sync_api import sync_playwright
        sess = load(BASE / "tabs.json",
                    {"seq": 0, "active": None, "tabs": {}})
        tabs = sess.setdefault("tabs", {})

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
            ctx = browser.contexts[0]
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
                page.click(args["selector"], timeout=args.get("timeout_ms", 10000))
                page.wait_for_timeout(500)
                out = {"tab_id": tid, "url": page.url}
            elif op == "fill":
                tid, page = resolve(ctx, args.get("tab_id"))
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
            save(BASE / "tabs.json", sess)
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
            r.output = f"screenshot -> {r.parsed.get('path')}"
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
    r = _res("fill", {"selector": sel, "value": str(args.get("value", "")),
                      "tab_id": args.get("tab_id")})
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
    "browser_download": browser_download,
    "browser_upload": browser_upload,
    "browser_js": browser_js,
    "browser_cookies": browser_cookies,
    "browser_storage": browser_storage,
    "browser_viewport": browser_viewport,
    "browser_ua": browser_ua,
    "browser_proxy": browser_proxy,
}

__all__ = ["BrowserResult", "TOOL_DEFS", "HANDLERS"]
