"""Policy enforcer — execution-layer enforcement of the 5 hard denials.

Per ahsan's order: ONLY the 5 denials are enforced. No other rules,
no sandbox, no approval gates. This is the single enforcement point.

The 5 denials (locked, can never be self-granted):
1. container_escape — host shell from inside container
2. boot_recovery — boot/recovery configuration modification
3. av_edr_disable — antivirus/EDR disabling
4. billing_fraud — third-party billing/subscription fraud
5. exfiltration — unauthorized data exfiltration
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Denial(Enum):
    CONTAINER_ESCAPE = "container_escape"
    BOOT_RECOVERY = "boot_recovery"
    AV_EDR_DISABLE = "av_edr_disable"
    BILLING_FRAUD = "billing_fraud"
    EXFILTRATION = "exfiltration"


@dataclass
class EnforcementResult:
    allowed: bool
    denial: Optional[Denial] = None
    reason: str = ""
    matched_pattern: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "denial": self.denial.value if self.denial else None,
            "reason": self.reason,
            "matched_pattern": self.matched_pattern,
        }


# Denial patterns — comprehensive but precise
_PATTERNS: Dict[Denial, List[str]] = {
    Denial.CONTAINER_ESCAPE: [
        r"\bnsenter\b",
        r"\bunshare\b.*\s-[a-z]*m",
        r"/proc/1/root",
        r"/proc/self/root",
        r"docker\s+.*--privileged",
        r"docker\s+.*-v\s+/\s*:",
        r"\bchroot\b.*\s/(\s|$)",
        r"container.*breakout",
        r"breakout.*container",
    ],
    Denial.BOOT_RECOVERY: [
        r"/boot/grub",
        r"/boot/efi",
        r"\befibootmgr\b",
        r"\bbcdedit\b",
        r"recovery\s+partition.*(format|delete|modify)",
        r"\bdd\b.*of=/dev/(sda|nvme|vda)",
    ],
    Denial.AV_EDR_DISABLE: [
        r"systemctl\s+(stop|disable|mask)\s+\S*(crowdstrike|sentinelone|"
        r"carbonblack|defender|clamav| Sophos|mcafee|symantec)",
        r"sc\s+(stop|config\s+\S+\s+start=\s*disabled)\s+\S*"
        r"(defender|windefend|mcafee|symantec)",
        r"Set-MpPreference\s+-DisableRealtimeMonitoring\s+\$true",
    ],
    Denial.BILLING_FRAUD: [
        r"stripe.*(refund|charge).*fraud",
        r"bypass.*payment.*subscription",
        r"fake.*subscription.*active",
        r"trial.*reset.*abuse",
    ],
    Denial.EXFILTRATION: [
        # Bulk exfil: archive piped directly to network upload
        r"tar\s+.*\|\s*curl\s+.*--data-binary",
        r"zip\s+.*\|\s*curl\s+.*--upload",
        # Credential harvesting for exfil
        r"(cat|grep)\s+.*(id_rsa|id_ed25519|\.pem)\s*\|\s*(curl|wget|nc)",
        r"env\s*\|\s*(curl|wget)\s+.*--data",
    ],
}


class PolicyEnforcer:
    """Single enforcement point for the 5 hard denials."""

    def __init__(self,
                 patterns: Optional[Dict[Denial, List[str]]] = None):
        self._patterns = patterns or _PATTERNS
        self._compiled: Dict[Denial, List[re.Pattern]] = {}
        for denial, pats in self._patterns.items():
            self._compiled[denial] = [
                re.compile(p, re.IGNORECASE) for p in pats]

    def check(self, tool_name: str,
              args: Dict[str, Any]) -> EnforcementResult:
        """Check a tool call against the 5 denials."""
        haystack = tool_name + " " + " ".join(
            str(v) for v in args.values())
        for denial, patterns in self._compiled.items():
            for rx in patterns:
                m = rx.search(haystack)
                if m:
                    return EnforcementResult(
                        allowed=False,
                        denial=denial,
                        reason=(
                            f"DENIED: '{denial.value}' is one of ahsan's "
                            f"5 hard denials. This can never be "
                            f"self-granted or overridden."),
                        matched_pattern=rx.pattern[:100])
        # Also scan generated code for tool_create
        if tool_name == "tool_create":
            code = str(args.get("code", ""))
            for denial, patterns in self._compiled.items():
                for rx in patterns:
                    if rx.search(code):
                        return EnforcementResult(
                            allowed=False,
                            denial=denial,
                            reason=(f"DENIED: generated tool code "
                                    f"violates '{denial.value}'"),
                            matched_pattern=rx.pattern[:100])
        return EnforcementResult(allowed=True, reason="ok")

    def check_command(self, command: str) -> EnforcementResult:
        """Check a raw shell command."""
        return self.check("shell", {"command": command})

    def check_code(self, code: str) -> EnforcementResult:
        """Check Python code for denial patterns."""
        return self.check("python", {"code": code})

    def denials(self) -> List[str]:
        return [d.value for d in Denial]

    def describe(self) -> str:
        lines = ["5 hard denials (locked, per ahsan's order):"]
        for d in Denial:
            lines.append(f"  - {d.value}: "
                         f"{len(self._patterns[d])} patterns")
        lines.append("Everything else: ALLOWED (no rules, no sandbox)")
        return "\n".join(lines)


# Singleton for convenience
_default_enforcer: Optional[PolicyEnforcer] = None


def get_enforcer() -> PolicyEnforcer:
    global _default_enforcer
    if _default_enforcer is None:
        _default_enforcer = PolicyEnforcer()
    return _default_enforcer


def is_allowed(tool_name: str, args: Dict[str, Any]) -> bool:
    """Quick check: is this tool call allowed?"""
    return get_enforcer().check(tool_name, args).allowed
