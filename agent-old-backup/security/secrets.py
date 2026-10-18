"""Secrets management — env-based secrets with redaction.

Per ChatGPT spec: agent/security/secrets.py
Secrets come from environment only. Values are never logged.
Redaction helpers for output sanitization.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional


# Patterns that look like secrets (for redaction)
_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)(['\"]?)([A-Za-z0-9_\-]{8,})\2"),
    re.compile(r"(?i)(token\s*[:=]\s*)(['\"]?)([A-Za-z0-9_\-]{8,})\2"),
    re.compile(r"(?i)(password\s*[:=]\s*)(['\"]?)([^'\"\s]{4,})\2"),
    re.compile(r"(?i)(secret\s*[:=]\s*)(['\"]?)([A-Za-z0-9_\-]{8,})\2"),
    re.compile(r"sk-[A-Za-z0-9]{8,}"),  # OpenAI-style
    re.compile(r"ghp_[A-Za-z0-9]{8,}"),  # GitHub PAT
    re.compile(r"xox[bap]-[A-Za-z0-9\-]{8,}"),  # Slack
]


class SecretsManager:
    """Reads secrets from environment; never stores in code."""

    def __init__(self, prefix: str = ""):
        self.prefix = prefix

    def get(self, name: str, default: str = "") -> str:
        """Get a secret by name (with optional prefix)."""
        return os.environ.get(self.prefix + name, default)

    def require(self, name: str) -> str:
        """Get a secret or raise."""
        val = self.get(name)
        if not val:
            raise KeyError(f"Required secret missing: {self.prefix + name}")
        return val

    def available(self) -> List[str]:
        """List secret names present (not values)."""
        out = []
        for k in os.environ:
            if self.prefix and not k.startswith(self.prefix):
                continue
            if any(s in k.upper() for s in
                   ["KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH"]):
                out.append(k)
        return sorted(out)

    def as_dict(self, names: List[str]) -> Dict[str, str]:
        """Get multiple secrets (use sparingly)."""
        return {n: self.get(n) for n in names}


def redact(text: str, replacement: str = "[REDACTED]") -> str:
    """Redact secret-looking values from text."""
    out = text
    for rx in _SECRET_PATTERNS:
        # Keep the label, redact the value
        def _sub(m: re.Match) -> str:
            groups = m.groups()
            if len(groups) >= 3:
                return groups[0] + groups[1] + replacement
            return replacement
        try:
            out = rx.sub(_sub, out)
        except Exception:
            out = rx.sub(replacement, out)
    return out


def redact_dict(d: Dict[str, Any]) -> Dict[str, Any]:
    """Redact secrets in a dict's string values."""
    out = {}
    for k, v in d.items():
        if isinstance(v, str):
            out[k] = redact(v)
        elif isinstance(v, dict):
            out[k] = redact_dict(v)
        else:
            out[k] = v
    return out


class SecretRef:
    """A reference to a secret (name only, no value)."""

    def __init__(self, name: str, manager: Optional[SecretsManager] = None):
        self.name = name
        self._manager = manager or SecretsManager()

    def resolve(self) -> str:
        return self._manager.get(self.name)

    def __repr__(self) -> str:
        return f"SecretRef({self.name}=***)"
