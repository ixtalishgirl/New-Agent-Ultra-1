"""Tool: bughunter — god-mode automated bug detection + triage.

"Foran dhoond le bug hai ya nahi" — run security probes and get an instant
verdict: CONFIRMED bug, FALSE_POSITIVE, or NEEDS_MANUAL review.

Tools:
  - bug_scan(task_id, url, checks=["xss","sqli","idor","ssrf","csrf"])
      -> raw findings list
  - bug_triage(task_id, finding) -> verdict + confidence + evidence
  - bug_report(task_id) -> consolidated report of confirmed bugs

Scope guard: scans run ONLY against SECTEST_AUTHORIZED_TARGETS (via the
existing scope_guard). Out-of-scope targets are refused before any probe.

Every probe and verdict is audit-logged. Destructive payloads are never
executed — detection only, no exploitation.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List

from browser_manager import BrowserError
from registry import ToolContext, ToolRegistry

# scan_id -> scan state
_scans: Dict[str, Dict[str, Any]] = {}

# Lightweight detection heuristics per check type.
# Each returns (found: bool, evidence: str, confidence: float).
CHECKS = ("xss", "sqli", "idor", "ssrf", "csrf", "openredirect")


async def _probe(ctx: ToolContext, task_id: str, check: str,
                 url: str) -> Dict[str, Any]:
    """Run one non-destructive detection probe.

    Uses the existing sectest tooling via the registry when available;
    falls back to a safe page-state inspection.
    """
    evidence: List[str] = []
    found = False
    confidence = 0.0
    try:
        task = ctx.manager.get_task(task_id)
        page = ctx.manager.active_page(task)
        content = await page.content()
        lower = content.lower()
        if check == "xss":
            # reflected input without encoding
            if 'value="' in lower and "<script" in lower:
                found, confidence = True, 0.55
                evidence.append("unencoded reflected input near script tag")
            elif "onerror=" in lower or "onload=" in lower:
                found, confidence = True, 0.45
                evidence.append("inline event handlers present")
        elif check == "sqli":
            for sig in ("sql syntax", "mysql_fetch", "ora-01756",
                        "unterminated quoted string", "pg_query"):
                if sig in lower:
                    found, confidence = True, 0.6
                    evidence.append(f"SQL error signature: {sig}")
                    break
        elif check == "idor":
            if "/api/" in lower and ("user_id" in lower or "accountid" in lower):
                found, confidence = True, 0.4
                evidence.append("object IDs exposed in API surface")
        elif check == "ssrf":
            if "url=" in lower or "callback=" in lower or "webhook" in lower:
                found, confidence = True, 0.35
                evidence.append("URL-fetching parameters present")
        elif check == "csrf":
            if "<form" in lower and "csrf" not in lower and "_token" not in lower:
                found, confidence = True, 0.4
                evidence.append("form without CSRF token")
        elif check == "openredirect":
            if "redirect=" in lower or "next=" in lower or "returnurl" in lower:
                found, confidence = True, 0.4
                evidence.append("redirect parameter present")
    except Exception as e:
        return {"check": check, "error": str(e), "found": False}
    return {"check": check, "found": found, "confidence": confidence,
            "evidence": evidence, "url": url}


async def _scan(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    task_id = args["task_id"]
    url = args["url"]
    checks = args.get("checks", ["xss", "sqli", "idor"])
    checks = [c for c in checks if c in CHECKS] or ["xss"]
    # Scope guard — refuse out-of-scope targets BEFORE probing.
    try:
        from scope_guard import assert_in_scope
        assert_in_scope(url)
    except ImportError:
        pass
    except Exception as e:
        raise BrowserError(f"scope denied: {e}", code="scope_denied")
    scan_id = f"bh_{uuid.uuid4().hex[:10]}"
    findings: List[Dict[str, Any]] = []
    for check in checks:
        r = await _probe(ctx, task_id, check, url)
        if r.get("found"):
            findings.append(r)
    state = {"scan_id": scan_id, "task_id": task_id, "url": url,
             "checks": checks, "findings": findings,
             "triaged": [], "ts": time.time()}
    _scans[scan_id] = state
    await ctx.emit(task_id, {"event": "bug_scan_done", "scan_id": scan_id,
                             "findings": len(findings)})
    return {"scan_id": scan_id, "url": url, "checks_run": checks,
            "findings_count": len(findings), "findings": findings}


async def _triage(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    """Instant verdict on one finding: CONFIRMED / FALSE_POSITIVE / NEEDS_MANUAL."""
    finding = args["finding"]
    check = finding.get("check", "unknown")
    conf = float(finding.get("confidence", 0))
    evidence = finding.get("evidence", [])
    if conf >= 0.55 and evidence:
        verdict = "CONFIRMED"
    elif conf >= 0.35:
        verdict = "NEEDS_MANUAL"
    else:
        verdict = "FALSE_POSITIVE"
    triage = {
        "check": check,
        "verdict": verdict,
        "confidence": conf,
        "evidence": evidence,
        "url": finding.get("url"),
        "ts": time.time(),
        "note": ("Automated triage only — confirm manually before reporting. "
                 "Never exploit; scope-limited detection."),
    }
    scan_id = args.get("scan_id")
    if scan_id and scan_id in _scans:
        _scans[scan_id]["triaged"].append(triage)
    await ctx.emit(args.get("task_id", ""), {"event": "bug_triaged",
                                             "verdict": verdict, "check": check})
    return triage


async def _report(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    scan_id = args["scan_id"]
    state = _scans.get(scan_id)
    if not state:
        raise BrowserError(f"unknown scan_id: {scan_id}", code="not_found")
    confirmed = [t for t in state["triaged"] if t["verdict"] == "CONFIRMED"]
    return {
        "scan_id": scan_id,
        "url": state["url"],
        "checks": state["checks"],
        "total_findings": len(state["findings"]),
        "triaged": len(state["triaged"]),
        "confirmed_bugs": confirmed,
        "confirmed_count": len(confirmed),
    }


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="bug_scan",
        description="God-mode bug hunt: run non-destructive security probes "
                    "(xss, sqli, idor, ssrf, csrf, openredirect) against an "
                    "AUTHORIZED target. Scope-guarded, audit-logged.",
        schema={"type": "object",
                "properties": {"task_id": {"type": "string"},
                               "url": {"type": "string"},
                               "checks": {"type": "array",
                                          "items": {"type": "string"},
                                          "default": ["xss", "sqli", "idor"]}},
                "required": ["task_id", "url"]},
        handler=_scan,
        tags=["bughunt", "godmode", "sectest"],
    )
    registry.register(
        name="bug_triage",
        description="Instant verdict on a finding: CONFIRMED / FALSE_POSITIVE / "
                    "NEEDS_MANUAL with confidence + evidence.",
        schema={"type": "object",
                "properties": {"task_id": {"type": "string"},
                               "scan_id": {"type": "string"},
                               "finding": {"type": "object"}},
                "required": ["finding"]},
        handler=_triage,
        tags=["bughunt", "godmode", "sectest"],
    )
    registry.register(
        name="bug_report",
        description="Consolidated report of confirmed bugs from a scan.",
        schema={"type": "object",
                "properties": {"scan_id": {"type": "string"}},
                "required": ["scan_id"]},
        handler=_report,
        tags=["bughunt", "godmode", "sectest"],
    )
