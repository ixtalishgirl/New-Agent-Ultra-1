"""Task scheduler — cron-like scheduling for agent tasks.

Supports:
- One-time delayed tasks
- Recurring interval tasks
- Cron-expression tasks (basic)

No personality. No rules. Direct execution.
"""
from __future__ import annotations

import heapq
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class ScheduleType(Enum):
    ONCE = "once"          # Run once after delay
    INTERVAL = "interval"  # Run every N seconds
    CRON = "cron"          # Run on cron schedule


@dataclass
class ScheduledTask:
    id: str
    name: str
    schedule_type: ScheduleType
    func: Callable = field(repr=False)
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)
    # For ONCE: delay_seconds. For INTERVAL: interval_seconds.
    delay_seconds: float = 0
    interval_seconds: float = 60
    cron_expr: str = ""
    max_runs: int = 0  # 0 = unlimited
    runs: int = 0
    last_run: Optional[float] = None
    next_run: float = 0
    enabled: bool = True
    created_at: float = field(default_factory=time.time)

    def __lt__(self, other: "ScheduledTask") -> bool:
        return self.next_run < other.next_run


def _parse_cron(expr: str) -> Dict[str, List[int]]:
    """Parse cron expression (min hour dom mon dow).

    Supports: *, */n, a-b, a,b, single values.
    Returns dict of field -> sorted list of values.
    """
    fields = expr.strip().split()
    if len(fields) != 5:
        raise ValueError(f"Invalid cron: {expr} (need 5 fields)")

    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    result = {}
    names = ["minute", "hour", "dom", "month", "dow"]

    for i, (f, (lo, hi)) in enumerate(zip(fields, ranges)):
        values = set()
        for part in f.split(","):
            part = part.strip()
            if part == "*":
                values.update(range(lo, hi + 1))
            elif part.startswith("*/"):
                step = int(part[2:])
                values.update(range(lo, hi + 1, step))
            elif "-" in part:
                a, b = part.split("-", 1)
                values.update(range(int(a), int(b) + 1))
            else:
                values.add(int(part))
        # Validate
        for v in values:
            if not lo <= v <= hi:
                raise ValueError(f"Cron value {v} out of range "
                                 f"[{lo},{hi}] for {names[i]}")
        result[names[i]] = sorted(values)
    return result


def _next_cron_time(cron: Dict[str, List[int]],
                    after: float) -> float:
    """Find next time matching cron spec (minute resolution)."""
    import datetime
    dt = datetime.datetime.fromtimestamp(after)
    # Start from next minute
    dt = dt.replace(second=0, microsecond=0) + \
        datetime.timedelta(minutes=1)
    # Search up to 366 days
    for _ in range(366 * 24 * 60):
        # Note: dow 0=Sunday in cron, Monday=0 in Python
        py_dow = (dt.weekday() + 1) % 7
        if (dt.minute in cron["minute"] and
                dt.hour in cron["hour"] and
                dt.day in cron["dom"] and
                dt.month in cron["month"] and
                py_dow in cron["dow"]):
            return dt.timestamp()
        dt += datetime.timedelta(minutes=1)
    return after + 86400  # fallback: tomorrow


class Scheduler:
    """Thread-based task scheduler."""

    def __init__(self):
        self._tasks: Dict[str, ScheduledTask] = {}
        self._heap: List[ScheduledTask] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wakeup = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._errors: List[Dict[str, Any]] = []

    # -- Scheduling ------------------------------------------------------

    def once(self, name: str, func: Callable,
             delay_seconds: float = 0,
             *args, **kwargs) -> str:
        """Schedule a one-time task."""
        task = ScheduledTask(
            id=f"sched_{uuid.uuid4().hex[:8]}",
            name=name, schedule_type=ScheduleType.ONCE,
            func=func, args=args, kwargs=kwargs,
            delay_seconds=delay_seconds,
            next_run=time.time() + delay_seconds,
        )
        return self._add(task)

    def every(self, name: str, func: Callable,
              interval_seconds: float,
              *args, **kwargs) -> str:
        """Schedule a recurring task."""
        if interval_seconds <= 0:
            raise ValueError("interval must be positive")
        task = ScheduledTask(
            id=f"sched_{uuid.uuid4().hex[:8]}",
            name=name, schedule_type=ScheduleType.INTERVAL,
            func=func, args=args, kwargs=kwargs,
            interval_seconds=interval_seconds,
            next_run=time.time() + interval_seconds,
        )
        return self._add(task)

    def cron(self, name: str, func: Callable, expr: str,
             *args, **kwargs) -> str:
        """Schedule with cron expression."""
        parsed = _parse_cron(expr)  # validates
        task = ScheduledTask(
            id=f"sched_{uuid.uuid4().hex[:8]}",
            name=name, schedule_type=ScheduleType.CRON,
            func=func, args=args, kwargs=kwargs,
            cron_expr=expr,
            next_run=_next_cron_time(parsed, time.time()),
        )
        task.kwargs["_cron_parsed"] = parsed
        return self._add(task)

    def _add(self, task: ScheduledTask) -> str:
        with self._lock:
            self._tasks[task.id] = task
            heapq.heappush(self._heap, task)
        self._wakeup.set()  # Wake the loop to recalculate sleep
        return task.id

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.pop(task_id, None)
            if task:
                task.enabled = False
                return True
            return False

    def enable(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task.enabled = True
                task.next_run = time.time()
                heapq.heappush(self._heap, task)
                return True
            return False

    def disable(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task.enabled = False
                return True
            return False

    def list_tasks(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [{
                "id": t.id, "name": t.name,
                "type": t.schedule_type.value,
                "enabled": t.enabled,
                "runs": t.runs,
                "next_run": t.next_run,
                "next_in_seconds": max(0, t.next_run - time.time()),
            } for t in self._tasks.values()]

    # -- Execution loop ----------------------------------------------------

    def start(self) -> None:
        """Start the scheduler thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop,
                                        daemon=True,
                                        name="agent-scheduler")
        self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            due: List[ScheduledTask] = []

            with self._lock:
                while self._heap and self._heap[0].next_run <= now:
                    task = heapq.heappop(self._heap)
                    # Skip if cancelled or disabled
                    if task.id not in self._tasks or not task.enabled:
                        continue
                    due.append(task)

            for task in due:
                self._run_task(task)

            # Sleep until next task, wakeup signal, or stop
            self._wakeup.clear()
            with self._lock:
                if self._heap:
                    sleep_for = max(0.05, min(1.0,
                                             self._heap[0].next_run - time.time()))
                else:
                    sleep_for = 1.0
            # Wait for wakeup (new task), stop, or timeout
            self._wakeup.wait(sleep_for)
            if self._stop.is_set():
                break

    def _run_task(self, task: ScheduledTask) -> None:
        try:
            task.func(*task.args, **task.kwargs)
        except Exception as e:
            self._errors.append({
                "task_id": task.id, "name": task.name,
                "error": str(e), "at": time.time(),
            })
        finally:
            task.runs += 1
            task.last_run = time.time()

            # Reschedule or remove
            with self._lock:
                if task.id not in self._tasks:
                    return
                if task.max_runs > 0 and task.runs >= task.max_runs:
                    del self._tasks[task.id]
                    return
                if task.schedule_type == ScheduleType.ONCE:
                    del self._tasks[task.id]
                    return
                elif task.schedule_type == ScheduleType.INTERVAL:
                    task.next_run = time.time() + task.interval_seconds
                elif task.schedule_type == ScheduleType.CRON:
                    parsed = task.kwargs.get("_cron_parsed")
                    if parsed:
                        task.next_run = _next_cron_time(
                            parsed, time.time())
                    else:
                        task.next_run = time.time() + 60
                if task.enabled:
                    heapq.heappush(self._heap, task)

    def errors(self) -> List[Dict[str, Any]]:
        return list(self._errors)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()
