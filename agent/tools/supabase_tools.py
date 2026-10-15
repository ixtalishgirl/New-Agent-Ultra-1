"""Supabase / Postgres executor — real database access for the agent.

Uses SUPABASE_DB_URL (direct Postgres, preferred — full SQL incl. CREATE TABLE,
INSERT, UPDATE, DELETE) via psycopg2. Falls back to the Supabase REST client
(SUPABASE_URL + SUPABASE_KEY / SUPABASE_ANON_KEY) for SELECT-style reads when
no direct DB URL is configured.

The user owns this database: writes and DDL are allowed (no read-only guard).
Credential values are NEVER written to logs or error output.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class SupabaseResult:
    ok: bool
    output: str = ""
    parsed: dict = field(default_factory=dict)
    error: str = ""


def _redact(text: str, secret: str = "") -> str:
    """Strip anything that looks like a credential from text."""
    if not text:
        return text
    if secret:
        text = text.replace(secret, "***")
    # redact password inside postgres URLs: postgresql://user:PASS@host
    text = re.sub(r"(://[^:/\s]+:)[^@\s]+(@)", r"\1***\2", text)
    # redact common secret assignments
    text = re.sub(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?\S+",
                  r"\1=***", text)
    return text


def _dsn_password(dsn: str) -> str:
    try:
        return urllib.parse.urlparse(dsn).password or ""
    except Exception:
        return ""


def _db_url() -> Optional[str]:
    return os.environ.get("SUPABASE_DB_URL") or None


def _rest_config() -> Optional[tuple[str, str]]:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_ANON_KEY")
    if url and key:
        return url.rstrip("/"), key
    return None


def _run_psycopg2(sql: str, dsn: str) -> SupabaseResult:
    try:
        import psycopg2  # type: ignore
        import psycopg2.extras  # type: ignore
    except ImportError:
        return SupabaseResult(ok=False, error="psycopg2 not installed")
    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql)
                rows: list[dict] = []
                try:
                    fetched = cur.fetchall()
                    rows = [dict(r) for r in fetched]
                except Exception:
                    pass  # DDL / INSERT without RETURNING -> no rows
                return SupabaseResult(
                    ok=True,
                    output=f"{cur.rowcount} row(s) affected; "
                           f"{len(rows)} row(s) returned",
                    parsed={"rows": rows, "rowcount": cur.rowcount},
                )
        finally:
            conn.close()
    except Exception as e:
        return SupabaseResult(ok=False,
                              error=_redact(str(e), _dsn_password(dsn))[:800])


def _run_rest(sql: str, url: str, key: str) -> SupabaseResult:
    """REST fallback: only supports simple SELECT via PostgREST rpc or table
    reads. Raw SQL is NOT supported over REST — report honestly."""
    stripped = sql.strip().rstrip(";")
    if not re.match(r"(?is)^\s*select\b", stripped):
        return SupabaseResult(
            ok=False,
            error="REST mode supports SELECT only; set SUPABASE_DB_URL for "
                  "full SQL (writes, CREATE TABLE).")
    try:
        import urllib.request
        # Try the pg-meta query endpoint if available, else honest failure.
        endpoint = f"{url}/rest/v1/rpc/exec_sql"
        payload = json.dumps({"query": stripped}).encode()
        req = urllib.request.Request(
            endpoint, data=payload, method="POST",
            headers={"apikey": key, "Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        rows = data if isinstance(data, list) else [data]
        return SupabaseResult(ok=True,
                              output=f"{len(rows)} row(s) returned (REST)",
                              parsed={"rows": rows})
    except Exception as e:
        return SupabaseResult(ok=False, error=_redact(str(e))[:800])


def supabase_query(args: dict) -> SupabaseResult:
    """Run SQL against the user's Supabase Postgres. SELECT, INSERT, UPDATE,
    DELETE, CREATE TABLE, etc. are all allowed (user owns the DB)."""
    sql = str(args.get("sql", "")).strip()
    if not sql:
        return SupabaseResult(ok=False, error="sql is required")
    # Note: psycopg2's execute() accepts a single statement; multi-statement
    # strings are rejected by the driver itself.
    dsn = _db_url()
    if dsn:
        return _run_psycopg2(sql, dsn)
    rest = _rest_config()
    if rest:
        return _run_rest(sql, *rest)
    return SupabaseResult(
        ok=False,
        error="Supabase not configured: set SUPABASE_DB_URL (preferred) or "
              "SUPABASE_URL + SUPABASE_KEY env vars.")


def _S(arg_type: str, required: bool, desc: str) -> dict:
    return {"type": arg_type, "required": required, "description": desc}


TOOL_DEFS: list[tuple] = [
    ("supabase_query", "Run SQL on the user's Supabase Postgres (SELECT, INSERT, UPDATE, DELETE, CREATE TABLE...)",
     {"sql": _S("string", True, "SQL statement to execute")},
     "data", 60),
]

HANDLERS: dict[str, Any] = {
    "supabase_query": supabase_query,
}
