"""Vector index — TF-IDF similarity search (ChatGPT spec).

Per spec: agent/memory/vector_index.py
Pure-stdlib TF-IDF with cosine similarity. No external dependencies.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


def tokenize(text: str) -> List[str]:
    """Simple tokenizer: lowercase words, drop stopwords."""
    stop = {"the", "a", "an", "of", "for", "to", "in", "on", "is",
            "are", "was", "were", "be", "been", "and", "or", "with",
            "as", "at", "by", "it", "this", "that", "from"}
    return [w for w in re.findall(r"[a-z0-9]+", text.lower())
            if w not in stop and len(w) > 1]


@dataclass
class IndexedDoc:
    id: str
    text: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


class VectorIndex:
    """TF-IDF vector index with cosine similarity search."""

    def __init__(self, db_path: Optional[str] = None):
        # In-memory by default; optional SQLite persistence
        self.db_path = db_path
        self._docs: Dict[str, IndexedDoc] = {}
        self._doc_freq: Dict[str, int] = {}
        self._conn: Optional[sqlite3.Connection] = None
        if db_path:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._init_db()
            self._load()

    def _init_db(self) -> None:
        assert self._conn
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS vec_docs (
                id TEXT PRIMARY KEY, text TEXT NOT NULL,
                metadata TEXT DEFAULT '{}', created_at REAL
            );
        """)
        self._conn.commit()

    def _load(self) -> None:
        assert self._conn
        for row in self._conn.execute("SELECT * FROM vec_docs"):
            doc = IndexedDoc(id=row[0], text=row[1],
                             metadata=json.loads(row[2] or "{}"),
                             created_at=row[3])
            self._docs[doc.id] = doc
        self._rebuild_df()

    def add(self, text: str,
            metadata: Optional[Dict[str, Any]] = None) -> str:
        doc_id = f"doc_{uuid.uuid4().hex[:8]}"
        doc = IndexedDoc(id=doc_id, text=text,
                         metadata=metadata or {})
        self._docs[doc_id] = doc
        for term in set(tokenize(text)):
            self._doc_freq[term] = self._doc_freq.get(term, 0) + 1
        if self._conn:
            self._conn.execute(
                "INSERT INTO vec_docs VALUES (?,?,?,?)",
                (doc_id, text, json.dumps(doc.metadata),
                 doc.created_at))
            self._conn.commit()
        return doc_id

    def add_many(self, texts: List[str]) -> List[str]:
        return [self.add(t) for t in texts]

    def remove(self, doc_id: str) -> bool:
        doc = self._docs.pop(doc_id, None)
        if not doc:
            return False
        self._rebuild_df()
        if self._conn:
            self._conn.execute("DELETE FROM vec_docs WHERE id=?",
                               (doc_id,))
            self._conn.commit()
        return True

    def _rebuild_df(self) -> None:
        self._doc_freq = {}
        for doc in self._docs.values():
            for term in set(tokenize(doc.text)):
                self._doc_freq[term] = self._doc_freq.get(term, 0) + 1

    def _tfidf(self, text: str) -> Dict[str, float]:
        tokens = tokenize(text)
        if not tokens:
            return {}
        n = len(self._docs) or 1
        tf: Dict[str, float] = {}
        for t in tokens:
            tf[t] = tf.get(t, 0) + 1
        vec = {}
        for t, c in tf.items():
            df = self._doc_freq.get(t, 0)
            idf = math.log((n + 1) / (df + 1)) + 1
            vec[t] = (c / len(tokens)) * idf
        return vec

    @staticmethod
    def _cosine(a: Dict[str, float],
                b: Dict[str, float]) -> float:
        if not a or not b:
            return 0.0
        dot = sum(a.get(t, 0) * v for t, v in b.items())
        na = math.sqrt(sum(v * v for v in a.values()))
        nb = math.sqrt(sum(v * v for v in b.values()))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    def search(self, query: str, top_k: int = 5,
               min_score: float = 0.0
               ) -> List[Tuple[IndexedDoc, float]]:
        """Return (doc, score) sorted by relevance."""
        qvec = self._tfidf(query)
        scored = []
        for doc in self._docs.values():
            dvec = self._tfidf(doc.text)
            score = self._cosine(qvec, dvec)
            if score >= min_score:
                scored.append((doc, score))
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]

    def count(self) -> int:
        return len(self._docs)

    def clear(self) -> None:
        self._docs.clear()
        self._doc_freq.clear()
        if self._conn:
            self._conn.execute("DELETE FROM vec_docs")
            self._conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
