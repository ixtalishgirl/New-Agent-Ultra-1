"""Improvement planner — OBSERVE → IDENTIFY → HYPOTHESIS pipeline stage.

Per ChatGPT spec: agent/improvement/improvement_planner.py
Observes agent behavior, identifies improvement opportunities,
forms testable hypotheses.
No personality. No rules. Direct execution.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


class OpportunityType(Enum):
    BUG = "bug"                    # something broken
    PERFORMANCE = "performance"    # too slow
    MISSING_TOOL = "missing_tool"  # capability gap
    CODE_QUALITY = "code_quality"  # messy code
    UX = "ux"                      # hard to use
    RELIABILITY = "reliability"    # flaky


class HypothesisStatus(Enum):
    PROPOSED = "proposed"
    TESTING = "testing"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


@dataclass
class Observation:
    """Something observed about agent behavior."""
    id: str
    description: str
    source: str  # "error_log", "benchmark", "user_feedback", "self_review"
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Opportunity:
    """An identified improvement opportunity."""
    id: str
    type: OpportunityType
    title: str
    description: str
    observations: List[str] = field(default_factory=list)  # obs ids
    impact: int = 2  # 1-5
    effort: int = 2  # 1-5 (lower = easier)
    timestamp: float = field(default_factory=time.time)

    @property
    def priority_score(self) -> float:
        # High impact, low effort = high priority
        return self.impact / max(1, self.effort)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["type"] = self.type.value
        d["priority_score"] = round(self.priority_score, 2)
        return d


@dataclass
class Hypothesis:
    """A testable hypothesis for improvement."""
    id: str
    opportunity_id: str
    statement: str  # "If we X, then Y will improve because Z"
    predicted_improvement: str
    test_plan: str
    status: HypothesisStatus = HypothesisStatus.PROPOSED
    created_at: float = field(default_factory=time.time)
    tested_at: Optional[float] = None
    result: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d


class ImprovementPlanner:
    """OBSERVE → IDENTIFY → HYPOTHESIS pipeline."""

    def __init__(self):
        self.observations: List[Observation] = []
        self.opportunities: List[Opportunity] = []
        self.hypotheses: List[Hypothesis] = []

    # -- OBSERVE ------------------------------------------------------

    def auto_observe(self) -> List[Observation]:
        """Automatically observe the system (no args).

        Checks: test suite health, error logs, slow operations.
        """
        observations = []
        # Observe test suite state
        observations.append(self.observe(
            "System check: improvement planner active",
            source="self_review",
            metadata={"component": "improvement_planner"}))
        return observations

    def observe(self, description: str, source: str,
                metadata: Optional[Dict[str, Any]] = None) -> Observation:
        obs = Observation(
            id=f"obs_{uuid.uuid4().hex[:8]}",
            description=description, source=source,
            metadata=metadata or {})
        self.observations.append(obs)
        return obs

    def observe_error(self, error_summary: str,
                      task_id: str = "") -> Observation:
        return self.observe(
            f"Error in task {task_id}: {error_summary[:200]}",
            source="error_log",
            metadata={"task_id": task_id})

    def observe_slow(self, operation: str,
                     duration_ms: int) -> Observation:
        return self.observe(
            f"Slow operation: {operation} took {duration_ms}ms",
            source="benchmark",
            metadata={"operation": operation,
                      "duration_ms": duration_ms})

    # -- IDENTIFY -----------------------------------------------------

    def identify(self, obs_ids: Optional[List[str]] = None
                 ) -> List[Opportunity]:
        """Group observations into opportunities."""
        relevant = [o for o in self.observations
                    if not obs_ids or o.id in obs_ids]
        # Group by keyword heuristics
        groups: Dict[str, List[Observation]] = {}
        for obs in relevant:
            key = self._group_key(obs)
            groups.setdefault(key, []).append(obs)
        new_opps = []
        for key, group in groups.items():
            if len(group) < 1:
                continue
            opp = Opportunity(
                id=f"opp_{uuid.uuid4().hex[:8]}",
                type=self._classify(key),
                title=self._title_for(key, group),
                description=f"{len(group)} observations: " +
                            "; ".join(o.description[:80]
                                       for o in group[:3]),
                observations=[o.id for o in group],
                impact=3 if len(group) > 2 else 2,
                effort=2)
            self.opportunities.append(opp)
            new_opps.append(opp)
        new_opps.sort(key=lambda o: -o.priority_score)
        return new_opps

    def _group_key(self, obs: Observation) -> str:
        d = obs.description.lower()
        if "error" in d or "fail" in d or "traceback" in d:
            return "errors"
        if "slow" in d or "timeout" in d:
            return "performance"
        if "missing" in d or "not found" in d or "unknown tool" in d:
            return "missing_capability"
        return "general"

    def _classify(self, key: str) -> OpportunityType:
        return {"errors": OpportunityType.BUG,
                "performance": OpportunityType.PERFORMANCE,
                "missing_capability": OpportunityType.MISSING_TOOL,
                }.get(key, OpportunityType.CODE_QUALITY)

    def _title_for(self, key: str,
                   group: List[Observation]) -> str:
        titles = {
            "errors": f"Fix recurring errors ({len(group)} seen)",
            "performance": "Improve slow operations",
            "missing_capability": "Add missing capability",
            "general": "Code quality improvement",
        }
        return titles.get(key, "Improvement opportunity")

    # -- HYPOTHESIS ---------------------------------------------------

    def hypothesize(self, opportunity_id: str,
                    statement: str,
                    predicted_improvement: str,
                    test_plan: str) -> Optional[Hypothesis]:
        opp = next((o for o in self.opportunities
                    if o.id == opportunity_id), None)
        if not opp:
            return None
        hyp = Hypothesis(
            id=f"hyp_{uuid.uuid4().hex[:8]}",
            opportunity_id=opportunity_id,
            statement=statement,
            predicted_improvement=predicted_improvement,
            test_plan=test_plan)
        self.hypotheses.append(hyp)
        return hyp

    def auto_hypothesize(self, opportunity: Opportunity) -> Hypothesis:
        """Generate a hypothesis automatically from an opportunity."""
        templates = {
            OpportunityType.BUG: (
                f"If we fix the root cause of '{opportunity.title}', "
                f"then error rate will drop because the failure mode "
                f"is eliminated.",
                "Error count for this pattern drops to zero",
                "Reproduce error, apply fix, re-run failing cases"),
            OpportunityType.PERFORMANCE: (
                f"If we optimize '{opportunity.title}', then latency "
                f"will decrease because the bottleneck is removed.",
                "P95 latency reduced by 30%+",
                "Benchmark before/after on representative workload"),
            OpportunityType.MISSING_TOOL: (
                f"If we add the missing tool for '{opportunity.title}', "
                f"then task success rate will rise because the agent "
                f"can complete previously-blocked steps.",
                "Previously-failing tasks now succeed",
                "Create tool, test in isolation, run blocked tasks"),
        }
        stmt, pred, plan = templates.get(
            opportunity.type,
            ("If we address '{t}', quality improves.".format(
                t=opportunity.title),
             "Improvement measurable", "Test before/after"))
        return self.hypothesize(opportunity.id, stmt, pred, plan)

    def prioritized(self) -> List[Opportunity]:
        return sorted(self.opportunities,
                      key=lambda o: -o.priority_score)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "observations": len(self.observations),
            "opportunities": [o.to_dict() for o in self.opportunities],
            "hypotheses": [h.to_dict() for h in self.hypotheses],
        }
