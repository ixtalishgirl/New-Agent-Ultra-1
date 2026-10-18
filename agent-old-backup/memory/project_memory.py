"""Project memory — persistent project-scoped memory (ChatGPT spec).

Per spec: agent/memory/project_memory.py
Key-value + document store per project, SQLite-backed.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class MemoryEntry:
    key: str
    value: Any
    project: str = "default"
    updated_at: float = field(default_factory=time.time)
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ProjectMemory:
    """Project-scoped persistent memory."""

    def __init__(self, db_path: str = "/tmp/haley_project_memory.db"):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS kv (
                project TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL,
                tags TEXT DEFAULT '[]',
                PRIMARY KEY (project, key)
            );
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                tags TEXT DEFAULT '[]'
            );
            CREATE INDEX IF NOT EXISTS idx_docs_project
                ON documents(project);
        """)
        self._conn.commit()

    # -- Key-value --------------------------------------------------------

    def set(self, key: str, value: Any, project: str = "default",
            tags: Optional[List[str]] = None) -> None:
        self._conn.execute(
            """INSERT INTO kv (project, key, value, updated_at, tags)
               VALUES (?,?,?,?,?)
               ON CONFLICT(project, key) DO UPDATE SET
                 value=excluded.value, updated_at=excluded.updated_at,
                 tags=excluded.tags""",
            (project, key, json.dumps(value, default=str),
             time.time(), json.dumps(tags or [])))
        self._conn.commit()

    def get(self, key: str, project: str = "default",
            default: Any = None) -> Any:
        row = self._conn.execute(
            "SELECT value FROM kv WHERE project=? AND key=?",
            (project, key)).fetchone()
        return json.loads(row["value"]) if row else default

    def delete(self, key: str, project: str = "default") -> bool:
        cur = self._conn.execute(
            "DELETE FROM kv WHERE project=? AND key=?",
            (project, key))
        self._conn.commit()
        return cur.rowcount > 0

    def keys(self, project: str = "default",
             prefix: str = "") -> List[str]:
        rows = self._conn.execute(
            "SELECT key FROM kv WHERE project=? AND key LIKE ?",
            (project, f"{prefix}%")).fetchall()
        return [r["key"] for r in rows]

    def all(self, project: str = "default") -> Dict[str, Any]:
        rows = self._conn.execute(
            "SELECT key, value FROM kv WHERE project=?",
            (project,)).fetchall()
        return {r["key"]: json.loads(r["value"]) for r in rows}

    # -- Documents --------------------------------------------------------

    def add_document(self, title: str, content: str,
                     project: str = "default",
                     tags: Optional[List[str]] = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO documents (project, title, content, created_at, tags)"
            " VALUES (?,?,?,?,?)",
            (project, title, content, time.time(),
             json.dumps(tags or [])))
        self._conn.commit()
        return cur.lastrowid or 0

    def get_document(self, doc_id: int) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        return dict(row) if row else None

    def search_documents(self, query: str, project: str = "default",
                         limit: int = 10) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT id, title, substr(content,1,500) as snippet,
                      created_at FROM documents
               WHERE project=? AND (title LIKE ? OR content LIKE ?)
               ORDER BY created_at DESC LIMIT ?""",
            (project, f"%{query}%", f"%{query}%", limit)).fetchall()
        return [dict(r) for r in rows]

    def list_documents(self, project: str = "default",
                       limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, title, created_at FROM documents "
            "WHERE project=? ORDER BY created_at DESC LIMIT ?",
            (project, limit)).fetchall()
        return [dict(r) for r in rows]

    def delete_document(self, doc_id: int) -> bool:
        cur = self._conn.execute(
            "DELETE FROM documents WHERE id=?", (doc_id,))
        self._conn.commit()
        return cur.rowcount > 0

    # -- Projects ---------------------------------------------------------

    def projects(self) -> List[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT project FROM kv UNION "
            "SELECT DISTINCT project FROM documents").fetchall()
        return [r[0] for r in rows]

    def close(self) -> None:
        self._conn.close()
