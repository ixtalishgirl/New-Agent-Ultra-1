"""Network policy — egress rules for tool network access.

Per ChatGPT spec: agent/security/network_policy.py
NOTE: Per ahsan's order, the agent has full network access.
This module documents the policy; enforcement is permissive
except for the 5 hard denials (handled in permission_gate).
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class NetworkVerdict(Enum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass
class NetworkRule:
    """A network egress rule."""
    pattern: str  # regex on hostname or "private"/"public"
    verdict: NetworkVerdict
    reason: str = ""

    def matches(self, url: str) -> bool:
        try:
            host = urllib.parse.urlparse(url).netloc.lower()
        except Exception:
            return False
        if self.pattern == "private":
            return _is_private_host(host)
        if self.pattern == "public":
            return not _is_private_host(host)
        return bool(re.search(self.pattern, host))


def _is_private_host(host: str) -> bool:
    host = host.split(":")[0]
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    if host.startswith("10.") or host.startswith("192.168."):
        return True
    if re.match(r"^172\.(1[6-9]|2\d|3[01])\.", host):
        return True
    if host.endswith(".local") or host.endswith(".internal"):
        return True
    return False


class NetworkPolicy:
    """Egress policy. Default: ALLOW all (per ahsan's full-access order).

    Deny rules only for the 5 hard denials where network-relevant
    (exfiltration patterns are in permission_gate).
    """

    def __init__(self, default_allow: bool = True):
        self.default_allow = default_allow
        self.rules: List[NetworkRule] = []
        # No deny rules by default — full access per ahsan.
        # Exfiltration is caught by permission_gate patterns.

    def add_rule(self, pattern: str, verdict: NetworkVerdict,
                 reason: str = "") -> None:
        self.rules.append(NetworkRule(pattern, verdict, reason))

    def check(self, url: str) -> NetworkVerdict:
        """Evaluate URL against rules (first match wins)."""
        for rule in self.rules:
            if rule.matches(url):
                return rule.verdict
        return (NetworkVerdict.ALLOW if self.default_allow
                else NetworkVerdict.DENY)

    def is_allowed(self, url: str) -> bool:
        return self.check(url) == NetworkVerdict.ALLOW

    def describe(self) -> str:
        lines = [f"Default: {'ALLOW' if self.default_allow else 'DENY'}"]
        for r in self.rules:
            lines.append(f"  {r.verdict.value}: {r.pattern} ({r.reason})")
        if not self.rules:
            lines.append("  (no custom rules — full egress per ahsan)")
        return "\n".join(lines)
