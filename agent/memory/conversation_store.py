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


def _norm_chat_id(chat_id: Optional[str]) -> str:
    """Normalize a chat namespace id; anything blank becomes "main"."""
    cid = (chat_id or "").strip()
    return cid if cid else "main"


@dataclass
class Turn:
    """One conversation turn.

    chat_id namespaces the turn to one chat (dashboard side-chat id or
    "main" for the main chat). Old rows written before the chat_id column
    existed are treated as belonging to "main".
    """
    id: str
    session_id: str
    role: str
    content: str
    timestamp: float = field(default_factory=time.time)
    tokens: int = 0
    chat_id: str = "main"

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
        # Migrate pre-existing DBs: add the chat_id column if missing and
        # backfill legacy rows (written before namespacing) to "main".
        cols = [r[1] for r in
                self._conn.execute("PRAGMA table_info(turns)").fetchall()]
        if "chat_id" not in cols:
            self._conn.execute(
                "ALTER TABLE turns ADD COLUMN chat_id TEXT "
                "NOT NULL DEFAULT 'main'")
            self._conn.execute(
                "UPDATE turns SET chat_id = 'main' "
                "WHERE chat_id IS NULL OR chat_id = ''")
            self._conn.commit()
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_turns_chat "
            "ON turns(chat_id, timestamp)")
        self._conn.commit()

    def add(self, session_id: str, role: str,
            content: str, chat_id: str = "main") -> Turn:
        turn = Turn(id=f"turn_{uuid.uuid4().hex[:8]}",
                    session_id=session_id, role=role,
                    content=content,
                    chat_id=_norm_chat_id(chat_id))
        self._conn.execute(
            "INSERT INTO turns "
            "(id, session_id, role, content, timestamp, tokens, chat_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (turn.id, turn.session_id, turn.role, turn.content,
             turn.timestamp, turn.tokens, turn.chat_id))
        self._conn.commit()
        return turn

    # -- Per-chat namespaced API -------------------------------------------

    def save_message(self, chat_id: str, role: str,
                     content: str) -> Turn:
        """Persist one message under a chat namespace.

        chat_id: dashboard side-chat id, or "main" for the main chat.
        """
        cid = _norm_chat_id(chat_id)
        return self.add(session_id=cid, role=role, content=content,
                        chat_id=cid)

    def get_history(self, chat_id: str = "main", limit: int = 50,
                    before: Optional[float] = None) -> List[Turn]:
        """Return a chat's turns, oldest first. Legacy rows without a
        chat_id are treated as belonging to "main"."""
        cid = _norm_chat_id(chat_id)
        q = ("SELECT * FROM turns "
             "WHERE COALESCE(chat_id, 'main') = ?"
             + (" AND timestamp < ?" if before else "")
             + " ORDER BY timestamp ASC LIMIT ?")
        params: tuple = (cid, before, limit) if before else (cid, limit)
        rows = self._conn.execute(q, params).fetchall()
        return [Turn(**dict(r)) for r in rows]

    def delete_chat(self, chat_id: str) -> int:
        """Delete every turn stored under a chat namespace."""
        cur = self._conn.execute(
            "DELETE FROM turns WHERE COALESCE(chat_id, 'main') = ?",
            (_norm_chat_id(chat_id),))
        self._conn.commit()
        return cur.rowcount

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

    def search(self, query: str, limit: int = 20,
               chat_id: Optional[str] = None) -> List[Turn]:
        q = ("SELECT * FROM turns WHERE content LIKE ?"
             + (" AND COALESCE(chat_id, 'main') = ?" if chat_id else "")
             + " ORDER BY timestamp DESC LIMIT ?")
        params: tuple = ((f"%{query}%", _norm_chat_id(chat_id), limit)
                         if chat_id else (f"%{query}%", limit))
        rows = self._conn.execute(q, params).fetchall()
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
