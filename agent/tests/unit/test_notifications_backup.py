"""Tests for notifications and backup."""
import unittest
import sys
import os
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

from agent.app.notifications import (
    NotificationManager, Notification, Severity,
    LogChannel, ConsoleChannel, CallbackChannel,
    WebhookChannel,
)
from agent.app.event_bus import EventBus, reset_bus
from agent.app.backup import BackupManager, BackupInfo


class TestNotificationManager(unittest.TestCase):
    def setUp(self):
        reset_bus()
        self.mgr = NotificationManager()

    def test_notify(self):
        n = self.mgr.notify(Severity.INFO, "Test", "Hello")
        self.assertEqual(n.severity, Severity.INFO)
        self.assertEqual(n.title, "Test")
        self.assertEqual(len(self.mgr.history), 1)

    def test_convenience(self):
        self.mgr.info("I", "info msg")
        self.mgr.warning("W", "warn msg")
        self.mgr.error("E", "error msg")
        self.mgr.debug("D", "debug msg")
        self.mgr.critical("C", "critical msg")
        self.assertEqual(len(self.mgr.history), 5)

    def test_recent(self):
        for i in range(10):
            self.mgr.info(f"T{i}", f"msg {i}")
        recent = self.mgr.recent(3)
        self.assertEqual(len(recent), 3)
        self.assertEqual(recent[-1].title, "T9")

    def test_recent_filtered(self):
        self.mgr.info("I", "info")
        self.mgr.error("E", "error")
        errors = self.mgr.recent(severity=Severity.ERROR)
        self.assertEqual(len(errors), 1)

    def test_clear(self):
        self.mgr.info("T", "msg")
        self.mgr.clear()
        self.assertEqual(len(self.mgr.history), 0)

    def test_callback_channel(self):
        received = []
        chan = CallbackChannel(lambda n: received.append(n))
        self.mgr.add_channel(chan)
        self.mgr.info("T", "msg")
        self.assertEqual(len(received), 1)
        self.assertTrue(self.mgr.remove_channel(chan))

    def test_log_channel(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=False,
                                         suffix=".log") as f:
            path = f.name
        try:
            chan = LogChannel(path)
            self.mgr.add_channel(chan)
            self.mgr.warning("TestWarn", "warning message")
            with open(path) as f:
                content = f.read()
            self.assertIn("WARNING", content)
            self.assertIn("TestWarn", content)
        finally:
            os.unlink(path)

    def test_console_channel_min_severity(self):
        chan = ConsoleChannel(use_color=False,
                              min_severity=Severity.ERROR)
        # Info should be skipped (not a failure)
        n = Notification(id="1", severity=Severity.INFO,
                         title="T", message="M")
        self.assertTrue(chan.send(n))

    def test_event_integration(self):
        # Task failure should trigger notification
        received = []
        chan = CallbackChannel(lambda n: received.append(n))
        self.mgr.add_channel(chan)
        from agent.app.event_bus import EventType
        self.mgr.bus.publish(EventType.TASK_FAILED,
                             {"task_id": "t1"})
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].severity, Severity.WARNING)


class TestBackupManager(unittest.TestCase):
    def test_create_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = BackupManager(backup_dir=os.path.join(tmp, "backups"),
                                data_dir=tmp)
            info = mgr.create("test backup", components=[])
            self.assertIsInstance(info, BackupInfo)
            self.assertTrue(os.path.exists(info.path))
            backups = mgr.list()
            self.assertEqual(len(backups), 1)

    def test_create_with_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Create a config dir
            cfg = os.path.join(tmp, "agent", "configs")
            os.makedirs(cfg)
            with open(os.path.join(cfg, "test.yaml"), "w") as f:
                f.write("key: value")
            mgr = BackupManager(backup_dir=os.path.join(tmp, "backups"),
                                data_dir=tmp)
            info = mgr.create("with config")
            self.assertIn("configs", info.components)

    def test_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = BackupManager(backup_dir=os.path.join(tmp, "backups"),
                                data_dir=tmp)
            info = mgr.create("test", components=[])
            result = mgr.verify(info.id)
            self.assertTrue(result["ok"])

    def test_verify_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = BackupManager(backup_dir=os.path.join(tmp, "backups"),
                                data_dir=tmp)
            result = mgr.verify("nonexistent")
            self.assertFalse(result["ok"])

    def test_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = BackupManager(backup_dir=os.path.join(tmp, "backups"),
                                data_dir=tmp)
            info = mgr.create("test", components=[])
            self.assertTrue(mgr.delete(info.id))
            self.assertEqual(len(mgr.list()), 0)
            self.assertFalse(mgr.delete("nonexistent"))

    def test_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = BackupManager(backup_dir=os.path.join(tmp, "backups"),
                                data_dir=tmp)
            for i in range(5):
                mgr.create(f"backup {i}", components=[])
                time.sleep(0.05)
            deleted = mgr.cleanup(keep=2)
            self.assertEqual(deleted, 3)
            self.assertEqual(len(mgr.list()), 2)

    def test_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Create backup with a file
            cfg = os.path.join(tmp, "agent", "configs")
            os.makedirs(cfg)
            with open(os.path.join(cfg, "app.yaml"), "w") as f:
                f.write("test: true")
            mgr = BackupManager(backup_dir=os.path.join(tmp, "backups"),
                                data_dir=tmp)
            info = mgr.create("test")
            # Restore to different dir
            restore_dir = os.path.join(tmp, "restored")
            os.makedirs(restore_dir)
            result = mgr.restore(info.id, dest_dir=restore_dir)
            self.assertTrue(result["ok"])
            self.assertTrue(os.path.exists(
                os.path.join(restore_dir, "configs", "app.yaml")))

    def test_restore_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = BackupManager(backup_dir=os.path.join(tmp, "backups"),
                                data_dir=tmp)
            result = mgr.restore("nonexistent")
            self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
