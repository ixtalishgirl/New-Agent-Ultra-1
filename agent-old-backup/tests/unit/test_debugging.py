"""Unit tests for agent.debugging module."""
import unittest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

from agent.debugging import (
    ErrorAnalyzer, ErrorClassifier, ErrorAnalysis, ErrorCategory,
    AutoFixer, FixApplier, Fix, FixAction,
    RegressionDetector, RegressionResult, RegressionSeverity,
    RegressionSuite, TestCase,
)


class TestErrorAnalyzer(unittest.TestCase):
    def setUp(self):
        self.analyzer = ErrorAnalyzer()

    def test_import_error(self):
        out = ('Traceback (most recent call last):\n'
               '  File "x.py", line 1, in <module>\n'
               "ModuleNotFoundError: No module named 'requests'\n")
        a = self.analyzer.analyze(out)
        self.assertEqual(a.category, ErrorCategory.IMPORT)
        self.assertIn("requests", a.suggested_fix)

    def test_name_error(self):
        out = ('Traceback (most recent call last):\n'
               '  File "x.py", line 5, in <module>\n'
               "NameError: name 'foo' is not defined\n")
        a = self.analyzer.analyze(out)
        self.assertEqual(a.category, ErrorCategory.NAME)

    def test_syntax_error(self):
        out = ('  File "x.py", line 3\n'
               '    def foo(\n'
               '            ^\n'
               'SyntaxError: invalid syntax\n')
        a = self.analyzer.analyze(out)
        # May parse as unknown if no traceback header; check it doesn't crash
        self.assertIsInstance(a, ErrorAnalysis)

    def test_policy_denied(self):
        a = self.analyzer.analyze("DENIED by policy 'container_escape'")
        self.assertEqual(a.category, ErrorCategory.POLICY_DENIED)
        self.assertFalse(a.retriable)

    def test_command_not_found(self):
        a = self.analyzer.analyze("foobar: command not found")
        self.assertIn("foobar", a.message)

    def test_timeout(self):
        a = self.analyzer.analyze("Operation TIMEOUT after 60s")
        self.assertEqual(a.category, ErrorCategory.TIMEOUT)
        self.assertTrue(a.retriable)

    def test_unknown_tool(self):
        a = self.analyzer.analyze("Unknown or disabled tool: frobnicate")
        self.assertEqual(a.category, ErrorCategory.TOOL_UNKNOWN)

    def test_fingerprint(self):
        c = ErrorClassifier()
        fp1 = c.fingerprint("Error: foo 'bar' failed at 123")
        fp2 = c.fingerprint("Error: foo 'baz' failed at 456")
        self.assertEqual(fp1, fp2)  # normalized


class TestAutoFixer(unittest.TestCase):
    def setUp(self):
        self.analyzer = ErrorAnalyzer()
        self.fixer = AutoFixer()

    def test_fix_import(self):
        a = self.analyzer.analyze(
            "ModuleNotFoundError: No module named 'requests'")
        fixes = self.fixer.fix(a)
        self.assertGreater(len(fixes), 0)
        self.assertEqual(fixes[0].action, FixAction.INSTALL_PACKAGE)
        self.assertEqual(fixes[0].package, "requests")

    def test_fix_undefined_name(self):
        out = "NameError: name 'json' is not defined"
        a = self.analyzer.analyze(out)
        fixes = self.fixer.fix(a, {"code": "print(json.dumps({}))"})
        self.assertTrue(any(f.action == FixAction.PATCH_CODE
                            for f in fixes))

    def test_fix_timeout(self):
        a = self.analyzer.analyze("TIMEOUT")
        fixes = self.fixer.fix(a)
        self.assertTrue(any(f.action == FixAction.RETRY_WITH_TIMEOUT
                            for f in fixes))

    def test_policy_no_fix(self):
        a = self.analyzer.analyze("DENIED by policy")
        fixes = self.fixer.fix(a)
        self.assertEqual(fixes[0].action, FixAction.SKIP)

    def test_apply_patch(self):
        applier = FixApplier()
        code = "print('hi')\n"
        fix = Fix(action=FixAction.PATCH_CODE, description="add import",
                  new="import json\n",
                  metadata={"insert_at_top": True})
        result = applier.apply(code, fix)
        self.assertIn("import json", result)
        self.assertIn("print('hi')", result)


class TestRegressionDetector(unittest.TestCase):
    def test_no_regression(self):
        d = RegressionDetector()
        case = TestCase(id="c1", name="echo",
                        run=lambda: "hello")
        d.capture_baseline(case)
        r = d.check(case)
        self.assertEqual(r.severity, RegressionSeverity.NONE)

    def test_regression_detected(self):
        d = RegressionDetector()
        outputs = ["hello", ""]  # second run returns empty
        calls = [0]
        def run():
            calls[0] += 1
            return outputs[min(calls[0] - 1, 1)]
        case = TestCase(id="c2", name="flaky", run=run)
        d.capture_baseline(case)
        r = d.check(case)
        self.assertNotEqual(r.severity, RegressionSeverity.NONE)

    def test_suite(self):
        from agent.debugging.regression_detector import RegressionSuite
        d = RegressionDetector()
        suite = RegressionSuite("test")
        suite.add(TestCase(id="a", name="A", run=lambda: "ok"))
        d.capture_baseline(suite.cases[0])
        result = suite.run(d)
        self.assertEqual(result["passed"], 1)


if __name__ == "__main__":
    unittest.main()
