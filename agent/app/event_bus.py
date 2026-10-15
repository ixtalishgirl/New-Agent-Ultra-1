"""Event bus — pub/sub for agent lifecycle events.

Events:
- task.started, task.completed, task.failed
- tool.called, tool.completed, tool.denied
- plan.created, plan.completed
- error.occurred, error.fixed
- memory.saved
- improvement.proposed, improvement.merged

No personality. No rules. Direct execution.
"""
from __future__ import annotations

import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class EventType(Enum):
    # Task lifecycle
    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_CANCELLED = "task.cancelled"
    # Tool lifecycle
    TOOL_CALLED = "tool.called"
    TOOL_COMPLETED = "tool.completed"
    TOOL_DENIED = "tool.denied"
    TOOL_FAILED = "tool.failed"
    # Planning
    PLAN_CREATED = "plan.created"
    PLAN_STARTED = "plan.started"
    PLAN_COMPLETED = "plan.completed"
    # Control loop phases
    PHASE_STARTED = "phase.started"
    PHASE_COMPLETED = "phase.completed"
    # Errors & debugging
    ERROR_OCCURRED = "error.occurred"
    ERROR_ANALYZED = "error.analyzed"
    ERROR_FIXED = "error.fixed"
    # Memory
    MEMORY_SAVED = "memory.saved"
    MEMORY_RETRIEVED = "memory.retrieved"
    # Self-improvement
    IMPROVEMENT_PROPOSED = "improvement.proposed"
    IMPROVEMENT_TESTED = "improvement.tested"
    IMPROVEMENT_MERGED = "improvement.merged"
    IMPROVEMENT_REJECTED = "improvement.rejected"
    # System
    SYSTEM_STARTED = "system.started"
    SYSTEM_STOPPED = "system.stopped"
    HEALTH_CHECK = "system.health_check"


@dataclass
class Event:
    """An event on the bus."""
    id: str
    type: EventType
    timestamp: float
    data: Dict[str, Any] = field(default_factory=dict)
    source: str = ""
    correlation_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["type"] = self.type.value
        return d


Handler = Callable[[Event], None]


class EventBus:
    """Publish/subscribe event bus."""

    def __init__(self, max_history: int = 1000):
        self._handlers: Dict[EventType, List[Handler]] = defaultdict(list)
        self._wildcards: List[Handler] = []
        self._history: List[Event] = []
        self._max_history = max_history
        self._paused = False

    # -- Subscribe ------------------------------------------------------

    def subscribe(self, event_type: EventType,
                  handler: Handler) -> str:
        """Subscribe to an event type. Returns subscription id."""
        sub_id = f"sub_{uuid.uuid4().hex[:8]}"
        # Wrap handler to track sub_id
        handler._sub_id = sub_id  # type: ignore
        self._handlers[event_type].append(handler)
        return sub_id

    def subscribe_all(self, handler: Handler) -> str:
        """Subscribe to all events."""
        sub_id = f"sub_{uuid.uuid4().hex[:8]}"
        handler._sub_id = sub_id  # type: ignore
        self._wildcards.append(handler)
        return sub_id

    def unsubscribe(self, sub_id: str) -> bool:
        """Remove a subscription."""
        for etype, handlers in self._handlers.items():
            for h in handlers:
                if getattr(h, "_sub_id", None) == sub_id:
                    handlers.remove(h)
                    return True
        for h in self._wildcards:
            if getattr(h, "_sub_id", None) == sub_id:
                self._wildcards.remove(h)
                return True
        return False

    # -- Publish --------------------------------------------------------

    def publish(self, event_type: EventType,
                data: Optional[Dict[str, Any]] = None,
                source: str = "",
                correlation_id: str = "") -> Event:
        """Publish an event."""
        event = Event(
            id=f"evt_{uuid.uuid4().hex[:8]}",
            type=event_type,
            timestamp=time.time(),
            data=data or {},
            source=source,
            correlation_id=correlation_id or f"corr_{uuid.uuid4().hex[:8]}",
        )
        self._history.append(event)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

        if not self._paused:
            # Specific handlers
            for handler in list(self._handlers.get(event_type, [])):
                try:
                    handler(event)
                except Exception:
                    pass  # Don't let handlers crash the bus
            # Wildcard handlers
            for handler in list(self._wildcards):
                try:
                    handler(event)
                except Exception:
                    pass
        return event

    # -- Convenience emitters --------------------------------------------

    def task_started(self, task_id: str, title: str,
                     **kwargs) -> Event:
        return self.publish(EventType.TASK_STARTED,
                            {"task_id": task_id, "title": title,
                             **kwargs})

    def task_completed(self, task_id: str, ok: bool,
                       output: str = "", **kwargs) -> Event:
        et = (EventType.TASK_COMPLETED if ok
              else EventType.TASK_FAILED)
        return self.publish(et, {"task_id": task_id,
                                 "output": output[:500], **kwargs})

    def tool_called(self, tool: str, args: Dict,
                    **kwargs) -> Event:
        return self.publish(EventType.TOOL_CALLED,
                            {"tool": tool,
                             "args_keys": list(args.keys()),
                             **kwargs})

    def tool_completed(self, tool: str, ok: bool,
                       **kwargs) -> Event:
        et = (EventType.TOOL_COMPLETED if ok
              else EventType.TOOL_FAILED)
        return self.publish(et, {"tool": tool, **kwargs})

    def tool_denied(self, tool: str, reason: str,
                    **kwargs) -> Event:
        return self.publish(EventType.TOOL_DENIED,
                            {"tool": tool, "reason": reason,
                             **kwargs})

    def phase_started(self, phase: str, task_id: str = "",
                      **kwargs) -> Event:
        return self.publish(EventType.PHASE_STARTED,
                            {"phase": phase, "task_id": task_id,
                             **kwargs})

    def phase_completed(self, phase: str, ok: bool = True,
                        **kwargs) -> Event:
        return self.publish(EventType.PHASE_COMPLETED,
                            {"phase": phase, "ok": ok, **kwargs})

    def error_occurred(self, error_type: str, message: str,
                       **kwargs) -> Event:
        return self.publish(EventType.ERROR_OCCURRED,
                            {"error_type": error_type,
                             "message": message[:500], **kwargs})

    # -- History & replay --------------------------------------------------

    def history(self, event_type: Optional[EventType] = None,
                limit: int = 100) -> List[Event]:
        """Get event history, optionally filtered."""
        events = self._history
        if event_type:
            events = [e for e in events if e.type == event_type]
        return events[-limit:]

    def count(self, event_type: Optional[EventType] = None) -> int:
        if event_type:
            return sum(1 for e in self._history
                       if e.type == event_type)
        return len(self._history)

    def clear_history(self) -> None:
        self._history.clear()

    def pause(self) -> None:
        """Pause event delivery (history still recorded)."""
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    # -- Statistics --------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        """Event statistics."""
        counts: Dict[str, int] = {}
        for e in self._history:
            key = e.type.value
            counts[key] = counts.get(key, 0) + 1
        return {
            "total_events": len(self._history),
            "by_type": counts,
            "subscriptions": sum(len(h)
                                 for h in self._handlers.values()),
            "wildcard_subscriptions": len(self._wildcards),
        }


# Global bus instance
_global_bus: Optional[EventBus] = None


def get_bus() -> EventBus:
    """Get the global event bus."""
    global _global_bus
    if _global_bus is None:
        _global_bus = EventBus()
    return _global_bus


def reset_bus() -> EventBus:
    """Reset the global bus (for testing)."""
    global _global_bus
    _global_bus = EventBus()
    return _global_bus
