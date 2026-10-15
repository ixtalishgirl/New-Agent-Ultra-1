"""Integration smoke tests — every agent module must import cleanly
(no broken imports like the old Phase-14 bug).
"""
import sys
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

MODULES = [
    "agent.app.main",
    "agent.app.config",
    "agent.app.orchestrator",
    "agent.app.task_tracker",
    "agent.model.provider",
    "agent.model.qwen_adapter",
    "agent.model.output_parser",
    "agent.model.prompt_builder",
    "agent.tools.registry",
    "agent.tools.permission_gate",
    "agent.tools.dispatcher",
    "agent.tools.factory",
    "agent.execution.shell_runner",
    "agent.execution.python_runner",
    "agent.security.policy",
    "agent.security.audit_log",
    "agent.api.server",
]


class TestImports(unittest.TestCase):
    def test_all_modules_import(self):
        failures = []
        for mod in MODULES:
            try:
                __import__(mod)
            except Exception as e:  # noqa: BLE001
                failures.append(f"{mod}: {e}")
        self.assertEqual(failures, [], "\n".join(failures))

    def test_system_prompt_contains_all_powers(self):
        from agent.model.prompt_builder import build_system_prompt
        prompt = build_system_prompt()
        self.assertIn("147", prompt)
        self.assertIn("shell", prompt)
        self.assertIn("YOU MUST USE THESE TOOLS", prompt)


if __name__ == "__main__":
    unittest.main()
