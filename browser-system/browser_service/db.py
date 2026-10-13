"""Optional SQLite research history.

Enabled with BROWSER_ENABLE_HISTORY=1. Stores short observations
(task_id, url, title, summary, timestamp) so the Node app can review
what the browser service has seen. Nothing sensitive is stored —
form field values are never recorded.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path


class HistoryStore:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS observations (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       task_id TEXT NOT NULL,
                       url TEXT NOT NULL,
                       title TEXT,
                       summary TEXT,
                       created_at REAL NOT NULL
                   )"""
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_obs_task ON observations(task_id)"
            )
            self._conn.commit()

    def record(self, task_id: str, url: str, title: str = "", summary: str = "") -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO observations (task_id, url, title, summary, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (task_id, url, title[:500], summary[:2000], time.time()),
            )
            self._conn.commit()
            return cur.lastrowid

    def list(self, task_id: str | None = None, limit: int = 100) -> list[dict]:
        with self._lock:
            if task_id:
                rows = self._conn.execute(
                    "SELECT id, task_id, url, title, summary, created_at"
                    " FROM observations WHERE task_id = ? ORDER BY id DESC LIMIT ?",
                    (task_id, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, task_id, url, title, summary, created_at"
                    " FROM observations ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [
            {
                "id": r[0], "task_id": r[1], "url": r[2], "title": r[3],
                "summary": r[4], "created_at": r[5],
            }
            for r in rows
        ]

    def clear(self, task_id: str | None = None) -> int:
        with self._lock:
            if task_id:
                cur = self._conn.execute(
                    "DELETE FROM observations WHERE task_id = ?", (task_id,))
            else:
                cur = self._conn.execute("DELETE FROM observations")
            self._conn.commit()
            return cur.rowcount
