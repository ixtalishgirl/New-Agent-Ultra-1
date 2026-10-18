"""Approval manager — REVIEW → APPROVAL stage of self-improvement.

Per ChatGPT spec: agent/improvement/approval_manager.py
NOTE: Per ahsan's order (no rules), approvals are advisory/logging only.
The agent does NOT block on approval except for the 5 hard denials
(enforced at execution layer) and the rm -rf toggle (his explicit order).
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


class ApprovalStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    AUTO_APPROVED = "auto_approved"  # no-rules mode
    EXPIRED = "expired"


class ChangeRisk(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class ApprovalRequest:
    """A request for change approval."""
    id: str
    title: str
    description: str
    change_type: str  # "patch", "tool_create", "deploy", "config"
    risk: ChangeRisk = ChangeRisk.LOW
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: float = field(default_factory=time.time)
    decided_at: Optional[float] = None
    decided_by: str = ""
    reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["risk"] = self.risk.value
        d["status"] = self.status.value
        return d


class ApprovalManager:
    """Tracks approvals. In no-rules mode, low-risk changes auto-approve."""

    # Per ahsan: no approval gates except 5 denials + rm -rf toggle.
    # This manager therefore auto-approves everything except what the
    # execution layer denies. It exists for audit completeness.
    AUTO_APPROVE_RISKS = {ChangeRisk.LOW, ChangeRisk.MEDIUM}

    def __init__(self, no_rules_mode: bool = True):
        self.no_rules_mode = no_rules_mode
        self._requests: Dict[str, ApprovalRequest] = {}

    def request(self, title: str, description: str,
                change_type: str,
                risk: ChangeRisk = ChangeRisk.LOW,
                metadata: Optional[Dict[str, Any]] = None) -> ApprovalRequest:
        req = ApprovalRequest(
            id=f"apr_{uuid.uuid4().hex[:8]}",
            title=title, description=description,
            change_type=change_type, risk=risk,
            metadata=metadata or {})
        # No-rules mode: auto-approve low/medium risk
        if self.no_rules_mode and risk in self.AUTO_APPROVE_RISKS:
            req.status = ApprovalStatus.AUTO_APPROVED
            req.decided_at = time.time()
            req.decided_by = "auto (no-rules mode)"
            req.reason = "Auto-approved: no rules per ahsan's order"
        self._requests[req.id] = req
        return req

    def approve(self, request_id: str, by: str = "ahsan",
                reason: str = "") -> bool:
        req = self._requests.get(request_id)
        if not req or req.status != ApprovalStatus.PENDING:
            return False
        req.status = ApprovalStatus.APPROVED
        req.decided_at = time.time()
        req.decided_by = by
        req.reason = reason
        return True

    def reject(self, request_id: str, by: str = "ahsan",
               reason: str = "") -> bool:
        req = self._requests.get(request_id)
        if not req or req.status != ApprovalStatus.PENDING:
            return False
        req.status = ApprovalStatus.REJECTED
        req.decided_at = time.time()
        req.decided_by = by
        req.reason = reason
        return True

    def is_approved(self, request_id: str) -> bool:
        req = self._requests.get(request_id)
        return req is not None and req.status in (
            ApprovalStatus.APPROVED, ApprovalStatus.AUTO_APPROVED)

    def pending(self) -> List[ApprovalRequest]:
        return [r for r in self._requests.values()
                if r.status == ApprovalStatus.PENDING]

    def history(self, limit: int = 50) -> List[ApprovalRequest]:
        reqs = list(self._requests.values())
        reqs.sort(key=lambda r: -r.created_at)
        return reqs[:limit]

    def stats(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for r in self._requests.values():
            k = r.status.value
            counts[k] = counts.get(k, 0) + 1
        return counts
