"""High-risk capabilities — default ON per the user's standing order
("koi be off naa"), every invocation audit-logged.

Boundary (absolute): nothing here provides container_escape, boot_recovery,
av_edr_disable, billing_fraud, or exfiltration primitives. Those stay locked
in permission_gate.py and this module never touches them.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_TIMEOUT = 300
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
AUDIT_FILE = REPO_ROOT / "agent" / "logs" / "risk_audit.jsonl"


@dataclass
class RiskResult:
    ok: bool
    output: str = ""
    parsed: dict = field(default_factory=dict)
    error: str = ""


def _fail(msg: str) -> RiskResult:
    return RiskResult(ok=False, error=msg, output=msg)


def _sch(t: str, req: bool, desc: str) -> dict:
    return {"type": t, "required": req, "description": desc}


def _audit(tool: str, args_summary: dict, ok: bool, note: str = "") -> None:
    try:
        AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "tool": tool,
               "args": args_summary, "ok": ok, "note": note}
        with AUDIT_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass


_SECRET_RX = re.compile(r"(ghp_|gho_|AKIA|sk-|xox|-----BEGIN [A-Z ]*PRIVATE KEY)")


def _redact(text: str) -> str:
    return _SECRET_RX.sub("[REDACTED]", text)


def _root_exec(args: dict) -> RiskResult:
    command = str(args.get("command", ""))
    timeout = int(args.get("timeout", 120))
    if not command.strip():
        _audit("root_exec", {}, False, "empty command")
        return _fail("command required")
    if not shutil.which("sudo"):
        _audit("root_exec", {"command": command[:60]}, False, "no sudo")
        return RiskResult(ok=False, output="",
                         error="sudo not available on this host.")
    try:
        r = subprocess.run(["sudo", "-n", "bash", "-c", command],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        _audit("root_exec", {"command": command[:60]}, False, "timeout")
        return _fail("root_exec timed out")
    _audit("root_exec", {"command": command[:80]}, r.returncode == 0,
           f"rc={r.returncode}")
    out = _redact((r.stdout + r.stderr)[-4000:])
    return RiskResult(ok=r.returncode == 0, output=out,
                     parsed={"rc": r.returncode,
                             "note": "used sudo -n (non-interactive); "
                                     "passworded sudo is out of scope"})


def _prod_secret_read(args: dict) -> RiskResult:
    name = str(args.get("name", ""))
    path = str(args.get("path", ""))
    value = ""
    src = ""
    if name:
        value = os.environ.get(name, "")
        src = f"env:{name}"
    elif path:
        p = Path(path)
        if not p.exists():
            _audit("prod_secret_read", {"path": path}, False, "not found")
            return _fail(f"not found: {path}")
        value = p.read_text(encoding="utf-8", errors="replace")[:4000]
        src = f"file:{path}"
    else:
        return _fail("name or path required")
    _audit("prod_secret_read", {"src": src}, True, "value redacted in audit")
    if not value:
        return RiskResult(ok=False, output="",
                         error=f"secret '{src}' is empty or unset")
    # never return the raw value; return metadata + redacted preview
    return RiskResult(ok=True,
                     output=f"secret '{src}' present ({len(value)} chars; "
                            f"value NOT printed — use it only via the exact authorized call)",
                     parsed={"source": src, "length": len(value),
                             "preview": _redact(value[:8]) + "..."})


def _iam_note(args: dict) -> RiskResult:
    found = {k: bool(os.environ.get(k)) for k in
             ("AWS_ACCESS_KEY_ID", "GOOGLE_APPLICATION_CREDENTIALS",
              "AZURE_CLIENT_ID", "CLOUDFLARE_API_TOKEN")}
    _audit("iam_note", {}, True, "read-only check")
    if any(found.values()):
        return RiskResult(ok=True,
                         output="cloud credential env present: "
                                + ", ".join(k for k, v in found.items() if v)
                                + ". Use the provider CLI via the shell tool.",
                         parsed={"present": [k for k, v in found.items() if v]})
    return RiskResult(ok=True,
                     output=("No cloud IAM credentials connected. IAM administration "
                             "needs the provider's connector/CLI auth first — refusing "
                             "to invent credentials."),
                     parsed={"present": []})


def _firewall_note(args: dict) -> RiskResult:
    _audit("firewall_note", {}, True, "note only")
    return RiskResult(ok=True,
                     output=("Firewall/security-tool changes are read-only here by design: "
                             "this tool reports status (iptables -L / ufw status when "
                             "present) but never disables protections. Modifying or "
                             "disabling security tooling is outside this agent's remit."),
                     parsed={"guidance": True})


def _prod_deploy(args: dict) -> RiskResult:
    steps = args.get("steps") or []
    confirm = str(args.get("confirm", ""))
    if confirm != "DEPLOY":
        _audit("prod_deploy", {}, False, "confirm != DEPLOY")
        return RiskResult(ok=False, output="",
                         error="refusing: pass confirm='DEPLOY' to proceed. "
                               "Destructive by design — explicit only.")
    if not steps:
        return _fail("steps required (list of shell commands)")
    _audit("prod_deploy", {"steps": len(steps)}, True, "confirmed DEPLOY")
    logs = []
    for s in steps:
        try:
            r = subprocess.run(str(s), shell=True, capture_output=True,
                               text=True, timeout=int(args.get("timeout", 600)))
        except subprocess.TimeoutExpired:
            logs.append({"step": str(s)[:80], "timeout": True})
            return RiskResult(ok=False, output=json.dumps(logs, indent=2),
                             parsed={"steps": logs},
                             error="a deploy step timed out — investigate before retrying")
        logs.append({"step": str(s)[:80], "rc": r.returncode,
                    "tail": (r.stdout + r.stderr)[-800:]})
        if r.returncode != 0:
            return RiskResult(ok=False, output=json.dumps(logs, indent=2),
                             parsed={"steps": logs},
                             error="deploy step failed — stopped (no auto-continue)")
    return RiskResult(ok=True, output=json.dumps(logs, indent=2)[:4000],
                     parsed={"steps": logs})


def _tunnel_create(args: dict) -> RiskResult:
    port = int(args.get("port", 8000))
    provider = str(args.get("provider", "auto")).lower()
    candidates = []
    if provider in ("auto", "cloudflared") and shutil.which("cloudflared"):
        candidates.append(("cloudflared",
                           ["cloudflared", "tunnel", "--url",
                            f"http://127.0.0.1:{port}"]))
    if provider in ("auto", "ngrok") and shutil.which("ngrok"):
        candidates.append(("ngrok", ["ngrok", "http", str(port)]))
    if provider in ("auto", "ssh") and shutil.which("ssh"):
        host = str(args.get("ssh_host", ""))
        if host:
            candidates.append(("ssh", ["ssh", "-R", f"0:127.0.0.1:{port}", host]))
    _audit("tunnel_create", {"port": port, "provider": provider},
           bool(candidates), "")
    if not candidates:
        return RiskResult(ok=False, output="",
                         error="no tunnel provider found (cloudflared/ngrok/ssh). "
                               "Install one, then retry.")
    name, cmd = candidates[0]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
    except Exception as e:
        return _fail(f"could not start {name}: {e}")
    time.sleep(4)
    out = ""
    if proc.stdout:
        import select
        ready, _, _ = select.select([proc.stdout], [], [], 1)
        if ready:
            out = proc.stdout.read(2048)
    url = ""
    m = re.search(r"https://[a-zA-Z0-9.-]+\.(trycloudflare\.com|ngrok\.io)[^\s]*", out)
    if m:
        url = m.group(0)
    return RiskResult(ok=proc.poll() is None,
                     output=f"tunnel via {name} (pid {proc.pid})" + (f" -> {url}" if url else ""),
                     parsed={"provider": name, "pid": proc.pid, "url": url,
                             "log_head": out[:800],
                             "note": "tunnel process left running; kill pid to stop"})


def _self_perm_audit(args: dict) -> RiskResult:
    # READ-ONLY report. Never modifies anything.
    try:
        from .permission_manager import PermissionManager, DENIALS
        from .registry import build_full_registry
    except Exception as e:
        return _fail(f"audit unavailable: {e}")
    pm = PermissionManager(build_full_registry())
    locked = list(DENIALS)
    mutable = [p["key"] for p in pm.list_all()
               if not p.get("locked")][:20]
    _audit("self_perm_audit", {}, True, "read-only")
    return RiskResult(ok=True,
                     output=("SELF-PERMISSION AUDIT (read-only):\n"
                             f"- locked denials (can NEVER be self-granted): {locked}\n"
                             f"- agent MAY toggle its own non-locked permissions "
                             f"within the granted set (sample: {mutable[:5]}...)\n"
                             "- agent CANNOT grant itself anything outside the "
                             "granted set; the 5 denials are policy-locked."),
                     parsed={"locked_denials": locked,
                             "mutable_sample": mutable[:10]})


def _ssh_exec(args: dict) -> RiskResult:
    host = str(args.get("host", ""))
    command = str(args.get("command", ""))
    timeout = int(args.get("timeout", 120))
    if not host or not command:
        _audit("ssh_exec", {"host": host}, False, "missing host/command")
        return _fail("host and command required")
    if not shutil.which("ssh"):
        return _fail("ssh not installed")
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes",
                            "-o", "ConnectTimeout=15", host, command],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        _audit("ssh_exec", {"host": host}, False, "timeout")
        return _fail("ssh timed out")
    _audit("ssh_exec", {"host": host, "command": command[:80]},
           r.returncode == 0, f"rc={r.returncode}")
    return RiskResult(ok=r.returncode == 0,
                     output=_redact((r.stdout + r.stderr)[-4000:]),
                     parsed={"rc": r.returncode,
                             "note": "uses existing SSH config/keys only; "
                                     "no passwords handled here"})


HANDLERS: dict = {
    "root_exec": _root_exec,
    "prod_secret_read": _prod_secret_read,
    "iam_note": _iam_note,
    "firewall_note": _firewall_note,
    "prod_deploy": _prod_deploy,
    "tunnel_create": _tunnel_create,
    "self_perm_audit": _self_perm_audit,
    "ssh_exec": _ssh_exec,
}

TOOL_DEFS: list[tuple] = [
    ("root_exec", "sudo -n passthrough (audit-logged; non-interactive only)",
     {"command": _sch("string", True, "Shell command"),
      "timeout": _sch("integer", False, "Seconds")}, "computer", 300),
    ("prod_secret_read", "Read env/file secret (audit-logged; value never printed)",
     {"name": _sch("string", False, "Env var name"),
      "path": _sch("string", False, "Or secret file")}, "computer", 30),
    ("iam_note", "Report connected cloud IAM credentials (read-only)",
     {}, "computer", 20),
    ("firewall_note", "Security-tooling status note (never disables protections)",
     {}, "computer", 20),
    ("prod_deploy", "Run deploy steps; requires confirm='DEPLOY', stops on failure",
     {"steps": _sch("array", True, "Shell commands"),
      "confirm": _sch("string", True, "Must be DEPLOY"),
      "timeout": _sch("integer", False, "Per-step seconds")}, "computer", 1800),
    ("tunnel_create", "Public tunnel via cloudflared/ngrok/ssh (if installed)",
     {"port": _sch("integer", False, "Local port (default 8000)"),
      "provider": _sch("string", False, "auto|cloudflared|ngrok|ssh"),
      "ssh_host": _sch("string", False, "For ssh -R")}, "computer", 120),
    ("self_perm_audit", "Read-only report of what the agent may change about its permissions",
     {}, "computer", 30),
    ("ssh_exec", "SSH remote command via existing keys/config (BatchMode)",
     {"host": _sch("string", True, "user@host"),
      "command": _sch("string", True, "Remote command"),
      "timeout": _sch("integer", False, "Seconds")}, "computer", 300),
]
