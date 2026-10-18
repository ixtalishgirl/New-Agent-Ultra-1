"""Episodic memory — timestamped events with actors, actions, outcomes.

SQLite backend (Supabase sync optional via stores.SupabaseClient).
Episodes are the agent's autobiographical log: what happened, who was
involved, what was done, and how it turned out.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Episode:
    """One remembered event."""
    id: str = ""
    timestamp: float = 0.0
    event: str = ""
    actors: list[str] = field(default_factory=list)
    action: str = ""
    outcome: str = ""
    emotion_tags: list[str] = field(default_factory=list)
    context: dict = field(default_factory=dict)
    importance: float = 0.5  # 0..1 — how worth keeping

    def to_dict(self) -> dict:
        return {
            "id": self.id, "timestamp": self.timestamp,
            "event": self.event, "actors": self.actors,
            "action": self.action, "outcome": self.outcome,
            "emotion_tags": self.emotion_tags, "context": self.context,
            "importance": self.importance,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Episode":
        return cls(
            id=row["id"], timestamp=row["timestamp"],
            event=row["event"], actors=json.loads(row["actors"] or "[]"),
            action=row["action"] or "", outcome=row["outcome"] or "",
            emotion_tags=json.loads(row["emotion_tags"] or "[]"),
            context=json.loads(row["context"] or "{}"),
            importance=row["importance"],
        )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY,
    timestamp REAL NOT NULL,
    event TEXT NOT NULL,
    actors TEXT NOT NULL DEFAULT '[]',
    action TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    emotion_tags TEXT NOT NULL DEFAULT '[]',
    context TEXT NOT NULL DEFAULT '{}',
    importance REAL NOT NULL DEFAULT 0.5,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_episodes_ts ON episodes(timestamp);
CREATE INDEX IF NOT EXISTS idx_episodes_importance ON episodes(importance);
"""


class EpisodicMemory:
    """Timestamped episode log with time/actor/tag recall and summarization."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path else Path(
            "agent/workspace/episodic.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    # -- setup -------------------------------------------------------------
    def _init(self) -> None:
        con = self._connect()
        con.executescript(_SCHEMA)
        con.commit()
        con.close()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    # -- writing -----------------------------------------------------------
    def store_episode(self, event: str, actors: list[str] | None = None,
                      action: str = "", outcome: str = "",
                      emotion_tags: list[str] | None = None,
                      context: dict | None = None,
                      importance: float = 0.5,
                      timestamp: float | None = None) -> str:
        """Store one episode; returns its id."""
        ep_id = uuid.uuid4().hex[:16]
        ts = timestamp if timestamp is not None else time.time()
        importance = max(0.0, min(1.0, float(importance)))
        con = self._connect()
        con.execute(
            """INSERT INTO episodes
               (id, timestamp, event, actors, action, outcome,
                emotion_tags, context, importance, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (ep_id, ts, event, json.dumps(actors or []), action, outcome,
             json.dumps(emotion_tags or []), json.dumps(context or {}),
             importance, time.time()))
        con.commit()
        con.close()
        return ep_id

    def store_episodes(self, episodes: list[Episode]) -> list[str]:
        """Batch insert; returns ids."""
        ids: list[str] = []
        con = self._connect()
        now = time.time()
        for ep in episodes:
            ep_id = ep.id or uuid.uuid4().hex[:16]
            con.execute(
                """INSERT OR REPLACE INTO episodes
                   (id, timestamp, event, actors, action, outcome,
                    emotion_tags, context, importance, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (ep_id, ep.timestamp or now, ep.event,
                 json.dumps(ep.actors), ep.action, ep.outcome,
                 json.dumps(ep.emotion_tags), json.dumps(ep.context),
                 max(0.0, min(1.0, ep.importance)), now))
            ids.append(ep_id)
        con.commit()
        con.close()
        return ids

    def update_episode(self, ep_id: str, **fields) -> bool:
        """Patch fields of an episode (event, action, outcome, importance...)."""
        allowed = {"event", "action", "outcome", "importance"}
        sets, params = [], []
        for key, value in fields.items():
            if key in allowed:
                sets.append(f"{key} = ?")
                params.append(value)
            elif key in ("actors", "emotion_tags", "context"):
                sets.append(f"{key} = ?")
                params.append(json.dumps(value))
        if not sets:
            return False
        params.append(ep_id)
        con = self._connect()
        cur = con.execute(
            f"UPDATE episodes SET {', '.join(sets)} WHERE id = ?", params)
        con.commit()
        changed = cur.rowcount > 0
        con.close()
        return changed

    def delete_episode(self, ep_id: str) -> bool:
        con = self._connect()
        cur = con.execute("DELETE FROM episodes WHERE id = ?", (ep_id,))
        con.commit()
        changed = cur.rowcount > 0
        con.close()
        return changed

    def get_episode(self, ep_id: str) -> Episode | None:
        con = self._connect()
        row = con.execute(
            "SELECT * FROM episodes WHERE id = ?", (ep_id,)).fetchone()
        con.close()
        return Episode.from_row(row) if row else None

    # -- recall ------------------------------------------------------------
    def recall_by_time(self, start: float, end: float,
                       limit: int = 100,
                       min_importance: float = 0.0) -> list[Episode]:
        """Episodes whose timestamp falls in [start, end], oldest first."""
        con = self._connect()
        rows = con.execute(
            """SELECT * FROM episodes
               WHERE timestamp >= ? AND timestamp <= ?
                 AND importance >= ?
               ORDER BY timestamp ASC LIMIT ?""",
            (start, end, min_importance, limit)).fetchall()
        con.close()
        return [Episode.from_row(r) for r in rows]

    def recall_recent(self, limit: int = 20,
                      min_importance: float = 0.0) -> list[Episode]:
        """Most recent episodes, newest first."""
        con = self._connect()
        rows = con.execute(
            """SELECT * FROM episodes WHERE importance >= ?
               ORDER BY timestamp DESC LIMIT ?""",
            (min_importance, limit)).fetchall()
        con.close()
        return [Episode.from_row(r) for r in rows]

    def recall_by_actor(self, actor: str, limit: int = 100) -> list[Episode]:
        """Episodes involving an actor (case-insensitive substring match)."""
        like = f"%{actor.lower()}%"
        con = self._connect()
        rows = con.execute(
            """SELECT * FROM episodes
               WHERE lower(actors) LIKE ?
               ORDER BY timestamp DESC LIMIT ?""",
            (like, limit)).fetchall()
        con.close()
        return [Episode.from_row(r) for r in rows]

    def recall_by_tag(self, tag: str, limit: int = 100) -> list[Episode]:
        """Episodes carrying an emotion tag (case-insensitive)."""
        like = f"%{tag.lower()}%"
        con = self._connect()
        rows = con.execute(
            """SELECT * FROM episodes
               WHERE lower(emotion_tags) LIKE ?
               ORDER BY timestamp DESC LIMIT ?""",
            (like, limit)).fetchall()
        con.close()
        return [Episode.from_row(r) for r in rows]

    def search(self, query: str, limit: int = 50) -> list[Episode]:
        """Full-text-ish search over event/action/outcome via LIKE.

        All query words must appear (AND semantics).
        """
        words = [w for w in re.findall(r"\w+", query.lower()) if w]
        if not words:
            return []
        clauses = " AND ".join(
            "(lower(event) LIKE ? OR lower(action) LIKE ? "
            "OR lower(outcome) LIKE ?)" for _ in words)
        params: list = []
        for w in words:
            like = f"%{w}%"
            params += [like, like, like]
        params.append(limit)
        con = self._connect()
        rows = con.execute(
            f"SELECT * FROM episodes WHERE {clauses} "
            f"ORDER BY importance DESC, timestamp DESC LIMIT ?",
            params).fetchall()
        con.close()
        return [Episode.from_row(r) for r in rows]

    def recall_around(self, timestamp: float, window_s: float = 3600,
                      limit: int = 50) -> list[Episode]:
        """Episodes within ±window_s of a timestamp."""
        return self.recall_by_time(timestamp - window_s,
                                   timestamp + window_s, limit=limit)

    def count(self) -> int:
        con = self._connect()
        n = con.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        con.close()
        return int(n)

    def actors_list(self) -> list[str]:
        """All distinct actors ever recorded."""
        seen: set[str] = set()
        con = self._connect()
        for (blob,) in con.execute("SELECT actors FROM episodes"):
            try:
                for a in json.loads(blob or "[]"):
                    seen.add(str(a))
            except (ValueError, TypeError):
                continue
        con.close()
        return sorted(seen)

    def tags_list(self) -> list[str]:
        """All distinct emotion tags ever recorded."""
        seen: set[str] = set()
        con = self._connect()
        for (blob,) in con.execute("SELECT emotion_tags FROM episodes"):
            try:
                for t in json.loads(blob or "[]"):
                    seen.add(str(t))
            except (ValueError, TypeError):
                continue
        con.close()
        return sorted(seen)

    # -- summarization -----------------------------------------------------
    def summarize_period(self, start: float, end: float,
                         max_episodes: int = 200) -> dict:
        """Generate a structured summary of episodes in a time range.

        Returns counts, top actors/tags, outcome sentiment split, and a
        generated narrative paragraph (template-based, no model needed).
        """
        episodes = self.recall_by_time(start, end, limit=max_episodes)
        if not episodes:
            return {"count": 0, "summary": "No episodes in this period.",
                    "actors": {}, "tags": {}, "outcomes": {}}

        actor_counts: dict[str, int] = {}
        tag_counts: dict[str, int] = {}
        outcome_kinds: dict[str, int] = {"success": 0, "failure": 0,
                                         "neutral": 0}
        for ep in episodes:
            for a in ep.actors:
                actor_counts[a] = actor_counts.get(a, 0) + 1
            for t in ep.emotion_tags:
                tag_counts[t] = tag_counts.get(t, 0) + 1
            outcome_kinds[self._classify_outcome(ep.outcome)] += 1

        top_actors = sorted(actor_counts.items(), key=lambda kv: kv[1],
                            reverse=True)[:5]
        top_tags = sorted(tag_counts.items(), key=lambda kv: kv[1],
                          reverse=True)[:5]
        top_events = [ep.event for ep in sorted(
            episodes, key=lambda e: e.importance, reverse=True)[:5]]

        dur_h = max(1.0, (end - start) / 3600.0)
        narrative = (
            f"In this period {len(episodes)} episodes were recorded "
            f"(~{len(episodes) / dur_h:.1f}/hour). "
            f"Outcomes: {outcome_kinds['success']} success, "
            f"{outcome_kinds['failure']} failure, "
            f"{outcome_kinds['neutral']} neutral. "
        )
        if top_actors:
            narrative += "Most involved: " + ", ".join(
                f"{a} ({c}x)" for a, c in top_actors) + ". "
        if top_tags:
            narrative += "Dominant tags: " + ", ".join(
                f"{t} ({c}x)" for t, c in top_tags) + ". "
        if top_events:
            narrative += "Key events: " + "; ".join(top_events[:3]) + "."

        return {
            "count": len(episodes),
            "start": start, "end": end,
            "actors": dict(top_actors),
            "tags": dict(top_tags),
            "outcomes": outcome_kinds,
            "key_events": top_events,
            "summary": narrative,
        }

    _POSITIVE_RES = re.compile(
        r"\b(succeed\w*|success\w*|complet\w*|done|fix\w*|pass\w*|"
        r"work\w*|liv\w*|ok|resolv\w*|shipp\w*|launch\w*)\b",
        re.I)
    _NEGATIVE_RES = re.compile(
        r"\b(fail\w*|error\w*|broke\w*|crash\w*|block\w*|stuck|"
        r"deni\w*|wrong|bug\w*|reject\w*|timed?\s*out)\b", re.I)

    @classmethod
    def _classify_outcome(cls, outcome: str) -> str:
        o = outcome or ""
        if cls._NEGATIVE_RES.search(o):
            return "failure"
        if cls._POSITIVE_RES.search(o):
            return "success"
        return "neutral"

    def daily_digest(self, day_timestamp: float) -> dict:
        """Summarize one calendar day (UTC) containing day_timestamp."""
        day_start = (day_timestamp // 86400) * 86400
        return self.summarize_period(day_start, day_start + 86400)

    # -- pruning -----------------------------------------------------------
    def forget_before(self, timestamp: float,
                      keep_important: float = 0.8) -> int:
        """Delete episodes older than timestamp.

        Episodes with importance >= keep_important are preserved.
        Returns the number deleted.
        """
        con = self._connect()
        cur = con.execute(
            "DELETE FROM episodes WHERE timestamp < ? AND importance < ?",
            (timestamp, keep_important))
        con.commit()
        n = cur.rowcount
        con.close()
        return int(n)

    def forget_by_tag(self, tag: str) -> int:
        """Delete all episodes carrying a given emotion tag."""
        like = f"%{tag.lower()}%"
        con = self._connect()
        cur = con.execute(
            "DELETE FROM episodes WHERE lower(emotion_tags) LIKE ?", (like,))
        con.commit()
        n = cur.rowcount
        con.close()
        return int(n)

    def prune_to_size(self, max_episodes: int,
                      keep_important: float = 0.8) -> int:
        """Keep at most max_episodes; drop oldest low-importance first."""
        total = self.count()
        if total <= max_episodes:
            return 0
        con = self._connect()
        rows = con.execute(
            """SELECT id FROM episodes WHERE importance < ?
               ORDER BY importance ASC, timestamp ASC LIMIT ?""",
            (keep_important, total - max_episodes)).fetchall()
        ids = [r[0] for r in rows]
        deleted = 0
        if ids:
            placeholders = ",".join("?" for _ in ids)
            cur = con.execute(
                f"DELETE FROM episodes WHERE id IN ({placeholders})", ids)
            deleted = cur.rowcount
        con.commit()
        con.close()
        return int(deleted)

    # -- import / export -----------------------------------------------------
    def export_json(self) -> list[dict]:
        """All episodes as JSON-serializable dicts (oldest first)."""
        con = self._connect()
        rows = con.execute(
            "SELECT * FROM episodes ORDER BY timestamp ASC").fetchall()
        con.close()
        return [Episode.from_row(r).to_dict() for r in rows]

    def import_json(self, data: list[dict]) -> int:
        """Import episodes from export_json output. Returns count added."""
        episodes = []
        for d in data:
            try:
                episodes.append(Episode(
                    id=d.get("id", ""), timestamp=float(d.get("timestamp", 0)),
                    event=d.get("event", ""), actors=d.get("actors", []),
                    action=d.get("action", ""), outcome=d.get("outcome", ""),
                    emotion_tags=d.get("emotion_tags", []),
                    context=d.get("context", {}),
                    importance=float(d.get("importance", 0.5))))
            except (TypeError, ValueError):
                continue
        self.store_episodes(episodes)
        return len(episodes)

    def timeline(self, limit: int = 50) -> list[str]:
        """Human-readable one-line timeline of recent episodes."""
        lines = []
        for ep in self.recall_recent(limit):
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(ep.timestamp))
            actors = f" [{', '.join(ep.actors)}]" if ep.actors else ""
            lines.append(f"{ts} — {ep.event}{actors} → {ep.outcome}")
        return lines

    # -- scored recall -------------------------------------------------------
    def recall_scored(self, query: str, limit: int = 20,
                      now: float | None = None) -> list[tuple[Episode, float]]:
        """Recall ranked by keyword overlap, importance, and recency.

        score = 2*keyword_hits + importance - recency_penalty
        Returns [(episode, score)] sorted best-first.
        """
        now = now if now is not None else time.time()
        words = set(re.findall(r"\w+", query.lower()))
        if not words:
            return []
        con = self._connect()
        rows = con.execute("SELECT * FROM episodes").fetchall()
        con.close()
        scored: list[tuple[Episode, float]] = []
        for row in rows:
            ep = Episode.from_row(row)
            blob = f"{ep.event} {ep.action} {ep.outcome}".lower()
            hits = sum(1 for w in words if w in blob)
            if not hits:
                continue
            age_days = max(0.0, (now - ep.timestamp) / 86400.0)
            recency_penalty = min(1.5, age_days / 30.0)  # full penalty at 30d
            score = 2.0 * hits + ep.importance - recency_penalty
            scored.append((ep, round(score, 3)))
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored[:limit]

    def recall_by_outcome(self, kind: str, limit: int = 100) -> list[Episode]:
        """Episodes whose outcome classifies as success/failure/neutral."""
        kind = kind.lower()
        if kind not in ("success", "failure", "neutral"):
            raise ValueError(f"unknown outcome kind: {kind}")
        con = self._connect()
        rows = con.execute(
            "SELECT * FROM episodes ORDER BY timestamp DESC").fetchall()
        con.close()
        out = [Episode.from_row(r) for r in rows
               if self._classify_outcome(Episode.from_row(r).outcome) == kind]
        return out[:limit]

    def context_window(self, ep_id: str, before: int = 3,
                       after: int = 3) -> list[Episode]:
        """Episodes surrounding a given one (by timestamp order)."""
        target = self.get_episode(ep_id)
        if not target:
            return []
        con = self._connect()
        rows = con.execute(
            "SELECT * FROM episodes ORDER BY timestamp ASC").fetchall()
        con.close()
        episodes = [Episode.from_row(r) for r in rows]
        idx = next((i for i, e in enumerate(episodes) if e.id == ep_id), -1)
        if idx < 0:
            return []
        lo = max(0, idx - before)
        hi = min(len(episodes), idx + after + 1)
        return episodes[lo:hi]

    # -- importance decay ----------------------------------------------------
    def apply_decay(self, half_life_days: float = 30.0,
                    floor: float = 0.1,
                    now: float | None = None) -> int:
        """Exponentially decay importance with age: imp *= 0.5^(age/half_life).

        Never decays below `floor`. Returns number of rows touched.
        """
        now = now if now is not None else time.time()
        con = self._connect()
        rows = con.execute("SELECT id, timestamp, importance FROM episodes"
                           ).fetchall()
        touched = 0
        for r in rows:
            age_days = max(0.0, (now - r["timestamp"]) / 86400.0)
            decayed = r["importance"] * (0.5 ** (age_days / half_life_days))
            new_imp = max(floor, decayed)
            if abs(new_imp - r["importance"]) > 1e-9:
                con.execute("UPDATE episodes SET importance = ? WHERE id = ?",
                            (new_imp, r["id"]))
                touched += 1
        con.commit()
        con.close()
        return touched

    def boost_importance(self, ep_id: str, delta: float = 0.2) -> bool:
        """Raise an episode's importance (clamped to 1.0)."""
        ep = self.get_episode(ep_id)
        if not ep:
            return False
        return self.update_episode(
            ep_id, importance=min(1.0, ep.importance + delta))

    # -- consolidation -------------------------------------------------------
    def find_similar(self, ep_id: str, threshold: float = 0.6,
                     limit: int = 10) -> list[tuple[Episode, float]]:
        """Episodes textually similar to the given one (Jaccard on words)."""
        target = self.get_episode(ep_id)
        if not target:
            return []
        target_words = set(re.findall(r"\w+",
                                      f"{target.event} {target.action}".lower()))
        if not target_words:
            return []
        con = self._connect()
        rows = con.execute("SELECT * FROM episodes WHERE id != ?",
                           (ep_id,)).fetchall()
        con.close()
        out: list[tuple[Episode, float]] = []
        for row in rows:
            ep = Episode.from_row(row)
            words = set(re.findall(
                r"\w+", f"{ep.event} {ep.action}".lower()))
            if not words:
                continue
            jaccard = len(target_words & words) / len(target_words | words)
            if jaccard >= threshold:
                out.append((ep, round(jaccard, 3)))
        out.sort(key=lambda t: t[1], reverse=True)
        return out[:limit]

    def consolidate(self, similarity_threshold: float = 0.75) -> int:
        """Merge near-duplicate episodes into one (keeps earliest).

        Merges actors/tags/context, keeps max importance. Returns the
        number of episodes removed.
        """
        con = self._connect()
        rows = con.execute(
            "SELECT * FROM episodes ORDER BY timestamp ASC").fetchall()
        con.close()
        episodes = [Episode.from_row(r) for r in rows]
        removed = 0
        consumed: set[str] = set()
        for ep in episodes:
            if ep.id in consumed:
                continue
            words = set(re.findall(r"\w+",
                                   f"{ep.event} {ep.action}".lower()))
            if not words:
                continue
            for other in episodes:
                if other.id == ep.id or other.id in consumed:
                    continue
                owords = set(re.findall(
                    r"\w+", f"{other.event} {other.action}".lower()))
                if not owords:
                    continue
                jaccard = len(words & owords) / len(words | owords)
                if jaccard >= similarity_threshold:
                    merged_actors = sorted(set(ep.actors) | set(other.actors))
                    merged_tags = sorted(
                        set(ep.emotion_tags) | set(other.emotion_tags))
                    merged_ctx = {**ep.context, **other.context}
                    self.update_episode(
                        ep.id, actors=merged_actors,
                        emotion_tags=merged_tags, context=merged_ctx,
                        importance=max(ep.importance, other.importance))
                    ep.actors, ep.emotion_tags = merged_actors, merged_tags
                    ep.importance = max(ep.importance, other.importance)
                    self.delete_episode(other.id)
                    consumed.add(other.id)
                    removed += 1
        return removed

    # -- statistics ----------------------------------------------------------
    def stats(self) -> dict:
        """Aggregate statistics about the episode store."""
        con = self._connect()
        n = con.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        bounds = con.execute(
            "SELECT MIN(timestamp), MAX(timestamp), AVG(importance) "
            "FROM episodes").fetchone()
        outcome_rows = con.execute(
            "SELECT outcome FROM episodes").fetchall()
        con.close()
        kinds = {"success": 0, "failure": 0, "neutral": 0}
        for (o,) in outcome_rows:
            kinds[self._classify_outcome(o or "")] += 1
        return {
            "count": int(n),
            "oldest": bounds[0], "newest": bounds[1],
            "avg_importance": round(bounds[2] or 0.0, 3),
            "outcomes": kinds,
            "actors": len(self.actors_list()),
            "tags": len(self.tags_list()),
            "db_path": str(self.db_path),
        }

    def vacuum(self) -> None:
        """Reclaim space after heavy pruning."""
        con = self._connect()
        con.execute("VACUUM")
        con.close()

    # -- persistence helpers -------------------------------------------------
    def backup_to_file(self, path: str | Path) -> int:
        """Write all episodes as JSON lines to a file. Returns count."""
        data = self.export_json()
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as fh:
            for d in data:
                fh.write(json.dumps(d, ensure_ascii=False) + "\n")
        return len(data)

    def restore_from_file(self, path: str | Path) -> int:
        """Load episodes from a backup_to_file JSONL file. Returns count."""
        episodes = []
        with Path(path).open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                try:
                    episodes.append(Episode(
                        id=d.get("id", ""), timestamp=float(d.get("timestamp", 0)),
                        event=d.get("event", ""), actors=d.get("actors", []),
                        action=d.get("action", ""), outcome=d.get("outcome", ""),
                        emotion_tags=d.get("emotion_tags", []),
                        context=d.get("context", {}),
                        importance=float(d.get("importance", 0.5))))
                except (TypeError, ValueError):
                    continue
        self.store_episodes(episodes)
        return len(episodes)

    def merge_from(self, other_path: str | Path) -> int:
        """Merge episodes from another episodic.db file (dedup by id)."""
        other = EpisodicMemory(db_path=other_path)
        data = other.export_json()
        before = self.count()
        self.import_json(data)
        return self.count() - before

    # -- filtered query builder ----------------------------------------------
    def query(self, actors: list[str] | None = None,
              tags: list[str] | None = None,
              start: float | None = None, end: float | None = None,
              text: str | None = None,
              min_importance: float = 0.0,
              outcome_kind: str | None = None,
              limit: int = 100) -> list[Episode]:
        """Combined filter query over all recall dimensions."""
        clauses = ["importance >= ?"]
        params: list = [min_importance]
        if start is not None:
            clauses.append("timestamp >= ?")
            params.append(start)
        if end is not None:
            clauses.append("timestamp <= ?")
            params.append(end)
        for actor in actors or []:
            clauses.append("lower(actors) LIKE ?")
            params.append(f"%{actor.lower()}%")
        for tag in tags or []:
            clauses.append("lower(emotion_tags) LIKE ?")
            params.append(f"%{tag.lower()}%")
        if text:
            words = [w for w in re.findall(r"\w+", text.lower()) if w]
            for w in words:
                clauses.append(
                    "(lower(event) LIKE ? OR lower(action) LIKE ? "
                    "OR lower(outcome) LIKE ?)")
                like = f"%{w}%"
                params += [like, like, like]
        params.append(limit)
        con = self._connect()
        rows = con.execute(
            f"SELECT * FROM episodes WHERE {' AND '.join(clauses)} "
            f"ORDER BY timestamp DESC LIMIT ?", params).fetchall()
        con.close()
        episodes = [Episode.from_row(r) for r in rows]
        if outcome_kind:
            kind = outcome_kind.lower()
            episodes = [e for e in episodes
                        if self._classify_outcome(e.outcome) == kind]
        return episodes


# ---------------------------------------------------------------------------
# Episode relations — cause/effect links between episodes
# ---------------------------------------------------------------------------

_RELATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS episode_links (
    src_id TEXT NOT NULL,
    dst_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (src_id, dst_id, relation)
);
CREATE INDEX IF NOT EXISTS idx_links_src ON episode_links(src_id);
CREATE INDEX IF NOT EXISTS idx_links_dst ON episode_links(dst_id);
"""

#: Allowed relation labels.
RELATIONS = frozenset({
    "caused_by", "causes", "follows", "contradicts", "supports",
    "supersedes", "related",
})


class EpisodeGraph:
    """Directed relations between episodes (cause/effect chains)."""

    def __init__(self, memory: EpisodicMemory):
        self.memory = memory
        con = memory._connect()
        con.executescript(_RELATION_SCHEMA)
        con.commit()
        con.close()

    def link(self, src_id: str, dst_id: str, relation: str) -> bool:
        """Add a relation edge. Returns False on unknown relation."""
        if relation not in RELATIONS:
            return False
        con = self.memory._connect()
        con.execute(
            """INSERT OR REPLACE INTO episode_links
               (src_id, dst_id, relation, created_at)
               VALUES (?,?,?,?)""",
            (src_id, dst_id, relation, time.time()))
        con.commit()
        con.close()
        return True

    def unlink(self, src_id: str, dst_id: str,
               relation: str | None = None) -> int:
        con = self.memory._connect()
        if relation:
            cur = con.execute(
                "DELETE FROM episode_links WHERE src_id=? AND dst_id=? "
                "AND relation=?", (src_id, dst_id, relation))
        else:
            cur = con.execute(
                "DELETE FROM episode_links WHERE src_id=? AND dst_id=?",
                (src_id, dst_id))
        con.commit()
        n = cur.rowcount
        con.close()
        return int(n)

    def outgoing(self, ep_id: str,
                 relation: str | None = None) -> list[tuple[str, str]]:
        """[(dst_id, relation)] edges leaving an episode."""
        con = self.memory._connect()
        if relation:
            rows = con.execute(
                "SELECT dst_id, relation FROM episode_links "
                "WHERE src_id=? AND relation=?", (ep_id, relation)).fetchall()
        else:
            rows = con.execute(
                "SELECT dst_id, relation FROM episode_links WHERE src_id=?",
                (ep_id,)).fetchall()
        con.close()
        return [(r[0], r[1]) for r in rows]

    def incoming(self, ep_id: str,
                 relation: str | None = None) -> list[tuple[str, str]]:
        """[(src_id, relation)] edges entering an episode."""
        con = self.memory._connect()
        if relation:
            rows = con.execute(
                "SELECT src_id, relation FROM episode_links "
                "WHERE dst_id=? AND relation=?", (ep_id, relation)).fetchall()
        else:
            rows = con.execute(
                "SELECT src_id, relation FROM episode_links WHERE dst_id=?",
                (ep_id,)).fetchall()
        con.close()
        return [(r[0], r[1]) for r in rows]

    def causes_chain(self, ep_id: str, max_depth: int = 5) -> list[Episode]:
        """Walk 'causes' edges forward from an episode (BFS)."""
        seen = {ep_id}
        queue: list[tuple[str, int]] = [(ep_id, 0)]
        chain: list[Episode] = []
        while queue:
            current, depth = queue.pop(0)
            if depth >= max_depth:
                continue
            for dst_id, rel in self.outgoing(current):
                if rel == "causes" and dst_id not in seen:
                    seen.add(dst_id)
                    ep = self.memory.get_episode(dst_id)
                    if ep:
                        chain.append(ep)
                    queue.append((dst_id, depth + 1))
        return chain

    def causes_of(self, ep_id: str, max_depth: int = 5) -> list[Episode]:
        """Walk 'caused_by' edges backward — what led to this episode."""
        seen = {ep_id}
        queue: list[tuple[str, int]] = [(ep_id, 0)]
        chain: list[Episode] = []
        while queue:
            current, depth = queue.pop(0)
            if depth >= max_depth:
                continue
            for src_id, rel in self.incoming(current):
                if rel in ("causes",) or True:
                    # incoming 'causes' edge means src caused current
                    if src_id not in seen:
                        seen.add(src_id)
                        ep = self.memory.get_episode(src_id)
                        if ep:
                            chain.append(ep)
                        queue.append((src_id, depth + 1))
        return chain
