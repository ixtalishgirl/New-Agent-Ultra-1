"""Smoke tests for the 22 added computer-control tools.

All tests execute the real handlers with safe args and assert sane output
shape — no crashes, honest capability reporting. Tests that need optional
backends (pytesseract, playwright, displays) assert the graceful-degradation
path, which is deterministic in this environment.
"""
import ast
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from agent.tools import vision_tools, browser_tools, filemgmt_tools
from agent.tools import app_tools, code_tools, memory_tools, safety_tools
from agent.tools.registry import build_full_registry


def _shape(r):
    assert hasattr(r, "ok"), "result needs .ok"
    assert isinstance(r.ok, bool)
    assert isinstance(r.output, str)
    assert isinstance(r.parsed, dict)
    assert isinstance(r.error, str)


# ===========================================================================
# Vision (3)
# ===========================================================================

class TestVisionAdditions(unittest.TestCase):
    def test_ocr_without_backend_reports_honestly(self):
        r = vision_tools.ocr_read({"image_path": "/nonexistent/x.png"})
        _shape(r)
        self.assertFalse(r.ok)
        self.assertIn("not found", r.error)

    def test_ocr_missing_pytesseract_is_honest(self):
        # make a real PNG with PIL so the failure is about OCR, not the file
        from PIL import Image, ImageDraw
        d = tempfile.mkdtemp()
        p = str(Path(d) / "t.png")
        img = Image.new("RGB", (200, 60), "white")
        ImageDraw.Draw(img).text((10, 10), "hello 123", fill="black")
        img.save(p)
        r = vision_tools.ocr_read({"image_path": p})
        _shape(r)
        try:
            import pytesseract  # noqa
            self.assertTrue(r.ok)
            self.assertIn("hello", r.parsed.get("text", "").lower())
        except ImportError:
            self.assertFalse(r.ok)
            self.assertIn("pytesseract", r.error)

    def test_multi_monitor_info(self):
        r = vision_tools.multi_monitor_info({})
        _shape(r)
        self.assertTrue(r.ok)
        self.assertIsInstance(r.parsed["monitors"], list)
        self.assertIn("backend", r.parsed)

    def test_screen_change_detect_no_display(self):
        r = vision_tools.screen_change_detect({"interval_sec": 0.3})
        _shape(r)
        # headless: no capture backend -> honest failure, no crash
        self.assertFalse(r.ok)
        self.assertTrue(r.error)


# ===========================================================================
# Browser (3)
# ===========================================================================

class TestBrowserAdditions(unittest.TestCase):
    def test_browser_launch_no_playwright(self):
        r = browser_tools.browser_launch({})
        _shape(r)
        try:
            import playwright  # noqa
        except ImportError:
            self.assertFalse(r.ok)
            self.assertIn("playwright", r.error.lower())

    def test_browser_close_when_not_running(self):
        r = browser_tools.browser_close({})
        _shape(r)
        self.assertTrue(r.ok)
        self.assertFalse(r.parsed["was_running"])

    def test_browser_scroll_graceful(self):
        r = browser_tools.browser_scroll({"direction": "down", "pixels": 100})
        _shape(r)
        # must not crash; ok depends on daemon availability
        self.assertIsInstance(r.ok, bool)


# ===========================================================================
# File (1)
# ===========================================================================

class TestFileAdditions(unittest.TestCase):
    def test_file_preview_text(self):
        d = tempfile.mkdtemp()
        p = Path(d) / "a.txt"
        p.write_text("\n".join(f"line {i}" for i in range(10)))
        r = filemgmt_tools._file_preview({"path": str(p), "max_lines": 3})
        _shape(r)
        self.assertTrue(r.ok)
        self.assertFalse(r.parsed["binary"])
        self.assertEqual(r.parsed["lines_shown"], 3)
        self.assertTrue(r.parsed["truncated"])

    def test_file_preview_binary(self):
        d = tempfile.mkdtemp()
        p = Path(d) / "b.bin"
        p.write_bytes(b"\x00\x01\x02hello" * 100)
        r = filemgmt_tools._file_preview({"path": str(p)})
        _shape(r)
        self.assertTrue(r.ok)
        self.assertTrue(r.parsed["binary"])
        self.assertIn("hex_head", r.parsed)

    def test_file_preview_missing(self):
        r = filemgmt_tools._file_preview({"path": "/nonexistent/f.txt"})
        _shape(r)
        self.assertFalse(r.ok)

    def test_file_preview_directory(self):
        r = filemgmt_tools._file_preview({"path": tempfile.gettempdir()})
        _shape(r)
        self.assertFalse(r.ok)

    def test_access_rules_deny_then_allow(self):
        d = tempfile.mkdtemp()
        victim = str(Path(d) / "v.txt")
        Path(victim).write_text("x")
        rules = {"mode": "denylist", "allow_prefixes": [],
                 "deny_prefixes": [d]}
        rp = filemgmt_tools._RULES_FILE
        had = rp.exists()
        backup = rp.read_text() if had else None
        try:
            rp.parent.mkdir(parents=True, exist_ok=True)
            rp.write_text(json.dumps(rules))
            r = filemgmt_tools._file_delete({"path": victim, "confirm": True})
            self.assertFalse(r.ok)
            self.assertIn("denied by file_access_rules", r.error)
            self.assertTrue(Path(victim).exists())  # untouched
            # now clear via the safety tool and retry
            s = safety_tools._file_access_rules(
                {"action": "clear", "confirm": True})
            self.assertTrue(s.ok)
            r2 = filemgmt_tools._file_delete({"path": victim, "confirm": True})
            self.assertTrue(r2.ok)
        finally:
            if had:
                rp.write_text(backup)
            elif rp.exists():
                rp.unlink()


# ===========================================================================
# App (4)
# ===========================================================================

class TestAppAdditions(unittest.TestCase):
    def test_window_resize_move_headless(self):
        r = app_tools._window_resize_move({"name": "nope", "width": 800,
                                           "height": 600})
        _shape(r)
        self.assertFalse(r.ok)
        self.assertTrue("wmctrl" in r.error or "DISPLAY" in r.error)

    def test_window_resize_move_needs_name(self):
        r = app_tools._window_resize_move({})
        self.assertFalse(r.ok)

    def test_menu_navigate_no_backend(self):
        r = app_tools._menu_navigate({"alt_menu": "f", "path": ["down"]})
        _shape(r)
        self.assertFalse(r.ok)  # no keyboard backend in this env

    def test_menu_navigate_needs_sequence(self):
        r = app_tools._menu_navigate({})
        self.assertFalse(r.ok)

    def test_dialog_handle_no_backend(self):
        r = app_tools._dialog_handle({"action": "confirm"})
        _shape(r)
        self.assertFalse(r.ok)

    def test_dialog_handle_bad_action(self):
        r = app_tools._dialog_handle({"action": "explode"})
        self.assertFalse(r.ok)

    def test_editor_control(self):
        d = tempfile.mkdtemp()
        p = str(Path(d) / "note.txt")
        Path(p).write_text("hi")
        env = dict(os.environ)
        env["EDITOR"] = "true"
        old = os.environ.get("EDITOR")
        os.environ["EDITOR"] = "true"
        try:
            r = app_tools._editor_control({"path": p, "line": 2})
        finally:
            if old is None:
                del os.environ["EDITOR"]
            else:
                os.environ["EDITOR"] = old
        _shape(r)
        self.assertTrue(r.ok)
        self.assertEqual(r.parsed["line"], 2)


# ===========================================================================
# Code (4)
# ===========================================================================

class TestCodeAdditions(unittest.TestCase):
    def test_code_generate_script(self):
        d = tempfile.mkdtemp()
        out = str(Path(d) / "demo.py")
        r = code_tools._code_generate({"kind": "script", "name": "demo",
                                       "output": out})
        _shape(r)
        self.assertTrue(r.ok)
        src = Path(out).read_text()
        ast.parse(src)  # must be valid python
        self.assertIn("demo", src)

    def test_code_generate_all_kinds(self):
        d = tempfile.mkdtemp()
        for kind in ("script", "class", "test", "cli"):
            out = str(Path(d) / f"g_{kind}.py")
            r = code_tools._code_generate({"kind": kind, "name": "thing",
                                           "output": out})
            self.assertTrue(r.ok, kind)
            ast.parse(Path(out).read_text())

    def test_code_generate_bad_kind(self):
        r = code_tools._code_generate({"kind": "fortran"})
        self.assertFalse(r.ok)

    def test_code_generate_no_overwrite(self):
        d = tempfile.mkdtemp()
        out = str(Path(d) / "e.py")
        Path(out).write_text("# keep me")
        r = code_tools._code_generate({"kind": "script", "name": "e",
                                       "output": out})
        self.assertFalse(r.ok)
        self.assertEqual(Path(out).read_text(), "# keep me")

    def test_error_log_read(self):
        r = code_tools._error_log_read({"tail": 5})
        _shape(r)
        self.assertTrue(r.ok)
        r2 = code_tools._error_log_read({"log": "no-such.log"})
        self.assertFalse(r2.ok)

    def test_tool_version_roundtrip(self):
        vf = code_tools._VERSIONS_FILE
        had = vf.exists()
        backup = vf.read_text() if had else None
        try:
            r = code_tools._tool_version({"action": "set", "name": "dummy_t",
                                          "version": "2.3.4"})
            self.assertTrue(r.ok)
            g = code_tools._tool_version({"action": "get", "name": "dummy_t"})
            self.assertEqual(g.parsed["version"], "2.3.4")
            bad = code_tools._tool_version({"action": "set", "name": "dummy_t",
                                            "version": "nope"})
            self.assertFalse(bad.ok)
            lst = code_tools._tool_version({"action": "list"})
            self.assertIn("dummy_t", lst.parsed["versions"])
        finally:
            if had:
                vf.write_text(backup)
            elif vf.exists():
                vf.unlink()

    def test_benchmark_run(self):
        calls = []

        def fake_exec(tool, args):
            calls.append(tool)
            time.sleep(0.01)

            class R:
                ok = True
                output = "fine"
                parsed = {}
                error = ""
            return R()

        code_tools.set_executor(fake_exec)
        try:
            r = code_tools._benchmark_run({"tool": "sysinfo", "times": 3})
        finally:
            code_tools.set_executor(None)
        _shape(r)
        self.assertTrue(r.ok)
        self.assertEqual(r.parsed["runs"], 3)
        self.assertEqual(r.parsed["ok"], 3)
        self.assertGreaterEqual(r.parsed["mean_s"], 0.005)
        self.assertEqual(len(calls), 3)

    def test_benchmark_no_executor(self):
        code_tools.set_executor(None)
        r = code_tools._benchmark_run({"tool": "sysinfo", "times": 2})
        self.assertFalse(r.ok)
        self.assertIn("executor", r.error)


# ===========================================================================
# Memory (2)
# ===========================================================================

class TestMemoryAdditions(unittest.TestCase):
    def test_progress_crud(self):
        pf = memory_tools._PROGRESS_FILE
        had = pf.exists()
        backup = pf.read_text() if had else None
        try:
            c = memory_tools._progress_report(
                {"action": "create", "name": "t1", "status": "running",
                 "percent": 10, "note": "n"})
            self.assertTrue(c.ok)
            u = memory_tools._progress_report(
                {"action": "update", "name": "t1", "percent": 55})
            self.assertEqual(u.parsed["entry"]["percent"], 55)
            g = memory_tools._progress_report({"action": "get", "name": "t1"})
            self.assertEqual(g.parsed["entry"]["status"], "running")
            l = memory_tools._progress_report({"action": "list"})
            self.assertIn("t1", l.parsed["entries"])
            d = memory_tools._progress_report({"action": "delete", "name": "t1"})
            self.assertTrue(d.ok)
            g2 = memory_tools._progress_report({"action": "get", "name": "t1"})
            self.assertFalse(g2.ok)
        finally:
            if had:
                pf.write_text(backup)
            elif pf.exists():
                pf.unlink()

    def test_error_detect_shape(self):
        r = memory_tools._error_detect({"hours": 1, "limit": 50})
        _shape(r)
        self.assertTrue(r.ok)
        self.assertIsInstance(r.parsed["findings"], list)


# ===========================================================================
# Safety (5)
# ===========================================================================

class TestSafetyAdditions(unittest.TestCase):
    def test_allowlist_roundtrip(self):
        af = safety_tools._ALLOWLIST_FILE
        had = af.exists()
        backup = af.read_text() if had else None
        try:
            g0 = safety_tools._website_allowlist({"action": "get"})
            self.assertTrue(g0.ok)
            a = safety_tools._website_allowlist({"action": "add",
                                                 "domain": "Example.COM"})
            self.assertTrue(a.ok)
            self.assertIn("example.com", a.parsed["domains"])
            g1 = safety_tools._website_allowlist({"action": "get"})
            self.assertEqual(g1.parsed["mode"], "allowlist")
            rm = safety_tools._website_allowlist({"action": "remove",
                                                  "domain": "example.com"})
            self.assertTrue(rm.ok)
            self.assertEqual(rm.parsed["domains"], [])
        finally:
            if had:
                af.write_text(backup)
            elif af.exists():
                af.unlink()

    def test_file_access_rules_roundtrip(self):
        rf = safety_tools._FILE_RULES_FILE
        had = rf.exists()
        backup = rf.read_text() if had else None
        try:
            g = safety_tools._file_access_rules({"action": "get"})
            self.assertTrue(g.ok)
            self.assertEqual(g.parsed["rules"]["mode"], "allow_all")
            s = safety_tools._file_access_rules(
                {"action": "set", "mode": "denylist",
                 "deny_prefixes": ["/tmp/secret"]})
            self.assertTrue(s.ok)
            self.assertEqual(s.parsed["rules"]["mode"], "denylist")
            bad = safety_tools._file_access_rules({"action": "set",
                                                   "mode": "bogus"})
            self.assertFalse(bad.ok)
            c = safety_tools._file_access_rules({"action": "clear"})
            self.assertFalse(c.ok)  # needs confirm
            c2 = safety_tools._file_access_rules({"action": "clear",
                                                  "confirm": True})
            self.assertTrue(c2.ok)
        finally:
            if had:
                rf.write_text(backup)
            elif rf.exists():
                rf.unlink()

    def test_op_timeout_fast_and_slow(self):
        def fake_exec(tool, args):
            class R:
                ok = True
                output = "done"
                parsed = {}
                error = ""
            if tool == "slow":
                time.sleep(3)
            return R()

        safety_tools.set_executor(fake_exec)
        try:
            fast = safety_tools._op_timeout({"tool": "fast",
                                             "timeout_sec": 5})
            self.assertTrue(fast.ok)
            self.assertFalse(fast.parsed["timed_out"])
            slow = safety_tools._op_timeout({"tool": "slow",
                                             "timeout_sec": 1})
            self.assertFalse(slow.ok)
            self.assertTrue(slow.parsed["timed_out"])
        finally:
            safety_tools.set_executor(None)

    def test_op_timeout_no_executor(self):
        safety_tools.set_executor(None)
        r = safety_tools._op_timeout({"tool": "x"})
        self.assertFalse(r.ok)

    def test_cred_protect_finds_and_redacts(self):
        text = ("key=AKIAIOSFODNN7EXAMPLE and password: hunter2 "
                "plus nothing secret here")
        r = safety_tools._cred_protect({"text": text})
        _shape(r)
        self.assertTrue(r.ok)
        self.assertGreater(r.parsed["secrets_found"], 0)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", r.parsed["redacted"])
        self.assertIn("REDACTED", r.parsed["redacted"])
        kinds = {f["kind"] for f in r.parsed["findings"]}
        self.assertIn("aws_access_key", kinds)

    def test_cred_protect_clean_text(self):
        r = safety_tools._cred_protect({"text": "hello world, no secrets"})
        self.assertTrue(r.ok)
        self.assertEqual(r.parsed["secrets_found"], 0)
        self.assertEqual(r.parsed["redacted"], "hello world, no secrets")

    def test_perm_change_log_roundtrip(self):
        lf = safety_tools._PERM_CHANGES_FILE
        had = lf.exists()
        backup = lf.read_text() if had else None
        try:
            w = safety_tools._perm_change_log(
                {"action": "log", "name": "tool:test_x", "change": "grant"})
            self.assertTrue(w.ok)
            q = safety_tools._perm_change_log(
                {"action": "query", "name": "tool:test_x"})
            self.assertTrue(q.ok)
            self.assertGreaterEqual(q.parsed["count"], 1)
            self.assertEqual(q.parsed["entries"][-1]["change"], "grant")
            t = safety_tools._perm_change_log({"action": "tail", "tail": 5})
            self.assertTrue(t.ok)
        finally:
            if had:
                lf.write_text(backup)
            elif lf.exists():
                lf.unlink()


# ===========================================================================
# Registry / permission integration for the new tools
# ===========================================================================

NEW_TOOL_NAMES = [
    "ocr_read", "multi_monitor_info", "screen_change_detect",
    "browser_launch", "browser_close", "browser_scroll",
    "file_preview",
    "window_resize_move", "menu_navigate", "dialog_handle", "editor_control",
    "code_generate", "error_log_read", "tool_version", "benchmark_run",
    "progress_report", "error_detect",
    "website_allowlist", "file_access_rules", "op_timeout", "cred_protect",
    "perm_change_log",
]


class TestNewToolsRegistered(unittest.TestCase):
    def test_all_22_registered_with_handlers(self):
        from agent.tools.registry import collect_handlers
        reg = build_full_registry()
        names = {t.name for t in reg.list()}
        handlers = collect_handlers()
        for n in NEW_TOOL_NAMES:
            self.assertIn(n, names, f"{n} not in registry")
            self.assertIn(n, handlers, f"{n} has no handler")

    def test_all_22_enabled_by_default(self):
        from agent.tools.permission_manager import PermissionManager
        import tempfile as _tf
        reg = build_full_registry()
        mgr = PermissionManager(registry=reg,
                                state_path=Path(_tf.mkdtemp()) / "s.json")
        for n in NEW_TOOL_NAMES:
            self.assertTrue(mgr.is_allowed(f"tool:{n}"), n)

    def test_denials_still_locked(self):
        from agent.tools.permission_manager import PermissionManager
        from agent.security.policy import DENIALS
        import tempfile as _tf
        mgr = PermissionManager(
            registry=build_full_registry(),
            state_path=Path(_tf.mkdtemp()) / "s.json")
        for d in DENIALS:
            self.assertFalse(mgr.is_allowed(f"denial:{d}"))
            self.assertFalse(mgr.is_allowed(d))


if __name__ == "__main__":
    unittest.main()
