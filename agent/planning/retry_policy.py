"""Retry policy — bounded retries for failed tasks (ChatGPT spec).

Per spec: agent/planning/retry_policy.py
Decides RETRY / SKIP / REPLAN / ABORT_PLAN per failure. No endless retries.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class RetryDecision(Enum):
    RETRY = "retry"            # try the same task again
    RETRY_MODIFIED = "retry_modified"  # retry with adjusted args
    SKIP = "skip"              # skip task, continue plan
    REPLAN = "replan"          # rebuild downstream plan
    ABORT_PLAN = "abort_plan"  # cancel everything
    ESCALATE = "escalate"      # needs user decision


@dataclass
class RetryPolicy:
    """Configurable bounded retry policy."""
    max_retries: int = 2
    backoff_base_seconds: float = 2.0
    backoff_max_seconds: float = 60.0
    # Error patterns that should never be retried
    fatal_patterns: List[str] = field(default_factory=lambda: [
        r"denied by policy",
        r"permission denied.*policy",
        r"container escape",
        r"authentication failed.*permanent",
    ])
    # Error patterns that suggest skipping
    skip_patterns: List[str] = field(default_factory=lambda: [
        r"not found.*optional",
        r"already exists",
        r"nothing to do",
    ])

    def max_retries_for(self, task: Any) -> int:
        """Per-task retry budget (critical tasks get more)."""
        priority = getattr(task, "priority", None)
        pname = getattr(priority, "name", "") or str(priority)
        if pname in ("CRITICAL", "URGENT"):
            return self.max_retries + 1
        return self.max_retries

    def backoff_for(self, attempt: int) -> float:
        """Exponential backoff with cap."""
        return min(self.backoff_max_seconds,
                   self.backoff_base_seconds * (2 ** attempt))

    def decide(self, task: Any, error: str) -> RetryDecision:
        """Decide what to do after a task failure."""
        err = error.lower()
        # Fatal: never retry
        for pat in self.fatal_patterns:
            if re.search(pat, err):
                return RetryDecision.ABORT_PLAN
        # Skippable
        for pat in self.skip_patterns:
            if re.search(pat, err):
                return RetryDecision.SKIP
        # Budget exhausted?
        retry_count = getattr(task, "retry_count", 0)
        max_r = self.max_retries_for(task)
        if retry_count >= max_r:
            # If task is on critical path, replan; else skip
            tags = getattr(task, "tags", []) or []
            if "critical" in tags:
                return RetryDecision.REPLAN
            return RetryDecision.SKIP
        # Transient errors → retry
        transient = ["timeout", "connection", "temporary", "rate limit",
                     "429", "503", "econnreset"]
        if any(t in err for t in transient):
            return RetryDecision.RETRY
        # Parse/arg errors → retry modified
        if any(t in err for t in ["invalid arg", "missing arg",
                                  "schema", "parse"]):
            return RetryDecision.RETRY_MODIFIED
        return RetryDecision.RETRY

    def should_escalate(self, task: Any, error: str) -> bool:
        """True when the failure needs a human decision."""
        err = error.lower()
        return any(k in err for k in [
            "approval", "confirm", "destructive", "rm -rf",
            "payment", "billing",
        ])


@dataclass
class RetryRecord:
    task_id: str
    attempt: int
    decision: str
    error: str
    backoff_seconds: float
    timestamp: float = field(default_factory=time.time)


class RetryTracker:
    """Records retry history per task."""

    def __init__(self):
        self._records: Dict[str, List[RetryRecord]] = {}

    def record(self, task_id: str, attempt: int,
               decision: RetryDecision, error: str,
               backoff: float) -> None:
        self._records.setdefault(task_id, []).append(RetryRecord(
            task_id=task_id, attempt=attempt,
            decision=decision.value, error=error[:300],
            backoff_seconds=backoff))

    def history(self, task_id: str) -> List[RetryRecord]:
        return list(self._records.get(task_id, []))

    def total_retries(self) -> int:
        return sum(len(v) for v in self._records.values())

    def summary(self) -> Dict[str, Any]:
        return {
            "tasks_retried": len(self._records),
            "total_retry_attempts": self.total_retries(),
            "by_task": {tid: len(recs)
                        for tid, recs in self._records.items()},
        }
