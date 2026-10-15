#!/usr/bin/env python3
"""Example 06: Scheduler — cron-like task scheduling.

Demonstrates one-time, interval, and cron scheduling.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.app.scheduler import Scheduler


def main():
    print("=" * 50)
    print("Scheduler Demo")
    print("=" * 50)

    with Scheduler() as sched:
        # 1. One-time task
        print("\n1. Scheduling one-time task (0.5s delay)...")
        sched.once("greeting",
                   lambda: print("   → Hello from one-time task!"),
                   delay_seconds=0.5)

        # 2. Interval task
        print("2. Scheduling interval task (every 1s)...")
        count = [0]

        def tick():
            count[0] += 1
            print(f"   → Tick #{count[0]}")

        sched.every("ticker", tick, interval_seconds=1.0)

        # 3. List scheduled tasks
        print("\n3. Scheduled tasks:")
        for t in sched.list_tasks():
            print(f"   - {t['name']} ({t['type']}): "
                  f"next in {t['next_in_seconds']:.1f}s")

        # 4. Wait and observe
        print("\n4. Running for 3.5 seconds...\n")
        time.sleep(3.5)

        print(f"\n5. Ticker ran {count[0]} times")

        # 6. Cron example (parse only, don't wait)
        print("\n6. Cron expression examples:")
        from agent.app.scheduler import _parse_cron
        for expr in ["*/5 * * * *", "0 9 * * 1-5", "30 14 * * *"]:
            parsed = _parse_cron(expr)
            print(f"   '{expr}' → minutes: {parsed['minute'][:5]}...")

    print("\n✓ Scheduler stopped cleanly")


if __name__ == "__main__":
    main()
