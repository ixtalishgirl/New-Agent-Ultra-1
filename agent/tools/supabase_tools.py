"""Supabase / Postgres executor — real database access for the agent.

Route chain (first working route wins, then remembered):
  1. Direct Postgres via SUPABASE_DB_URL — full SQL incl. CREATE TABLE.
     Retries with a forced-IPv4 dial address (hosts without IPv6 egress),
     then the Supabase shared pooler regions.
  2. REST (PostgREST) via SUPABASE_URL + key — SELECT/INSERT/UPDATE/DELETE
     translated from simple SQL. DDL is impossible over REST and is
     reported honestly.

The user owns this database: writes and DDL are allowed (no read-only guard).
Credential values are NEVER written to logs or error output.
"""
from __future__ import annotations

import json
import os
import re
import socket
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


def _connect_with_ipv4_fallback(dsn: str):
    """Connect via psycopg2, retrying with a forced-IPv4 dial address.

    Some hosts (e.g. Render) have no IPv6 egress while the DB hostname
    resolves IPv6-first, producing "Network is unreachable". The retry
    resolves the hostname to IPv4 itself and passes it as ``hostaddr`` —
    libpq still uses the original ``host`` for TLS/SNI, only the dial
    address changes.
    """
    import psycopg2  # type: ignore
    try:
        return psycopg2.connect(dsn)
    except Exception as first_err:
        msg = str(first_err)
        if "Network is unreachable" not in msg and "could not connect" not in msg:
            raise
        parts = urllib.parse.urlparse(dsn)
        host = parts.hostname or ""
        port = parts.port or 5432
        if not host:
            raise
        infos = socket.getaddrinfo(host, port, family=socket.AF_INET,
                                   type=socket.SOCK_STREAM)
        if not infos:
            raise
        ipv4 = infos[0][4][0]
        return psycopg2.connect(dsn, hostaddr=ipv4)


# Regions to try for the Supabase shared pooler (Supavisor, IPv4). The pooler
# is the documented route for hosts without IPv6 egress.
_POOLER_REGIONS = ("eu-central-1", "eu-west-1", "us-east-1", "ap-southeast-1",
                   "us-west-1", "ap-northeast-1", "sa-east-1")


def _pooler_targets(dsn: str) -> list[dict]:
    """Build pooler connection kwargs from a direct-DB DSN."""
    parts = urllib.parse.urlparse(dsn)
    host_parts = (parts.hostname or "").split(".")
    # db.<ref>.supabase.co  ->  ref
    ref = host_parts[1] if len(host_parts) >= 4 and host_parts[0] == "db" else ""
    if not ref or not parts.password:
        return []
    return [dict(host=f"aws-0-{region}.pooler.supabase.com", port=6543,
                 user=f"postgres.{ref}", password=parts.password,
                 dbname=(parts.path.lstrip("/") or "postgres"),
                 sslmode="require", connect_timeout=10)
            for region in _POOLER_REGIONS]


# Substrings marking a *network-level* failure (worth retrying via pooler).
# Auth/config errors are raised immediately — the pooler would fail identically.
_NET_HINTS = ("could not connect", "network is unreachable",
              "no address associated", "timeout", "timed out",
              "connection refused", "name resolution")


def _pg_connect(dsn: str):
    """Postgres-protocol connection via every available route.

    Order: direct DSN (with IPv4 fallback) -> shared pooler regions.
    Raises the collected errors when nothing works.
    """
    import psycopg2  # type: ignore
    errors: list[str] = []
    try:
        return _connect_with_ipv4_fallback(dsn)
    except Exception as e:
        errors.append(f"direct: {e}")
        if not any(h in str(e).lower() for h in _NET_HINTS):
            raise  # auth/config error — pooler would fail the same way
    for kw in _pooler_targets(dsn):
        try:
            return psycopg2.connect(**kw)
        except Exception as e:
            errors.append(f"pooler@{kw['host'].split('.')[1]}: {e}")
    raise Exception("Postgres unreachable (" +
                    " | ".join(str(x)[:180] for x in errors) + ")")


def _run_psycopg2(sql: str, dsn: str) -> SupabaseResult:
    try:
        import psycopg2  # type: ignore
        import psycopg2.extras  # type: ignore
    except ImportError:
        return SupabaseResult(ok=False, error="psycopg2 not installed")
    try:
        conn = _pg_connect(dsn)
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


# ---------------------------------------------------------------------------
# REST fallback: translate simple SQL DML into PostgREST calls.
# Works over plain HTTPS (IPv4) when no Postgres-protocol route exists.
# DDL (CREATE/ALTER/DROP) is impossible over REST — reported honestly.
# ---------------------------------------------------------------------------

_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CMP_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(=|!=|<>|>=|<=|>|<)\s*(.+?)\s*$",
                     re.S)
_IS_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s+IS\s+(NOT\s+)?NULL\s*$", re.I)
_IN_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s+IN\s*\((.+)\)\s*$", re.I | re.S)
_PG_OPS = {"=": "eq", "!=": "neq", "<>": "neq", ">": "gt", "<": "lt",
           ">=": "gte", "<=": "lte"}


class _RestSQL(ValueError):
    """Raised when a statement can't be translated to PostgREST."""


def _clean_table(name: str) -> str:
    name = name.strip().strip('"')
    if name.lower().startswith("public."):
        name = name[7:]
    if not _TABLE_RE.match(name):
        raise _RestSQL(f"unsupported table name {name!r} for REST mode")
    return name


def _split_top(s: str, sep: str = ",") -> list[str]:
    """Split on sep, ignoring separators inside quotes or parens."""
    parts, depth, q, cur = [], 0, None, []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if q:
            cur.append(ch)
            if ch == q:
                if i + 1 < n and s[i + 1] == q:
                    cur.append(s[i + 1])
                    i += 1
                else:
                    q = None
        elif ch in ("'", '"'):
            q = ch
            cur.append(ch)
        elif ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth -= 1
            cur.append(ch)
        elif ch == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return parts


def _split_and(s: str) -> list[str]:
    """Split a WHERE clause on top-level AND (quote/paren aware)."""
    parts, depth, q, cur = [], 0, None, []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if q:
            cur.append(ch)
            if ch == q:
                if i + 1 < n and s[i + 1] == q:
                    cur.append(s[i + 1])
                    i += 1
                else:
                    q = None
            i += 1
            continue
        if ch in ("'", '"'):
            q = ch
            cur.append(ch)
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if depth == 0 and s[i:i + 3].upper() == "AND":
            before = s[i - 1] if i > 0 else " "
            after = s[i + 3] if i + 3 < n else " "
            if not (before.isalnum() or before == "_") and \
               not (after.isalnum() or after == "_"):
                parts.append("".join(cur))
                cur = []
                i += 3
                continue
        cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return [p for p in (x.strip() for x in parts) if p]


def _parse_value(tok: str):
    tok = tok.strip()
    up = tok.upper()
    if up == "NULL":
        return None
    if up == "TRUE":
        return True
    if up == "FALSE":
        return False
    if len(tok) >= 2 and tok[0] == "'" and tok[-1] == "'":
        return tok[1:-1].replace("''", "'")
    if len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"':
        return tok[1:-1]
    try:
        return int(tok)
    except ValueError:
        pass
    try:
        return float(tok)
    except ValueError:
        pass
    return tok


def _pg_val(v) -> str:
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    return str(v)


def _where_to_params(where: str | None) -> dict:
    params: dict[str, str] = {}
    if not where or not where.strip():
        return params
    for cond in _split_and(where):
        m = _IS_RE.match(cond)
        if m:
            params[m.group(1)] = "not.is.null" if m.group(2) else "is.null"
            continue
        m = _IN_RE.match(cond)
        if m:
            vals = ",".join(_pg_val(_parse_value(v))
                            for v in _split_top(m.group(2)))
            params[m.group(1)] = f"in.({vals})"
            continue
        m = _CMP_RE.match(cond)
        if m:
            col, op, raw = m.group(1), m.group(2), m.group(3)
            params[col] = f"{_PG_OPS[op]}.{_pg_val(_parse_value(raw))}"
            continue
        raise _RestSQL(
            f"unsupported WHERE condition {cond!r} — REST mode supports AND "
            "of simple comparisons (=, !=, >, <, >=, <=, IN, IS NULL)")
    return params


def _top_paren_groups(s: str) -> list[str]:
    """Extract top-level (...) groups, e.g. the tuples of a VALUES list."""
    groups, depth, q, cur = [], 0, None, []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if q:
            cur.append(ch)
            if ch == q:
                if i + 1 < n and s[i + 1] == q:  # '' escape: consume both
                    cur.append(s[i + 1])
                    i += 1
                else:
                    q = None
        elif ch in ("'", '"'):
            q = ch
            cur.append(ch)
        elif ch == "(":
            if depth == 0:
                cur = []
            else:
                cur.append(ch)
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                groups.append("".join(cur))
                cur = []
            else:
                cur.append(ch)
        elif depth > 0:
            cur.append(ch)
        i += 1
    return groups


def _rest_call(url: str, key: str, method: str, path: str,
               params: dict | None = None, body=None) -> tuple[list, int]:
    import urllib.request
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"apikey": key, "Authorization": f"Bearer {key}",
               "Content-Type": "application/json",
               "Prefer": "return=representation"}
    req = urllib.request.Request(url + path + qs, data=data, method=method,
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode()
        status = resp.status
    rows = json.loads(raw) if raw.strip() else []
    if isinstance(rows, dict):
        rows = [rows]
    return rows, status


def _translate_rest(sql: str):
    """Return (method, path, params, body, describe) for simple DML."""
    s = sql.strip().rstrip(";").strip()
    first = s.split(None, 1)[0].upper() if s else ""
    if first in ("CREATE", "ALTER", "DROP", "TRUNCATE"):
        raise _RestSQL(
            "DDL (CREATE TABLE etc.) is impossible over the REST API and no "
            "direct Postgres route is reachable from this host "
            "(DB is IPv6-only; enable connection pooling or IPv4 in the "
            "Supabase dashboard for full SQL).")
    if first == "SELECT":
        m = re.match(r"(?is)^\s*select\s+(?P<cols>.+?)\s+from\s+(?P<table>\S+)"
                     r"(?P<rest>.*)$", s)
        if not m:
            raise _RestSQL("could not parse SELECT")
        table = _clean_table(m.group("table"))
        rest = m.group("rest") or ""
        cols = m.group("cols").strip()
        params: dict[str, str] = {}
        params["select"] = "*" if cols == "*" else ",".join(
            c.strip().strip('"') for c in _split_top(cols))
        wm = re.search(r"(?is)\bwhere\b(.*?)(?=\border\s+by\b|\blimit\b|"
                       r"\boffset\b|$)", rest)
        params.update(_where_to_params(wm.group(1) if wm else None))
        om = re.search(r"(?is)\border\s+by\s+([A-Za-z_][A-Za-z0-9_]*)"
                       r"(\s+(asc|desc))?", rest)
        if om:
            params["order"] = om.group(1) + (".desc" if (om.group(3) or "")
                                             .lower() == "desc" else "")
        lm = re.search(r"(?is)\blimit\s+(\d+)", rest)
        if lm:
            params["limit"] = lm.group(1)
        fm = re.search(r"(?is)\boffset\s+(\d+)", rest)
        if fm:
            params["offset"] = fm.group(1)
        return "GET", f"/rest/v1/{table}", params, None, "SELECT"
    if first == "INSERT":
        m = re.match(r"(?is)^\s*insert\s+into\s+(?P<table>\S+)\s*"
                     r"\((?P<cols>[^)]*)\)\s*values\s*(?P<vals>.+)$", s)
        if not m:
            raise _RestSQL("REST INSERT needs: INSERT INTO t (a, b) VALUES (..)")
        table = _clean_table(m.group("table"))
        cols = [c.strip().strip('"') for c in _split_top(m.group("cols"))]
        rows = []
        for grp in _top_paren_groups(m.group("vals")):
            vals = [_parse_value(v) for v in _split_top(grp)]
            if len(vals) != len(cols):
                raise _RestSQL("VALUES count does not match column count")
            rows.append(dict(zip(cols, vals)))
        if not rows:
            raise _RestSQL("no VALUES rows found")
        return "POST", f"/rest/v1/{table}", None, rows, "INSERT"
    if first == "UPDATE":
        m = re.match(r"(?is)^\s*update\s+(?P<table>\S+)\s+set\s+(?P<sets>.+?)"
                     r"(?:\bwhere\b(?P<where>.+))?$", s)
        if not m:
            raise _RestSQL("could not parse UPDATE")
        table = _clean_table(m.group("table"))
        if not m.group("where"):
            raise _RestSQL("REST UPDATE requires a WHERE clause (safety)")
        body = {}
        for piece in _split_top(m.group("sets")):
            cm = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)\s*$",
                          piece, re.S)
            if not cm:
                raise _RestSQL(f"could not parse SET item {piece!r}")
            body[cm.group(1)] = _parse_value(cm.group(2))
        params = _where_to_params(m.group("where"))
        return "PATCH", f"/rest/v1/{table}", params, body, "UPDATE"
    if first == "DELETE":
        m = re.match(r"(?is)^\s*delete\s+from\s+(?P<table>\S+)"
                     r"(?:\s+where\s+(?P<where>.+))?$", s)
        if not m:
            raise _RestSQL("could not parse DELETE")
        table = _clean_table(m.group("table"))
        if not m.group("where"):
            raise _RestSQL("REST DELETE requires a WHERE clause (safety)")
        params = _where_to_params(m.group("where"))
        return "DELETE", f"/rest/v1/{table}", params, None, "DELETE"
    raise _RestSQL(f"statement type {first!r} not supported over REST")


def _run_rest(sql: str, url: str, key: str) -> SupabaseResult:
    """REST fallback: SELECT/INSERT/UPDATE/DELETE via PostgREST.

    DDL over REST is impossible — reported honestly with the reason.
    """
    try:
        method, path, params, body, kind = _translate_rest(sql)
        rows, status = _rest_call(url, key, method, path, params, body)
        n = len(rows)
        if kind in ("INSERT", "UPDATE", "DELETE"):
            return SupabaseResult(
                ok=True,
                output=f"{kind} ok (REST, HTTP {status}); {n} row(s) returned",
                parsed={"rows": rows, "rowcount": n})
        return SupabaseResult(ok=True,
                              output=f"{n} row(s) returned (REST)",
                              parsed={"rows": rows})
    except _RestSQL as e:
        return SupabaseResult(ok=False, error=str(e))
    except Exception as e:
        return SupabaseResult(ok=False, error=_redact(str(e))[:800])


# Remembers which route worked last so repeat queries skip dead routes.
_ROUTE: dict = {"mode": None}  # "pg" | "rest" | None


def supabase_query(args: dict) -> SupabaseResult:
    """Run SQL against the user's Supabase Postgres. SELECT, INSERT, UPDATE,
    DELETE, CREATE TABLE, etc. are all allowed (user owns the DB).

    Route chain: direct Postgres (direct -> IPv4-forced -> pooler regions)
    first for full SQL incl. DDL; REST (PostgREST) fallback for
    SELECT/INSERT/UPDATE/DELETE when no Postgres route is reachable.
    """
    sql = str(args.get("sql", "")).strip()
    if not sql:
        return SupabaseResult(ok=False, error="sql is required")
    # Note: psycopg2's execute() accepts a single statement; multi-statement
    # strings are rejected by the driver itself.
    dsn = _db_url()
    rest = _rest_config()
    if not dsn and not rest:
        return SupabaseResult(
            ok=False,
            error="Supabase not configured: set SUPABASE_DB_URL (preferred) or "
                  "SUPABASE_URL + SUPABASE_KEY env vars.")
    ordered: list[str] = []
    if _ROUTE["mode"] in ("pg", "rest"):
        ordered.append(_ROUTE["mode"])
    if dsn and "pg" not in ordered:
        ordered.append("pg")
    if rest and "rest" not in ordered:
        ordered.append("rest")
    last_err = ""
    for mode in ordered:
        res = _run_psycopg2(sql, dsn) if mode == "pg" else _run_rest(sql, *rest)
        if res.ok:
            _ROUTE["mode"] = mode
            res.parsed["route"] = mode
            return res
        last_err = res.error
    return SupabaseResult(ok=False, error=last_err or "all routes failed")


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
