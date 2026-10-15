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
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent.parent))

from agent.app.orchestrator import Orchestrator
from agent.tools.registry import default_registry
from agent.tools.dispatcher import Dispatcher
from agent.security.audit_log import AuditLog


APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
audit = AuditLog()


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

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/health":
            return self._json(200, {"ok": True, "agent": "haley-py"})
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
                    Handler.orchestrator = Orchestrator()
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
