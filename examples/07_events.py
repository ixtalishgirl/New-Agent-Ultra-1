#!/usr/bin/env python3
"""Example 07: Event bus — pub/sub for agent lifecycle.

Demonstrates event publishing, subscribing, and notifications.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.app.event_bus import EventBus, EventType, reset_bus
from agent.app.notifications import (
    NotificationManager, Severity, ConsoleChannel,
)


def main():
    print("=" * 50)
    print("Event Bus Demo")
    print("=" * 50)

    reset_bus()
    bus = EventBus()

    # 1. Subscribe to events
    print("\n1. Subscribing to task events...")
    events_seen = []

    def on_task(event):
        events_seen.append(event)
        print(f"   → {event.type.value}: "
              f"{event.data.get('task_id')}")

    bus.subscribe(EventType.TASK_STARTED, on_task)
    bus.subscribe(EventType.TASK_COMPLETED, on_task)
    bus.subscribe(EventType.TASK_FAILED, on_task)

    # 2. Publish events
    print("\n2. Publishing events...")
    bus.task_started("task_1", "Process data")
    time.sleep(0.1)
    bus.task_completed("task_1", True, "Done!")
    bus.task_started("task_2", "Risky operation")
    bus.task_completed("task_2", False, "Something broke")

    print(f"\n   Seen {len(events_seen)} task events")

    # 3. Wildcard subscription
    print("\n3. Wildcard subscription (all events)...")
    all_events = []
    bus.subscribe_all(lambda e: all_events.append(e))
    bus.tool_called("shell", {"command": "ls"})
    bus.tool_denied("shell", "container_escape detected")
    print(f"   Seen {len(all_events)} events via wildcard")

    # 4. Notifications integration
    print("\n4. Notifications (auto from events)...")
    notifier = NotificationManager(bus=bus)
    notifier.add_channel(ConsoleChannel(use_color=False))

    # This triggers a warning notification via the event bus
    bus.publish(EventType.TOOL_DENIED, {
        "tool": "shell",
        "reason": "test denial",
    })

    # 5. Event history and stats
    print("\n5. Event statistics:")
    stats = bus.stats()
    print(f"   Total events: {stats['total_events']}")
    print(f"   By type:")
    for etype, count in sorted(stats["by_type"].items()):
        print(f"     - {etype}: {count}")

    # 6. History filtering
    print("\n6. Filtering history...")
    denied = bus.history(EventType.TOOL_DENIED)
    print(f"   Found {len(denied)} TOOL_DENIED events")

    print("\n✓ Event bus demo complete")


if __name__ == "__main__":
    main()
