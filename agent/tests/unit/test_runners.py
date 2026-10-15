"""Smoke tests for the sandboxed execution runners."""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from agent.execution.shell_runner import run_shell
from agent.execution.python_runner import run_python


class TestRunners(unittest.TestCase):
    def test_shell_echo_runs(self):
        r = run_shell("echo smoke123")
        self.assertTrue(r.ok, r.stderr)
        self.assertIn("smoke123", r.stdout)

    def test_shell_destructive_blocked_without_flag(self):
        r = run_shell("rm -rf /tmp/this-should-never-run")
        self.assertFalse(r.ok)
        self.assertEqual(r.exit_code, 126)
        self.assertIn("BLOCKED", r.stderr)

    def test_python_runner_basic(self):
        r = run_python("print(2 + 2)")
        self.assertTrue(r.ok, r.stderr)
        self.assertIn("4", r.stdout)


if __name__ == "__main__":
    unittest.main()
