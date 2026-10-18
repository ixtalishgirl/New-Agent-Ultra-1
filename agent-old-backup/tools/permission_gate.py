"""Permission gate — TECHNICAL enforcement at the execution layer.

The model is never trusted to police itself. Every tool call passes through
check_tool_call() BEFORE execution. The 5 denials are hard-coded here and can
never be self-granted (policy-level lock).
"""
from __future__ import annotations
import re
from dataclasses import dataclass


@dataclass
class GateResult:
    allowed: bool
    reason: str = ""


# Patterns that map to the 5 denials
_DENIAL_PATTERNS = {
    "container_escape": [
        r"nsenter", r"unshare\s+.*-m", r"/proc/1/root",
        r"docker\s+.*--privileged.*host", r"breakout",
    ],
    "boot_recovery": [
        r"/boot/", r"efibootmgr", r"bcdedit", r"recovery\s+partition",
    ],
    "av_edr_disable": [
        r"systemctl\s+(stop|disable)\s+.*(crowdstrike|sentinelone|carbon|defender|clamav)",
        r"sc\s+stop\s+.*(defender|windefend)",
    ],
    "billing_fraud": [
        r"stripe.*refund.*fraud", r"subscription.*bypass.*payment",
    ],
    "exfiltration": [
        r"tar\s+.*\|\s*curl\s+.*--data-binary\s+@",
    ],
}


def _matches_any(text: str, patterns: list[str]) -> str | None:
    for p in patterns:
        if re.search(p, text, re.IGNORECASE):
            return p
    return None


def check_tool_call(tool_name: str, args: dict) -> GateResult:
    """Return allowed/denied for a tool call. Denials are absolute."""
    haystack = tool_name + " " + " ".join(str(v) for v in args.values())
    for denial, patterns in _DENIAL_PATTERNS.items():
        hit = _matches_any(haystack, patterns)
        if hit:
            return GateResult(
                allowed=False,
                reason=f"DENIED by policy '{denial}' (matched: {hit}). "
                       f"This denial is locked and can never be self-granted.",
            )
    # Sandbox-escape guard: generated code must not weaken containment
    if tool_name == "tool_create":
        code = str(args.get("code", ""))
        for denial, patterns in _DENIAL_PATTERNS.items():
            hit = _matches_any(code, patterns)
            if hit:
                return GateResult(
                    allowed=False,
                    reason=f"DENIED: generated tool violates '{denial}'.",
                )
    return GateResult(allowed=True)
