"""Semantic memory — facts as (subject, predicate, object) triples.

SQLite backend with confidence-weighted retrieval, Bayesian-ish
confidence updates, contradiction detection, and knowledge-graph export.
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
class Fact:
    """One (subject, predicate, object) triple with confidence."""
    id: str = ""
    subject: str = ""
    predicate: str = ""
    object: str = ""
    confidence: float = 0.5
    source: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    observations: int = 1  # how many times this fact was seen/confirmed

    def triple(self) -> tuple[str, str, str]:
        return (self.subject, self.predicate, self.object)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "subject": self.subject,
            "predicate": self.predicate, "object": self.object,
            "confidence": self.confidence, "source": self.source,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "observations": self.observations,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Fact":
        return cls(
            id=row["id"], subject=row["subject"], predicate=row["predicate"],
            object=row["object"], confidence=row["confidence"],
            source=row["source"] or "", created_at=row["created_at"],
            updated_at=row["updated_at"],
            observations=row["observations"])


_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.5,
    source TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    observations INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_facts_spo ON facts(subject, predicate, object);
CREATE INDEX IF NOT EXISTS idx_facts_subject ON facts(subject);
CREATE INDEX IF NOT EXISTS idx_facts_predicate ON facts(predicate);
CREATE INDEX IF NOT EXISTS idx_facts_conf ON facts(confidence);
"""

#: Predicates where only one object value can be true at a time.
FUNCTIONAL_PREDICATES = frozenset({
    "is", "capital_of", "born_on", "located_in", "author_of",
    "ceo_of", "founded_in", "population_of", "version_of",
    "owner_of", "price_of", "type_of",
})


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class SemanticMemory:
    """Confidence-weighted triple store with contradiction detection."""

    def __init__(self, db_path: str | Path | None = None,
                 functional_predicates: set[str] | None = None):
        self.db_path = Path(db_path) if db_path else Path(
            "agent/workspace/semantic.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.functional = (functional_predicates
                           if functional_predicates is not None
                           else set(FUNCTIONAL_PREDICATES))
        self._init()

    def _init(self) -> None:
        con = self._connect()
        con.executescript(_SCHEMA)
        con.commit()
        con.close()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    @staticmethod
    def _norm(text: str) -> str:
        """Normalize a triple component for matching."""
        text = re.sub(r"\s+", " ", (text or "").strip().lower())
        return text

    # -- writing -----------------------------------------------------------
    def store_fact(self, subject: str, predicate: str, object: str,
                   confidence: float = 0.5, source: str = "",
                   merge: bool = True) -> Fact:
        """Store a fact. With merge=True, an identical triple is confirmed
        (observations+1, confidence updated) instead of duplicated."""
        subject_n = self._norm(subject)
        predicate_n = self._norm(predicate)
        object_n = self._norm(object)
        if not subject_n or not predicate_n or not object_n:
            raise ValueError("subject, predicate and object must be non-empty")
        confidence = max(0.0, min(1.0, float(confidence)))
        now = time.time()

        con = self._connect()
        existing = None
        if merge:
            existing = con.execute(
                "SELECT * FROM facts WHERE subject=? AND predicate=? "
                "AND object=?",
                (subject_n, predicate_n, object_n)).fetchone()
        if existing:
            fact = Fact.from_row(existing)
            fact.confidence = _confirm(fact.confidence, confidence)
            fact.observations += 1
            fact.updated_at = now
            if source:
                fact.source = source
            con.execute(
                """UPDATE facts SET confidence=?, observations=?,
                   updated_at=?, source=? WHERE id=?""",
                (fact.confidence, fact.observations, now, fact.source,
                 fact.id))
            con.commit()
            con.close()
            return fact

        fact = Fact(
            id=uuid.uuid4().hex[:16], subject=subject_n,
            predicate=predicate_n, object=object_n,
            confidence=confidence, source=source,
            created_at=now, updated_at=now, observations=1)
        con.execute(
            """INSERT INTO facts (id, subject, predicate, object,
               confidence, source, created_at, updated_at, observations)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (fact.id, fact.subject, fact.predicate, fact.object,
             fact.confidence, fact.source, fact.created_at,
             fact.updated_at, fact.observations))
        con.commit()
        con.close()
        return fact

    def store_facts(self, triples: list[tuple[str, str, str]],
                    confidence: float = 0.5, source: str = "") -> list[Fact]:
        """Batch store. Returns the stored facts."""
        return [self.store_fact(s, p, o, confidence=confidence, source=source)
                for s, p, o in triples]

    def retract_fact(self, subject: str, predicate: str,
                     object: str) -> bool:
        """Remove a fact entirely."""
        con = self._connect()
        cur = con.execute(
            "DELETE FROM facts WHERE subject=? AND predicate=? AND object=?",
            (self._norm(subject), self._norm(predicate),
             self._norm(object)))
        con.commit()
        n = cur.rowcount
        con.close()
        return n > 0

    def delete_fact(self, fact_id: str) -> bool:
        con = self._connect()
        cur = con.execute("DELETE FROM facts WHERE id=?", (fact_id,))
        con.commit()
        n = cur.rowcount
        con.close()
        return n > 0

    def get_fact(self, fact_id: str) -> Fact | None:
        con = self._connect()
        row = con.execute("SELECT * FROM facts WHERE id=?",
                          (fact_id,)).fetchone()
        con.close()
        return Fact.from_row(row) if row else None

    # -- querying ----------------------------------------------------------
    def query_facts(self, subject: str | None = None,
                    predicate: str | None = None,
                    object: str | None = None,
                    min_confidence: float = 0.0,
                    limit: int = 100) -> list[Fact]:
        """Query by any combination of subject/predicate/object."""
        clauses, params = ["confidence >= ?"], [min_confidence]
        if subject is not None:
            clauses.append("subject = ?")
            params.append(self._norm(subject))
        if predicate is not None:
            clauses.append("predicate = ?")
            params.append(self._norm(predicate))
        if object is not None:
            clauses.append("object = ?")
            params.append(self._norm(object))
        params.append(limit)
        con = self._connect()
        rows = con.execute(
            f"SELECT * FROM facts WHERE {' AND '.join(clauses)} "
            f"ORDER BY confidence DESC, observations DESC LIMIT ?",
            params).fetchall()
        con.close()
        return [Fact.from_row(r) for r in rows]

    def query_subject(self, subject: str,
                      min_confidence: float = 0.0) -> list[Fact]:
        """Everything known about a subject."""
        return self.query_facts(subject=subject,
                                min_confidence=min_confidence)

    def ask(self, subject: str, predicate: str) -> str | None:
        """Best-guess object for (subject, predicate): highest confidence."""
        facts = self.query_facts(subject=subject, predicate=predicate,
                                 limit=1)
        return facts[0].object if facts else None

    def ask_with_confidence(self, subject: str,
                            predicate: str) -> tuple[str, float] | None:
        """(object, confidence) for the best-matching fact."""
        facts = self.query_facts(subject=subject, predicate=predicate,
                                 limit=1)
        return (facts[0].object, facts[0].confidence) if facts else None

    def search_text(self, query: str, limit: int = 50) -> list[Fact]:
        """Substring search across all triple components."""
        like = f"%{query.lower()}%"
        con = self._connect()
        rows = con.execute(
            """SELECT * FROM facts
               WHERE subject LIKE ? OR predicate LIKE ? OR object LIKE ?
               ORDER BY confidence DESC LIMIT ?""",
            (like, like, like, limit)).fetchall()
        con.close()
        return [Fact.from_row(r) for r in rows]

    def all_facts(self, limit: int = 1000) -> list[Fact]:
        con = self._connect()
        rows = con.execute(
            "SELECT * FROM facts ORDER BY confidence DESC LIMIT ?",
            (limit,)).fetchall()
        con.close()
        return [Fact.from_row(r) for r in rows]

    def count(self) -> int:
        con = self._connect()
        n = con.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        con.close()
        return int(n)

    def subjects(self) -> list[str]:
        con = self._connect()
        rows = con.execute(
            "SELECT DISTINCT subject FROM facts ORDER BY subject").fetchall()
        con.close()
        return [r[0] for r in rows]

    def predicates(self) -> list[str]:
        con = self._connect()
        rows = con.execute(
            "SELECT DISTINCT predicate FROM facts ORDER BY predicate"
        ).fetchall()
        con.close()
        return [r[0] for r in rows]

    # -- confidence updates --------------------------------------------------
    def update_confidence(self, fact_id: str, evidence_confidence: float,
                          supports: bool = True) -> Fact | None:
        """Bayesian-ish update of one fact given new evidence.

        Supporting evidence:  c' = 1 - (1-c)(1-e)   (noisy-OR)
        Contradicting evidence: c' = c * (1-e)
        """
        fact = self.get_fact(fact_id)
        if not fact:
            return None
        e = max(0.0, min(1.0, float(evidence_confidence)))
        if supports:
            fact.confidence = _confirm(fact.confidence, e)
            fact.observations += 1
        else:
            fact.confidence = fact.confidence * (1.0 - e)
        fact.updated_at = time.time()
        con = self._connect()
        con.execute(
            """UPDATE facts SET confidence=?, observations=?,
               updated_at=? WHERE id=?""",
            (fact.confidence, fact.observations, fact.updated_at,
             fact.id))
        con.commit()
        con.close()
        return fact

    def confirm(self, subject: str, predicate: str, object: str,
                evidence_confidence: float = 0.7) -> Fact | None:
        """Confirm an existing triple with new supporting evidence."""
        facts = self.query_facts(subject=subject, predicate=predicate,
                                 object=object, limit=1)
        if not facts:
            return None
        return self.update_confidence(facts[0].id, evidence_confidence,
                                      supports=True)

    def contradict(self, subject: str, predicate: str, object: str,
                   evidence_confidence: float = 0.7) -> Fact | None:
        """Weaken an existing triple with contradicting evidence."""
        facts = self.query_facts(subject=subject, predicate=predicate,
                                 object=object, limit=1)
        if not facts:
            return None
        return self.update_confidence(facts[0].id, evidence_confidence,
                                      supports=False)

    def decay_confidence(self, half_life_days: float = 90.0,
                         floor: float = 0.05,
                         now: float | None = None) -> int:
        """Age-decay confidence of stale facts (not recently updated)."""
        now = now if now is not None else time.time()
        con = self._connect()
        rows = con.execute(
            "SELECT id, updated_at, confidence FROM facts").fetchall()
        touched = 0
        for r in rows:
            age_days = max(0.0, (now - r["updated_at"]) / 86400.0)
            new_c = max(floor, r["confidence"]
                        * (0.5 ** (age_days / half_life_days)))
            if abs(new_c - r["confidence"]) > 1e-9:
                con.execute("UPDATE facts SET confidence=? WHERE id=?",
                            (new_c, r["id"]))
                touched += 1
        con.commit()
        con.close()
        return touched

    def prune_low_confidence(self, threshold: float = 0.15) -> int:
        """Delete facts below a confidence threshold. Returns count."""
        con = self._connect()
        cur = con.execute("DELETE FROM facts WHERE confidence < ?",
                          (threshold,))
        con.commit()
        n = cur.rowcount
        con.close()
        return int(n)

    # -- contradiction detection ---------------------------------------------
    def contradict_detect(self, subject: str | None = None,
                          predicate: str | None = None) -> list[dict]:
        """Find conflicting facts: same (subject, predicate) with
        different objects, where the predicate is functional.

        Returns [{subject, predicate, facts: [Fact, ...]}].
        """
        clauses, params = [], []
        if subject is not None:
            clauses.append("subject = ?")
            params.append(self._norm(subject))
        if predicate is not None:
            clauses.append("predicate = ?")
            params.append(self._norm(predicate))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        con = self._connect()
        groups = con.execute(
            f"""SELECT subject, predicate, COUNT(DISTINCT object) AS n
                FROM facts {where}
                GROUP BY subject, predicate HAVING n > 1""",
            params).fetchall()
        conflicts: list[dict] = []
        for g in groups:
            if g["predicate"] not in self.functional:
                continue
            rows = con.execute(
                "SELECT * FROM facts WHERE subject=? AND predicate=? "
                "ORDER BY confidence DESC",
                (g["subject"], g["predicate"])).fetchall()
            facts = [Fact.from_row(r) for r in rows]
            conflicts.append({
                "subject": g["subject"], "predicate": g["predicate"],
                "facts": facts,
                "winner": facts[0] if facts else None,
            })
        con.close()
        return conflicts

    def resolve_conflict(self, subject: str, predicate: str,
                         keep_object: str) -> dict:
        """Resolve a functional-predicate conflict by keeping one object.

        The kept fact is confirmed (+0.2), the others are contradicted
        (-0.5 evidence). Returns the resolution record.
        """
        keep_n = self._norm(keep_object)
        facts = self.query_facts(subject=subject, predicate=predicate)
        kept, dropped = None, []
        for fact in facts:
            if fact.object == keep_n:
                self.update_confidence(fact.id, 0.2, supports=True)
                kept = self.get_fact(fact.id)
            else:
                self.update_confidence(fact.id, 0.5, supports=False)
                dropped.append(fact.object)
        return {"subject": self._norm(subject),
                "predicate": self._norm(predicate),
                "kept": kept.to_dict() if kept else None,
                "dropped": dropped}

    # -- knowledge graph export ----------------------------------------------
    def export_knowledge_graph(self,
                               min_confidence: float = 0.0) -> dict:
        """Export as {nodes: [...], edges: [...]} for visualization."""
        facts = [f for f in self.all_facts() if f.confidence >= min_confidence]
        node_ids: dict[str, int] = {}
        nodes: list[dict] = []
        edges: list[dict] = []

        def node_id(label: str) -> int:
            # One node per entity label — subjects and objects merge when
            # they refer to the same thing.
            if label not in node_ids:
                node_ids[label] = len(nodes)
                nodes.append({"id": len(nodes), "label": label,
                              "kind": "entity"})
            return node_ids[label]

        for fact in facts:
            s = node_id(fact.subject)
            o = node_id(fact.object)
            edges.append({
                "source": s, "target": o, "predicate": fact.predicate,
                "confidence": round(fact.confidence, 3),
            })
        return {"nodes": nodes, "edges": edges}

    def export_triples(self, min_confidence: float = 0.0) -> list[dict]:
        """All facts as JSON-serializable dicts."""
        return [f.to_dict() for f in self.all_facts()
                if f.confidence >= min_confidence]

    def import_triples(self, data: list[dict]) -> int:
        """Import from export_triples output. Returns count stored."""
        n = 0
        for d in data:
            try:
                self.store_fact(
                    d["subject"], d["predicate"], d["object"],
                    confidence=float(d.get("confidence", 0.5)),
                    source=d.get("source", ""))
                n += 1
            except (KeyError, TypeError, ValueError):
                continue
        return n

    # -- natural-language-ish helpers ------------------------------------------
    def describe_subject(self, subject: str) -> str:
        """One-paragraph summary of everything known about a subject."""
        facts = self.query_subject(subject)
        if not facts:
            return f"Nothing known about '{subject}'."
        lines = [f"- {f.subject} {f.predicate} {f.object} "
                 f"(confidence {f.confidence:.2f})" for f in facts[:20]]
        return f"Known about '{self._norm(subject)}':\n" + "\n".join(lines)

    def stats(self) -> dict:
        con = self._connect()
        n = con.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        avg = con.execute("SELECT AVG(confidence) FROM facts").fetchone()[0]
        con.close()
        return {
            "facts": int(n),
            "avg_confidence": round(avg or 0.0, 3),
            "subjects": len(self.subjects()),
            "predicates": len(self.predicates()),
            "conflicts": len(self.contradict_detect()),
            "db_path": str(self.db_path),
        }


# ---------------------------------------------------------------------------
# Confidence math
# ---------------------------------------------------------------------------

def _confirm(current: float, evidence: float) -> float:
    """Noisy-OR combination of supporting evidence."""
    return 1.0 - (1.0 - current) * (1.0 - evidence)


def combine_supporting(confidences: list[float]) -> float:
    """Combine several independent supporting confidences (noisy-OR)."""
    result = 0.0
    for c in confidences:
        result = _confirm(result, max(0.0, min(1.0, c)))
    return result


def combine_contradicting(base: float,
                          contradictions: list[float]) -> float:
    """Weaken a confidence by several contradicting evidences."""
    result = max(0.0, min(1.0, base))
    for c in contradictions:
        result *= 1.0 - max(0.0, min(1.0, c))
    return result


# ---------------------------------------------------------------------------
# Triple parsing from simple sentences
# ---------------------------------------------------------------------------
_TRIPLE_RES = [
    # "X is Y"
    re.compile(r"^\s*(.+?)\s+is\s+(?:a|an|the\s+)?(.+?)\s*$", re.I),
    # "X has Y"
    re.compile(r"^\s*(.+?)\s+has\s+(?:a|an|the\s+)?(.+?)\s*$", re.I),
    # "X verb Y"  (generic fallback, e.g. "Paris located_in France" no —
    # matches "X <verb> Y" with a single-word verb)
    re.compile(r"^\s*([A-Za-z0-9][\w\s]{0,60}?)\s+([a-z_]{3,20})\s+(.+?)\s*$"),
]


def parse_simple_sentence(sentence: str) -> tuple[str, str, str] | None:
    """Parse 'X is Y' / 'X has Y' style sentences into a triple.

    Returns (subject, predicate, object) or None when unparseable.
    """
    s = (sentence or "").strip().rstrip(".")
    if not s:
        return None
    m = _TRIPLE_RES[0].match(s)
    if m:
        return (m.group(1).strip(), "is", m.group(2).strip())
    m = _TRIPLE_RES[1].match(s)
    if m:
        return (m.group(1).strip(), "has", m.group(2).strip())
    m = _TRIPLE_RES[2].match(s)
    if m:
        subj, verb, obj = m.group(1).strip(), m.group(2).strip(), \
            m.group(3).strip()
        if len(subj.split()) <= 6 and len(obj.split()) <= 12:
            return (subj, verb, obj)
    return None


def learn_from_text(memory: "SemanticMemory", text: str,
                    confidence: float = 0.4,
                    source: str = "") -> list[Fact]:
    """Split text into sentences, parse simple ones, store as facts."""
    facts: list[Fact] = []
    for sentence in re.split(r"[.!?]\s+", text or ""):
        triple = parse_simple_sentence(sentence)
        if triple:
            facts.append(memory.store_fact(
                *triple, confidence=confidence, source=source))
    return facts


# ---------------------------------------------------------------------------
# Inference — simple forward-chaining rules
# ---------------------------------------------------------------------------

#: Transitive predicates: (A p B) + (B p C) -> (A p C).
TRANSITIVE_PREDICATES = frozenset({
    "located_in", "part_of", "type_of", "subclass_of", "ancestor_of",
})

#: Symmetric predicates: (A p B) -> (B p A).
SYMMETRIC_PREDICATES = frozenset({
    "married_to", "sibling_of", "connected_to", "similar_to",
})


class InferenceEngine:
    """Forward-chaining inference over a SemanticMemory.

    Applies transitive and symmetric rules iteratively until fixpoint
    (bounded by max_iterations). Inferred facts get confidence =
    min(premise confidences) * rule_discount and source 'inferred:<rule>'.
    """

    def __init__(self, memory: SemanticMemory, rule_discount: float = 0.9):
        self.memory = memory
        self.rule_discount = rule_discount

    def _inferred_conf(self, *confs: float) -> float:
        return min(confs) * self.rule_discount if confs else 0.0

    def apply_transitive(self) -> int:
        """One pass of transitive inference. Returns new facts count."""
        new = 0
        for pred in TRANSITIVE_PREDICATES:
            facts = self.memory.query_facts(predicate=pred)
            by_subject: dict[str, list[Fact]] = {}
            for f in facts:
                by_subject.setdefault(f.subject, []).append(f)
            for f1 in facts:
                for f2 in by_subject.get(f1.object, []):
                    if f2.subject == f1.object and f2.object != f1.subject:
                        existing = self.memory.query_facts(
                            subject=f1.subject, predicate=pred,
                            object=f2.object, limit=1)
                        if not existing:
                            self.memory.store_fact(
                                f1.subject, pred, f2.object,
                                confidence=self._inferred_conf(
                                    f1.confidence, f2.confidence),
                                source=f"inferred:transitive({pred})",
                                merge=False)
                            new += 1
        return new

    def apply_symmetric(self) -> int:
        """One pass of symmetric inference. Returns new facts count."""
        new = 0
        for pred in SYMMETRIC_PREDICATES:
            for f in self.memory.query_facts(predicate=pred):
                existing = self.memory.query_facts(
                    subject=f.object, predicate=pred,
                    object=f.subject, limit=1)
                if not existing:
                    self.memory.store_fact(
                        f.object, pred, f.subject,
                        confidence=self._inferred_conf(f.confidence),
                        source=f"inferred:symmetric({pred})",
                        merge=False)
                    new += 1
        return new

    def run(self, max_iterations: int = 5) -> dict[str, int]:
        """Iterate rules to fixpoint. Returns {rule: new_facts}."""
        totals = {"transitive": 0, "symmetric": 0}
        for _ in range(max_iterations):
            t = self.apply_transitive()
            s = self.apply_symmetric()
            totals["transitive"] += t
            totals["symmetric"] += s
            if t == 0 and s == 0:
                break
        return totals

    def inferred_facts(self) -> list[Fact]:
        """All facts whose source starts with 'inferred:'."""
        return [f for f in self.memory.all_facts()
                if f.source.startswith("inferred:")]


# ---------------------------------------------------------------------------
# Fact history — audit trail of confidence changes
# ---------------------------------------------------------------------------

_HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS fact_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_id TEXT NOT NULL,
    old_confidence REAL NOT NULL,
    new_confidence REAL NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hist_fact ON fact_history(fact_id);
"""


class FactHistory:
    """Audit trail: every confidence change recorded with reason."""

    def __init__(self, memory: SemanticMemory):
        self.memory = memory
        con = memory._connect()
        con.executescript(_HISTORY_SCHEMA)
        con.commit()
        con.close()

    def record(self, fact_id: str, old: float, new: float,
               reason: str = "") -> None:
        con = self.memory._connect()
        con.execute(
            "INSERT INTO fact_history (fact_id, old_confidence, "
            "new_confidence, reason, at) VALUES (?,?,?,?,?)",
            (fact_id, old, new, reason, time.time()))
        con.commit()
        con.close()

    def history(self, fact_id: str) -> list[dict]:
        con = self.memory._connect()
        rows = con.execute(
            "SELECT old_confidence, new_confidence, reason, at "
            "FROM fact_history WHERE fact_id=? ORDER BY at ASC",
            (fact_id,)).fetchall()
        con.close()
        return [{"old": r[0], "new": r[1], "reason": r[2], "at": r[3]}
                for r in rows]

    def explain(self, fact_id: str) -> str:
        """Human-readable provenance for a fact."""
        fact = self.memory.get_fact(fact_id)
        if not fact:
            return f"No fact with id {fact_id}."
        lines = [
            f"Fact: {fact.subject} --{fact.predicate}--> {fact.object}",
            f"Confidence: {fact.confidence:.3f} "
            f"(seen {fact.observations}x, source: {fact.source or 'unknown'})",
        ]
        hist = self.history(fact_id)
        if hist:
            lines.append("History:")
            for h in hist:
                ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(h["at"]))
                lines.append(f"  {ts}: {h['old']:.3f} -> {h['new']:.3f} "
                             f"({h['reason'] or 'update'})")
        else:
            lines.append("No recorded updates.")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Bulk utilities
# ---------------------------------------------------------------------------

def dedup_facts(memory: SemanticMemory) -> int:
    """Remove exact-duplicate triple rows (keeps highest confidence)."""
    seen: dict[tuple[str, str, str], str] = {}
    removed = 0
    for fact in memory.all_facts(limit=100000):
        key = fact.triple()
        if key in seen:
            memory.delete_fact(fact.id)
            removed += 1
        else:
            seen[key] = fact.id
    return removed


def top_subjects(memory: SemanticMemory, limit: int = 20) -> list[tuple[str, int]]:
    """Subjects ranked by number of facts."""
    counts: dict[str, int] = {}
    for fact in memory.all_facts(limit=100000):
        counts[fact.subject] = counts.get(fact.subject, 0) + 1
    return sorted(counts.items(), key=lambda kv: kv[1],
                  reverse=True)[:limit]
