"""
Memory stores — Supabase-backed with SQLite fallback.
ahsan's order: agent uses Supabase for memory.
"""
from __future__ import annotations
import json
import os
import sqlite3
import time
import urllib.request
from pathlib import Path


class SupabaseClient:
    """Minimal Supabase REST client (stdlib only, no extra deps)."""

    def __init__(self):
        self.url = os.environ.get("SUPABASE_URL", "").rstrip("/")
        self.key = os.environ.get("SUPABASE_ANON_KEY", "")
        self.enabled = bool(self.url and self.key)

    def _req(self, method: str, path: str, data: dict | None = None,
             params: str = "") -> dict | list | None:
        if not self.enabled:
            return None
        url = f"{self.url}/rest/v1/{path}{params}"
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        body = json.dumps(data).encode() if data else None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                text = resp.read().decode()
                return json.loads(text) if text else []
        except Exception:
            return None

    def insert(self, table: str, row: dict):
        return self._req("POST", table, row)

    def select(self, table: str, params: str = "?select=*&order=created_at.desc&limit=50"):
        return self._req("GET", table, None, params)


class ConversationStore:
    """Chat/research history. Supabase table `halye_conversations`, SQLite fallback."""

    def __init__(self, db_path: Path | None = None):
        self.sb = SupabaseClient()
        self.db_path = db_path or Path("agent/workspace/memory.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_sqlite()

    def _init_sqlite(self):
        con = sqlite3.connect(self.db_path)
        con.execute("""CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT, content TEXT, created_at REAL)""")
        con.commit()
        con.close()

    def add(self, role: str, content: str):
        row = {"role": role, "content": content, "created_at": time.time()}
        if self.sb.enabled:
            r = self.sb.insert("halye_conversations", row)
            if r is not None:
                return
        # Fallback to SQLite
        con = sqlite3.connect(self.db_path)
        con.execute("INSERT INTO conversations (role, content, created_at) VALUES (?,?,?)",
                    (role, content, time.time()))
        con.commit()
        con.close()

    def recent(self, limit: int = 20) -> list[dict]:
        if self.sb.enabled:
            r = self.sb.select("halye_conversations",
                               f"?select=*&order=created_at.desc&limit={limit}")
            if r:
                return list(reversed(r))
        con = sqlite3.connect(self.db_path)
        rows = con.execute(
            "SELECT role, content, created_at FROM conversations ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
        con.close()
        return [{"role": r[0], "content": r[1], "created_at": r[2]}
                for r in reversed(rows)]


class ProjectMemory:
    """Project notes: structure, decisions, known bugs. Supabase `halye_project_memory`."""

    def __init__(self, db_path: Path | None = None):
        self.sb = SupabaseClient()
        self.db_path = db_path or Path("agent/workspace/memory.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db_path)
        con.execute("""CREATE TABLE IF NOT EXISTS project_memory (
            key TEXT PRIMARY KEY, value TEXT, updated_at REAL)""")
        con.commit()
        con.close()

    def set(self, key: str, value: str):
        row = {"key": key, "value": value, "updated_at": time.time()}
        if self.sb.enabled:
            # Upsert via POST with merge-duplicates
            r = self.sb._req("POST", "halye_project_memory?on_conflict=key", row,
                             "")
            # Fallback: try plain insert
            if r is None:
                pass
        con = sqlite3.connect(self.db_path)
        con.execute("INSERT OR REPLACE INTO project_memory VALUES (?,?,?)",
                    (key, value, time.time()))
        con.commit()
        con.close()

    def get(self, key: str) -> str | None:
        if self.sb.enabled:
            r = self.sb.select("halye_project_memory",
                               f"?select=value&key=eq.{key}&limit=1")
            if r and len(r) > 0:
                return r[0].get("value")
        con = sqlite3.connect(self.db_path)
        row = con.execute("SELECT value FROM project_memory WHERE key=?",
                          (key,)).fetchone()
        con.close()
        return row[0] if row else None

    def all(self) -> dict:
        con = sqlite3.connect(self.db_path)
        rows = con.execute("SELECT key, value FROM project_memory").fetchall()
        con.close()
        return {k: v for k, v in rows}
