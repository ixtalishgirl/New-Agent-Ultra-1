"""Unit tests for the Rust performance bridge (agent/perf/rust_bridge.py).

Requires the compiled haley_rust_core extension (rust-core/build.sh).
No personality. No rules. Direct execution.
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from agent.perf import rust_bridge as b


class TestBridgeAvailable(unittest.TestCase):
    def test_extension_present(self):
        self.assertTrue(b.is_rust_available())
        self.assertEqual(b.rust_version(), "1.0.0")
        self.assertGreaterEqual(b.quick_cpu_count(), 1)


class TestScheduler(unittest.TestCase):
    def test_priority_order(self):
        s = b.get_scheduler()
        s.push("low", 1)
        s.push("high", 10)
        s.push("mid", 5)
        self.assertEqual(s.pop()["id"], "high")
        self.assertEqual(s.pop()["id"], "mid")
        self.assertEqual(s.pop()["id"], "low")
        self.assertIsNone(s.pop())

    def test_cancel_and_stats(self):
        s = b.get_scheduler()
        s.push("a", 1)
        s.push("b", 9)
        self.assertTrue(s.cancel("b"))
        self.assertFalse(s.cancel("missing"))
        self.assertEqual(s.pop()["id"], "a")
        pending, _, cancelled = s.stats()
        self.assertEqual(pending, 0)
        self.assertEqual(cancelled, 1)

    def test_drain(self):
        s = b.get_scheduler()
        for i in range(5):
            s.push(f"t{i}", i)
        got = s.drain(3)
        self.assertEqual(len(got), 3)
        self.assertEqual(got[0]["id"], "t4")
        self.assertEqual(s.len(), 2)


class TestQueue(unittest.TestCase):
    def test_roundtrip(self):
        q = b.get_queue(8)
        self.assertTrue(q.send("k", "v", "s"))
        m = q.try_recv()
        self.assertEqual((m["kind"], m["body"], m["sender"]), ("k", "v", "s"))
        self.assertTrue(q.is_empty())

    def test_backpressure(self):
        q = b.get_queue(2)
        self.assertTrue(q.try_send("k", "1", "s"))
        self.assertTrue(q.try_send("k", "2", "s"))
        self.assertFalse(q.try_send("k", "3", "s"))


class TestMonitor(unittest.TestCase):
    def test_sample(self):
        m = b.get_monitor()
        m.sample()  # baseline
        time.sleep(0.05)
        s = m.sample()
        self.assertGreater(s["mem_total_kb"], 0)
        self.assertGreaterEqual(s["cpu_percent"], 0.0)
        self.assertLessEqual(s["cpu_percent"], 100.0)


class TestEventLog(unittest.TestCase):
    def test_emit_query(self):
        e = b.get_event_log(100)
        e.emit("task.started", "sched", "info", "hello")
        e.emit("task.failed", "sched", "error", "boom")
        self.assertEqual(e.count("task."), 2)
        self.assertEqual(len(e.query("", "error", 0)), 1)
        self.assertEqual(len(e.query("task.started", "debug", 0)), 1)


class TestRateLimit(unittest.TestCase):
    def test_token_bucket(self):
        rl = b.get_rate_limiter(2.0, 0.0)
        self.assertTrue(rl.check("k")[0])
        self.assertTrue(rl.check("k")[0])
        ok, wait = rl.check("k")
        self.assertFalse(ok)
        self.assertGreater(wait, 0)

    def test_sliding_window(self):
        sw = b.get_sliding_window(60)
        self.assertEqual(sw.record("u"), 1)
        self.assertEqual(sw.record("u"), 2)
        self.assertEqual(sw.count("u"), 2)


class TestToolMeta(unittest.TestCase):
    def test_record_stats(self):
        tm = b.get_tool_meta(100)
        tm.record("shell", "ok", 100)
        tm.record("shell", "ok", 200)
        tm.record("py", "error", 5, "boom")
        calls, errors, avg = tm.tool_stats("shell")
        self.assertEqual((calls, errors), (2, 0))
        self.assertAlmostEqual(avg, 150.0)
        self.assertEqual(tm.slowest(1)[0][0], "shell")


class TestJson(unittest.TestCase):
    def test_helpers(self):
        self.assertTrue(b.json_validate('{"a":1}')[0])
        self.assertFalse(b.json_validate("{bad")[0])
        self.assertEqual(b.json_get_field('{"k":"v"}', "k"), "v")
        self.assertIsNone(b.json_get_field('{"k":1}', "zz"))
        import json as _j
        self.assertEqual(_j.loads(b.json_merge('{"a":1}', '{"b":2}')), {"a": 1, "b": 2})
        self.assertEqual(b.json_encode_str_list(["x", "y"]), '["x","y"]')
        self.assertEqual(b.json_decode_str_list('["x","y"]'), ["x", "y"])


class TestWorkerPool(unittest.TestCase):
    def test_pool(self):
        pool = b.get_worker_pool(2, 32, lambda jid, kind, p: (True, p.upper()))
        try:
            for i in range(6):
                self.assertTrue(pool.submit(f"j{i}", "k", f"p{i}"))
            got = pool.wait_for(6, 10000)
            self.assertEqual(len(got), 6)
            by_id = {g[0]: g for g in got}
            self.assertEqual(by_id["j0"][2], "P0")
            s, f, failed, _ = pool.stats()
            self.assertEqual((s, f, failed), (6, 6, 0))
        finally:
            pool.shutdown()


class TestLogProc(unittest.TestCase):
    def test_all(self):
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            for i in range(200):
                f.write(f"line {i} {'ERROR' if i % 10 == 0 else 'ok'}\n")
            path = f.name
        try:
            self.assertEqual(b.log_count_lines(path), 200)
            self.assertEqual(b.log_grep_count(path, "ERROR"), 20)
            tail = b.log_tail_lines(path, 2)
            self.assertEqual(len(tail), 2)
            self.assertIn("line 199", tail[-1])
            offs = b.log_find_offsets(path, "line 0 ", 5)
            self.assertTrue(len(offs) >= 1)
        finally:
            os.unlink(path)


class TestIndex(unittest.TestCase):
    def test_index_and_search(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "main.py"), "w").write("x")
            sub = os.path.join(d, "sub")
            os.mkdir(sub)
            open(os.path.join(sub, "lib.rs"), "w").write("y")
            entries = b.index_directory(d, max_depth=5)
            paths = [e["path"] for e in entries]
            self.assertTrue(any(p.endswith("main.py") for p in paths))
            hits = b.index_search(entries, "lib.rs", 5)
            self.assertEqual(len(hits), 1)


if __name__ == "__main__":
    unittest.main()
