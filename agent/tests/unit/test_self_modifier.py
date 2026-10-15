"""Tests for self-modification engine."""
import unittest
import sys
import os
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

from agent.improvement.self_modifier import (
    SelfModifier, CodePatch, PatchStatus, PatchType,
    Baseline, GitHelper, PatchApplier, BenchmarkRunner,
    TestRunner,
)
from agent.improvement import ImprovementPlanner


class TestCodePatch(unittest.TestCase):
    def test_create(self):
        p = CodePatch(
            id="p1", type=PatchType.BUG_FIX,
            file="test.py", old_code="a", new_code="b",
            reason="fix")
        self.assertEqual(p.status, PatchStatus.PROPOSED)
        d = p.to_dict()
        self.assertEqual(d["type"], "bug_fix")
        self.assertEqual(d["status"], "proposed")

    def test_patch_types(self):
        for pt in PatchType:
            p = CodePatch(id="x", type=pt, file="f",
                          old_code="", new_code="", reason="")
            self.assertEqual(p.type, pt)


class TestPatchApplier(unittest.TestCase):
    def test_apply_success(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py",
                                         delete=False) as f:
            f.write("x = 1\ny = 2\n")
            path = f.name
        try:
            ok, msg = PatchApplier.apply(path, "x = 1", "x = 10")
            self.assertTrue(ok)
            with open(path) as f:
                self.assertIn("x = 10", f.read())
        finally:
            os.unlink(path)

    def test_apply_not_found(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py",
                                         delete=False) as f:
            f.write("x = 1\n")
            path = f.name
        try:
            ok, msg = PatchApplier.apply(path, "zzz = 999",
                                         "a = 1")
            self.assertFalse(ok)
            self.assertIn("not found", msg.lower())
        finally:
            os.unlink(path)

    def test_apply_missing_file(self):
        ok, msg = PatchApplier.apply("/tmp/nope_xyz.py", "a", "b")
        self.assertFalse(ok)

    def test_verify_syntax_ok(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py",
                                         delete=False) as f:
            f.write("x = 1\nprint(x)\n")
            path = f.name
        try:
            ok, msg = PatchApplier.verify_syntax(path)
            self.assertTrue(ok)
        finally:
            os.unlink(path)

    def test_verify_syntax_fail(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py",
                                         delete=False) as f:
            f.write("def foo(:\n")
            path = f.name
        try:
            ok, msg = PatchApplier.verify_syntax(path)
            self.assertFalse(ok)
            self.assertIn("Syntax", msg)
        finally:
            os.unlink(path)


class TestBenchmarkRunner(unittest.TestCase):
    def test_benchmark(self):
        b = BenchmarkRunner()
        result = b.benchmark("test", lambda: sum(range(100)),
                             iterations=10)
        self.assertIn("mean_ms", result)
        self.assertIn("p95_ms", result)
        self.assertEqual(result["iterations"], 10)
        self.assertGreaterEqual(result["min_ms"], 0)

    def test_compare_improved(self):
        before = {"mean_ms": 100.0}
        after = {"mean_ms": 50.0}
        cmp = BenchmarkRunner.compare(before, after)
        self.assertTrue(cmp["improved"])
        self.assertEqual(cmp["improvement_pct"], 50.0)

    def test_compare_regression(self):
        before = {"mean_ms": 50.0}
        after = {"mean_ms": 100.0}
        cmp = BenchmarkRunner.compare(before, after)
        self.assertTrue(cmp["regression"])

    def test_compare_no_baseline(self):
        cmp = BenchmarkRunner.compare({}, {"mean_ms": 10})
        self.assertFalse(cmp["improved"])


class TestGitHelper(unittest.TestCase):
    def test_current_branch(self):
        g = GitHelper("/tmp")
        # Should not crash even outside a repo
        branch = g.current_branch()
        self.assertIsInstance(branch, str)

    def test_current_commit(self):
        g = GitHelper("/tmp")
        commit = g.current_commit()
        self.assertIsInstance(commit, str)


class TestSelfModifier(unittest.TestCase):
    def test_propose(self):
        sm = SelfModifier(repo_path="/tmp", auto_approve=True)
        p = sm.propose(PatchType.BUG_FIX, "test.py",
                       "old", "new", "test reason")
        self.assertEqual(p.status, PatchStatus.PROPOSED)
        self.assertEqual(len(sm.patches), 1)

    def test_summary_empty(self):
        sm = SelfModifier(repo_path="/tmp")
        s = sm.summary()
        self.assertEqual(s["total"], 0)
        self.assertEqual(s["merged"], 0)

    def test_max_attempts(self):
        self.assertEqual(SelfModifier.MAX_ATTEMPTS, 3)


class TestImprovementPlannerAuto(unittest.TestCase):
    def test_auto_observe(self):
        planner = ImprovementPlanner()
        obs = planner.auto_observe()
        self.assertGreater(len(obs), 0)
        self.assertTrue(all(hasattr(o, "description") for o in obs))

    def test_identify_from_auto(self):
        planner = ImprovementPlanner()
        planner.auto_observe()
        opps = planner.identify()
        self.assertIsInstance(opps, list)


if __name__ == "__main__":
    unittest.main()
