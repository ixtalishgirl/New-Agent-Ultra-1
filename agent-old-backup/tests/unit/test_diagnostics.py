"""Tests for diagnostics."""
import unittest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

from agent.app.diagnostics import (
    Diagnostics, CheckResult, CheckStatus,
)


class TestCheckResult(unittest.TestCase):
    def test_to_dict(self):
        r = CheckResult(name="test", status=CheckStatus.PASS,
                        message="ok")
        d = r.to_dict()
        self.assertEqual(d["status"], "pass")
        self.assertEqual(d["name"], "test")


class TestDiagnostics(unittest.TestCase):
    def setUp(self):
        repo = os.path.join(os.path.dirname(__file__), "../../..")
        self.diag = Diagnostics(repo)

    def test_python_check(self):
        r = self.diag.check_python()
        self.assertEqual(r.status, CheckStatus.PASS)
        self.assertIn("Python", r.message)

    def test_platform_check(self):
        r = self.diag.check_platform()
        self.assertEqual(r.status, CheckStatus.PASS)

    def test_packages_check(self):
        r = self.diag.check_packages()
        self.assertIn(r.status, [CheckStatus.PASS, CheckStatus.WARN])

    def test_repo_structure(self):
        r = self.diag.check_repo_structure()
        self.assertEqual(r.status, CheckStatus.PASS)

    def test_tool_registry(self):
        r = self.diag.check_tool_registry()
        self.assertEqual(r.status, CheckStatus.PASS)
        self.assertGreater(r.details["count"], 0)

    def test_memory(self):
        r = self.diag.check_memory()
        self.assertEqual(r.status, CheckStatus.PASS)

    def test_disk(self):
        r = self.diag.check_disk()
        self.assertIn(r.status, [CheckStatus.PASS, CheckStatus.WARN])

    def test_permissions(self):
        r = self.diag.check_permissions()
        self.assertEqual(r.status, CheckStatus.PASS)
        self.assertEqual(len(r.details["denials"]), 5)

    def test_run_all(self):
        results = self.diag.run_all()
        self.assertGreater(len(results), 5)
        # All should have duration
        for r in results:
            self.assertGreaterEqual(r.duration_ms, 0)

    def test_summary(self):
        self.diag.run_all()
        s = self.diag.summary()
        self.assertIn("total", s)
        self.assertIn("by_status", s)
        self.assertIn("healthy", s)

    def test_fix_suggestions(self):
        self.diag.run_all()
        suggestions = self.diag.fix_suggestions()
        self.assertIsInstance(suggestions, list)


if __name__ == "__main__":
    unittest.main()
