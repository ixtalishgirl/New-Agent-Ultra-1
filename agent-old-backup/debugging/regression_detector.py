"""Regression detector — detects behavior regressions between versions.

Compares baseline vs current outputs, finds changed behavior,
and supports bisection to find the culprit commit.
No personality. No rules. Direct execution.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class RegressionSeverity(Enum):
    NONE = "none"
    MINOR = "minor"      # cosmetic output changes
    MAJOR = "major"      # behavior change, still works
    CRITICAL = "critical"  # broken functionality


@dataclass
class TestCase:
    """A regression test case: input + expected output."""
    id: str
    name: str
    run: Callable[[], str]  # returns output string
    normalize: Optional[Callable[[str], str]] = None

    def execute(self) -> str:
        out = self.run()
        if self.normalize:
            out = self.normalize(out)
        return out


@dataclass
class RegressionResult:
    case_id: str
    case_name: str
    severity: RegressionSeverity
    baseline_hash: str
    current_hash: str
    diff: str = ""
    baseline_output: str = ""
    current_output: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity.value
        return d


class RegressionDetector:
    """Detects regressions by comparing outputs against baselines."""

    def __init__(self):
        self._baselines: Dict[str, str] = {}  # case_id -> output hash
        self._baseline_outputs: Dict[str, str] = {}

    def capture_baseline(self, case: TestCase) -> str:
        """Run case and store output as baseline. Returns hash."""
        out = case.execute()
        h = self._hash(out)
        self._baselines[case.id] = h
        self._baseline_outputs[case.id] = out
        return h

    def check(self, case: TestCase) -> RegressionResult:
        """Run case and compare against baseline."""
        current = case.execute()
        current_hash = self._hash(current)
        baseline_hash = self._baselines.get(case.id, "")
        baseline_out = self._baseline_outputs.get(case.id, "")
        if not baseline_hash:
            return RegressionResult(
                case_id=case.id, case_name=case.name,
                severity=RegressionSeverity.NONE,
                baseline_hash="", current_hash=current_hash,
                current_output=current[:2000])
        if baseline_hash == current_hash:
            return RegressionResult(
                case_id=case.id, case_name=case.name,
                severity=RegressionSeverity.NONE,
                baseline_hash=baseline_hash,
                current_hash=current_hash)
        # Outputs differ — classify severity
        diff = "\n".join(difflib.unified_diff(
            baseline_out.splitlines(), current.splitlines(),
            lineterm="", n=2))[:3000]
        severity = self._classify(baseline_out, current)
        return RegressionResult(
            case_id=case.id, case_name=case.name,
            severity=severity,
            baseline_hash=baseline_hash,
            current_hash=current_hash,
            diff=diff,
            baseline_output=baseline_out[:2000],
            current_output=current[:2000])

    def check_all(self, cases: List[TestCase]) -> List[RegressionResult]:
        return [self.check(c) for c in cases]

    def _hash(self, text: str) -> str:
        return hashlib.sha256(text.encode(errors="ignore")).hexdigest()[:16]

    def _classify(self, baseline: str, current: str) -> RegressionSeverity:
        # Heuristics
        if not current.strip():
            return RegressionSeverity.CRITICAL  # was output, now empty
        if "traceback" in current.lower() and "traceback" not in baseline.lower():
            return RegressionSeverity.CRITICAL
        if "error" in current.lower().split() and len(current) < len(baseline) // 2:
            return RegressionSeverity.MAJOR
        # Minor: whitespace/formatting only
        if baseline.split() == current.split():
            return RegressionSeverity.MINOR
        # Measure change ratio
        sm = difflib.SequenceMatcher(None, baseline, current)
        ratio = sm.ratio()
        if ratio > 0.9:
            return RegressionSeverity.MINOR
        if ratio > 0.5:
            return RegressionSeverity.MAJOR
        return RegressionSeverity.CRITICAL

    def save_baselines(self, path: str) -> None:
        with open(path, "w") as f:
            json.dump({
                "baselines": self._baselines,
                "outputs": {k: v[:5000] for k, v in
                            self._baseline_outputs.items()},
            }, f, indent=2)

    def load_baselines(self, path: str) -> None:
        with open(path) as f:
            data = json.load(f)
        self._baselines = data.get("baselines", {})
        self._baseline_outputs = data.get("outputs", {})


class Bisector:
    """Bisects a commit range to find the regression culprit.

    Uses git to checkout commits and run a test case at each step.
    """

    def __init__(self, repo_dir: str = "."):
        self.repo_dir = repo_dir

    def _git(self, *args: str, timeout: int = 30) -> Tuple[bool, str]:
        import subprocess
        try:
            p = subprocess.run(
                ["git", "-C", self.repo_dir, *args],
                capture_output=True, text=True, timeout=timeout)
            return p.returncode == 0, (p.stdout + p.stderr)[:4000]
        except Exception as e:
            return False, str(e)

    def find_culprit(self, case: TestCase,
                     good_ref: str, bad_ref: str = "HEAD",
                     max_steps: int = 10) -> Dict[str, Any]:
        """Binary search for the first bad commit."""
        ok, out = self._git("rev-list", f"{good_ref}..{bad_ref}")
        if not ok:
            return {"error": f"rev-list failed: {out[:200]}"}
        commits = [c for c in out.split() if c]
        if not commits:
            return {"error": "no commits in range"}
        commits.reverse()  # oldest first
        original, _ = self._git("rev-parse", "HEAD")
        original = original.strip()
        try:
            lo, hi = 0, len(commits) - 1
            culprit = commits[hi]
            steps = 0
            while lo <= hi and steps < max_steps:
                steps += 1
                mid = (lo + hi) // 2
                self._git("checkout", "--quiet", commits[mid])
                try:
                    out_now = case.execute()
                except Exception as e:
                    out_now = f"ERROR: {e}"
                # Compare with baseline behavior: use case's own check
                # Simplified: non-empty output without traceback = good
                good = bool(out_now.strip()) and "traceback" not in out_now.lower()
                if good:
                    lo = mid + 1
                else:
                    culprit = commits[mid]
                    hi = mid - 1
            # Get culprit info
            _, info = self._git("log", "--oneline", "-1", culprit)
            return {
                "culprit": culprit,
                "info": info.strip(),
                "steps": steps,
                "commits_searched": len(commits),
            }
        finally:
            self._git("checkout", "--quiet", original)


@dataclass
class RegressionSuite:
    """A named suite of regression test cases."""
    name: str
    cases: List[TestCase] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def add(self, case: TestCase) -> None:
        self.cases.append(case)

    def run(self, detector: RegressionDetector
            ) -> Dict[str, Any]:
        results = detector.check_all(self.cases)
        by_sev: Dict[str, int] = {}
        for r in results:
            k = r.severity.value
            by_sev[k] = by_sev.get(k, 0) + 1
        return {
            "suite": self.name,
            "total": len(results),
            "by_severity": by_sev,
            "regressions": [r.to_dict() for r in results
                            if r.severity != RegressionSeverity.NONE],
            "passed": by_sev.get("none", 0),
        }
