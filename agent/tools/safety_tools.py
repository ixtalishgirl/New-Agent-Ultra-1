"""Access & Safety Controls — permissions, audit, approvals, backups.

- Permission tools query ``agent.tools.permission_manager`` with a lazy
  import (reporting "permission manager not installed" if it is absent). ``perm_grant`` enforces
  the hard lock itself: the 5 denials can NEVER be granted here, regardless
  of whether the manager exists.
- ``audit_log`` writes through ``agent.security.audit_log.AuditLog``
  (secrets redacted by that class).
- ``approval_request`` records to ``agent.security.approval.ApprovalLog``
  AND appends a dashboard-visible entry to agent/logs/approvals.jsonl.
- ``confirm_prompt`` is non-interactive: it returns the confirmation
  question payload; the actual human confirmation happens in the dashboard.
- ``safe_mode`` is a master switch that re-asserts safe defaults; it can
  never disable the 5 hard denials.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import sys
import tarfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

DEFAULT_TIMEOUT = 60
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LOG_DIR = REPO_ROOT / "agent" / "logs"
_PROCESS_START = time.time()

# The 5 hard denials — locked. Never implemented, never granted, never weakened.
LOCKED_DENIALS = ("container_escape", "boot_recovery", "av_edr_disable",
                  "billing_fraud", "exfiltration")


# ---------------------------------------------------------------------------
# Result type (local; decoupled from other tool modules by design)
# ---------------------------------------------------------------------------

@dataclass
class SafetyResult:
    """Structured result of a safety/access-control operation."""
    ok: bool
    output: str = ""
    parsed: dict = field(default_factory=dict)
    error: str = ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _result_from_exc(exc: Exception) -> SafetyResult:
    return SafetyResult(ok=False, error=f"{type(exc).__name__}: {exc}")


def _load_permission_manager():
    """Lazy import to avoid import cycles. Returns (PermissionManager, error_str).

    The manager is built over the FULL central registry so every
    tool:<name> permission resolves.
    """
    try:
        from agent.tools.permission_manager import build_manager
        from agent.tools.registry import build_full_registry
        return build_manager(registry=build_full_registry()), ""
    except ImportError:
        return None, "permission manager not installed"


def _perm_summary(mgr: Any, name: str) -> dict:
    perm = mgr.get(name)
    if perm is None:
        return {"name": name, "known": False}
    return {"name": name, "known": True, "key": perm.key,
            "category": perm.category, "title": perm.title,
            "locked": perm.locked, "allowed": mgr.is_allowed(name),
            "default_enabled": perm.default_enabled}


def _audit() -> Any:
    from agent.security.audit_log import AuditLog
    return AuditLog(log_path=LOG_DIR / "audit.jsonl")


def _approval_log() -> Any:
    from agent.security.approval import ApprovalLog
    return ApprovalLog()


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _perm_check(args: dict) -> SafetyResult:
    try:
        name = args["name"]
        if name in LOCKED_DENIALS:
            return SafetyResult(ok=True, output=f"{name}: LOCKED denial (always denied).",
                                parsed={"name": name, "known": True, "locked": True,
                                        "allowed": False, "state": "locked_denial"})
        mgr, err = _load_permission_manager()
        if mgr is None:
            return SafetyResult(ok=False, error=err)
        info = _perm_summary(mgr, name)
        if not info.get("known"):
            return SafetyResult(ok=False, error=f"Unknown permission: {name}")
        state = "allowed" if info["allowed"] else "denied"
        return SafetyResult(ok=True, output=f"{name}: {state}"
                                            f"{' (locked)' if info['locked'] else ''}.",
                            parsed={**info, "state": state})
    except Exception as e:
        return _result_from_exc(e)


def _perm_grant(args: dict) -> SafetyResult:
    try:
        name = args["name"]
        if name in LOCKED_DENIALS:
            return SafetyResult(ok=False, error=f"locked denial: {name} can never be granted")
        mgr, err = _load_permission_manager()
        if mgr is None:
            return SafetyResult(ok=False, error=err)
        res = mgr.enable(name)
        if res.allowed:
            _audit().record("perm.grant", details={"name": name})
            _perm_change_log({"action": "log", "name": name, "change": "grant"})
        return SafetyResult(ok=res.allowed, output=res.reason,
                            parsed={"name": name, "granted": res.allowed},
                            error="" if res.allowed else res.reason)
    except Exception as e:
        return _result_from_exc(e)


def _perm_revoke(args: dict) -> SafetyResult:
    try:
        name = args["name"]
        mgr, err = _load_permission_manager()
        if mgr is None:
            return SafetyResult(ok=False, error=err)
        res = mgr.disable(name)
        if res.allowed:
            _audit().record("perm.revoke", details={"name": name})
            _perm_change_log({"action": "log", "name": name, "change": "revoke"})
        return SafetyResult(ok=res.allowed, output=res.reason,
                            parsed={"name": name, "revoked": res.allowed},
                            error="" if res.allowed else res.reason)
    except Exception as e:
        return _result_from_exc(e)


def _audit_log(args: dict) -> SafetyResult:
    try:
        event = args["event"]
        details = args.get("details") or {}
        if not isinstance(details, dict):
            details = {"detail": str(details)}
        _audit().record(event, actor="agent", details=details)
        return SafetyResult(ok=True, output=f"Audited event: {event}",
                            parsed={"event": event})
    except Exception as e:
        return _result_from_exc(e)


def _session_info(args: dict) -> SafetyResult:
    try:
        env_marker = "render" if os.environ.get("RENDER") else (
            "replit" if os.environ.get("REPL_ID") else "local")
        info = {
            "pid": os.getpid(),
            "uptime_sec": round(time.time() - _PROCESS_START, 1),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cwd": os.getcwd(),
            "env": env_marker,
        }
        return SafetyResult(
            ok=True,
            output=(f"pid={info['pid']} uptime={info['uptime_sec']}s "
                    f"python={info['python']} cwd={info['cwd']} env={info['env']}"),
            parsed=info)
    except Exception as e:
        return _result_from_exc(e)


def _rate_limit_status(args: dict) -> SafetyResult:
    try:
        bucket_file = LOG_DIR / "rate_limit.json"
        defaults = {"bucket": "tool-calls", "capacity": 120, "refill_per_sec": 2.0,
                    "tokens": 120, "configured": False}
        if bucket_file.exists():
            try:
                data = json.loads(bucket_file.read_text(encoding="utf-8"))
                data["configured"] = True
                return SafetyResult(ok=True, output=f"Rate limit: {data}",
                                    parsed=data)
            except (json.JSONDecodeError, OSError):
                pass
        return SafetyResult(ok=True,
                            output="No token-bucket file; reporting defaults (120 burst, 2/s refill).",
                            parsed=defaults)
    except Exception as e:
        return _result_from_exc(e)


def _approval_request(args: dict) -> SafetyResult:
    try:
        action = args["action"]
        detail = str(args.get("detail", ""))
        rec = _approval_log().auto_approve(action, detail)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        entry = {"ts": time.time(),
                 "iso": datetime.now(timezone.utc).isoformat(),
                 "id": rec.id, "action": action, "detail": detail,
                 "decision": rec.decision.value, "decided_by": rec.decided_by,
                 "status": "pending-dashboard"}
        with open(LOG_DIR / "approvals.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        return SafetyResult(
            ok=True,
            output=f"Approval request recorded ({rec.id}); visible in dashboard approvals.jsonl.",
            parsed=entry)
    except Exception as e:
        return _result_from_exc(e)


def _confirm_prompt(args: dict) -> SafetyResult:
    try:
        action = args["action"]
        detail = str(args.get("detail", ""))
        payload = {
            "question": f"Confirm: {action}?",
            "detail": detail,
            "options": ["confirm", "cancel"],
            "note": ("Non-interactive tool. The actual human confirmation happens "
                     "in the dashboard; this payload is what the dashboard should present."),
        }
        return SafetyResult(
            ok=True,
            output=f"CONFIRM REQUIRED: {action}\n{detail}\nAwaiting dashboard confirmation.",
            parsed=payload)
    except Exception as e:
        return _result_from_exc(e)


def _safe_mode(args: dict) -> SafetyResult:
    try:
        enabled = bool(args.get("enabled", True))
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        state = {"enabled": enabled, "ts": time.time(),
                 "iso": datetime.now(timezone.utc).isoformat(),
                 "locked_denials": list(LOCKED_DENIALS),
                 "note": ("Safe mode re-asserts defaults (everything ON except the 5 denials). "
                          "It can NEVER disable the 5 denials; they stay locked regardless.")}
        (LOG_DIR / "safe_mode.json").write_text(json.dumps(state, indent=2),
                                                encoding="utf-8")
        _audit().record("safe_mode", details={"enabled": enabled})
        return SafetyResult(
            ok=True,
            output=(f"Safe mode {'ENABLED' if enabled else 'DISABLED'}; "
                    f"5 denials remain locked: {', '.join(LOCKED_DENIALS)}."),
            parsed=state)
    except Exception as e:
        return _result_from_exc(e)


def _backup_create(args: dict) -> SafetyResult:
    try:
        paths = args["paths"]
        output = Path(args["output"])
        if isinstance(paths, str):
            paths = [paths]
        output.parent.mkdir(parents=True, exist_ok=True)
        added = []
        with tarfile.open(output, "w:gz") as tar:
            for p in paths:
                src = Path(p)
                if not src.exists():
                    continue
                tar.add(src, arcname=src.name if src.is_absolute() else str(src))
                added.append(str(p))
        size = output.stat().st_size
        return SafetyResult(
            ok=True, output=f"Backup {output} ({size} bytes, {len(added)} paths).",
            parsed={"output": str(output), "size_bytes": size, "paths": added})
    except Exception as e:
        return _result_from_exc(e)


def _restore_point(args: dict) -> SafetyResult:
    try:
        label = str(args["label"])
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in label) or "point"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        rp_dir = LOG_DIR / "restore_points"
        rp_dir.mkdir(parents=True, exist_ok=True)
        output = rp_dir / f"{stamp}_{safe}.tar.gz"
        paths = args.get("paths") or [str(REPO_ROOT / "agent")]
        res = _backup_create({"paths": paths, "output": str(output)})
        if not res.ok:
            return res
        index = rp_dir / "index.json"
        entries = []
        if index.exists():
            try:
                entries = json.loads(index.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                entries = []
        entries.append({"label": label, "file": str(output), "ts": time.time(),
                        "iso": datetime.now(timezone.utc).isoformat()})
        index.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        return SafetyResult(
            ok=True, output=f"Restore point '{label}' -> {output.name}",
            parsed={"label": label, "file": str(output)})
    except Exception as e:
        return _result_from_exc(e)


def _health_check(args: dict) -> SafetyResult:
    try:
        checks: dict[str, Any] = {}
        checks["python"] = platform.python_version()
        try:
            du = shutil.disk_usage(REPO_ROOT)
            checks["disk_free_gb"] = round(du.free / 1e9, 2)
            checks["disk_ok"] = du.free > 1_000_000_000
        except OSError as e:
            checks["disk_ok"] = False
            checks["disk_error"] = str(e)
        try:
            from agent.tools.registry import ToolRegistry  # noqa
            checks["registry_import"] = True
        except ImportError as e:
            checks["registry_import"] = False
            checks["registry_error"] = str(e)
        pm, pm_err = _load_permission_manager()
        checks["permission_manager"] = "present" if pm else f"missing ({pm_err})"
        ok = all([checks["registry_import"], checks.get("disk_ok", False)])
        return SafetyResult(
            ok=ok,
            output=("Health: " + ", ".join(f"{k}={v}" for k, v in checks.items())),
            parsed=checks)
    except Exception as e:
        return _result_from_exc(e)




# ---------------------------------------------------------------------------
# Executor injection (bound by Dispatcher so nested tool calls are gated)
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


_ALLOWLIST_FILE = LOG_DIR / "website_allowlist.yaml"
_FILE_RULES_FILE = LOG_DIR / "file_access_rules.json"
_PERM_CHANGES_FILE = LOG_DIR / "perm_changes.jsonl"


def _website_allowlist(args: dict) -> SafetyResult:
    """Manage the website allowlist (persisted YAML). Empty list = allow all (user's standing)."""
    try:
        import yaml  # type: ignore
        action = str(args.get("action", "get")).lower()
        try:
            data = yaml.safe_load(_ALLOWLIST_FILE.read_text()) or {} if _ALLOWLIST_FILE.exists() else {}
        except Exception:
            data = {}
        domains = list(dict.fromkeys(data.get("domains", []) or []))
        if action == "get":
            return SafetyResult(ok=True,
                output=("allowlist: " + ", ".join(domains)) if domains else "allowlist empty -> all websites allowed",
                parsed={"domains": domains, "mode": "allow_all" if not domains else "allowlist"})
        if action == "add":
            d = str(args.get("domain", "")).strip().lower()
            if not d:
                return SafetyResult(ok=False, error="domain is required")
            if d not in domains:
                domains.append(d)
            data["domains"] = domains
            _ALLOWLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
            _ALLOWLIST_FILE.write_text(yaml.safe_dump(data, default_flow_style=False))
            return SafetyResult(ok=True, output=f"allowlisted {d}",
                                parsed={"domains": domains})
        if action == "remove":
            d = str(args.get("domain", "")).strip().lower()
            domains = [x for x in domains if x != d]
            data["domains"] = domains
            _ALLOWLIST_FILE.write_text(yaml.safe_dump(data, default_flow_style=False))
            return SafetyResult(ok=True, output=f"removed {d} from allowlist",
                                parsed={"domains": domains})
        if action == "clear":
            if not args.get("confirm"):
                return SafetyResult(ok=False, error="clear needs confirm=true")
            _ALLOWLIST_FILE.write_text(yaml.safe_dump({"domains": []}, default_flow_style=False))
            return SafetyResult(ok=True, output="allowlist cleared -> all websites allowed",
                                parsed={"domains": []})
        return SafetyResult(ok=False, error="action must be get|add|remove|clear")
    except Exception as e:
        return _result_from_exc(e)


def _file_access_rules(args: dict) -> SafetyResult:
    """Manage file access rules (persisted JSON). Default: allow all paths."""
    try:
        action = str(args.get("action", "get")).lower()
        try:
            rules = json.loads(_FILE_RULES_FILE.read_text()) if _FILE_RULES_FILE.exists() else {}
        except Exception:
            rules = {}
        rules.setdefault("mode", "allow_all")
        rules.setdefault("allow_prefixes", [])
        rules.setdefault("deny_prefixes", [])
        if action == "get":
            return SafetyResult(ok=True, output=json.dumps(rules, indent=2),
                                parsed={"rules": rules})
        if action == "set":
            mode = str(args.get("mode", "allow_all")).lower()
            if mode not in ("allow_all", "allowlist", "denylist"):
                return SafetyResult(ok=False, error="mode must be allow_all|allowlist|denylist")
            rules["mode"] = mode
            if "allow_prefixes" in args:
                rules["allow_prefixes"] = [str(p) for p in (args["allow_prefixes"] or [])]
            if "deny_prefixes" in args:
                rules["deny_prefixes"] = [str(p) for p in (args["deny_prefixes"] or [])]
        elif action == "add_allow":
            p = str(args.get("path", ""))
            if p and p not in rules["allow_prefixes"]:
                rules["allow_prefixes"].append(p)
        elif action == "add_deny":
            p = str(args.get("path", ""))
            if p and p not in rules["deny_prefixes"]:
                rules["deny_prefixes"].append(p)
        elif action == "clear":
            if not args.get("confirm"):
                return SafetyResult(ok=False, error="clear needs confirm=true")
            rules = {"mode": "allow_all", "allow_prefixes": [], "deny_prefixes": []}
        else:
            return SafetyResult(ok=False, error="action must be get|set|add_allow|add_deny|clear")
        _FILE_RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
        _FILE_RULES_FILE.write_text(json.dumps(rules, indent=2))
        return SafetyResult(ok=True, output=f"file access rules updated (mode={rules['mode']})",
                            parsed={"rules": rules})
    except Exception as e:
        return _result_from_exc(e)


def _op_timeout(args: dict) -> SafetyResult:
    """Execute a registered tool with a hard timeout; report timeout instead of hanging."""
    try:
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as _TE
        tool = args.get("tool")
        if not tool:
            return SafetyResult(ok=False, error="tool is required")
        if _EXECUTOR is None:
            return SafetyResult(ok=False, error=
                "no executor bound (Dispatcher binds set_executor at runtime)")
        timeout = max(1.0, min(float(args.get("timeout_sec", 60)), 3600.0))
        targs = args.get("args") or {}
        with ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(_EXECUTOR, tool, dict(targs))
            try:
                r = fut.result(timeout=timeout)
            except _TE:
                return SafetyResult(ok=False,
                    output=f"TIMEOUT: {tool} exceeded {timeout}s (worker thread abandoned; "
                           f"CPython cannot kill threads, side effects may still complete)",
                    parsed={"tool": tool, "timeout_sec": timeout, "timed_out": True},
                    error=f"timed out after {timeout}s")
        ok = bool(getattr(r, "ok", False))
        return SafetyResult(ok=ok,
            output=str(getattr(r, "output", "") or getattr(r, "error", ""))[:2000],
            parsed={"tool": tool, "timed_out": False,
                    "inner": getattr(r, "parsed", {})},
            error="" if ok else str(getattr(r, "error", "tool failed"))[:300])
    except Exception as e:
        return _result_from_exc(e)


_CRED_PATTERNS = [
    ("aws_access_key", r"AKIA[0-9A-Z]{16}"),
    ("github_pat", r"ghp_[A-Za-z0-9]{36}"),
    ("github_oauth", r"gho_[A-Za-z0-9]{36}"),
    ("openai_key", r"sk-[A-Za-z0-9]{20,}"),
    ("private_key", r"-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----"),
    ("bearer_token", r"(?i)bearer\s+[A-Za-z0-9\-_.]{16,}"),
    ("password_assign", r"(?i)(password|passwd|pwd)\s*[:=]\s*['\"]?([^\s'\"]{4,})"),
    ("secret_assign", r"(?i)(api[_-]?key|apikey|secret|token)\s*[:=]\s*['\"]?([A-Za-z0-9\-_.]{12,})['\"]?"),
]


def _cred_protect(args: dict) -> SafetyResult:
    """Scan text for secret patterns; return redacted text + findings."""
    try:
        import re as _re
        text = str(args.get("text", ""))
        findings: list = []
        redacted = text
        for kind, pat in _CRED_PATTERNS:
            try:
                rx = _re.compile(pat)
            except _re.error:
                continue
            matches = rx.findall(redacted)
            n = len(matches)
            if n:
                findings.append({"kind": kind, "count": n})
                redacted = rx.sub(f"***REDACTED:{kind}***", redacted)
        return SafetyResult(ok=True,
            output=redacted[:4000] if redacted else "(empty)",
            parsed={"redacted": redacted, "findings": findings,
                    "secrets_found": sum(f["count"] for f in findings)})
    except Exception as e:
        return _result_from_exc(e)


def _perm_change_log(args: dict) -> SafetyResult:
    """Append/query the permission grant/revoke change history (JSONL)."""
    try:
        action = str(args.get("action", "tail")).lower()
        if action == "log":
            name = str(args.get("name", ""))
            change = str(args.get("change", ""))
            if not name or not change:
                return SafetyResult(ok=False, error="log needs name and change")
            entry = {"t": datetime.now(timezone.utc).isoformat(), "name": name,
                     "change": change, "actor": str(args.get("actor", "agent"))}
            _PERM_CHANGES_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(_PERM_CHANGES_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            return SafetyResult(ok=True, output=f"logged {change} on {name}",
                                parsed={"entry": entry})
        tail = max(1, min(int(args.get("tail", 50)), 1000))
        name_filter = str(args.get("name", ""))
        entries = []
        if _PERM_CHANGES_FILE.exists():
            for line in _PERM_CHANGES_FILE.read_text(encoding="utf-8").splitlines()[-tail:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if name_filter and rec.get("name") != name_filter:
                    continue
                entries.append(rec)
        if action == "query":
            return SafetyResult(ok=True,
                output=json.dumps(entries, indent=2) if entries else "(no matching entries)",
                parsed={"entries": entries, "count": len(entries)})
        if action == "tail":
            return SafetyResult(ok=True,
                output=json.dumps(entries, indent=2) if entries else "(no permission changes logged yet)",
                parsed={"entries": entries, "count": len(entries)})
        return SafetyResult(ok=False, error="action must be log|query|tail")
    except Exception as e:
        return _result_from_exc(e)


HANDLERS: dict = {
    "perm_check": _perm_check,
    "perm_grant": _perm_grant,
    "perm_revoke": _perm_revoke,
    "audit_log": _audit_log,
    "session_info": _session_info,
    "rate_limit_status": _rate_limit_status,
    "approval_request": _approval_request,
    "confirm_prompt": _confirm_prompt,
    "safe_mode": _safe_mode,
    "backup_create": _backup_create,
    "restore_point": _restore_point,
    "health_check": _health_check,
    "website_allowlist": _website_allowlist,
    "file_access_rules": _file_access_rules,
    "op_timeout": _op_timeout,
    "cred_protect": _cred_protect,
    "perm_change_log": _perm_change_log,

}


def _sch(t: str, req: bool, desc: str) -> dict:
    return {"type": t, "required": req, "description": desc}


TOOL_DEFS: list[tuple] = [
    ("perm_check", "Check a permission's state (denials always report locked)",
     {"name": _sch("string", True, "Permission name")},
     "computer", 20),
    ("perm_grant", "Grant a permission (REFUSES the 5 locked denials)",
     {"name": _sch("string", True, "Permission name")},
     "computer", 20),
    ("perm_revoke", "Revoke a permission",
     {"name": _sch("string", True, "Permission name")},
     "computer", 20),
    ("audit_log", "Record a security event (secrets auto-redacted)",
     {"event": _sch("string", True, "Event name"),
      "details": _sch("object", False, "Details dict")},
     "computer", 20),
    ("session_info", "Current session: pid, uptime, python, cwd, env",
     {},
     "computer", 20),
    ("rate_limit_status", "Token-bucket status (file if present, else defaults)",
     {},
     "computer", 20),
    ("approval_request", "Log an approval request (ApprovalLog + approvals.jsonl for dashboard)",
     {"action": _sch("string", True, "Action needing approval"),
      "detail": _sch("string", False, "Detail text")},
     "computer", 20),
    ("confirm_prompt", "Return a non-interactive confirmation question payload (dashboard confirms)",
     {"action": _sch("string", True, "Action to confirm"),
      "detail": _sch("string", False, "Detail text")},
     "computer", 20),
    ("safe_mode", "Master safe-mode switch; can never disable the 5 denials",
     {"enabled": _sch("boolean", False, "Default true")},
     "computer", 20),
    ("backup_create", "Create a tar.gz backup of workspace paths",
     {"paths": _sch("array", True, "Paths to include"),
      "output": _sch("string", True, "Output .tar.gz path")},
     "computer", 300),
    ("restore_point", "Labeled backup into agent/logs/restore_points/",
     {"label": _sch("string", True, "Label"),
      "paths": _sch("array", False, "Paths (default: agent/)")},
     "computer", 300),
    ("health_check", "python version, disk free, registry import, permission-manager presence",
     {},
     "computer", 30),
    ("website_allowlist", "Manage website allowlist (YAML); empty = allow all",
     {"action": _sch("string", False, "get|add|remove|clear (default get)"),
      "domain": _sch("string", False, "Domain for add/remove"),
      "confirm": _sch("boolean", False, "Confirm clear")},
     "computer", 30),
    ("file_access_rules", "Manage file access rules (JSON); default allow all paths",
     {"action": _sch("string", False, "get|set|add_allow|add_deny|clear (default get)"),
      "mode": _sch("string", False, "allow_all|allowlist|denylist"),
      "allow_prefixes": _sch("array", False, "Allowed path prefixes"),
      "deny_prefixes": _sch("array", False, "Denied path prefixes"),
      "path": _sch("string", False, "Path for add_allow/add_deny"),
      "confirm": _sch("boolean", False, "Confirm clear")},
     "computer", 30),
    ("op_timeout", "Execute a registered tool with a hard timeout",
     {"tool": _sch("string", True, "Tool name"),
      "args": _sch("object", False, "Tool args"),
      "timeout_sec": _sch("number", False, "Timeout seconds (default 60)")},
     "computer", 3700),
    ("cred_protect", "Scan text for secrets (keys/tokens/passwords); return redacted text + findings",
     {"text": _sch("string", True, "Text to scan")},
     "computer", 30),
    ("perm_change_log", "Append/query permission grant/revoke change history",
     {"action": _sch("string", False, "log|query|tail (default tail)"),
      "name": _sch("string", False, "Permission name"),
      "change": _sch("string", False, "grant|revoke for log"),
      "actor": _sch("string", False, "Who made the change"),
      "tail": _sch("integer", False, "Max entries (default 50)")},
     "computer", 30),

]
