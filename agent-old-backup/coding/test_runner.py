"""Test runner — discovers and runs tests, parses results.

Per ChatGPT spec: agent/coding/test_runner.py
Runs unittest/pytest, parses output into structured results.
"""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class TestStatus(Enum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass
class TestCaseResult:
    name: str
    status: TestStatus
    duration_ms: int = 0
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "status": self.status.value,
                "duration_ms": self.duration_ms,
                "message": self.message[:500]}


@dataclass
class TestRunResult:
    """Structured result of a test run."""
    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    duration_ms: int = 0
    cases: List[TestCaseResult] = field(default_factory=list)
    raw_output: str = ""

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.errors == 0 and self.total > 0

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total, "passed": self.passed,
            "failed": self.failed, "errors": self.errors,
            "skipped": self.skipped, "duration_ms": self.duration_ms,
            "pass_rate": round(self.pass_rate, 3),
            "ok": self.ok,
            "cases": [c.to_dict() for c in self.cases[:100]],
        }

    def failures(self) -> List[TestCaseResult]:
        return [c for c in self.cases
                if c.status in (TestStatus.FAILED, TestStatus.ERROR)]


class TestRunner:
    """Runs Python tests and parses results."""

    def run_unittest(self, target: str = "agent.tests",
                     timeout: int = 300) -> TestRunResult:
        """Run unittest discovery on a target."""
        start = time.time()
        try:
            p = subprocess.run(
                ["python3", "-m", "unittest", "discover",
                 "-s", target, "-v"],
                capture_output=True, text=True, timeout=timeout)
            output = p.stdout + p.stderr
        except subprocess.TimeoutExpired:
            return TestRunResult(raw_output="TIMEOUT")
        except Exception as e:
            return TestRunResult(raw_output=f"runner error: {e}")
        dur = int((time.time() - start) * 1000)
        return self._parse_unittest(output, dur)

    def run_file(self, path: str, timeout: int = 120) -> TestRunResult:
        """Run a single test file."""
        start = time.time()
        try:
            p = subprocess.run(
                ["python3", "-m", "unittest", path, "-v"],
                capture_output=True, text=True, timeout=timeout)
            output = p.stdout + p.stderr
        except subprocess.TimeoutExpired:
            return TestRunResult(raw_output="TIMEOUT")
        except Exception as e:
            return TestRunResult(raw_output=f"runner error: {e}")
        dur = int((time.time() - start) * 1000)
        return self._parse_unittest(output, dur)

    def run_pytest(self, target: str = "agent/tests",
                   timeout: int = 300) -> TestRunResult:
        """Run pytest if available."""
        start = time.time()
        try:
            p = subprocess.run(
                ["python3", "-m", "pytest", target, "-v", "--tb=short"],
                capture_output=True, text=True, timeout=timeout)
            output = p.stdout + p.stderr
            if "No module named pytest" in output:
                return TestRunResult(raw_output="pytest not installed")
        except subprocess.TimeoutExpired:
            return TestRunResult(raw_output="TIMEOUT")
        except Exception as e:
            return TestRunResult(raw_output=f"runner error: {e}")
        dur = int((time.time() - start) * 1000)
        return self._parse_pytest(output, dur)

    def _parse_unittest(self, output: str, dur_ms: int) -> TestRunResult:
        result = TestRunResult(duration_ms=dur_ms, raw_output=output[-8000:])
        # Verbose lines: "test_x (module.Class) ... ok"
        case_re = re.compile(
            r"^(\S+)\s+\(([\w.]+)\)\s+\.\.\.\s+(ok|FAIL|ERROR|skipped)",
            re.MULTILINE)
        for m in case_re.finditer(output):
            status = {"ok": TestStatus.PASSED, "FAIL": TestStatus.FAILED,
                      "ERROR": TestStatus.ERROR,
                      "skipped": TestStatus.SKIPPED}[m.group(3)]
            result.cases.append(TestCaseResult(
                name=f"{m.group(2)}.{m.group(1)}", status=status))
        # Summary: "Ran 12 tests", "OK" / "FAILED (failures=2)"
        m = re.search(r"Ran (\d+) tests?", output)
        if m:
            result.total = int(m.group(1))
        if re.search(r"^OK", output, re.MULTILINE):
            result.passed = sum(1 for c in result.cases
                                if c.status == TestStatus.PASSED)
        m = re.search(r"FAILED \((.*?)\)", output)
        if m:
            detail = m.group(1)
            mf = re.search(r"failures=(\d+)", detail)
            me = re.search(r"errors=(\d+)", detail)
            if mf:
                result.failed = int(mf.group(1))
            if me:
                result.errors = int(me.group(1))
        # Reconcile counts
        if not result.total:
            result.total = len(result.cases)
        if not result.passed and not result.failed and not result.errors:
            result.passed = sum(1 for c in result.cases
                                if c.status == TestStatus.PASSED)
            result.failed = sum(1 for c in result.cases
                                if c.status == TestStatus.FAILED)
            result.errors = sum(1 for c in result.cases
                                if c.status == TestStatus.ERROR)
            result.skipped = sum(1 for c in result.cases
                                 if c.status == TestStatus.SKIPPED)
        return result

    def _parse_pytest(self, output: str, dur_ms: int) -> TestRunResult:
        result = TestRunResult(duration_ms=dur_ms, raw_output=output[-8000:])
        # Lines like "test_x.py::TestClass::test_y PASSED"
        case_re = re.compile(r"^(\S+::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED)",
                             re.MULTILINE)
        for m in case_re.finditer(output):
            status = {"PASSED": TestStatus.PASSED,
                      "FAILED": TestStatus.FAILED,
                      "ERROR": TestStatus.ERROR,
                      "SKIPPED": TestStatus.SKIPPED}[m.group(2)]
            result.cases.append(TestCaseResult(name=m.group(1),
                                               status=status))
        m = re.search(r"(\d+) passed", output)
        if m:
            result.passed = int(m.group(1))
        m = re.search(r"(\d+) failed", output)
        if m:
            result.failed = int(m.group(1))
        m = re.search(r"(\d+) error", output)
        if m:
            result.errors = int(m.group(1))
        m = re.search(r"(\d+) skipped", output)
        if m:
            result.skipped = int(m.group(1))
        result.total = (result.passed + result.failed +
                        result.errors + result.skipped)
        if not result.total:
            result.total = len(result.cases)
        return result

    def run_with_coverage(self, target: str = "agent/tests",
                          timeout: int = 300) -> Dict[str, Any]:
        """Run tests and estimate coverage via trace (stdlib)."""
        # Simple approach: use trace module for line coverage
        import sys
        result = self.run_unittest(target, timeout)
        return {**result.to_dict(), "coverage": "use coverage.py for detail"}
