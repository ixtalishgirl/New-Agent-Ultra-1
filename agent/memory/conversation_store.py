"""Conversation store — persistent conversation memory (ChatGPT spec).

Per spec: agent/memory/conversation_store.py
SQLite-backed conversation history with search and summarization.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class Turn:
    """One conversation turn."""
    id: str
    session_id: str
    role: str
    content: str
    timestamp: float = field(default_factory=time.time)
    tokens: int = 0

    def __post_init__(self) -> None:
        if not self.tokens:
            self.tokens = max(1, len(self.content) // 4)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ConversationStore:
    """SQLite-backed conversation storage."""

    def __init__(self, db_path: str = "/tmp/haley_conversations.db"):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS turns (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp REAL NOT NULL,
                tokens INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_turns_session
                ON turns(session_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_turns_time
                ON turns(timestamp);
        """)
        self._conn.commit()

    def add(self, session_id: str, role: str,
            content: str) -> Turn:
        turn = Turn(id=f"turn_{uuid.uuid4().hex[:8]}",
                    session_id=session_id, role=role,
                    content=content)
        self._conn.execute(
            "INSERT INTO turns VALUES (?,?,?,?,?,?)",
            (turn.id, turn.session_id, turn.role, turn.content,
             turn.timestamp, turn.tokens))
        self._conn.commit()
        return turn

    def history(self, session_id: str, limit: int = 50,
                before: Optional[float] = None) -> List[Turn]:
        q = ("SELECT * FROM turns WHERE session_id = ?"
             + (" AND timestamp < ?" if before else "")
             + " ORDER BY timestamp DESC LIMIT ?")
        params: tuple = (session_id, before, limit) if before else (
            session_id, limit)
        rows = self._conn.execute(q, params).fetchall()
        turns = [Turn(**dict(r)) for r in rows]
        return list(reversed(turns))

    def search(self, query: str, limit: int = 20) -> List[Turn]:
        rows = self._conn.execute(
            "SELECT * FROM turns WHERE content LIKE ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (f"%{query}%", limit)).fetchall()
        return [Turn(**dict(r)) for r in rows]

    def sessions(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT session_id, COUNT(*) as turns,
                      MIN(timestamp) as started,
                      MAX(timestamp) as last_active,
                      SUM(tokens) as tokens
               FROM turns GROUP BY session_id
               ORDER BY last_active DESC LIMIT ?""",
            (limit,)).fetchall()
        return [dict(r) for r in rows]

    def delete_session(self, session_id: str) -> int:
        cur = self._conn.execute(
            "DELETE FROM turns WHERE session_id = ?", (session_id,))
        self._conn.commit()
        return cur.rowcount

    def prune(self, older_than_days: int = 90) -> int:
        cutoff = time.time() - older_than_days * 86400
        cur = self._conn.execute(
            "DELETE FROM turns WHERE timestamp < ?", (cutoff,))
        self._conn.commit()
        return cur.rowcount

    def stats(self) -> Dict[str, Any]:
        row = self._conn.execute(
            "SELECT COUNT(*) as turns, SUM(tokens) as tokens, "
            "COUNT(DISTINCT session_id) as sessions FROM turns").fetchone()
        return dict(row) if row else {}

    def close(self) -> None:
        self._conn.close()
