"""Notifications — alerts for agent events.

Channels:
- Log file
- Webhook (HTTP POST)
- Console

No personality. No rules. Direct execution.
"""
from __future__ import annotations

import json
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .event_bus import EventBus, EventType, Event, get_bus


class Severity(Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass
class Notification:
    id: str
    severity: Severity
    title: str
    message: str
    timestamp: float = field(default_factory=time.time)
    data: Dict[str, Any] = field(default_factory=dict)
    source: str = "agent"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "severity": self.severity.value,
            "title": self.title,
            "message": self.message,
            "timestamp": self.timestamp,
            "data": self.data,
            "source": self.source,
        }


class NotificationChannel:
    """Base class for notification channels."""

    def send(self, notification: Notification) -> bool:
        raise NotImplementedError


class LogChannel(NotificationChannel):
    """Write notifications to a log file."""

    def __init__(self, path: str = "notifications.log"):
        self.path = Path(path)

    def send(self, notification: Notification) -> bool:
        try:
            line = (f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
                    f"[{notification.severity.value.upper()}] "
                    f"{notification.title}: "
                    f"{notification.message}\n")
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
            return True
        except Exception:
            return False


class WebhookChannel(NotificationChannel):
    """POST notifications to a webhook URL."""

    def __init__(self, url: str, timeout: int = 10,
                 headers: Optional[Dict[str, str]] = None):
        self.url = url
        self.timeout = timeout
        self.headers = headers or {}

    def send(self, notification: Notification) -> bool:
        try:
            data = json.dumps(notification.to_dict()).encode()
            headers = {"Content-Type": "application/json",
                       **self.headers}
            req = urllib.request.Request(
                self.url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req,
                                        timeout=self.timeout):
                pass
            return True
        except Exception:
            return False


class ConsoleChannel(NotificationChannel):
    """Print notifications to console."""

    COLORS = {
        Severity.DEBUG: "\033[36m",    # cyan
        Severity.INFO: "\033[32m",     # green
        Severity.WARNING: "\033[33m",  # yellow
        Severity.ERROR: "\033[31m",    # red
        Severity.CRITICAL: "\033[35m",  # magenta
    }
    RESET = "\033[0m"

    def __init__(self, use_color: bool = True,
                 min_severity: Severity = Severity.INFO):
        self.use_color = use_color
        self.min_severity = min_severity
        self._order = [Severity.DEBUG, Severity.INFO,
                       Severity.WARNING, Severity.ERROR,
                       Severity.CRITICAL]

    def send(self, notification: Notification) -> bool:
        if (self._order.index(notification.severity) <
                self._order.index(self.min_severity)):
            return True  # Skip, but not a failure
        try:
            if self.use_color:
                color = self.COLORS.get(notification.severity, "")
                prefix = (f"{color}[{notification.severity.value.upper()}]"
                          f"{self.RESET}")
            else:
                prefix = f"[{notification.severity.value.upper()}]"
            print(f"{prefix} {notification.title}: "
                  f"{notification.message}")
            return True
        except Exception:
            return False


class CallbackChannel(NotificationChannel):
    """Call a function for each notification."""

    def __init__(self, callback: Callable[[Notification], None]):
        self.callback = callback

    def send(self, notification: Notification) -> bool:
        try:
            self.callback(notification)
            return True
        except Exception:
            return False


class NotificationManager:
    """Manage notifications across channels."""

    def __init__(self, bus: Optional[EventBus] = None):
        self.bus = bus or get_bus()
        self.channels: List[NotificationChannel] = []
        self.history: List[Notification] = []
        self.max_history = 500
        self._id_counter = 0
        self._setup_event_handlers()

    def _setup_event_handlers(self) -> None:
        """Auto-notify on important events."""
        # Task failures → warning
        self.bus.subscribe(
            EventType.TASK_FAILED,
            lambda e: self.notify(
                Severity.WARNING,
                "Task failed",
                f"Task {e.data.get('task_id')} failed",
                data=e.data))
        # Tool denials → warning
        self.bus.subscribe(
            EventType.TOOL_DENIED,
            lambda e: self.notify(
                Severity.WARNING,
                "Tool denied",
                f"{e.data.get('tool')}: {e.data.get('reason')}",
                data=e.data))
        # Errors → error
        self.bus.subscribe(
            EventType.ERROR_OCCURRED,
            lambda e: self.notify(
                Severity.ERROR,
                "Error occurred",
                e.data.get("message", "")[:200],
                data=e.data))
        # Improvements merged → info
        self.bus.subscribe(
            EventType.IMPROVEMENT_MERGED,
            lambda e: self.notify(
                Severity.INFO,
                "Improvement merged",
                f"Patch {e.data.get('patch_id')} merged",
                data=e.data))

    def add_channel(self, channel: NotificationChannel) -> None:
        self.channels.append(channel)

    def remove_channel(self, channel: NotificationChannel) -> bool:
        if channel in self.channels:
            self.channels.remove(channel)
            return True
        return False

    def notify(self, severity: Severity, title: str,
               message: str,
               data: Optional[Dict[str, Any]] = None,
               source: str = "agent") -> Notification:
        """Send a notification through all channels."""
        self._id_counter += 1
        notif = Notification(
            id=f"notif_{self._id_counter}_{int(time.time())}",
            severity=severity, title=title, message=message,
            data=data or {}, source=source)
        self.history.append(notif)
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history:]
        for channel in self.channels:
            try:
                channel.send(notif)
            except Exception:
                pass
        return notif

    # Convenience methods
    def debug(self, title: str, message: str, **kwargs) -> Notification:
        return self.notify(Severity.DEBUG, title, message, **kwargs)

    def info(self, title: str, message: str, **kwargs) -> Notification:
        return self.notify(Severity.INFO, title, message, **kwargs)

    def warning(self, title: str, message: str,
                **kwargs) -> Notification:
        return self.notify(Severity.WARNING, title, message, **kwargs)

    def error(self, title: str, message: str, **kwargs) -> Notification:
        return self.notify(Severity.ERROR, title, message, **kwargs)

    def critical(self, title: str, message: str,
                 **kwargs) -> Notification:
        return self.notify(Severity.CRITICAL, title, message, **kwargs)

    def recent(self, limit: int = 50,
               severity: Optional[Severity] = None) -> List[Notification]:
        notifs = self.history
        if severity:
            notifs = [n for n in notifs if n.severity == severity]
        return notifs[-limit:]

    def clear(self) -> None:
        self.history.clear()


# Global instance
_global_manager: Optional[NotificationManager] = None


def get_notifier() -> NotificationManager:
    global _global_manager
    if _global_manager is None:
        _global_manager = NotificationManager()
        _global_manager.add_channel(ConsoleChannel())
    return _global_manager
