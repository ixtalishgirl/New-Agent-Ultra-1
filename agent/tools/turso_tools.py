"""Turso (cloud SQLite) — the agent's primary memory/data store.

Why Turso: it speaks plain HTTPS (IPv4-safe, no IPv6/Postgres issues),
and it is real SQLite — CREATE TABLE and full SQL work over the HTTP
pipeline API. Perfect for agent memory, custom-tool backup, and any
structured data.

Auth: TURSO_URL + TURSO_TOKEN env vars (set on Render). The token is
NEVER logged, printed, or returned in outputs.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TursoResult:
    ok: bool
    output: str = ""
    parsed: dict = field(default_factory=dict)
    error: str = ""


def _redact(text: str) -> str:
    return re.sub(r"(?i)(bearer\s+[A-Za-z0-9._\-]{8})[A-Za-z0-9._\-]*",
                  r"\1***", text)


def _cfg() -> tuple[str, str]:
    url = os.environ.get("TURSO_URL", "").strip().rstrip("/")
    token = os.environ.get("TURSO_TOKEN", "").strip()
    if not url or not token:
        raise RuntimeError("TURSO_URL / TURSO_TOKEN env vars are not set")
    return url, token


def _to_arg(v: Any) -> dict:
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": repr(v)}
    if isinstance(v, (bytes, bytearray)):
        return {"type": "blob",
                "base64": base64.b64encode(bytes(v)).decode()}
    return {"type": "text", "value": str(v)}


def _from_val(col_type: str, v: Any) -> Any:
    # Turso returns values as {"type":..., "value":...} or base64 blobs.
    if isinstance(v, dict):
        t = v.get("type")
        if t == "null":
            return None
        if t == "integer":
            try:
                return int(v.get("value"))
            except Exception:
                return v.get("value")
        if t == "float":
            try:
                return float(v.get("value"))
            except Exception:
                return v.get("value")
        if t == "blob":
            return v.get("base64", "")
        return v.get("value")
    return v


def turso_query(args: dict) -> TursoResult:
    """Run SQL against Turso (SQLite over HTTPS).

    args: {sql: str, params?: [...]}. Supports CREATE TABLE, SELECT,
    INSERT, UPDATE, DELETE — the full SQLite dialect.
    """
    sql = (args.get("sql") or "").strip()
    if not sql:
        return TursoResult(ok=False, error="sql is required")
    if len(sql) > 20000:
        return TursoResult(ok=False, error="sql too long (max 20000 chars)")
    try:
        url, token = _cfg()
    except RuntimeError as e:
        return TursoResult(ok=False, error=str(e))
    params = args.get("params") or []
    body = json.dumps({
        "requests": [
            {"type": "execute",
             "stmt": {"sql": sql, "args": [_to_arg(p) for p in params]}},
            {"type": "close"},
        ]
    }).encode()
    req = urllib.request.Request(
        url + "/v2/pipeline", data=body, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode())
    except Exception as e:
        return TursoResult(ok=False, error=_redact(str(e))[:500])
    try:
        results = payload.get("results", [])
        first = results[0] if results else {}
        if first.get("type") == "error":
            err = first.get("error", {})
            return TursoResult(
                ok=False,
                error=f"Turso error: {err.get('message', err)}"[:500])
        res = first.get("response", {}).get("result", {})
        cols = [c.get("name") for c in res.get("cols", [])]
        rows = [[_from_val("", v) for v in row]
                for row in res.get("rows", [])]
        dicts = [dict(zip(cols, r)) for r in rows] if cols else []
        summary = (f"{len(dicts)} row(s)" if cols
                   else "ok (no rows returned)")
        return TursoResult(ok=True, output=summary,
                           parsed={"rows": dicts,
                                   "rowcount": len(dicts)})
    except Exception as e:
        return TursoResult(ok=False, error=_redact(str(e))[:500])


def _S(arg_type: str, required: bool, desc: str) -> dict:
    return {"type": arg_type, "required": required, "description": desc}


TOOL_DEFS: list[tuple] = [
    ("turso_query", "Run SQL against Turso cloud SQLite (CREATE TABLE works)",
     {"sql": _S("string", True, "SQL statement (SQLite dialect)"),
      "params": _S("array", False, "Positional parameters")},
     "data", 60),
]

HANDLERS: dict[str, Any] = {
    "turso_query": turso_query,
}
