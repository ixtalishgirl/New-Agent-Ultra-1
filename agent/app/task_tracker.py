"""
Task observability — timeline, status, cancellation.
Every task records: ID, model config, plan, tool calls, durations, tests, files changed.
"""
from __future__ import annotations
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TaskEvent:
    ts: float
    kind: str  # plan, tool_call, tool_result, test, file_change, approval, done
    detail: dict = field(default_factory=dict)


@dataclass
class Task:
    id: str
    prompt: str
    status: str = "PENDING"  # PENDING, RUNNING, WAITING_FOR_APPROVAL, SUCCEEDED, FAILED, CANCELLED
    events: list = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    cancelled: bool = False

    def log(self, kind: str, **detail):
        self.events.append(TaskEvent(ts=time.time(), kind=kind, detail=detail))

    def timeline(self) -> list[dict]:
        return [
            {"ts": e.ts, "kind": e.kind, **e.detail}
            for e in self.events
        ]


class TaskTracker:
    """Tracks all tasks with persistent JSONL log."""

    def __init__(self, log_dir: Path | None = None):
        self.log_dir = log_dir or Path("agent/logs")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.tasks: dict[str, Task] = {}

    def create(self, prompt: str) -> Task:
        task = Task(id=f"t_{uuid.uuid4().hex[:8]}", prompt=prompt[:200])
        task.status = "RUNNING"
        task.log("plan", prompt=prompt[:500])
        self.tasks[task.id] = task
        return task

    def get(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id)

    def cancel(self, task_id: str) -> bool:
        t = self.tasks.get(task_id)
        if t and t.status == "RUNNING":
            t.cancelled = True
            t.status = "CANCELLED"
            t.log("cancel", reason="user requested")
            self._persist(t)
            return True
        return False

    def finish(self, task: Task, ok: bool, summary: str = ""):
        task.status = "SUCCEEDED" if ok else "FAILED"
        task.log("done", ok=ok, summary=summary[:500])
        self._persist(task)

    def _persist(self, task: Task):
        path = self.log_dir / f"task_{task.id}.json"
        path.write_text(json.dumps({
            "id": task.id,
            "prompt": task.prompt,
            "status": task.status,
            "created_at": task.created_at,
            "timeline": task.timeline(),
        }, indent=2))
