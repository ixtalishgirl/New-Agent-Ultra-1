"""Memory & Automation — key/value memory, macros, schedules, workflows.

Memory backend: when ``SUPABASE_URL`` + ``SUPABASE_ANON_KEY`` are set, uses
``agent.memory.stores.ProjectMemory`` (Supabase with SQLite fallback);
otherwise a local append-only JSONL at ``agent/logs/local_memory.jsonl``.
Never fails: every memory op has the JSONL fallback.

Automation:
- macros: named lists of {tool, args} stored as JSON in agent/logs/macros/;
  recorded via macro_record/macro_append, executed via macro_replay.
- sched_add/sched_run_due: a persistent JSON schedule file
  (agent/logs/schedule.json). NOTE: the in-process agent/app/scheduler.py
  Scheduler is not persistent, so this module uses the JSON file instead.
  ``sched_run_due`` takes no executor: it advances due entries and RETURNS
  the due list so the dispatcher can execute them itself.
- workflow_run / branch_if / loop_repeat / parallel_run / retry_backoff /
  macro_replay call other tools through an executor injected via the
  module-level ``set_executor(fn)`` (fn(tool_name, args) -> result-like).
  With no executor bound they return ok=False "no executor bound".
- event_emit appends to agent/logs/events.jsonl and, best-effort, publishes
  to agent.app.event_bus when the event name matches a known EventType.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

DEFAULT_TIMEOUT = 60
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LOG_DIR = REPO_ROOT / "agent" / "logs"
MEMORY_JSONL = LOG_DIR / "local_memory.jsonl"
MEMORY_DB = REPO_ROOT / "agent" / "workspace" / "memory.db"
MACRO_DIR = LOG_DIR / "macros"
SCHEDULE_FILE = LOG_DIR / "schedule.json"
EVENTS_JSONL = LOG_DIR / "events.jsonl"


# ---------------------------------------------------------------------------
# Result type (local; decoupled from other tool modules by design)
# ---------------------------------------------------------------------------

@dataclass
class MemoryResult:
    """Structured result of a memory/automation operation."""
    ok: bool
    output: str = ""
    parsed: dict = field(default_factory=dict)
    error: str = ""


# ---------------------------------------------------------------------------
# Executor injection
# ---------------------------------------------------------------------------

_EXECUTOR: Optional[Callable[[str, dict], Any]] = None


def set_executor(fn: Callable[[str, dict], Any]) -> None:
    """Bind the dispatcher executor: fn(tool_name, args) -> result with .ok/.output/.parsed/.error."""
    global _EXECUTOR
    _EXECUTOR = fn


def _exec(tool: str, args: dict) -> Any:
    if _EXECUTOR is None:
        return None
    return _EXECUTOR(tool, args)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _result_from_exc(exc: Exception) -> MemoryResult:
    return MemoryResult(ok=False, error=f"{type(exc).__name__}: {exc}")


def _ns_key(namespace: str, key: str) -> str:
    return f"{namespace or 'default'}::{key}"


def _supabase_configured() -> bool:
    return bool(os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_ANON_KEY"))


def _project_memory():
    """Return ProjectMemory (Supabase+SQLite) or None on any failure."""
    try:
        from agent.memory.stores import ProjectMemory
        return ProjectMemory(db_path=MEMORY_DB)
    except Exception:
        return None


def _jsonl_append(entry: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(MEMORY_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _jsonl_all() -> list[dict]:
    if not MEMORY_JSONL.exists():
        return []
    out = []
    for line in MEMORY_JSONL.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _macro_path(name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError(f"Unsafe macro name: {name!r}")
    MACRO_DIR.mkdir(parents=True, exist_ok=True)
    return MACRO_DIR / f"{name}.json"


def _load_schedule() -> dict:
    if SCHEDULE_FILE.exists():
        try:
            return json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"entries": []}


def _save_schedule(data: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SCHEDULE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(SCHEDULE_FILE)


def _res_summary(res: Any) -> dict:
    return {"ok": bool(getattr(res, "ok", False)),
            "output": str(getattr(res, "output", ""))[:2000],
            "error": str(getattr(res, "error", ""))[:500],
            "parsed": getattr(res, "parsed", {}) or {}}


# ---------------------------------------------------------------------------
# Memory handlers
# ---------------------------------------------------------------------------

def _memory_save(args: dict) -> MemoryResult:
    try:
        namespace = str(args.get("namespace", "default"))
        key = str(args["key"])
        value = args["value"]
        value_str = value if isinstance(value, str) else json.dumps(value)
        nkey = _ns_key(namespace, key)
        backend = "jsonl"
        if _supabase_configured():
            pm = _project_memory()
            if pm is not None:
                pm.set(nkey, value_str)
                backend = "supabase/sqlite"
            else:
                _jsonl_append({"ts": time.time(), "namespace": namespace,
                               "key": key, "value": value_str})
        else:
            _jsonl_append({"ts": time.time(), "namespace": namespace,
                           "key": key, "value": value_str})
        return MemoryResult(
            ok=True, output=f"Saved [{namespace}] {key} via {backend}.",
            parsed={"namespace": namespace, "key": key, "backend": backend})
    except Exception as e:
        return _result_from_exc(e)


def _memory_get(args: dict) -> MemoryResult:
    try:
        namespace = str(args.get("namespace", "default"))
        key = str(args["key"])
        nkey = _ns_key(namespace, key)
        if _supabase_configured():
            pm = _project_memory()
            if pm is not None:
                val = pm.get(nkey)
                if val is not None:
                    return MemoryResult(ok=True, output=f"[{namespace}] {key} = {val[:500]}",
                                        parsed={"namespace": namespace, "key": key,
                                                "value": val, "backend": "supabase/sqlite"})
        latest = None
        for row in _jsonl_all():
            if row.get("namespace") == namespace and row.get("key") == key:
                latest = row.get("value")
        if latest is None:
            return MemoryResult(ok=False, error=f"No entry for [{namespace}] {key}")
        return MemoryResult(ok=True, output=f"[{namespace}] {key} = {str(latest)[:500]}",
                            parsed={"namespace": namespace, "key": key,
                                    "value": latest, "backend": "jsonl"})
    except Exception as e:
        return _result_from_exc(e)


def _memory_search(args: dict) -> MemoryResult:
    try:
        query = str(args["query"]).lower()
        namespace = args.get("namespace")
        hits: list[dict] = []
        if _supabase_configured():
            pm = _project_memory()
            if pm is not None:
                for k, v in (pm.all() or {}).items():
                    ns, _, bare = k.partition("::")
                    if namespace and ns != namespace:
                        continue
                    if query in k.lower() or query in str(v).lower():
                        hits.append({"namespace": ns or "default", "key": bare or k,
                                     "value": v, "backend": "supabase/sqlite"})
        seen: dict[tuple, dict] = {}
        for row in _jsonl_all():
            ns = row.get("namespace", "default")
            if namespace and ns != namespace:
                continue
            key = row.get("key", "")
            val = str(row.get("value", ""))
            if query in key.lower() or query in val.lower():
                seen[(ns, key)] = {"namespace": ns, "key": key, "value": val,
                                   "backend": "jsonl", "ts": row.get("ts")}
        hits.extend(seen.values())
        lines = [f"[{h['namespace']}] {h['key']}: {str(h['value'])[:120]}" for h in hits[:50]]
        return MemoryResult(
            ok=True,
            output=f"{len(hits)} hit(s) for '{args['query']}':\n" + "\n".join(lines),
            parsed={"query": args["query"], "count": len(hits), "hits": hits[:50]})
    except Exception as e:
        return _result_from_exc(e)


def _memory_delete(args: dict) -> MemoryResult:
    try:
        if not args.get("confirm", False):
            return MemoryResult(ok=False, error="Refused: memory_delete requires confirm=True.")
        namespace = str(args.get("namespace", "default"))
        key = str(args["key"])
        nkey = _ns_key(namespace, key)
        removed = 0
        backend = "jsonl"
        if _supabase_configured():
            pm = _project_memory()
            if pm is not None:
                backend = "supabase/sqlite"
                try:
                    con = sqlite3.connect(MEMORY_DB)
                    cur = con.execute("DELETE FROM project_memory WHERE key=?", (nkey,))
                    removed += cur.rowcount
                    con.commit()
                    con.close()
                except Exception:
                    pass
                try:
                    sb = getattr(pm, "sb", None)
                    if sb is not None and getattr(sb, "enabled", False):
                        sb._req("DELETE", "halye_project_memory",
                                None, f"?key=eq.{nkey}")
                except Exception:
                    pass
        rows = [r for r in _jsonl_all()
                if not (r.get("namespace") == namespace and r.get("key") == key)]
        dropped = len(_jsonl_all()) - len(rows)
        if dropped:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            MEMORY_JSONL.write_text(
                "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            removed += dropped
        return MemoryResult(
            ok=removed > 0,
            output=(f"Deleted [{namespace}] {key} ({removed} record(s), {backend})."
                    if removed else f"No entry for [{namespace}] {key}."),
            parsed={"namespace": namespace, "key": key, "removed": removed,
                    "backend": backend})
    except Exception as e:
        return _result_from_exc(e)


# ---------------------------------------------------------------------------
# Macro handlers
# ---------------------------------------------------------------------------

def _macro_record(args: dict) -> MemoryResult:
    try:
        name = str(args["name"])
        path = _macro_path(name)
        path.write_text(json.dumps({"name": name, "steps": [],
                                    "created_at": time.time()}, indent=2),
                        encoding="utf-8")
        return MemoryResult(ok=True, output=f"Macro '{name}' recording started.",
                            parsed={"name": name, "path": str(path)})
    except Exception as e:
        return _result_from_exc(e)


def _macro_append(args: dict) -> MemoryResult:
    try:
        name = str(args["name"])
        path = _macro_path(name)
        if not path.exists():
            return MemoryResult(ok=False, error=f"Macro '{name}' not found; call macro_record first.")
        macro = json.loads(path.read_text(encoding="utf-8"))
        macro.setdefault("steps", []).append(
            {"tool": str(args["tool"]), "args": dict(args.get("args") or {})})
        path.write_text(json.dumps(macro, indent=2), encoding="utf-8")
        return MemoryResult(
            ok=True,
            output=f"Appended {args['tool']} to macro '{name}' ({len(macro['steps'])} steps).",
            parsed={"name": name, "steps": len(macro["steps"])})
    except Exception as e:
        return _result_from_exc(e)


def _macro_replay(args: dict) -> MemoryResult:
    try:
        if _EXECUTOR is None:
            return MemoryResult(ok=False, error="no executor bound")
        name = str(args["name"])
        path = _macro_path(name)
        if not path.exists():
            return MemoryResult(ok=False, error=f"Macro '{name}' not found.")
        steps = json.loads(path.read_text(encoding="utf-8")).get("steps", [])
        results = []
        for i, step in enumerate(steps):
            res = _exec(step["tool"], step.get("args") or {})
            summary = _res_summary(res)
            summary["step"] = i
            summary["tool"] = step["tool"]
            results.append(summary)
            if not summary["ok"] and not args.get("continue_on_error", False):
                break
        all_ok = all(r["ok"] for r in results)
        return MemoryResult(
            ok=all_ok,
            output=f"Replayed macro '{name}': {sum(r['ok'] for r in results)}/{len(results)} ok.",
            parsed={"name": name, "results": results})
    except Exception as e:
        return _result_from_exc(e)


# ---------------------------------------------------------------------------
# Schedule handlers (persistent JSON file)
# ---------------------------------------------------------------------------

def _sched_add(args: dict) -> MemoryResult:
    try:
        name = str(args["name"])
        every_sec = float(args["every_sec"])
        if every_sec <= 0:
            return MemoryResult(ok=False, error="every_sec must be > 0.")
        sched = _load_schedule()
        entries = [e for e in sched.get("entries", []) if e.get("name") != name]
        entry = {"name": name, "every_sec": every_sec,
                 "tool": str(args["tool"]), "args": dict(args.get("args") or {}),
                 "next_run": time.time() + every_sec,
                 "created_at": time.time()}
        entries.append(entry)
        sched["entries"] = entries
        _save_schedule(sched)
        return MemoryResult(
            ok=True, output=f"Scheduled '{name}' every {every_sec}s -> {entry['tool']}.",
            parsed={"entry": entry})
    except Exception as e:
        return _result_from_exc(e)


def _sched_run_due(args: dict) -> MemoryResult:
    try:
        now = time.time()
        sched = _load_schedule()
        due = []
        for e in sched.get("entries", []):
            if e.get("next_run", 0) <= now:
                due.append({"name": e["name"], "tool": e["tool"],
                            "args": e.get("args") or {}})
                e["next_run"] = now + float(e.get("every_sec", 60))
                e["last_run"] = now
        if due:
            _save_schedule(sched)
        return MemoryResult(
            ok=True,
            output=(f"{len(due)} due entr(ies). The dispatcher must execute them itself: "
                    "this tool only advances the schedule and returns the due list."),
            parsed={"due": due, "count": len(due)})
    except Exception as e:
        return _result_from_exc(e)


# ---------------------------------------------------------------------------
# Workflow handlers (executor-driven)
# ---------------------------------------------------------------------------

def _workflow_run(args: dict) -> MemoryResult:
    try:
        if _EXECUTOR is None:
            return MemoryResult(ok=False, error="no executor bound")
        steps = args.get("steps") or []
        continue_on_error = bool(args.get("continue_on_error", False))
        results = []
        for i, step in enumerate(steps):
            res = _exec(step["tool"], step.get("args") or {})
            summary = _res_summary(res)
            summary["step"] = i
            summary["tool"] = step["tool"]
            results.append(summary)
            if not summary["ok"] and not continue_on_error:
                break
        all_ok = all(r["ok"] for r in results) if results else True
        return MemoryResult(
            ok=all_ok,
            output=f"Workflow: {sum(r['ok'] for r in results)}/{len(results)} steps ok.",
            parsed={"results": results})
    except Exception as e:
        return _result_from_exc(e)


def _branch_if(args: dict) -> MemoryResult:
    try:
        if _EXECUTOR is None:
            return MemoryResult(ok=False, error="no executor bound")
        cond = _exec(args["condition_tool"], args.get("condition_args") or {})
        truthy = bool(getattr(cond, "ok", False)) or bool(str(getattr(cond, "output", "")).strip())
        branch = "then" if truthy else "else"
        tool_key = f"{branch}_tool"
        args_key = f"{branch}_args"
        if args.get(tool_key) is None:
            return MemoryResult(ok=True, output=f"Condition {branch}; no {tool_key} given, nothing to run.",
                                parsed={"condition_ok": truthy, "branch": branch,
                                        "condition": _res_summary(cond)})
        res = _exec(args[tool_key], args.get(args_key) or {})
        summary = _res_summary(res)
        return MemoryResult(
            ok=summary["ok"],
            output=f"Condition {branch} ({args[tool_key]}): {summary['output'][:500]}",
            parsed={"condition_ok": truthy, "branch": branch,
                    "condition": _res_summary(cond), "result": summary},
            error=summary["error"])
    except Exception as e:
        return _result_from_exc(e)


def _loop_repeat(args: dict) -> MemoryResult:
    try:
        if _EXECUTOR is None:
            return MemoryResult(ok=False, error="no executor bound")
        times = int(args["times"])
        if times < 1 or times > 1000:
            return MemoryResult(ok=False, error="times must be 1..1000.")
        tool = args["tool"]
        base_args = dict(args.get("args") or {})
        results = []
        for i in range(times):
            call_args = dict(base_args)
            call_args["iteration"] = i
            res = _exec(tool, call_args)
            summary = _res_summary(res)
            summary["iteration"] = i
            results.append(summary)
            if not summary["ok"] and not args.get("continue_on_error", False):
                break
        return MemoryResult(
            ok=all(r["ok"] for r in results),
            output=f"loop_repeat {tool} x{times}: {sum(r['ok'] for r in results)}/{len(results)} ok.",
            parsed={"tool": tool, "results": results})
    except Exception as e:
        return _result_from_exc(e)


def _parallel_run(args: dict) -> MemoryResult:
    try:
        if _EXECUTOR is None:
            return MemoryResult(ok=False, error="no executor bound")
        calls = args.get("calls") or []
        if not calls:
            return MemoryResult(ok=False, error="calls is empty.")
        max_workers = min(max(1, int(args.get("max_workers", 4))), len(calls))

        def _one(call: dict) -> dict:
            res = _exec(call["tool"], call.get("args") or {})
            summary = _res_summary(res)
            summary["tool"] = call["tool"]
            return summary

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            results = list(pool.map(_one, calls))
        return MemoryResult(
            ok=all(r["ok"] for r in results),
            output=f"parallel_run: {sum(r['ok'] for r in results)}/{len(results)} ok.",
            parsed={"results": results})
    except Exception as e:
        return _result_from_exc(e)


def _retry_backoff(args: dict) -> MemoryResult:
    try:
        if _EXECUTOR is None:
            return MemoryResult(ok=False, error="no executor bound")
        max_retries = int(args.get("max_retries", 3))
        base_delay = float(args.get("base_delay", 1.0))
        tool = args["tool"]
        call_args = dict(args.get("args") or {})
        attempts = []
        for attempt in range(max_retries + 1):
            res = _exec(tool, call_args)
            summary = _res_summary(res)
            summary["attempt"] = attempt
            attempts.append(summary)
            if summary["ok"]:
                return MemoryResult(ok=True, output=f"{tool} succeeded on attempt {attempt}.",
                                    parsed={"tool": tool, "attempts": attempts})
            if attempt < max_retries:
                time.sleep(base_delay * (2 ** attempt))
        return MemoryResult(
            ok=False,
            output=f"{tool} failed after {max_retries + 1} attempts.",
            parsed={"tool": tool, "attempts": attempts},
            error=f"{tool} failed after {max_retries + 1} attempts: {attempts[-1]['error']}")
    except Exception as e:
        return _result_from_exc(e)


def _event_emit(args: dict) -> MemoryResult:
    try:
        event = str(args["event"])
        data = args.get("data") or {}
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        entry = {"ts": time.time(),
                 "iso": datetime.now(timezone.utc).isoformat(),
                 "event": event, "data": data}
        with open(EVENTS_JSONL, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        bus_result = "not attempted"
        try:
            from agent.app.event_bus import EventBus, EventType
            et = EventType(event)  # raises ValueError for unknown event names
            EventBus().publish(et, data=data if isinstance(data, dict) else {"data": data},
                               source="memory_tools.event_emit")
            bus_result = "published"
        except Exception as be:
            bus_result = f"skipped: {type(be).__name__}"
        return MemoryResult(
            ok=True, output=f"Event '{event}' logged ({bus_result}).",
            parsed={"event": event, "bus": bus_result})
    except Exception as e:
        return _result_from_exc(e)




_PROGRESS_FILE = LOG_DIR / "progress.json"


def _progress_report(args: dict) -> MemoryResult:
    """Create/update/query named task-progress entries (persisted JSON)."""
    try:
        action = str(args.get("action", "list")).lower()
        try:
            data = json.loads(_PROGRESS_FILE.read_text()) if _PROGRESS_FILE.exists() else {}
        except Exception:
            data = {}
        if action == "create" or action == "update":
            name = str(args.get("name", "")).strip()
            if not name:
                return MemoryResult(ok=False, error="name is required")
            entry = data.get(name, {"name": name, "history": []})
            if "status" in args:
                entry["status"] = str(args["status"])
            if "percent" in args:
                entry["percent"] = max(0, min(100, int(args["percent"])))
            if "note" in args:
                entry["note"] = str(args["note"])
            entry["updated_at"] = datetime.now(timezone.utc).isoformat()
            entry["history"].append({"t": entry["updated_at"],
                                    "status": entry.get("status"),
                                    "percent": entry.get("percent"),
                                    "note": entry.get("note")})
            entry["history"] = entry["history"][-50:]
            data[name] = entry
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            _PROGRESS_FILE.write_text(json.dumps(data, indent=2))
            return MemoryResult(ok=True,
                output=f"progress '{name}': {entry.get('status')} {entry.get('percent', '?')}%",
                parsed={"entry": entry})
        if action == "get":
            name = str(args.get("name", ""))
            entry = data.get(name)
            if entry is None:
                return MemoryResult(ok=False, error=f"no progress entry '{name}'")
            return MemoryResult(ok=True, output=json.dumps(entry, indent=2),
                                parsed={"entry": entry})
        if action == "delete":
            name = str(args.get("name", ""))
            if name in data:
                del data[name]
                _PROGRESS_FILE.write_text(json.dumps(data, indent=2))
            return MemoryResult(ok=True, output=f"deleted progress '{name}'",
                                parsed={"deleted": name})
        if action == "list":
            brief = {k: {"status": v.get("status"), "percent": v.get("percent"),
                         "updated_at": v.get("updated_at")}
                     for k, v in data.items()}
            return MemoryResult(ok=True,
                output=json.dumps(brief, indent=2) if brief else "(no progress entries)",
                parsed={"entries": brief, "count": len(brief)})
        return MemoryResult(ok=False, error="action must be create|update|get|list|delete")
    except Exception as e:
        return _result_from_exc(e)


def _error_detect(args: dict) -> MemoryResult:
    """Scan the audit log for recent errors; return structured findings."""
    try:
        hours = max(0.1, min(float(args.get("hours", 24)), 24 * 30))
        limit = max(10, min(int(args.get("limit", 200)), 5000))
        log_path = LOG_DIR / "audit.jsonl"
        findings = []
        if log_path.exists():
            cutoff = time.time() - hours * 3600
            for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts = rec.get("ts", 0)
                if ts and ts < cutoff:
                    continue
                event = str(rec.get("event", ""))
                details = rec.get("details", {}) or {}
                blob = (event + " " + json.dumps(details)).lower()
                is_err = ("error" in blob or "fail" in blob or "denied" in blob
                          or "exception" in blob or "timeout" in blob)
                if is_err:
                    findings.append({"ts": ts, "event": event,
                                     "actor": rec.get("actor"),
                                     "detail": str(details)[:300]})
        return MemoryResult(ok=True,
            output=(f"{len(findings)} error-ish audit record(s) in last {hours}h"
                    if findings else f"no errors in audit log (last {hours}h)"),
            parsed={"findings": findings, "count": len(findings), "hours": hours})
    except Exception as e:
        return _result_from_exc(e)


HANDLERS: dict = {
    "memory_save": _memory_save,
    "memory_search": _memory_search,
    "memory_get": _memory_get,
    "memory_delete": _memory_delete,
    "macro_record": _macro_record,
    "macro_replay": _macro_replay,
    "macro_append": _macro_append,
    "sched_add": _sched_add,
    "sched_run_due": _sched_run_due,
    "workflow_run": _workflow_run,
    "branch_if": _branch_if,
    "loop_repeat": _loop_repeat,
    "parallel_run": _parallel_run,
    "retry_backoff": _retry_backoff,
    "event_emit": _event_emit,
    "progress_report": _progress_report,
    "error_detect": _error_detect,

}


def _sch(t: str, req: bool, desc: str) -> dict:
    return {"type": t, "required": req, "description": desc}


TOOL_DEFS: list[tuple] = [
    ("memory_save", "Save a key/value in a namespace (Supabase if configured, else local JSONL)",
     {"key": _sch("string", True, "Key"),
      "value": _sch("string", True, "Value (string; objects are JSON-encoded)"),
      "namespace": _sch("string", False, "Namespace (default 'default')")},
     "computer", 30),
    ("memory_search", "Substring search over stored memory entries",
     {"query": _sch("string", True, "Search text"),
      "namespace": _sch("string", False, "Restrict to namespace")},
     "computer", 30),
    ("memory_get", "Fetch a single memory entry",
     {"key": _sch("string", True, "Key"),
      "namespace": _sch("string", False, "Namespace (default 'default')")},
     "computer", 30),
    ("memory_delete", "Delete a memory entry (requires confirm)",
     {"key": _sch("string", True, "Key"),
      "namespace": _sch("string", False, "Namespace (default 'default')"),
      "confirm": _sch("boolean", False, "Must be true")},
     "computer", 30),
    ("macro_record", "Start recording a named macro (list of tool calls)",
     {"name": _sch("string", True, "Macro name [A-Za-z0-9_.-]")},
     "computer", 20),
    ("macro_append", "Append a {tool, args} step to a macro",
     {"name": _sch("string", True, "Macro name"),
      "tool": _sch("string", True, "Tool name"),
      "args": _sch("object", False, "Args dict")},
     "computer", 20),
    ("macro_replay", "Replay a macro via the bound executor",
     {"name": _sch("string", True, "Macro name"),
      "continue_on_error": _sch("boolean", False, "Keep going after a failed step")},
     "computer", 300),
    ("sched_add", "Add/update a persistent interval schedule entry",
     {"name": _sch("string", True, "Entry name"),
      "every_sec": _sch("number", True, "Interval in seconds"),
      "tool": _sch("string", True, "Tool to run"),
      "args": _sch("object", False, "Args dict")},
     "computer", 20),
    ("sched_run_due", "Advance due schedule entries; RETURN the due list for the dispatcher to execute",
     {},
     "computer", 20),
    ("workflow_run", "Run steps sequentially; stop on first failure unless continue_on_error",
     {"steps": _sch("array", True, "[{tool, args}, ...]"),
      "continue_on_error": _sch("boolean", False, "Default false")},
     "computer", 600),
    ("branch_if", "Run condition tool; truthy ok/output -> then branch, else else branch",
     {"condition_tool": _sch("string", True, "Tool name"),
      "condition_args": _sch("object", False, "Args"),
      "then_tool": _sch("string", False, "Tool if truthy"),
      "then_args": _sch("object", False, "Args"),
      "else_tool": _sch("string", False, "Tool if falsy"),
      "else_args": _sch("object", False, "Args")},
     "computer", 120),
    ("loop_repeat", "Repeat a tool call N times (iteration injected into args)",
     {"tool": _sch("string", True, "Tool name"),
      "args": _sch("object", False, "Base args"),
      "times": _sch("integer", True, "Repeat count 1..1000"),
      "continue_on_error": _sch("boolean", False, "Default false")},
     "computer", 600),
    ("parallel_run", "Run tool calls concurrently via ThreadPoolExecutor",
     {"calls": _sch("array", True, "[{tool, args}, ...]"),
      "max_workers": _sch("integer", False, "Default 4")},
     "computer", 600),
    ("retry_backoff", "Retry a tool with exponential backoff",
     {"tool": _sch("string", True, "Tool name"),
      "args": _sch("object", False, "Args"),
      "max_retries": _sch("integer", False, "Default 3"),
      "base_delay": _sch("number", False, "Seconds, default 1.0")},
     "computer", 600),
    ("event_emit", "Log an event to events.jsonl (+ best-effort event-bus publish)",
     {"event": _sch("string", True, "Event name"),
      "data": _sch("object", False, "Payload")},
     "computer", 20),
    ("progress_report", "Create/update/query named task-progress entries (persisted)",
     {"action": _sch("string", False, "create|update|get|list|delete (default list)"),
      "name": _sch("string", False, "Entry name"),
      "status": _sch("string", False, "Status text"),
      "percent": _sch("integer", False, "0-100"),
      "note": _sch("string", False, "Note")},
     "computer", 30),
    ("error_detect", "Scan the audit log for recent errors; structured findings",
     {"hours": _sch("number", False, "Lookback hours (default 24)"),
      "limit": _sch("integer", False, "Max records to scan (default 200)")},
     "computer", 60),

]
