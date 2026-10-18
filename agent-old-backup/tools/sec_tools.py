"""Security & Reliability tools.

Real implementations: pattern/bandit SAST, pip-audit dependency scanning
(with honest fallback), license inventory, config auditing (debug flags,
open permissions, default secrets), permission auditing, DLP pattern checks,
and supply-chain hygiene checks. Nothing here exfiltrates or weakens
containment; the 5 hard denials stay enforced by permission_gate.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TIMEOUT = 180
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass
class SecResult:
    ok: bool
    output: str = ""
    parsed: dict = field(default_factory=dict)
    error: str = ""


def _fail(msg: str) -> SecResult:
    return SecResult(ok=False, error=msg, output=msg)


def _sch(t: str, req: bool, desc: str) -> dict:
    return {"type": t, "required": req, "description": desc}


def _sast_scan(args: dict) -> SecResult:
    root = Path(str(args.get("root", "."))).expanduser()
    bandit = shutil.which("bandit")
    if bandit:
        try:
            r = subprocess.run([bandit, "-r", str(root), "-f", "json",
                                "-q"], capture_output=True, text=True,
                               timeout=240)
            data = json.loads(r.stdout or "{}")
            results = data.get("results", [])
            issues = [{"file": x.get("filename"), "line": x.get("line_number"),
                      "severity": x.get("issue_severity"),
                      "msg": x.get("issue_text")}
                     for x in results[:200]]
            return SecResult(ok=True,
                            output=f"bandit: {len(results)} issues",
                            parsed={"tool": "bandit", "issues": issues})
        except Exception as e:
            return _fail(f"bandit failed: {e}")
    # fallback: pattern-based
    patterns = [
        (re.compile(r"\beval\s*\("), "high", "eval()"),
        (re.compile(r"\bexec\s*\("), "high", "exec()"),
        (re.compile(r"shell\s*=\s*True"), "high", "shell=True"),
        (re.compile(r"pickle\.loads?"), "medium", "pickle deserialization"),
        (re.compile(r"yaml\.load\s*\("), "medium", "yaml.load without Loader"),
        (re.compile(r"hashlib\.(md5|sha1)\("), "low", "weak hash"),
        (re.compile(r"random\.random\(\)"), "low", "non-crypto RNG"),
        (re.compile(r"verify\s*=\s*False"), "high", "TLS verify disabled"),
        (re.compile(r"SECRET|PASSWORD|API_KEY['\"]?\s*[:=]\s*['\"][^'\"]+",
                    re.I), "high", "possible hardcoded secret"),
    ]
    files = [p for p in root.rglob("*.py") if "__pycache__" not in p.parts
             and ".git" not in p.parts][:400]
    issues = []
    for p in files:
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for rx, sev, label in patterns:
            for m in rx.finditer(src):
                ln = src.count("\n", 0, m.start()) + 1
                issues.append({"file": str(p), "line": ln,
                              "severity": sev, "msg": label})
                if len(issues) >= 300:
                    break
    return SecResult(ok=True,
                    output=(f"pattern SAST: {len(issues)} findings "
                            f"(`pip install bandit` for deeper analysis)"),
                    parsed={"tool": "patterns", "issues": issues})


def _vuln_scan_deps(args: dict) -> SecResult:
    pip_audit = shutil.which("pip-audit")
    if pip_audit:
        try:
            r = subprocess.run([pip_audit, "--format=json"], capture_output=True,
                               text=True, timeout=240)
            data = json.loads(r.stdout or "[]")
            vulns = data if isinstance(data, list) else data.get("dependencies", [])
            return SecResult(ok=not vulns,
                            output=f"pip-audit: {len(vulns)} vulnerable deps",
                            parsed={"tool": "pip-audit", "vulns": vulns[:100]})
        except Exception as e:
            return _fail(f"pip-audit failed: {e}")
    # fallback: list installed packages (honest scope)
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "list",
                            "--format=json"], capture_output=True, text=True,
                           timeout=60)
        pkgs = json.loads(r.stdout or "[]")
    except Exception as e:
        return _fail(f"pip list failed: {e}")
    return SecResult(ok=True,
                    output=(f"pip-audit not installed — inventoried {len(pkgs)} packages. "
                            "`pip install pip-audit` for CVE matching."),
                    parsed={"tool": "inventory", "packages": len(pkgs),
                            "sample": [p["name"] for p in pkgs[:20]]})


def _license_check(args: dict) -> SecResult:
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "show", "-f", "x"],
                           capture_output=True, text=True, timeout=30)
    except Exception:
        pass
    licenses: dict[str, str] = {}
    try:
        import importlib.metadata as md
        for dist in md.distributions():
            name = dist.metadata.get("Name", "?")
            lic = dist.metadata.get("License", "") or ""
            # also check classifiers
            if not lic:
                for c in dist.metadata.get_all("Classifier") or []:
                    if c.startswith("License ::"):
                        lic = c.split("::")[-1].strip()
                        break
            licenses[name] = lic or "UNKNOWN"
    except Exception as e:
        return _fail(f"metadata scan failed: {e}")
    risky = {k: v for k, v in licenses.items()
             if re.search(r"GPL|AGPL", v, re.I)}
    return SecResult(ok=True,
                    output=f"{len(licenses)} packages; {len(risky)} copyleft-flagged",
                    parsed={"total": len(licenses), "copyleft": risky,
                            "note": "flagged != violating; review obligations per use"})


def _config_audit(args: dict) -> SecResult:
    root = Path(str(args.get("root", "."))).expanduser()
    issues = []
    for p in list(root.rglob("*.env")) + list(root.rglob("*.ini")) + \
            list(root.rglob("*.yaml")) + list(root.rglob("*.yml")) + \
            list(root.rglob("settings.py")):
        if ".git" in p.parts:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        rel = str(p)
        if re.search(r"DEBUG\s*=\s*True", text):
            issues.append({"file": rel, "msg": "DEBUG=True in config"})
        if re.search(r"(password|secret|key)\s*=\s*['\"](admin|password|123|test|changeme)['\"]",
                     text, re.I):
            issues.append({"file": rel, "msg": "default/weak credential"})
        if re.search(r"0\.0\.0\.0", text):
            issues.append({"file": rel, "msg": "binds 0.0.0.0 — confirm intended"})
        try:
            mode = stat.S_IMODE(p.stat().st_mode)
            if mode & 0o077:
                issues.append({"file": rel,
                              "msg": f"world/group-readable perms {oct(mode)}"})
        except OSError:
            pass
    return SecResult(ok=not issues,
                    output=f"{len(issues)} config issues",
                    parsed={"issues": issues[:100]})


def _perm_audit(args: dict) -> SecResult:
    # read-only audit of the PermissionManager state
    try:
        from .permission_manager import PermissionManager
        from .registry import build_full_registry
    except Exception as e:
        return _fail(f"audit unavailable: {e}")
    pm = PermissionManager(build_full_registry())
    allp = pm.list_all()
    locked = [p for p in allp if p.get("locked")]
    off = [p for p in allp if not p.get("enabled") and not p.get("locked")]
    return SecResult(ok=True,
                    output=f"{len(allp)} permissions: {len(locked)} locked denials, "
                           f"{len(off)} toggled off",
                    parsed={"total": len(allp),
                            "locked": [p["key"] for p in locked],
                            "toggled_off": [p["key"] for p in off][:50]})


def _auth_test(args: dict) -> SecResult:
    url = str(args.get("url", ""))
    if not url:
        return _fail("url required")
    results = []
    # unauthenticated probe
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            results.append({"probe": "no-auth", "status": r.status,
                           "exposed": r.status == 200})
    except Exception as e:
        results.append({"probe": "no-auth", "status": -1,
                       "error": f"{type(e).__name__}"})
    # bad-token probe
    try:
        req = urllib.request.Request(url,
                                     headers={"Authorization": "Bearer invalid-token-xyz"})
        with urllib.request.urlopen(req, timeout=15) as r:
            results.append({"probe": "bad-token", "status": r.status,
                           "accepted_bad_token": r.status == 200})
    except Exception as e:
        code = getattr(e, "code", -1)
        results.append({"probe": "bad-token", "status": code,
                       "accepted_bad_token": False})
    bad = [r for r in results if r.get("exposed") or r.get("accepted_bad_token")]
    return SecResult(ok=not bad,
                    output="; ".join(f"{r['probe']}->{r['status']}" for r in results)
                    + (" — WARNING: endpoint may lack auth" if bad else " — auth enforced"),
                    parsed={"probes": results})


def _input_fuzz(args: dict) -> SecResult:
    # reuse fuzz_run logic lightly: random malformed inputs to a URL/endpoint
    url = str(args.get("url", ""))
    n = min(int(args.get("requests", 30)), 200)
    if not url:
        return _fail("url required")
    import random
    rnd = random.Random(int(args.get("seed", 0)))
    payloads = ["'" * 50, '"' * 50, "<script>alert(1)</script>", "../" * 10,
                "%00", "\x00" * 10, "A" * 10000, "${7*7}", "{{7*7}}",
                "1;DROP TABLE x", "\n", "null", "undefined"]
    crashes = []
    for i in range(n):
        p = rnd.choice(payloads)
        try:
            req = urllib.request.Request(url + ("&" if "?" in url else "?") +
                                         f"q={urllib.parse.quote(p)}",
                                         headers={"User-Agent": "haley-fuzz/1.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                if r.status >= 500:
                    crashes.append({"payload": p[:40], "status": r.status})
        except Exception as e:
            code = getattr(e, "code", 0)
            if code and code >= 500:
                crashes.append({"payload": p[:40], "status": code})
    return SecResult(ok=True, output=f"{n} malformed inputs, {len(crashes)} 5xx",
                    parsed={"requests": n, "server_errors": crashes[:20],
                            "note": "authorized targets only"})


def _dast_note(args: dict) -> SecResult:
    return SecResult(ok=True,
                    output=("Dynamic testing: use auth_test + input_fuzz + load_test "
                            "against YOUR staging. Real DAST suites (OWASP ZAP) can run "
                            "via the shell tool if installed — authorized targets only."),
                    parsed={"guidance": True})


def _sec_regression(args: dict) -> SecResult:
    root = str(args.get("root", "."))
    s = _sast_scan({"root": root})
    v = _vuln_scan_deps({})
    total = len(s.parsed.get("issues", [])) + len(v.parsed.get("vulns", []))
    return SecResult(ok=True,
                    output=f"security regression: {total} open findings "
                           f"({len(s.parsed.get('issues', []))} sast, "
                           f"{len(v.parsed.get('vulns', []))} deps)",
                    parsed={"sast_issues": len(s.parsed.get("issues", [])),
                            "dep_vulns": len(v.parsed.get("vulns", []))})


def _dlp_check(args: dict) -> SecResult:
    root = Path(str(args.get("root", "."))).expanduser()
    patterns = [
        (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "SSN-like"),
        (re.compile(r"\b(?:\d[ -]*?){13,16}\b"), "card-number-like"),
        (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "email"),
        (re.compile(r"(?i)(aws_|AKIA)[A-Z0-9]{10,}"), "aws-key-like"),
        (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "github-token-like"),
    ]
    hits = []
    for p in root.rglob("*"):
        if not p.is_file() or ".git" in p.parts or "__pycache__" in p.parts:
            continue
        if p.suffix in (".png", ".jpg", ".zip", ".pyc"):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if len(text) > 2_000_000:
            continue
        for rx, label in patterns:
            for m in rx.finditer(text):
                ln = text.count("\n", 0, m.start()) + 1
                hits.append({"file": str(p), "line": ln, "kind": label})
                if len(hits) >= 200:
                    break
    return SecResult(ok=not hits, output=f"{len(hits)} DLP hits",
                    parsed={"hits": hits})


def _supply_chain_check(args: dict) -> SecResult:
    root = Path(str(args.get("root", "."))).expanduser()
    req = root / "requirements.txt"
    lock = list(root.glob("*.lock")) + list(root.glob("Pipfile.lock"))
    findings = []
    if not req.exists():
        findings.append("no requirements.txt found")
    else:
        pinned = unpinned = 0
        for ln in req.read_text().splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            if "==" in ln:
                pinned += 1
            else:
                unpinned += 1
                findings.append(f"unpinned: {ln[:60]}")
        findings.insert(0, f"pinned={pinned} unpinned={unpinned}")
    if not lock:
        findings.append("no lockfile — consider pip freeze > requirements.lock")
    return SecResult(ok=all("unpinned" not in f and "no " not in f for f in findings[1:]),
                    output="; ".join(findings[:20]),
                    parsed={"findings": findings[:50]})


HANDLERS: dict = {
    "sast_scan": _sast_scan,
    "vuln_scan_deps": _vuln_scan_deps,
    "license_check": _license_check,
    "config_audit": _config_audit,
    "perm_audit": _perm_audit,
    "auth_test": _auth_test,
    "input_fuzz": _input_fuzz,
    "dast_note": _dast_note,
    "sec_regression": _sec_regression,
    "dlp_check": _dlp_check,
    "supply_chain_check": _supply_chain_check,
}

TOOL_DEFS: list[tuple] = [
    ("sast_scan", "bandit if installed, else pattern-based SAST",
     {"root": _sch("string", False, "Repo root")}, "computer", 300),
    ("vuln_scan_deps", "pip-audit if installed, else package inventory",
     {}, "computer", 300),
    ("license_check", "Inventory dependency licenses; flag copyleft",
     {}, "computer", 120),
    ("config_audit", "Find debug flags, weak creds, open perms in configs",
     {"root": _sch("string", False, "Root")}, "computer", 120),
    ("perm_audit", "Read-only audit of PermissionManager state",
     {}, "computer", 30),
    ("auth_test", "Probe an endpoint with no-auth and bad-token",
     {"url": _sch("string", True, "URL")}, "computer", 60),
    ("input_fuzz", "Send malformed inputs to a URL; count 5xx (authorized only)",
     {"url": _sch("string", True, "URL"),
      "requests": _sch("integer", False, "Default 30, max 200"),
      "seed": _sch("integer", False, "Seed")}, "computer", 180),
    ("dast_note", "Guidance for dynamic testing",
     {}, "computer", 10),
    ("sec_regression", "Re-run SAST + dep scan; count open findings",
     {"root": _sch("string", False, "Repo root")}, "computer", 600),
    ("dlp_check", "Scan files for SSN/card/email/secret patterns",
     {"root": _sch("string", False, "Root")}, "computer", 300),
    ("supply_chain_check", "Pinning + lockfile hygiene for requirements",
     {"root": _sch("string", False, "Root")}, "computer", 60),
]
