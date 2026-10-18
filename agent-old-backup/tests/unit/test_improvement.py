"""Unit tests for agent.improvement module."""
import unittest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

from agent.improvement.improvement_planner import (
    ImprovementPlanner, OpportunityType, HypothesisStatus,
)
from agent.improvement.benchmark import Benchmark
from agent.improvement.regression_suite import (
    RegressionSuite, SuiteResult, build_standard_suite,
)
from agent.improvement.approval_manager import (
    ApprovalManager, ApprovalStatus, ChangeRisk,
)
from agent.improvement.rollback import RollbackManager


class TestImprovementPlanner(unittest.TestCase):
    def test_observe_identify(self):
        p = ImprovementPlanner()
        p.observe("Error: connection failed", source="error_log")
        p.observe("Error: timeout", source="error_log")
        opps = p.identify()
        self.assertGreater(len(opps), 0)

    def test_hypothesize(self):
        p = ImprovementPlanner()
        p.observe("Slow query", source="benchmark")
        opps = p.identify()
        hyp = p.auto_hypothesize(opps[0])
        self.assertIsNotNone(hyp)
        self.assertEqual(hyp.status, HypothesisStatus.PROPOSED)

    def test_prioritized(self):
        p = ImprovementPlanner()
        p.observe("Error A", source="error_log")
        p.identify()
        pri = p.prioritized()
        self.assertGreater(len(pri), 0)


class TestBenchmark(unittest.TestCase):
    def test_run(self):
        b = Benchmark()
        r = b.run("test", lambda: sum(range(100)), runs=3)
        self.assertEqual(r.runs, 3)
        self.assertGreater(r.mean_ms, 0)
        self.assertEqual(r.success_rate, 1.0)

    def test_compare(self):
        b = Benchmark()
        base = b.run("op", lambda: None, runs=2)
        b.set_baseline(base)
        curr = b.run("op", lambda: None, runs=2)
        comp = b.compare(curr)
        self.assertIsNotNone(comp)
        self.assertIn(comp.verdict, ["improved", "regressed", "same"])


class TestRegressionSuite(unittest.TestCase):
    def test_all_pass(self):
        s = RegressionSuite("t")
        s.add("a", lambda: True, category="unit")
        s.add("b", lambda: True, category="unit")
        run = s.run()
        self.assertEqual(run.verdict, SuiteResult.PASS)
        self.assertTrue(s.gate(run))

    def test_failure(self):
        s = RegressionSuite("t")
        s.add("a", lambda: True)
        s.add("b", lambda: False)
        run = s.run()
        self.assertEqual(run.verdict, SuiteResult.PARTIAL)
        self.assertFalse(s.gate(run))

    def test_build_standard(self):
        s = build_standard_suite({
            "test_unit_a": lambda: True,
            "test_security_b": lambda: True,
        })
        self.assertEqual(len(s.cases), 2)


class TestApprovalManager(unittest.TestCase):
    def test_auto_approve(self):
        m = ApprovalManager(no_rules_mode=True)
        req = m.request("Test", "desc", "patch", risk=ChangeRisk.LOW)
        self.assertEqual(req.status, ApprovalStatus.AUTO_APPROVED)
        self.assertTrue(m.is_approved(req.id))

    def test_manual(self):
        m = ApprovalManager(no_rules_mode=False)
        req = m.request("Test", "desc", "patch", risk=ChangeRisk.HIGH)
        self.assertEqual(req.status, ApprovalStatus.PENDING)
        self.assertTrue(m.approve(req.id, by="tester"))
        self.assertTrue(m.is_approved(req.id))

    def test_reject(self):
        m = ApprovalManager(no_rules_mode=False)
        req = m.request("T", "d", "patch", risk=ChangeRisk.HIGH)
        self.assertTrue(m.reject(req.id, reason="no"))
        self.assertFalse(m.is_approved(req.id))


class TestRollbackManager(unittest.TestCase):
    def test_snapshot(self):
        import tempfile
        import subprocess
        # Create a temp git repo
        tmp = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", tmp], capture_output=True)
        subprocess.run(["git", "-C", tmp, "config", "user.email", "t@t"],
                       capture_output=True)
        subprocess.run(["git", "-C", tmp, "config", "user.name", "t"],
                       capture_output=True)
        with open(os.path.join(tmp, "f.txt"), "w") as f:
            f.write("hi")
        subprocess.run(["git", "-C", tmp, "add", "."],
                       capture_output=True)
        subprocess.run(["git", "-C", tmp, "commit", "-qm", "init"],
                       capture_output=True)
        rm = RollbackManager(tmp)
        snap = rm.snapshot("test")
        self.assertIsNotNone(snap)
        self.assertTrue(len(snap.ref) >= 7)


if __name__ == "__main__":
    unittest.main()
