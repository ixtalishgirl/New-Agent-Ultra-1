"""
Haley agent API server — stdlib only (no Flask/FastAPI dependency).
Endpoints:
  POST /api/chat       — agentic loop (auth required)
  GET  /api/health     — no auth
  GET  /api/tools      — list tools (auth)
  POST /api/tools/execute — run tool (auth)
  GET  /api/tunnel/status — tunnel health (auth)
Auth: x-app-password header or ?password= query.
"""
from __future__ import annotations
import json
import mimetypes
import os
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent.parent))

from agent.app.orchestrator import Orchestrator
from agent.tools.registry import build_full_registry as default_registry
from agent.tools.dispatcher import Dispatcher
from agent.tools.permission_manager import build_manager
from agent.security.audit_log import AuditLog


APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
audit = AuditLog()

# Dashboard: built web UI served as static files (additive — API routes unchanged).
# `dashboard/dist/` is produced by `npm --prefix dashboard run build`.
DASHBOARD_DIR = Path(__file__).resolve().parent.parent.parent / "dashboard" / "dist"


def check_auth(handler: BaseHTTPRequestHandler) -> bool:
    if not APP_PASSWORD:
        return True  # no password configured = open (dev only)
    # Header
    if handler.headers.get("x-app-password") == APP_PASSWORD:
        return True
    # Query param
    qs = urllib.parse.urlparse(handler.path).query
    params = urllib.parse.parse_qs(qs)
    if params.get("password", [""])[0] == APP_PASSWORD:
        return True
    return False


def _make_dispatcher(workspace: str) -> Dispatcher:
    """Dispatcher with the Permission Manager wired in (all ON except the
    5 locked denials). Single shared construction point."""
    reg = default_registry()
    return Dispatcher(reg, workspace=workspace,
                      permission_manager=build_manager(reg))


def _make_orchestrator():
    """Build the orchestrator the same way the CLI does (provider + dispatcher).
    Model URL resolves from HALEY_API_URL (set on Render to the ngrok tunnel)."""
    from agent.model.qwen_adapter import QwenAdapter
    base_url = os.environ.get(
        "HALEY_API_URL",
        os.environ.get("QWEN_BASE_URL", "http://localhost:8001"),
    )
    provider = QwenAdapter(base_url=base_url)
    workspace = os.environ.get("AGENT_WORKSPACE", str(Path.home() / "workspace"))
    os.makedirs(workspace, exist_ok=True)
    dispatcher = _make_dispatcher(workspace)
    return Orchestrator(provider, dispatcher)


class Handler(BaseHTTPRequestHandler):
    orchestrator: Orchestrator | None = None
    chat_lock = threading.Lock()

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # CORS: allow the dashboard on the sibling Render service to call
        # the browser APIs directly (location selector: Germany/USA).
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, x-app-password")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        # CORS preflight for cross-service dashboard calls.
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, x-app-password")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            try:
                return json.loads(self.rfile.read(length))
            except Exception:
                return {}
        return {}

    def _serve_dashboard(self, path: str):
        """Serve the built React dashboard (dashboard/dist). No auth — the UI
        itself collects the app password for its API calls. API routes are
        untouched; only non-/api/ paths reach here."""
        if not DASHBOARD_DIR.is_dir():
            return self._json(404, {"error": "not found"})
        rel = (path.lstrip("/") or "index.html").split("?")[0]
        target = (DASHBOARD_DIR / rel).resolve()
        # Block path traversal outside dashboard/dist
        if DASHBOARD_DIR.resolve() not in target.parents and target != DASHBOARD_DIR.resolve():
            return self._json(404, {"error": "not found"})
        if not target.is_file():
            # SPA fallback: extensionless routes serve index.html
            if "." not in Path(rel).name:
                target = DASHBOARD_DIR / "index.html"
            else:
                return self._json(404, {"error": "not found"})
        try:
            data = target.read_bytes()
        except OSError:
            return self._json(404, {"error": "not found"})
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache" if target.name == "index.html" else "public, max-age=31536000")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/health":
            return self._json(200, {"ok": True, "agent": "haley-py"})
        if path == "/api/ip":
            # Egress IP + geolocation check (for Germany/region verification)
            try:
                with urllib.request.urlopen("http://ip-api.com/json/", timeout=10) as r:
                    data = json.loads(r.read().decode())
                return self._json(200, {
                    "ip": data.get("query"),
                    "country": data.get("country"),
                    "countryCode": data.get("countryCode"),
                    "city": data.get("city"),
                    "region": os.environ.get("RENDER_REGION", "unknown"),
                })
            except Exception as e:
                return self._json(500, {"error": str(e)})
        if not path.startswith("/api/"):
            return self._serve_dashboard(path)
        if not check_auth(self):
            return self._json(401, {"error": "unauthorized"})
        if path == "/api/tools":
            reg = default_registry()
            return self._json(200, {"tools": [
                {"name": t.name, "description": t.description, "status": t.status}
                for t in reg.list()
            ]})
        if path == "/api/tunnel/status":
            return self._json(200, {
                "tunnel": "external",
                "note": "ngrok tunnel managed externally (Kaggle GPU side)",
            })
        if path == "/api/permissions":
            reg = default_registry()
            pm = build_manager(reg)
            perms = pm.list_all()
            return self._json(200, {
                "total": len(perms),
                "enabled": sum(1 for p in perms if p["enabled"]),
                "locked": sum(1 for p in perms if p.get("locked")),
                "permissions": [
                    {"name": p["title"], "category": p["category"],
                     "enabled": p["enabled"], "locked": bool(p.get("locked"))}
                    for p in perms
                ],
            })
        if path == "/api/browser/shot":
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            url = (params.get("url", [""])[0] or "").strip()
            if not url:
                return self._json(400, {"error": "url required"})
            try:
                disp = _make_dispatcher(os.getcwd())
                res = disp.dispatch("browser_screenshot", {"url": url})
                if not res.ok:
                    return self._json(502, {"error": res.output[:300]})
                try:
                    payload = json.loads(res.output)
                except Exception:
                    return self._json(502, {"error": "bad screenshot payload"})
                return self._json(200, payload)
            except Exception as e:
                return self._json(500, {"error": str(e)[:300]})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not check_auth(self):
            return self._json(401, {"error": "unauthorized"})
        body = self._body()

        if path == "/api/chat":
            prompt = body.get("prompt", body.get("message", ""))
            if not prompt:
                return self._json(400, {"error": "prompt required"})
            audit.record("chat", details={"prompt_len": len(prompt)})
            try:
                if not Handler.orchestrator:
                    Handler.orchestrator = _make_orchestrator()
                # Serialize chat runs: the orchestrator keeps per-run history.
                with Handler.chat_lock:
                    result = Handler.orchestrator.run(prompt)
                data = result.to_dict() if hasattr(result, "to_dict") else dict(result)
                return self._json(200, {"success": True, **data})
            except Exception as e:
                return self._json(500, {"success": False, "error": str(e)[:500]})

        if path == "/api/selfmod/demo":
            # Live demo: one safe self-mod (dashboard accent color rotation),
            # verified before/after. Visible proof the agent modifies itself.
            audit.record("selfmod_demo", details={})
            try:
                from agent.tools.selfmod_tools import selfmod_demo
                res = selfmod_demo()
                return self._json(200, {"success": bool(res.get("ok")), **res})
            except Exception as e:
                return self._json(500, {"success": False, "error": str(e)[:500]})

        if path == "/api/tools/execute":
            name = body.get("name", "")
            args = body.get("args", {})
            audit.record("tool_execute", details={"tool": name})
            try:
                disp = _make_dispatcher(os.getcwd())
                res = disp.dispatch(name, args)
                return self._json(200, {
                    "success": res.ok,
                    "result": res.output,
                    **({} if res.ok else {"error": res.output[:500]}),
                })
            except Exception as e:
                return self._json(500, {"success": False, "error": str(e)[:500]})

        return self._json(404, {"error": "not found"})

    def log_message(self, *args):
        pass  # quiet


def run(port: int | None = None):
    port = port or int(os.environ.get("PORT", "8000"))
    Handler.orchestrator = None  # lazy init per request
    # Start Tor in the background for onion browsing (best effort).
    try:
        import threading

        def _tor():
            try:
                from agent.browser.tor_manager import ensure_tor
            except ImportError:
                from browser.tor_manager import ensure_tor  # type: ignore
            ensure_tor()
        threading.Thread(target=_tor, daemon=True).start()
    except Exception:
        pass
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    print(f"Haley agent API on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    run()
