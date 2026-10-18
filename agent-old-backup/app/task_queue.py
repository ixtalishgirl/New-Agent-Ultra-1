"""Task queue — FIFO/priority queue for agent tasks with persistence.

Per ChatGPT spec: agent/app/task_queue.py.
No personality. No rules. Direct execution.
"""
from __future__ import annotations

import heapq
import json
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class QueueItemStatus(Enum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class QueueItem:
    """A unit of work in the queue."""
    id: str
    payload: Dict[str, Any]  # {"tool": str, "args": dict} or task spec
    priority: int = 5  # lower = more urgent (heap)
    status: QueueItemStatus = QueueItemStatus.QUEUED
    created_at: float = field(default_factory=time.time)
    claimed_at: Optional[float] = None
    completed_at: Optional[float] = None
    attempts: int = 0
    max_attempts: int = 3
    result: Optional[str] = None
    error: Optional[str] = None
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "QueueItem":
        d = dict(d)
        d["status"] = QueueItemStatus(d["status"])
        return cls(**d)

    def __lt__(self, other: "QueueItem") -> bool:
        # Heap ordering: priority, then created_at
        return (self.priority, self.created_at) < (
            other.priority, other.created_at)


class TaskQueue:
    """Thread-safe priority task queue."""

    def __init__(self, persist_path: Optional[str] = None):
        self._heap: List[QueueItem] = []
        self._items: Dict[str, QueueItem] = {}
        self._lock = threading.Lock()
        self._persist_path = persist_path
        if persist_path:
            self._load()

    def enqueue(self, payload: Dict[str, Any], priority: int = 5,
                max_attempts: int = 3,
                tags: Optional[List[str]] = None) -> str:
        """Add work. Returns item id."""
        item = QueueItem(
            id=f"q_{uuid.uuid4().hex[:10]}",
            payload=payload,
            priority=priority,
            max_attempts=max_attempts,
            tags=tags or [],
        )
        with self._lock:
            heapq.heappush(self._heap, item)
            self._items[item.id] = item
        self._persist()
        return item.id

    def dequeue(self, block: bool = False,
                timeout: float = 1.0) -> Optional[QueueItem]:
        """Claim the highest-priority queued item."""
        deadline = time.time() + timeout if block else 0
        while True:
            with self._lock:
                while self._heap and self._heap[0].status != QueueItemStatus.QUEUED:
                    heapq.heappop(self._heap)
                if self._heap:
                    item = heapq.heappop(self._heap)
                    item.status = QueueItemStatus.CLAIMED
                    item.claimed_at = time.time()
                    item.attempts += 1
                    self._persist()
                    return item
            if not block or time.time() >= deadline:
                return None
            time.sleep(0.05)

    def complete(self, item_id: str, result: str = "") -> bool:
        with self._lock:
            item = self._items.get(item_id)
            if not item:
                return False
            item.status = QueueItemStatus.DONE
            item.completed_at = time.time()
            item.result = result
        self._persist()
        return True

    def fail(self, item_id: str, error: str = "",
             requeue: bool = True) -> bool:
        """Mark failed. Requeues if attempts remain and requeue=True."""
        with self._lock:
            item = self._items.get(item_id)
            if not item:
                return False
            item.error = error
            if requeue and item.attempts < item.max_attempts:
                item.status = QueueItemStatus.QUEUED
                item.claimed_at = None
                heapq.heappush(self._heap, item)
            else:
                item.status = QueueItemStatus.FAILED
                item.completed_at = time.time()
        self._persist()
        return True

    def cancel(self, item_id: str) -> bool:
        with self._lock:
            item = self._items.get(item_id)
            if not item:
                return False
            item.status = QueueItemStatus.CANCELLED
            item.completed_at = time.time()
        self._persist()
        return True

    def get(self, item_id: str) -> Optional[QueueItem]:
        with self._lock:
            return self._items.get(item_id)

    def list(self, status: Optional[QueueItemStatus] = None,
             limit: int = 100) -> List[QueueItem]:
        with self._lock:
            items = list(self._items.values())
        if status:
            items = [i for i in items if i.status == status]
        items.sort(key=lambda i: -i.created_at)
        return items[:limit]

    def stats(self) -> Dict[str, int]:
        with self._lock:
            counts: Dict[str, int] = {}
            for item in self._items.values():
                k = item.status.value
                counts[k] = counts.get(k, 0) + 1
            counts["heap_size"] = len(self._heap)
            return counts

    def clear_done(self, older_than_seconds: float = 3600) -> int:
        """Remove done/failed/cancelled items older than threshold."""
        cutoff = time.time() - older_than_seconds
        removed = 0
        with self._lock:
            for iid in list(self._items.keys()):
                item = self._items[iid]
                if item.status in (QueueItemStatus.DONE,
                                   QueueItemStatus.FAILED,
                                   QueueItemStatus.CANCELLED):
                    if (item.completed_at or 0) < cutoff:
                        del self._items[iid]
                        removed += 1
            # Rebuild heap without removed items
            self._heap = [i for i in self._heap if i.id in self._items]
            heapq.heapify(self._heap)
        self._persist()
        return removed

    def _persist(self) -> None:
        if not self._persist_path:
            return
        try:
            with self._lock:
                data = [i.to_dict() for i in self._items.values()]
            with open(self._persist_path, "w") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception:
            pass

    def _load(self) -> None:
        import os
        if not self._persist_path or not os.path.exists(self._persist_path):
            return
        try:
            with open(self._persist_path) as f:
                data = json.load(f)
            with self._lock:
                for d in data:
                    item = QueueItem.from_dict(d)
                    # Reset claimed items to queued on restart
                    if item.status == QueueItemStatus.CLAIMED:
                        item.status = QueueItemStatus.QUEUED
                        item.claimed_at = None
                    self._items[item.id] = item
                    if item.status == QueueItemStatus.QUEUED:
                        heapq.heappush(self._heap, item)
        except Exception:
            pass


class QueueWorker:
    """Background worker that drains a TaskQueue."""

    def __init__(self, queue: TaskQueue,
                 handler: Callable[[Dict[str, Any]], Dict[str, Any]],
                 num_threads: int = 2):
        self.queue = queue
        self.handler = handler
        self.num_threads = num_threads
        self._threads: List[threading.Thread] = []
        self._stop = threading.Event()

    def start(self) -> None:
        self._stop.clear()
        for i in range(self.num_threads):
            t = threading.Thread(target=self._loop, name=f"queue-worker-{i}",
                                 daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self, wait: bool = True) -> None:
        self._stop.set()
        if wait:
            for t in self._threads:
                t.join(timeout=5)
        self._threads = []

    def _loop(self) -> None:
        while not self._stop.is_set():
            item = self.queue.dequeue(block=True, timeout=0.5)
            if not item:
                continue
            try:
                res = self.handler(item.payload)
                if res.get("ok"):
                    self.queue.complete(item.id, str(res.get("output", ""))[:2000])
                else:
                    self.queue.fail(item.id, str(res.get("output", ""))[:500])
            except Exception as e:
                self.queue.fail(item.id, f"handler crash: {e}")
