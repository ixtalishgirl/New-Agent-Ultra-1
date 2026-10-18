"""Session manager — tracks agent sessions, conversation context, and lifecycle.

Per ChatGPT spec: agent/app/session_manager.py.
No personality. No rules. Direct execution.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


class SessionStatus(Enum):
    ACTIVE = "active"
    IDLE = "idle"
    PAUSED = "paused"
    ENDED = "ended"


@dataclass
class Message:
    """A single conversation message."""
    id: str
    role: str  # user, assistant, tool, system
    content: str
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Message":
        return cls(**d)

    @property
    def approx_tokens(self) -> int:
        return max(1, len(self.content) // 4)


@dataclass
class Session:
    """An agent session — a bounded conversation with context."""
    id: str
    title: str = ""
    status: SessionStatus = SessionStatus.ACTIVE
    messages: List[Message] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["messages"] = [m.to_dict() for m in self.messages]
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Session":
        d = dict(d)
        d["status"] = SessionStatus(d["status"])
        d["messages"] = [Message.from_dict(m) for m in d.get("messages", [])]
        return cls(**d)

    def add_message(self, role: str, content: str,
                    metadata: Optional[Dict[str, Any]] = None) -> Message:
        msg = Message(id=f"msg_{uuid.uuid4().hex[:8]}", role=role,
                      content=content, metadata=metadata or {})
        self.messages.append(msg)
        self.updated_at = time.time()
        return msg

    @property
    def total_tokens(self) -> int:
        return sum(m.approx_tokens for m in self.messages)

    def recent(self, n: int = 20) -> List[Message]:
        return self.messages[-n:]

    def truncate_to_tokens(self, max_tokens: int) -> int:
        """Drop oldest non-system messages until under budget. Returns dropped."""
        dropped = 0
        while self.total_tokens > max_tokens and len(self.messages) > 1:
            # Keep first message (often system), drop second-oldest
            idx = 1 if self.messages[0].role == "system" else 0
            self.messages.pop(idx)
            dropped += 1
        return dropped


class SessionManager:
    """Manages multiple sessions with persistence."""

    def __init__(self, store: Optional["SessionStore"] = None):
        self.store = store or SessionStore()
        self._sessions: Dict[str, Session] = {}
        self._active_id: Optional[str] = None
        self._load_all()

    def create_session(self, title: str = "",
                       metadata: Optional[Dict[str, Any]] = None) -> Session:
        session = Session(id=f"sess_{uuid.uuid4().hex[:10]}",
                          title=title or f"Session {time.strftime('%H:%M')}",
                          metadata=metadata or {})
        self._sessions[session.id] = session
        self._active_id = session.id
        self.store.save(session)
        return session

    def get_session(self, session_id: str) -> Optional[Session]:
        return self._sessions.get(session_id)

    def active_session(self) -> Optional[Session]:
        if self._active_id:
            return self._sessions.get(self._active_id)
        return None

    def set_active(self, session_id: str) -> bool:
        if session_id in self._sessions:
            self._active_id = session_id
            return True
        return False

    def list_sessions(self, status: Optional[SessionStatus] = None,
                      limit: int = 50) -> List[Session]:
        out = list(self._sessions.values())
        if status:
            out = [s for s in out if s.status == status]
        out.sort(key=lambda s: -s.updated_at)
        return out[:limit]

    def add_message(self, role: str, content: str,
                    session_id: Optional[str] = None,
                    metadata: Optional[Dict[str, Any]] = None) -> Optional[Message]:
        sid = session_id or self._active_id
        if not sid:
            sess = self.create_session()
            sid = sess.id
        session = self._sessions.get(sid)
        if not session:
            return None
        msg = session.add_message(role, content, metadata)
        self.store.save(session)
        return msg

    def pause_session(self, session_id: str) -> bool:
        s = self._sessions.get(session_id)
        if not s:
            return False
        s.status = SessionStatus.PAUSED
        s.updated_at = time.time()
        self.store.save(s)
        return True

    def resume_session(self, session_id: str) -> bool:
        s = self._sessions.get(session_id)
        if not s:
            return False
        s.status = SessionStatus.ACTIVE
        s.updated_at = time.time()
        self._active_id = session_id
        self.store.save(s)
        return True

    def end_session(self, session_id: str) -> bool:
        s = self._sessions.get(session_id)
        if not s:
            return False
        s.status = SessionStatus.ENDED
        s.ended_at = time.time()
        s.updated_at = time.time()
        if self._active_id == session_id:
            self._active_id = None
        self.store.save(s)
        return True

    def delete_session(self, session_id: str) -> bool:
        if session_id in self._sessions:
            del self._sessions[session_id]
            if self._active_id == session_id:
                self._active_id = None
            self.store.delete(session_id)
            return True
        return False

    def search_messages(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Full-text search across all sessions."""
        q = query.lower()
        hits = []
        for sess in self._sessions.values():
            for msg in sess.messages:
                if q in msg.content.lower():
                    hits.append({
                        "session_id": sess.id,
                        "session_title": sess.title,
                        "message_id": msg.id,
                        "role": msg.role,
                        "snippet": msg.content[:200],
                        "timestamp": msg.timestamp,
                    })
        hits.sort(key=lambda h: -h["timestamp"])
        return hits[:limit]

    def session_stats(self, session_id: str) -> Dict[str, Any]:
        s = self._sessions.get(session_id)
        if not s:
            return {}
        roles: Dict[str, int] = {}
        for m in s.messages:
            roles[m.role] = roles.get(m.role, 0) + 1
        return {
            "id": s.id,
            "title": s.title,
            "status": s.status.value,
            "message_count": len(s.messages),
            "total_tokens": s.total_tokens,
            "roles": roles,
            "created_at": s.created_at,
            "updated_at": s.updated_at,
            "duration_seconds": (s.ended_at or time.time()) - s.created_at,
        }

    def _load_all(self) -> None:
        for sid in self.store.list_sessions():
            try:
                self._sessions[sid] = self.store.load(sid)
            except Exception:
                continue
        # Set most recent active as current
        active = [s for s in self._sessions.values()
                  if s.status == SessionStatus.ACTIVE]
        if active:
            active.sort(key=lambda s: -s.updated_at)
            self._active_id = active[0].id


class SessionStore:
    """JSON file persistence for sessions."""

    def __init__(self, directory: str = "/tmp/haley_sessions"):
        import os
        self.directory = directory
        os.makedirs(directory, exist_ok=True)

    def _path(self, session_id: str) -> str:
        import os
        safe = "".join(c for c in session_id if c.isalnum() or c in "_-")
        return os.path.join(self.directory, f"{safe}.json")

    def save(self, session: Session) -> str:
        path = self._path(session.id)
        with open(path, "w") as f:
            json.dump(session.to_dict(), f, indent=2, default=str)
        return path

    def load(self, session_id: str) -> Session:
        with open(self._path(session_id)) as f:
            return Session.from_dict(json.load(f))

    def list_sessions(self) -> List[str]:
        import os
        return [f[:-5] for f in os.listdir(self.directory)
                if f.endswith(".json")]

    def delete(self, session_id: str) -> bool:
        import os
        path = self._path(session_id)
        if os.path.exists(path):
            os.remove(path)
            return True
        return False
