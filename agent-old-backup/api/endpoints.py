"""API endpoints — full REST API for the agent (ChatGPT spec).

Per spec: agent/api/ endpoints
Chat, tools, plans, sessions, memory, improvements, health.
Uses stdlib http.server. Bearer token auth.
"""
from __future__ import annotations

import json
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable, Dict, List, Optional, Tuple

from .authentication import AuthManager, RateLimiter
from .schemas import (
    ChatRequest, ChatResponse, ToolCallRequest, ToolCallResponse,
    PlanRequest, ErrorResponse, HealthResponse, paginate,
)


class Router:
    """Simple router: (method, path_pattern) -> handler."""

    def __init__(self):
        self._routes: List[Tuple[str, str, Callable]] = []

    def add(self, method: str, path: str, handler: Callable) -> None:
        self._routes.append((method.upper(), path, handler))

    def get(self, path: str, handler: Callable) -> None:
        self.add("GET", path, handler)

    def post(self, path: str, handler: Callable) -> None:
        self.add("POST", path, handler)

    def delete(self, path: str, handler: Callable) -> None:
        self.add("DELETE", path, handler)

    def match(self, method: str,
              path: str) -> Tuple[Optional[Callable], Dict[str, str]]:
        """Match route. Supports :param segments."""
        clean = urllib.parse.urlparse(path).path
        for m, pattern, handler in self._routes:
            if m != method.upper():
                continue
            params = self._match_pattern(pattern, clean)
            if params is not None:
                return handler, params
        return None, {}

    @staticmethod
    def _match_pattern(pattern: str,
                       path: str) -> Optional[Dict[str, str]]:
        pp = [p for p in pattern.split("/") if p]
        cp = [p for p in path.split("/") if p]
        if len(pp) != len(cp):
            return None
        params = {}
        for a, b in zip(pp, cp):
            if a.startswith(":"):
                params[a[1:]] = urllib.parse.unquote(b)
            elif a != b:
                return None
        return params


class APIContext:
    """Shared context for endpoint handlers."""

    def __init__(self):
        self.auth = AuthManager()
        self.rate_limiter = RateLimiter(max_requests=200)
        self.orchestrator: Any = None
        self.dispatcher: Any = None
        self.session_manager: Any = None
        self.goal_manager: Any = None
        self.start_time = time.time()

    def uptime_seconds(self) -> float:
        return time.time() - self.start_time


def _json_response(code: int, obj: Dict[str, Any]) -> Tuple[int, Dict, str]:
    return code, {"Content-Type": "application/json"}, json.dumps(
        obj, default=str)


def _require_auth(ctx: APIContext, headers: Dict[str, str],
                  query: Dict[str, List[str]]) -> Tuple[bool, str]:
    """Check bearer auth. Returns (ok, error)."""
    # Skip auth if no tokens configured (dev mode)
    if not ctx.auth._tokens:
        return True, ""
    auth_h = headers.get("authorization", "") or headers.get(
        "Authorization", "")
    token = AuthManager.extract_bearer(auth_h)
    if not token:
        token = (query.get("token") or [""])[0]
    if ctx.auth.verify(token):
        return True, ""
    return False, "invalid or missing bearer token"


def _rate_limit(ctx: APIContext, key: str) -> Tuple[bool, str]:
    if ctx.rate_limiter.allow(key):
        return True, ""
    return False, "rate limit exceeded"


# ---------------------------------------------------------------------------
# Endpoint handlers — each takes (ctx, params, query, headers, body)
# ---------------------------------------------------------------------------

def health(ctx: APIContext, params: Dict, query: Dict,
           headers: Dict, body: Dict) -> Tuple[int, Dict, str]:
    return _json_response(200, HealthResponse().to_dict())


def chat(ctx: APIContext, params: Dict, query: Dict,
         headers: Dict, body: Dict) -> Tuple[int, Dict, str]:
    ok, err = _require_auth(ctx, headers, query)
    if not ok:
        return _json_response(401, ErrorResponse(err, 401).to_dict())
    ok, err = _rate_limit(ctx, "chat")
    if not ok:
        return _json_response(429, ErrorResponse(err, 429).to_dict())
    req = ChatRequest.from_dict(body)
    errors = req.validate()
    if errors:
        return _json_response(400,
                              ErrorResponse("; ".join(errors)).to_dict())
    start = time.time()
    # Route through orchestrator if available, else echo
    reply = ""
    tool_calls: List[Dict] = []
    if ctx.orchestrator:
        try:
            result = ctx.orchestrator.run(req.message)
            reply = str(result.get("reply", result))
            tool_calls = result.get("tool_calls", [])
        except Exception as e:
            reply = f"Orchestrator error: {e}"
    else:
        reply = f"(no orchestrator) You said: {req.message[:500]}"
    session_id = req.session_id or "default"
    if ctx.session_manager:
        ctx.session_manager.add_message("user", req.message,
                                        session_id=session_id)
        ctx.session_manager.add_message("assistant", reply,
                                        session_id=session_id)
    resp = ChatResponse(
        reply=reply, session_id=session_id,
        tool_calls=tool_calls,
        duration_ms=int((time.time() - start) * 1000))
    return _json_response(200, resp.to_dict())


def list_tools(ctx: APIContext, params: Dict, query: Dict,
               headers: Dict, body: Dict) -> Tuple[int, Dict, str]:
    ok, err = _require_auth(ctx, headers, query)
    if not ok:
        return _json_response(401, ErrorResponse(err, 401).to_dict())
    tools = []
    if ctx.dispatcher and hasattr(ctx.dispatcher, "registry"):
        for t in ctx.dispatcher.registry.list():
            tools.append({"name": t.name,
                          "description": getattr(t, "description", "")})
    return _json_response(200, {"tools": tools, "count": len(tools)})


def call_tool(ctx: APIContext, params: Dict, query: Dict,
              headers: Dict, body: Dict) -> Tuple[int, Dict, str]:
    ok, err = _require_auth(ctx, headers, query)
    if not ok:
        return _json_response(401, ErrorResponse(err, 401).to_dict())
    req = ToolCallRequest.from_dict(body)
    errors = req.validate()
    if errors:
        return _json_response(400,
                              ErrorResponse("; ".join(errors)).to_dict())
    if not ctx.dispatcher:
        return _json_response(503, ErrorResponse(
            "dispatcher not available", 503).to_dict())
    try:
        result = ctx.dispatcher.dispatch(req.name, req.args)
        resp = ToolCallResponse(ok=result.ok, output=result.output,
                                tool=req.name)
        return _json_response(200, resp.to_dict())
    except Exception as e:
        return _json_response(500, ErrorResponse(str(e), 500).to_dict())


def create_plan(ctx: APIContext, params: Dict, query: Dict,
                headers: Dict, body: Dict) -> Tuple[int, Dict, str]:
    ok, err = _require_auth(ctx, headers, query)
    if not ok:
        return _json_response(401, ErrorResponse(err, 401).to_dict())
    req = PlanRequest.from_dict(body)
    errors = req.validate()
    if errors:
        return _json_response(400,
                              ErrorResponse("; ".join(errors)).to_dict())
    try:
        from agent.planning import Planner
        planner = Planner()
        plan = planner.plan(req.goal)
        return _json_response(200, plan.to_dict())
    except Exception as e:
        return _json_response(500, ErrorResponse(str(e), 500).to_dict())


def list_sessions(ctx: APIContext, params: Dict, query: Dict,
                  headers: Dict, body: Dict) -> Tuple[int, Dict, str]:
    ok, err = _require_auth(ctx, headers, query)
    if not ok:
        return _json_response(401, ErrorResponse(err, 401).to_dict())
    if not ctx.session_manager:
        return _json_response(200, {"sessions": []})
    page = int((query.get("page") or ["1"])[0])
    sessions = ctx.session_manager.list_sessions()
    data = [{"id": s.id, "title": s.title,
             "status": s.status.value,
             "messages": len(s.messages)} for s in sessions]
    return _json_response(200, paginate(data, page).to_dict())


def get_session(ctx: APIContext, params: Dict, query: Dict,
                headers: Dict, body: Dict) -> Tuple[int, Dict, str]:
    ok, err = _require_auth(ctx, headers, query)
    if not ok:
        return _json_response(401, ErrorResponse(err, 401).to_dict())
    if not ctx.session_manager:
        return _json_response(404, ErrorResponse("no sessions", 404).to_dict())
    s = ctx.session_manager.get_session(params.get("id", ""))
    if not s:
        return _json_response(404,
                              ErrorResponse("session not found", 404).to_dict())
    return _json_response(200, s.to_dict())


def list_goals(ctx: APIContext, params: Dict, query: Dict,
               headers: Dict, body: Dict) -> Tuple[int, Dict, str]:
    ok, err = _require_auth(ctx, headers, query)
    if not ok:
        return _json_response(401, ErrorResponse(err, 401).to_dict())
    if not ctx.goal_manager:
        return _json_response(200, {"goals": []})
    goals = ctx.goal_manager.list_goals()
    data = [{"id": g.id, "title": g.title,
             "status": g.status.value,
             "progress": round(g.progress, 2)} for g in goals]
    return _json_response(200, {"goals": data})


def server_stats(ctx: APIContext, params: Dict, query: Dict,
                 headers: Dict, body: Dict) -> Tuple[int, Dict, str]:
    ok, err = _require_auth(ctx, headers, query)
    if not ok:
        return _json_response(401, ErrorResponse(err, 401).to_dict())
    return _json_response(200, {
        "uptime_seconds": round(ctx.uptime_seconds(), 1),
        "auth_tokens": len(ctx.auth.list_tokens()),
    })


def build_router() -> Router:
    """Build the full API router."""
    r = Router()
    r.get("/api/health", health)
    r.post("/api/chat", chat)
    r.get("/api/tools", list_tools)
    r.post("/api/tools/call", call_tool)
    r.post("/api/plan", create_plan)
    r.get("/api/sessions", list_sessions)
    r.get("/api/sessions/:id", get_session)
    r.get("/api/goals", list_goals)
    r.get("/api/stats", server_stats)
    return r


# OpenAPI-ish documentation
API_DOCS = {
    "openapi": "3.0.0",
    "info": {"title": "Haley Agent API", "version": "2.0.0"},
    "paths": {
        "/api/health": {"get": {"summary": "Health check (no auth)"}},
        "/api/chat": {"post": {"summary": "Agentic chat (bearer auth)"}},
        "/api/tools": {"get": {"summary": "List tools"}},
        "/api/tools/call": {"post": {"summary": "Execute a tool"}},
        "/api/plan": {"post": {"summary": "Create an execution plan"}},
        "/api/sessions": {"get": {"summary": "List sessions"}},
        "/api/sessions/:id": {"get": {"summary": "Get session"}},
        "/api/goals": {"get": {"summary": "List goals"}},
        "/api/stats": {"get": {"summary": "Server stats"}},
    },
}
