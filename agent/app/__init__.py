"""App module — orchestrator, CLI, sessions, events, scheduling."""
from .orchestrator import Orchestrator, AgentResult, IterationRecord
from .event_bus import EventBus, Event, EventType, get_bus, reset_bus
from .scheduler import Scheduler, ScheduledTask, ScheduleType
from .notifications import (
    NotificationManager, Notification, Severity,
    LogChannel, WebhookChannel, ConsoleChannel, CallbackChannel,
    get_notifier,
)
from .backup import BackupManager, BackupInfo
from .diagnostics import Diagnostics, CheckResult, CheckStatus

__all__ = [
    "Orchestrator", "AgentResult", "IterationRecord",
    "EventBus", "Event", "EventType", "get_bus", "reset_bus",
    "Scheduler", "ScheduledTask", "ScheduleType",
    "NotificationManager", "Notification", "Severity",
    "LogChannel", "WebhookChannel", "ConsoleChannel",
    "CallbackChannel", "get_notifier",
    "BackupManager", "BackupInfo",
    "Diagnostics", "CheckResult", "CheckStatus",
]
