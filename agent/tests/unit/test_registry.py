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
    # Phase-2 tool modules
    "git_status", "git_log", "git_diff", "git_branch", "git_commit",
    "file_tree", "file_find", "file_checksum",
    "http_get", "http_post", "dns_lookup", "port_check",
    "json_validate", "csv_stats",
    "sysinfo", "disk_usage",
    # Media tools
    "image_info", "image_resize", "image_convert", "image_thumbnail",
    "audio_info", "video_info",
    # Archive tools
    "archive_create", "archive_extract", "archive_list", "archive_info",
    "gzip_compress", "gzip_decompress",
    # Text tools
    "text_search", "text_replace", "text_stats", "text_diff",
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
