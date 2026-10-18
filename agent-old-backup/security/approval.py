"""Approval — records approval decisions for audit (ChatGPT spec).

Per ChatGPT spec: agent/security/approval.py
NOTE: Per ahsan's no-rules order, approvals are advisory only.
Nothing blocks except the 5 hard denials + rm -rf toggle.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


class ApprovalDecision(Enum):
    APPROVED = "approved"
    DENIED = "denied"
    AUTO = "auto"  # no-rules mode


@dataclass
class ApprovalRecord:
    id: str
    action: str
    detail: str
    decision: ApprovalDecision
    decided_by: str
    timestamp: float = field(default_factory=time.time)
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["decision"] = self.decision.value
        return d


class ApprovalLog:
    """Append-only log of approval decisions."""

    def __init__(self):
        self._records: List[ApprovalRecord] = []

    def log(self, action: str, detail: str,
            decision: ApprovalDecision,
            decided_by: str = "system",
            reason: str = "") -> ApprovalRecord:
        rec = ApprovalRecord(
            id=f"apl_{uuid.uuid4().hex[:8]}",
            action=action, detail=detail[:500],
            decision=decision, decided_by=decided_by,
            reason=reason)
        self._records.append(rec)
        return rec

    def auto_approve(self, action: str, detail: str = "") -> ApprovalRecord:
        return self.log(action, detail, ApprovalDecision.AUTO,
                        decided_by="auto (no-rules)",
                        reason="Per ahsan: no approval gates")

    def deny(self, action: str, detail: str,
             reason: str) -> ApprovalRecord:
        return self.log(action, detail, ApprovalDecision.DENIED,
                        decided_by="policy",
                        reason=reason)

    def history(self, limit: int = 100) -> List[ApprovalRecord]:
        return self._records[-limit:]

    def denials(self) -> List[ApprovalRecord]:
        return [r for r in self._records
                if r.decision == ApprovalDecision.DENIED]
