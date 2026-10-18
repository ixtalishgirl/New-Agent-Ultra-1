"""Regression suite — REGRESSION stage of self-improvement.

Per ChatGPT spec: agent/improvement/regression_suite.py
Runs the full regression battery after a change. Any failure → reject.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class SuiteResult(Enum):
    PASS = "pass"
    FAIL = "fail"
    PARTIAL = "partial"  # some skipped


@dataclass
class SuiteCase:
    name: str
    fn: Callable[[], bool]  # returns True on pass
    category: str = "general"  # unit, integration, security, regression
    timeout: int = 120


@dataclass
class SuiteRun:
    suite_name: str
    results: Dict[str, bool] = field(default_factory=dict)
    durations_ms: Dict[str, float] = field(default_factory=dict)
    errors: Dict[str, str] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None

    @property
    def passed(self) -> int:
        return sum(1 for v in self.results.values() if v)

    @property
    def failed(self) -> int:
        return sum(1 for v in self.results.values() if not v)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def verdict(self) -> SuiteResult:
        if self.failed == 0:
            return SuiteResult.PASS
        if self.passed == 0:
            return SuiteResult.FAIL
        return SuiteResult.PARTIAL

    def to_dict(self) -> Dict[str, Any]:
        return {
            "suite": self.suite_name,
            "total": self.total, "passed": self.passed,
            "failed": self.failed,
            "verdict": self.verdict.value,
            "failed_cases": [k for k, v in self.results.items()
                             if not v],
            "errors": {k: v[:300] for k, v in self.errors.items()},
        }


class RegressionSuite:
    """Runs categorized regression suites."""

    def __init__(self, name: str = "full"):
        self.name = name
        self.cases: List[SuiteCase] = []

    def add(self, name: str, fn: Callable[[], bool],
            category: str = "general", timeout: int = 120) -> None:
        self.cases.append(SuiteCase(name, fn, category, timeout))

    def add_many(self, cases: List[SuiteCase]) -> None:
        self.cases.extend(cases)

    def run(self, categories: Optional[List[str]] = None,
            stop_on_first_failure: bool = False) -> SuiteRun:
        run = SuiteRun(suite_name=self.name)
        for case in self.cases:
            if categories and case.category not in categories:
                continue
            start = time.time()
            try:
                # Timeout via signal would need main thread; use best effort
                ok = bool(case.fn())
                run.results[case.name] = ok
            except Exception as e:
                run.results[case.name] = False
                run.errors[case.name] = str(e)[:500]
            run.durations_ms[case.name] = (time.time() - start) * 1000
            if stop_on_first_failure and not run.results[case.name]:
                break
        run.completed_at = time.time()
        return run

    def run_category(self, category: str) -> SuiteRun:
        return self.run(categories=[category])

    def gate(self, run: SuiteRun) -> bool:
        """True if the change may proceed (no failures)."""
        return run.verdict == SuiteResult.PASS


def build_standard_suite(checks: Dict[str, Callable[[], bool]]) -> RegressionSuite:
    """Build a suite from a name->check dict."""
    suite = RegressionSuite("standard")
    for name, fn in checks.items():
        # Categorize by name prefix
        cat = "general"
        for prefix, c in [("test_unit", "unit"), ("test_integration", "integration"),
                          ("test_security", "security"),
                          ("test_regression", "regression")]:
            if name.startswith(prefix):
                cat = c
                break
        suite.add(name, fn, category=cat)
    return suite
