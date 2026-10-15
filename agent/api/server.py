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
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent.parent))

from agent.app.orchestrator import Orchestrator
from agent.tools.registry import default_registry
from agent.tools.dispatcher import Dispatcher
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
    dispatcher = Dispatcher(default_registry(), workspace=workspace)
    return Orchestrator(provider, dispatcher)


class Handler(BaseHTTPRequestHandler):
    orchestrator: Orchestrator | None = None

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
                result = Handler.orchestrator.run(prompt)
                return self._json(200, {"success": True, **result})
            except Exception as e:
                return self._json(500, {"success": False, "error": str(e)[:500]})

        if path == "/api/tools/execute":
            name = body.get("name", "")
            args = body.get("args", {})
            audit.record("tool_execute", details={"tool": name})
            try:
                disp = Dispatcher()
                result = disp.execute(name, args)
                return self._json(200, {"success": True, "result": result})
            except Exception as e:
                return self._json(500, {"success": False, "error": str(e)[:500]})

        return self._json(404, {"error": "not found"})

    def log_message(self, *args):
        pass  # quiet


def run(port: int | None = None):
    port = port or int(os.environ.get("PORT", "8000"))
    Handler.orchestrator = None  # lazy init per request
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"Haley agent API on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    run()
