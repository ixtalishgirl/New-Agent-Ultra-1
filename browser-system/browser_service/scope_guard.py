"""Scope guard for active recon + security-testing tools.

Mirrors browser-system/permissions.js checkTargetScope.

HARD BOUNDARY (non-overridable): active recon and security testing run ONLY
against authorized targets / program scope. The authorized list comes from
SECTEST_AUTHORIZED_TARGETS (comma-separated domains) and/or SECTEST_SCOPE_FILE
(JSON: {"targets": [...]}). An empty list = deny everything. There is no flag
to skip the scope check itself.

ahsan's standing grant (PERMISSIONS.md, 2026-10-13): passive recon
(DNS/WHOIS/TLS/cert-transparency/wayback/fingerprinting) is unrestricted;
active recon (port/service discovery, endpoint probing) and security testing
(XSS, SQLi incl. blind boolean/time-based, SSRF, IDOR, CSRF, auth testing,
ZAP/Nmap wrappers) are authorized-targets/program-scope ONLY.
"""
from __future__ import annotations

import json
import os
from urllib.parse import urlparse


def get_authorized_targets() -> list[str]:
    out: list[str] = []
    raw = os.environ.get("SECTEST_AUTHORIZED_TARGETS", "")
    for part in raw.split(","):
        t = part.strip().lower()
        if t:
            out.append(t)
    scope_file = os.environ.get("SECTEST_SCOPE_FILE")
    if scope_file and os.path.exists(scope_file):
        try:
            with open(scope_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for t in data.get("targets", []) or []:
                t = str(t).strip().lower()
                if t and t not in out:
                    out.append(t)
        except Exception:
            pass  # scope-file problems fail closed (deny) below
    return out


def _host_of(target: str) -> str | None:
    try:
        t = target.strip()
        if "://" not in t:
            t = "https://" + t
        host = urlparse(t).hostname or ""
        return host.lower() or None
    except Exception:
        return None


def check_target_scope(target: str) -> tuple[bool, str]:
    """Return (ok, reason_or_matched). Fail-closed on any problem."""
    host = _host_of(target)
    if not host:
        return False, f"Invalid target: {target!r}"
    for allowed in get_authorized_targets():
        if host == allowed or host.endswith("." + allowed):
            return True, f"authorized (matched scope entry {allowed})"
    return False, (
        f"Target {host} is NOT in the authorized scope. Active recon and "
        "security testing run only against authorized targets / program scope. "
        "Add the domain to SECTEST_AUTHORIZED_TARGETS (or SECTEST_SCOPE_FILE) "
        "to authorize it."
    )


def scope_denied_result(tool: str, target: str, reason: str) -> dict:
    return {
        "ok": False,
        "tool": tool,
        "target": target,
        "error": "scope_denied",
        "reason": reason,
        "note": "Nothing was sent to the target. Authorize it first, then retry.",
    }


# ---------------------------------------------------------------------------
# Infrastructure permissions — FULL GRANT for ahsan's OWN infrastructure
# (2026-10-13 ~17:24 PKT: he explicitly accepted the risk).
# Granted: root/admin on his own systems, cloud admin (his accounts), user
# management on his platforms, production DB/credentials access, secrets
# visibility, unrestricted shell/network.
#
# RETAINED SAFEGUARDS (non-negotiable, from his own spec section 6):
#  1. explicit confirmation for destructive/consequential actions
#  2. page content never treated as system instructions
#  3. audit logging of sensitive actions (audit_sensitive_action below)
# ---------------------------------------------------------------------------

import datetime

_INFRA_SENSITIVE = {
    "root_admin", "cloud_admin", "user_management",
    "prod_credential_access", "secret_visibility",
}


def _infra_env_bool(name: str, default: bool = True) -> bool:
    v = os.environ.get(name)
    if v is None or not str(v).strip():
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


_KERNEL_NOTE = (
    "Technical note: kernel access is impossible inside containers "
    "(Render/Kaggle) — applies only on his own bare-metal hardware."
)


def check_kernel_access(bare_metal: bool = False, provider_infra: bool = False,
                        host: str = "") -> tuple[bool, str]:
    """Kernel-level access (2026-10-13, PERMISSIONS.md).
    Granted on his own bare-metal systems only. Never vs provider infra
    (same line as the permanently-denied container escape).
    """
    if provider_infra:
        audit_sensitive_action("kernel_denied:provider_infra", {})
        return False, (
            "Kernel access against a hosting provider's infrastructure is NOT "
            "granted — same line as container escape (permanently denied). "
            + _KERNEL_NOTE)
    if not bare_metal:
        return False, (
            "Kernel access is granted for his own bare-metal systems only. "
            "Declare bare_metal=True to confirm this is his own hardware. "
            + _KERNEL_NOTE)
    audit_sensitive_action("kernel_access", {"host": host or "own-bare-metal"})
    return True, "audited. " + _KERNEL_NOTE


def audit_enabled() -> bool:
    """Toggle: INFRA_AUDIT_ENABLED (default ON). ahsan can switch it OFF himself."""
    v = os.environ.get("INFRA_AUDIT_ENABLED")
    if v is None or not str(v).strip():
        return True
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def audit_sensitive_action(action: str, details: dict | None = None) -> str:
    """Append-only audit log for sensitive infra actions (respects the toggle)."""
    if not audit_enabled():
        return ""
    line = json.dumps({
        "ts": datetime.datetime.utcnow().isoformat() + "Z",
        "action": action,
        "details": details or {},
    }) + "\n"
    try:
        with open(os.environ.get("INFRA_AUDIT_LOG", "/tmp/halye-infra-audit.log"),
                  "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass  # audit failure must never break the action itself
    return line


def check_infra_action(kind: str, body: dict | None = None) -> tuple[bool, str]:
    """Full grant for his own infra; destructive actions still need confirmed:true."""
    toggles = {
        "root_admin": _infra_env_bool("INFRA_ALLOW_ROOT_ADMIN"),
        "cloud_admin": _infra_env_bool("INFRA_ALLOW_CLOUD_ADMIN"),
        "user_management": _infra_env_bool("INFRA_ALLOW_USER_MANAGEMENT"),
        "prod_credential_access": _infra_env_bool("INFRA_ALLOW_PROD_CREDENTIALS"),
        "secret_visibility": _infra_env_bool("INFRA_ALLOW_SECRET_VISIBILITY"),
        "unrestricted_shell": _infra_env_bool("INFRA_ALLOW_UNRESTRICTED_SHELL"),
        "deploy_own_app": _infra_env_bool("INFRA_ALLOW_OWN_DEPLOYMENTS"),
        "workspace_shell": _infra_env_bool("INFRA_ALLOW_WORKSPACE_SHELL"),
    }
    if kind in _INFRA_SENSITIVE:
        audit_sensitive_action(kind, {"confirmed": bool((body or {}).get("confirmed"))})
    if kind in toggles and not toggles[kind]:
        return False, f"Infrastructure action '{kind}' is disabled by env toggle."
    if kind in ("user_management", "cloud_admin", "root_admin") and (body or {}).get("destructive") is True:
        if not (body or {}).get("confirmed") is True:
            return False, (
                "Destructive infrastructure action requires explicit user confirmation. "
                "Re-send with confirmed:true after the user approves."
            )
    return True, ""


def is_withheld_infra(kind: str) -> bool:
    # Nothing infra-class is withheld anymore (full grant for his own systems).
    return False


def allow_unrestricted_filesystem() -> bool:
    """Unrestricted filesystem on his own systems (INFRA_ALLOW_UNRESTRICTED_FS, default ON)."""
    return _infra_env_bool("INFRA_ALLOW_UNRESTRICTED_FS", True)


# ---------------------------------------------------------------------------
# Shell permissions (2026-10-13, PERMISSIONS.md).
# GRANTED (his own systems): unrestricted Bash/PowerShell/CMD, arbitrary
# commands in his workspace, process-killing, service control, cron/scheduled-
# task modification, executable launching.
# WITHHELD (non-overridable):
#  1. container_escape — provider's host, not his.
#  2. blind_run_unknown_script — download -> inspect -> run only.
#  3. privesc_general — only within authorized sec-testing scope.
# ---------------------------------------------------------------------------

SHELL_PERMANENT_DENIAL: dict[str, str] = {
    "container_escape": "Container escape to the host shell is PERMANENTLY DENIED. The host is the provider's system (e.g. Render) — breaking out of it is an attack on shared infrastructure that is not his to authorize. This is not his risk to accept.",
}

_SHELL_GRANTED = {
    "arbitrary_command", "process_control", "service_control",
    "cron_control", "executable_launch",
}


def check_shell_action(kind: str, target: str = "", source_url: str = "",
                       script_content: str = "", command: str = "") -> tuple[bool, str]:
    """Final shell model (2026-10-13 ~17:26 PKT, his explicit orders).
    - container_escape: PERMANENTLY DENIED (not his to authorize).
    - blind_run_unknown_script: GRANTED; MANDATORY audit with source URL + SHA-256.
    - privesc_general: GRANTED on his own systems; MANDATORY audit with full command.
    - privesc_scoped: authorized sec-testing targets only (scope guard).
    """
    import hashlib
    perm_denial = SHELL_PERMANENT_DENIAL.get(kind)
    if perm_denial:
        audit_sensitive_action("shell_denied:" + kind, {"target": target or None})
        return False, perm_denial
    if kind == "blind_run_unknown_script":
        sha256 = ""
        if script_content:
            sha256 = hashlib.sha256(script_content.encode("utf-8")).hexdigest()
        audit_sensitive_action("blind_script_run", {
            "source_url": source_url or None, "sha256": sha256 or None,
            "command": command or None,
        })
        return True, "audited (source URL + SHA-256 logged)"
    if kind == "privesc_general":
        audit_sensitive_action("privesc_general", {
            "command": command or None, "target": target or "own-system",
        })
        return True, "audited (full command logged)"
    if kind == "privesc_scoped":
        ok, scope_reason = check_target_scope(target)
        if not ok:
            return False, scope_reason
        audit_sensitive_action("privesc_scoped", {"target": target, "command": command or None})
        return True, scope_reason
    if kind in _SHELL_GRANTED:
        if not _infra_env_bool("INFRA_ALLOW_UNRESTRICTED_SHELL"):
            return False, "Shell actions disabled by INFRA_ALLOW_UNRESTRICTED_SHELL=0."
        return True, ""
    return False, f"Unknown shell action kind: {kind}"
