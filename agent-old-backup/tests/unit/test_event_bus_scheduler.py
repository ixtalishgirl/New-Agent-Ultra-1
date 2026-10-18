"""Tests for event bus and scheduler."""
import unittest
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

from agent.app.event_bus import (
    EventBus, Event, EventType, get_bus, reset_bus,
)
from agent.app.scheduler import (
    Scheduler, ScheduleType, _parse_cron, _next_cron_time,
)


class TestEventBus(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()

    def test_publish_subscribe(self):
        received = []
        self.bus.subscribe(EventType.TASK_STARTED,
                           lambda e: received.append(e))
        self.bus.publish(EventType.TASK_STARTED,
                         {"task_id": "t1"})
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].data["task_id"], "t1")

    def test_wildcard(self):
        received = []
        self.bus.subscribe_all(lambda e: received.append(e))
        self.bus.publish(EventType.TASK_STARTED, {})
        self.bus.publish(EventType.TOOL_CALLED, {})
        self.assertEqual(len(received), 2)

    def test_unsubscribe(self):
        received = []
        sub = self.bus.subscribe(EventType.TASK_STARTED,
                                 lambda e: received.append(e))
        self.assertTrue(self.bus.unsubscribe(sub))
        self.bus.publish(EventType.TASK_STARTED, {})
        self.assertEqual(len(received), 0)

    def test_unsubscribe_invalid(self):
        self.assertFalse(self.bus.unsubscribe("nonexistent"))

    def test_convenience_emitters(self):
        e = self.bus.task_started("t1", "Test task")
        self.assertEqual(e.type, EventType.TASK_STARTED)
        e = self.bus.task_completed("t1", True, "done")
        self.assertEqual(e.type, EventType.TASK_COMPLETED)
        e = self.bus.task_completed("t1", False)
        self.assertEqual(e.type, EventType.TASK_FAILED)
        e = self.bus.tool_denied("shell", "denied reason")
        self.assertEqual(e.type, EventType.TOOL_DENIED)

    def test_history(self):
        self.bus.publish(EventType.TASK_STARTED, {})
        self.bus.publish(EventType.TOOL_CALLED, {})
        hist = self.bus.history()
        self.assertEqual(len(hist), 2)
        hist = self.bus.history(EventType.TASK_STARTED)
        self.assertEqual(len(hist), 1)

    def test_count(self):
        self.bus.publish(EventType.TASK_STARTED, {})
        self.bus.publish(EventType.TASK_STARTED, {})
        self.assertEqual(self.bus.count(EventType.TASK_STARTED), 2)
        self.assertEqual(self.bus.count(), 2)

    def test_pause_resume(self):
        received = []
        self.bus.subscribe(EventType.TASK_STARTED,
                           lambda e: received.append(e))
        self.bus.pause()
        self.bus.publish(EventType.TASK_STARTED, {})
        self.assertEqual(len(received), 0)
        self.assertEqual(self.bus.count(), 1)  # history recorded
        self.bus.resume()
        self.bus.publish(EventType.TASK_STARTED, {})
        self.assertEqual(len(received), 1)

    def test_stats(self):
        self.bus.publish(EventType.TASK_STARTED, {})
        stats = self.bus.stats()
        self.assertEqual(stats["total_events"], 1)
        self.assertIn("task.started", stats["by_type"])

    def test_handler_exception_safe(self):
        def bad_handler(e):
            raise RuntimeError("oops")
        self.bus.subscribe(EventType.TASK_STARTED, bad_handler)
        # Should not crash
        e = self.bus.publish(EventType.TASK_STARTED, {})
        self.assertIsNotNone(e)

    def test_global_bus(self):
        b1 = get_bus()
        b2 = get_bus()
        self.assertIs(b1, b2)
        reset_bus()
        b3 = get_bus()
        self.assertIsNot(b1, b3)


class TestCronParsing(unittest.TestCase):
    def test_star(self):
        c = _parse_cron("* * * * *")
        self.assertEqual(len(c["minute"]), 60)
        self.assertEqual(len(c["hour"]), 24)

    def test_specific(self):
        c = _parse_cron("30 14 * * *")
        self.assertEqual(c["minute"], [30])
        self.assertEqual(c["hour"], [14])

    def test_step(self):
        c = _parse_cron("*/15 * * * *")
        self.assertEqual(c["minute"], [0, 15, 30, 45])

    def test_range(self):
        c = _parse_cron("0 9-17 * * *")
        self.assertEqual(c["hour"], list(range(9, 18)))

    def test_list(self):
        c = _parse_cron("0 9,12,15 * * *")
        self.assertEqual(c["hour"], [9, 12, 15])

    def test_invalid(self):
        with self.assertRaises(ValueError):
            _parse_cron("* * * *")

    def test_out_of_range(self):
        with self.assertRaises(ValueError):
            _parse_cron("70 * * * *")

    def test_next_time(self):
        c = _parse_cron("* * * * *")
        now = time.time()
        nxt = _next_cron_time(c, now)
        self.assertGreater(nxt, now)
        self.assertLess(nxt - now, 120)


class TestScheduler(unittest.TestCase):
    def test_once(self):
        s = Scheduler()
        ran = []
        tid = s.once("test", lambda: ran.append(1),
                     delay_seconds=0.1)
        self.assertIsInstance(tid, str)
        s.start()
        try:
            time.sleep(0.5)
        finally:
            s.stop()
        self.assertEqual(len(ran), 1)

    def test_interval(self):
        s = Scheduler()
        ran = []
        s.every("test", lambda: ran.append(1),
                interval_seconds=0.2)
        s.start()
        try:
            time.sleep(0.7)
        finally:
            s.stop()
        self.assertGreaterEqual(len(ran), 2)

    def test_cancel(self):
        s = Scheduler()
        tid = s.once("test", lambda: None, delay_seconds=10)
        self.assertTrue(s.cancel(tid))
        self.assertFalse(s.cancel("nonexistent"))

    def test_enable_disable(self):
        s = Scheduler()
        tid = s.every("test", lambda: None,
                      interval_seconds=60)
        self.assertTrue(s.disable(tid))
        self.assertTrue(s.enable(tid))

    def test_list_tasks(self):
        s = Scheduler()
        s.once("task1", lambda: None, delay_seconds=60)
        s.every("task2", lambda: None, interval_seconds=60)
        tasks = s.list_tasks()
        self.assertEqual(len(tasks), 2)

    def test_cron_schedule(self):
        s = Scheduler()
        tid = s.cron("test", lambda: None, "* * * * *")
        self.assertIsInstance(tid, str)
        tasks = s.list_tasks()
        self.assertEqual(tasks[0]["type"], "cron")

    def test_invalid_interval(self):
        s = Scheduler()
        with self.assertRaises(ValueError):
            s.every("test", lambda: None, interval_seconds=0)

    def test_error_handling(self):
        s = Scheduler()
        def bad():
            raise RuntimeError("task failed")
        s.once("bad", bad, delay_seconds=0.1)
        s.start()
        try:
            time.sleep(0.5)
        finally:
            s.stop()
        errors = s.errors()
        self.assertEqual(len(errors), 1)
        self.assertIn("task failed", errors[0]["error"])

    def test_context_manager(self):
        ran = []
        with Scheduler() as s:
            s.once("test", lambda: ran.append(1),
                   delay_seconds=0.1)
            time.sleep(1.0)
        self.assertEqual(len(ran), 1)


if __name__ == "__main__":
    unittest.main()
