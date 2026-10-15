"""Smoke tests for the tool registry — all real powers must be registered."""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from agent.tools.registry import default_registry

EXPECTED = [
    "shell", "python", "pip",
    "file_read", "file_write",
    "browser_navigate", "browser_extract", "browser_screenshot",
    "web_search", "germany_fetch", "supabase_query",
    "self_modify", "tool_create",
]


class TestRegistry(unittest.TestCase):
    def test_core_powers_registered(self):
        reg = default_registry()
        names = {t.name for t in reg.list()}
        for tool in EXPECTED:
            self.assertIn(tool, names, f"missing tool: {tool}")

    def test_tool_count(self):
        reg = default_registry()
        self.assertEqual(len(reg.list()), len(EXPECTED))

    def test_unknown_tool_returns_none(self):
        reg = default_registry()
        self.assertIsNone(reg.get("does_not_exist"))


if __name__ == "__main__":
    unittest.main()
