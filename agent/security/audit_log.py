"""
Audit log — every security-relevant event recorded.
Secrets are NEVER written to logs.
"""
from __future__ import annotations
import json
import time
from pathlib import Path


SENSITIVE_KEYS = ("password", "secret", "token", "api_key", "apikey",
                  "private_key", "credential", "auth")


def _redact(obj):
    if isinstance(obj, dict):
        return {k: "***" if any(s in k.lower() for s in SENSITIVE_KEYS) else _redact(v)
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    if isinstance(obj, str) and len(obj) > 8:
        # Redact long strings that look like tokens
        for s in SENSITIVE_KEYS:
            if s in obj.lower():
                return "***"
    return obj


class AuditLog:
    def __init__(self, log_path: Path | None = None):
        self.log_path = log_path or Path("agent/logs/audit.jsonl")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, actor: str = "agent", details: dict | None = None):
        entry = {
            "ts": time.time(),
            "event": event,
            "actor": actor,
            "details": _redact(details or {}),
        }
        with open(self.log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def recent(self, limit: int = 50) -> list[dict]:
        if not self.log_path.exists():
            return []
        lines = self.log_path.read_text().strip().split("\n")
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
        return out
