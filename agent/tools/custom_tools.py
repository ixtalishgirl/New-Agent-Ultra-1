"""Runtime-created tools — real tools the agent builds and uses itself.

Contract for the user: he never touches code. He asks ("make me a tool that
..."), the agent writes the implementation via `tool_create`, and the tool is
kept in `agent/tools/custom/<name>.py`, registered in the central ToolRegistry
IMMEDIATELY (usable in the same session through the normal Dispatcher path),
and listed in `agent/tools/custom/manifest.json`.

Persistence: Render's disk is ephemeral, so every created tool is also backed
up to the Supabase table `custom_tools` (created if missing). On startup, if
the local custom/ dir is empty but Supabase holds tools, they are restored.

Generated modules must define `run(args: dict) -> dict`; the file template
below wraps it into the standard handler shape (ok/output/parsed/error).
"""
from __future__ import annotations

import ast
import datetime
import importlib
import json
import sys
from pathlib import Path
from typing import Any

CUSTOM_DIR = Path(__file__).parent / "custom"
MANIFEST = CUSTOM_DIR / "manifest.json"

_SUPABASE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS custom_tools (
  name TEXT PRIMARY KEY,
  code TEXT NOT NULL,
  schema_json JSONB,
  description TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);
""".strip()


def sanitize_name(name: str) -> str:
    safe = "".join(c if (c.isalnum() or c == "_") else "_" for c in name.strip())
    safe = safe.strip("_") or "custom_tool"
    if safe[0].isdigit():
        safe = "t_" + safe
    return safe[:64]


def validate_code(code: str) -> str | None:
    """Return an error string if the code is invalid, else None."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"syntax error: {e}"
    funcs = [n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == "run"]
    if not funcs:
        return "code must define run(args: dict) -> dict"
    return None


def render_tool_module(name: str, description: str, schema: dict,
                       code: str) -> str:
    safe = sanitize_name(name)
    schema_json = json.dumps(schema or {"type": "object"}, indent=2)
    created = datetime.datetime.utcnow().isoformat() + "Z"
    return f'''"""Custom tool '{safe}' (v1.0.0) — generated {{created}} via tool_create.

Kept in agent/tools/custom/ and backed up to Supabase `custom_tools`.
Regenerate with tool_create; do not hand-edit.
"""
from typing import Any

# ==================== agent-supplied implementation ====================
{code}
# ========================================================================


class _R:
    def __init__(self, ok, output="", parsed=None, error=""):
        self.ok = ok
        self.output = output
        self.parsed = parsed or {{}}
        self.error = error


def handle(args: dict) -> _R:
    try:
        res = run(args or {{}})
        if isinstance(res, dict):
            return _R(ok=bool(res.get("ok", True)),
                      output=str(res.get("output", res.get("text", ""))),
                      parsed=res.get("parsed", {{}}),
                      error=str(res.get("error", "")))
        return _R(ok=True, output=str(res))
    except Exception as e:
        return _R(ok=False, error=f"{{type(e).__name__}}: {{e}}"[:500])


TOOL_DEFS = [
    ("{safe}", {description!r}, {schema_json}, "custom", 60),
]

HANDLERS = {{"{safe}": handle}}

__version__ = "1.0.0"
'''.replace("{created}", created)


def _read_manifest() -> list[dict]:
    try:
        return json.loads(MANIFEST.read_text())
    except Exception:
        return []


def _write_manifest(entries: list[dict]) -> None:
    CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(entries, indent=2))


def save_custom_tool(name: str, description: str, schema: dict,
                     code: str) -> tuple[str, Path]:
    """Write the tool module + manifest entry. Returns (safe_name, path)."""
    safe = sanitize_name(name)
    err = validate_code(code)
    if err:
        raise ValueError(err)
    CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
    path = CUSTOM_DIR / f"{safe}.py"
    path.write_text(render_tool_module(safe, description, schema, code))
    entries = [e for e in _read_manifest() if e.get("name") != safe]
    entries.append({
        "name": safe,
        "description": description,
        "version": "1.0.0",
        "created_at": datetime.datetime.utcnow().isoformat() + "Z",
    })
    _write_manifest(entries)
    return safe, path


def load_custom_module(safe: str) -> Any:
    """Import (or reload) a custom tool module."""
    mod_name = f"agent.tools.custom.{safe}"
    if mod_name in sys.modules:
        return importlib.reload(sys.modules[mod_name])
    return importlib.import_module(mod_name)


def list_custom_names() -> list[str]:
    return [e["name"] for e in _read_manifest()
            if (CUSTOM_DIR / f"{e['name']}.py").exists()]


def collect_custom_handlers() -> dict:
    """{tool_name: handler} for every custom tool on disk."""
    handlers: dict = {}
    for safe in list_custom_names():
        try:
            mod = load_custom_module(safe)
            mh = getattr(mod, "HANDLERS", None)
            if isinstance(mh, dict):
                handlers.update(mh)
        except Exception:
            continue
    return handlers


def register_all(registry) -> int:
    """Register every custom tool's TOOL_DEFS into the registry. Returns count."""
    added = 0
    from agent.tools.registry import ToolDef
    for safe in list_custom_names():
        try:
            mod = load_custom_module(safe)
        except Exception:
            continue
        for item in getattr(mod, "TOOL_DEFS", []) or []:
            try:
                name, desc, schema = item[0], item[1], item[2]
                profile = item[3] if len(item) > 3 else "custom"
                timeout = item[4] if len(item) > 4 else 60
            except Exception:
                continue
            if registry.get(name) is not None:
                continue
            registry.register(ToolDef(
                name=name, version="1.0.0", description=desc,
                input_schema=schema, permission_profile=profile,
                timeout=timeout))
            added += 1
    return added


# ---------------------------------------------------------------------------
# Supabase backup / restore (Render disk is ephemeral)
# ---------------------------------------------------------------------------

def _supabase() -> Any | None:
    try:
        from agent.tools import supabase_tools as st
        return st
    except Exception:
        return None


def backup_to_supabase(name: str, code: str, schema: dict,
                       description: str) -> tuple[bool, str]:
    """Upsert the tool into Supabase custom_tools. Best-effort: (ok, note)."""
    st = _supabase()
    if st is None:
        return False, "supabase module unavailable"
    if not (st._db_url() or st._rest_config()):
        return False, "Supabase not configured — local file only"
    schema_json = json.dumps(schema or {"type": "object"})
    # escape single quotes for the SQL literal
    def lit(v: str) -> str:
        return "'" + v.replace("'", "''") + "'"
    sql = (
        _SUPABASE_TABLE_SQL + ";\n"
        f"INSERT INTO custom_tools (name, code, schema_json, description) VALUES "
        f"({lit(name)}, {lit(code)}, {lit(schema_json)}::jsonb, {lit(description)}) "
        f"ON CONFLICT (name) DO UPDATE SET code=EXCLUDED.code, "
        f"schema_json=EXCLUDED.schema_json, description=EXCLUDED.description;"
    )
    try:
        res = st.supabase_query({"sql": sql})
    except Exception as e:
        return False, f"backup failed: {type(e).__name__}"
    if res.ok:
        return True, "backed up to Supabase custom_tools"
    return False, f"backup failed: {res.error[:200]}"


def restore_from_supabase() -> int:
    """If local custom/ is empty, restore tools from Supabase. Returns count."""
    existing = [p for p in CUSTOM_DIR.glob("*.py") if p.name != "__init__.py"]
    if existing:
        return 0
    st = _supabase()
    if st is None:
        return 0
    if not (st._db_url() or st._rest_config()):
        return 0
    try:
        res = st.supabase_query(
            {"sql": "SELECT name, code, schema_json, description FROM custom_tools;"})
    except Exception:
        return 0
    if not res.ok:
        return 0
    rows = res.parsed.get("rows", []) if isinstance(res.parsed, dict) else []
    count = 0
    for row in rows:
        try:
            name = str(row.get("name", "")).strip()
            code = str(row.get("code", ""))
            if not name or not code:
                continue
            schema = row.get("schema_json") or {"type": "object"}
            if isinstance(schema, str):
                schema = json.loads(schema)
            save_custom_tool(name, str(row.get("description", "")),
                             schema, code)
            count += 1
        except Exception:
            continue
    return count


def ensure_restored() -> int:
    """Restore from Firestore (primary), then Supabase, when local is empty.
    Never raises."""
    try:
        n = restore_from_firestore()
        if n:
            return n
    except Exception:
        pass
    try:
        return restore_from_supabase()
    except Exception:
        return 0


# Firestore backup / restore (primary — HTTPS, works from any host)
# ---------------------------------------------------------------------------

def _firestore() -> Any | None:
    try:
        from agent.tools import firestore_tools as ft
        return ft
    except Exception:
        return None


def backup_to_firestore(name: str, code: str, schema: dict,
                        description: str) -> tuple[bool, str]:
    """Upsert the tool into Firestore collection custom_tools.
    Best-effort: (ok, note)."""
    ft = _firestore()
    if ft is None:
        return False, "firestore module unavailable"
    try:
        res = ft.firestore_set({
            "collection": "custom_tools",
            "doc_id": name,
            "data": {"name": name, "code": code,
                     "schema": schema or {"type": "object"},
                     "description": description},
            "merge": True,
        })
    except Exception as e:
        return False, f"backup failed: {type(e).__name__}"
    if res.ok:
        return True, "backed up to Firestore custom_tools"
    return False, f"backup failed: {res.error[:200]}"


def restore_from_firestore() -> int:
    """If local custom/ is empty, restore tools from Firestore. Returns count."""
    existing = [p for p in CUSTOM_DIR.glob("*.py") if p.name != "__init__.py"]
    if existing:
        return 0
    ft = _firestore()
    if ft is None:
        return 0
    try:
        res = ft.firestore_list({"collection": "custom_tools", "limit": 500})
    except Exception:
        return 0
    if not res.ok:
        return 0
    rows = res.parsed.get("docs", []) if isinstance(res.parsed, dict) else []
    count = 0
    for row in rows:
        try:
            data = row.get("data", row)
            name = str(data.get("name", row.get("_id", ""))).strip()
            code = str(data.get("code", ""))
            if not name or not code:
                continue
            schema = data.get("schema") or {"type": "object"}
            save_custom_tool(name, str(data.get("description", "")),
                             schema, code)
            count += 1
        except Exception:
            continue
    return count


def backup_tool(name: str, code: str, schema: dict,
                description: str) -> tuple[bool, str]:
    """Best-effort cloud backup: Firestore first, Supabase second."""
    ok, note = backup_to_firestore(name, code, schema, description)
    if ok:
        return ok, note
    return backup_to_supabase(name, code, schema, description)
