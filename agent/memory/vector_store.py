"""Vector store — TF-IDF similarity search implemented from scratch.

No external dependencies: tokenization, IDF computation, and sparse
cosine similarity are all stdlib (re + math). Documents persist to a
JSON file so the index survives restarts.
"""
from __future__ import annotations

import json
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")

#: Common English stopwords removed before indexing.
STOPWORDS = frozenset("""
a about above after again against all am an and any are as at be because been
before being below between both but by can did do does doing down during each
few for from further had has have having he her here hers herself him himself
his how i if in into is it its itself just like me more most my myself no nor
not now of off on once only or other ought our ours ourselves out over own same
she should so some such than that the their theirs them themselves then there
these they this those through to too under until up very was we were what when
where which while who whom why with would you your yours yourself yourselves
""".split())


def tokenize(text: str, remove_stopwords: bool = True,
             min_length: int = 2) -> list[str]:
    """Lowercase alphanumeric tokenization with stopword removal."""
    tokens = _WORD_RE.findall((text or "").lower())
    out = []
    for tok in tokens:
        if len(tok) < min_length:
            continue
        if remove_stopwords and tok in STOPWORDS:
            continue
        out.append(tok)
    return out


def term_frequencies(tokens: list[str]) -> dict[str, int]:
    """Raw term counts."""
    tf: dict[str, int] = {}
    for tok in tokens:
        tf[tok] = tf.get(tok, 0) + 1
    return tf


# ---------------------------------------------------------------------------
# Document model
# ---------------------------------------------------------------------------

@dataclass
class Document:
    id: str = ""
    text: str = ""
    metadata: dict = field(default_factory=dict)
    added_at: float = 0.0

    def to_dict(self) -> dict:
        return {"id": self.id, "text": self.text,
                "metadata": self.metadata, "added_at": self.added_at}


@dataclass
class SearchHit:
    doc_id: str
    score: float
    text: str
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Vector store
# ---------------------------------------------------------------------------

class VectorStore:
    """TF-IDF document store with cosine-similarity search.

    Vectors are sparse dicts {term: tf-idf weight}. IDF is recomputed
    lazily and cached; any mutation invalidates the cache.
    """

    def __init__(self, persist_path: str | Path | None = None,
                 remove_stopwords: bool = True):
        self.persist_path = Path(persist_path) if persist_path else None
        self.remove_stopwords = remove_stopwords
        self.documents: dict[str, Document] = {}
        self._doc_tokens: dict[str, list[str]] = {}
        self._doc_tf: dict[str, dict[str, int]] = {}
        self._doc_freq: dict[str, int] = {}   # term -> #docs containing it
        self._idf_cache: dict[str, float] | None = None
        self._vec_cache: dict[str, dict[str, float]] = {}
        if self.persist_path and self.persist_path.exists():
            self.load()

    # -- mutation ------------------------------------------------------------
    def add_document(self, text: str, doc_id: str | None = None,
                     metadata: dict | None = None) -> str:
        """Add (or replace) a document. Returns its id."""
        doc_id = doc_id or uuid.uuid4().hex[:16]
        if doc_id in self.documents:
            self.remove_document(doc_id)
        tokens = tokenize(text, remove_stopwords=self.remove_stopwords)
        tf = term_frequencies(tokens)
        self.documents[doc_id] = Document(
            id=doc_id, text=text, metadata=metadata or {},
            added_at=time.time())
        self._doc_tokens[doc_id] = tokens
        self._doc_tf[doc_id] = tf
        for term in tf:
            self._doc_freq[term] = self._doc_freq.get(term, 0) + 1
        self._invalidate_cache()
        return doc_id

    def add_documents(self, texts: list[str],
                      metadatas: list[dict] | None = None) -> list[str]:
        """Batch add. Returns ids."""
        ids = []
        for i, text in enumerate(texts):
            meta = metadatas[i] if metadatas and i < len(metadatas) else None
            ids.append(self.add_document(text, metadata=meta))
        return ids

    def remove_document(self, doc_id: str) -> bool:
        """Remove a document and update document frequencies."""
        if doc_id not in self.documents:
            return False
        tf = self._doc_tf.pop(doc_id, {})
        for term in tf:
            self._doc_freq[term] = self._doc_freq.get(term, 1) - 1
            if self._doc_freq[term] <= 0:
                del self._doc_freq[term]
        self._doc_tokens.pop(doc_id, None)
        self.documents.pop(doc_id, None)
        self._invalidate_cache()
        return True

    def update_document(self, doc_id: str, text: str,
                        metadata: dict | None = None) -> bool:
        """Replace a document's text (keeps its id)."""
        if doc_id not in self.documents:
            return False
        meta = metadata if metadata is not None else \
            self.documents[doc_id].metadata
        self.add_document(text, doc_id=doc_id, metadata=meta)
        return True

    def clear(self) -> None:
        self.documents.clear()
        self._doc_tokens.clear()
        self._doc_tf.clear()
        self._doc_freq.clear()
        self._invalidate_cache()

    def _invalidate_cache(self) -> None:
        self._idf_cache = None
        self._vec_cache.clear()

    def __len__(self) -> int:
        return len(self.documents)

    # -- TF-IDF math -----------------------------------------------------------
    def _idf(self) -> dict[str, float]:
        """Inverse document frequency with smoothing.

        idf(t) = log((1 + N) / (1 + df(t))) + 1
        """
        if self._idf_cache is None:
            n = max(1, len(self.documents))
            self._idf_cache = {
                term: math.log((1 + n) / (1 + df)) + 1.0
                for term, df in self._doc_freq.items()
            }
        return self._idf_cache

    def _doc_vector(self, doc_id: str) -> dict[str, float]:
        """Sparse TF-IDF vector for a document (cached)."""
        if doc_id not in self._vec_cache:
            idf = self._idf()
            tf = self._doc_tf.get(doc_id, {})
            total = sum(tf.values()) or 1
            vec = {term: (count / total) * idf.get(term, 1.0)
                   for term, count in tf.items()}
            self._vec_cache[doc_id] = vec
        return self._vec_cache[doc_id]

    def _query_vector(self, query: str) -> dict[str, float]:
        """TF-IDF vector for a query string (uses corpus IDF)."""
        idf = self._idf()
        tokens = tokenize(query, remove_stopwords=self.remove_stopwords)
        tf = term_frequencies(tokens)
        total = sum(tf.values()) or 1
        # Unknown terms get a high IDF (rare) so they still match.
        n = max(1, len(self.documents))
        unseen_idf = math.log(1 + n) + 1.0
        return {term: (count / total) * idf.get(term, unseen_idf)
                for term, count in tf.items()}

    @staticmethod
    def cosine_similarity(a: dict[str, float],
                          b: dict[str, float]) -> float:
        """Cosine similarity of two sparse vectors."""
        if not a or not b:
            return 0.0
        # Iterate over the smaller vector.
        if len(a) > len(b):
            a, b = b, a
        dot = sum(weight * b.get(term, 0.0) for term, weight in a.items())
        if dot == 0.0:
            return 0.0
        norm_a = math.sqrt(sum(w * w for w in a.values()))
        norm_b = math.sqrt(sum(w * w for w in b.values()))
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return dot / (norm_a * norm_b)

    # -- search ------------------------------------------------------------------
    def search(self, query: str, top_k: int = 5,
               min_score: float = 0.0) -> list[SearchHit]:
        """Rank documents by cosine similarity to the query TF-IDF vector."""
        qvec = self._query_vector(query)
        if not qvec:
            return []
        hits: list[SearchHit] = []
        for doc_id, doc in self.documents.items():
            score = self.cosine_similarity(qvec, self._doc_vector(doc_id))
            if score >= min_score and score > 0.0:
                hits.append(SearchHit(doc_id=doc_id, score=round(score, 4),
                                      text=doc.text, metadata=doc.metadata))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

    def search_with_scores(self, query: str,
                           top_k: int = 5) -> list[tuple[str, float]]:
        """Lightweight search returning (doc_id, score) pairs."""
        return [(h.doc_id, h.score) for h in self.search(query, top_k)]

    def most_similar_to_doc(self, doc_id: str, top_k: int = 5) -> list[SearchHit]:
        """Find documents most similar to a given document (excludes itself)."""
        if doc_id not in self.documents:
            return []
        target = self._doc_vector(doc_id)
        hits: list[SearchHit] = []
        for other_id, doc in self.documents.items():
            if other_id == doc_id:
                continue
            score = self.cosine_similarity(target, self._doc_vector(other_id))
            if score > 0.0:
                hits.append(SearchHit(doc_id=other_id,
                                      score=round(score, 4),
                                      text=doc.text,
                                      metadata=doc.metadata))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

    def query_expanded_search(self, query: str, top_k: int = 5,
                              expand_k: int = 3) -> list[SearchHit]:
        """Pseudo-relevance feedback: search, then expand the query with
        top terms from the best hits and search again."""
        first = self.search(query, top_k=expand_k)
        if not first:
            return []
        idf = self._idf()
        term_weights: dict[str, float] = {}
        for hit in first:
            for term, weight in self._doc_vector(hit.doc_id).items():
                term_weights[term] = term_weights.get(term, 0.0) + \
                    weight * hit.score
        query_terms = set(tokenize(query,
                                   remove_stopwords=self.remove_stopwords))
        expansion = [t for t, _ in sorted(
            term_weights.items(), key=lambda kv: kv[1], reverse=True)
            if t not in query_terms][:5]
        expanded = query + " " + " ".join(expansion)
        return self.search(expanded, top_k=top_k)

    # -- corpus statistics -------------------------------------------------------
    def vocabulary(self) -> list[str]:
        """All indexed terms, most frequent first."""
        return sorted(self._doc_freq, key=lambda t: self._doc_freq[t],
                      reverse=True)

    def vocabulary_size(self) -> int:
        return len(self._doc_freq)

    def term_stats(self, term: str) -> dict:
        """DF, IDF, and posting list size for one term."""
        term = term.lower()
        df = self._doc_freq.get(term, 0)
        return {
            "term": term,
            "document_frequency": df,
            "idf": round(self._idf().get(term, 0.0), 4),
            "in_documents": df,
        }

    def top_terms(self, doc_id: str, k: int = 10) -> list[tuple[str, float]]:
        """Highest TF-IDF terms in a document (its 'keywords')."""
        vec = self._doc_vector(doc_id)
        return sorted(vec.items(), key=lambda kv: kv[1],
                      reverse=True)[:k]

    def corpus_stats(self) -> dict:
        total_tokens = sum(len(t) for t in self._doc_tokens.values())
        n = len(self.documents)
        return {
            "documents": n,
            "vocabulary_size": len(self._doc_freq),
            "total_tokens": total_tokens,
            "avg_doc_length": round(total_tokens / n, 1) if n else 0.0,
        }

    # -- persistence ---------------------------------------------------------------
    def save(self, path: str | Path | None = None) -> Path:
        """Persist documents + metadata to JSON (vectors are rebuilt)."""
        target = Path(path) if path else self.persist_path
        if target is None:
            raise ValueError("no persist path configured")
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "remove_stopwords": self.remove_stopwords,
            "documents": [d.to_dict() for d in self.documents.values()],
        }
        target.write_text(json.dumps(payload, ensure_ascii=False),
                          encoding="utf-8")
        self.persist_path = target
        return target

    def load(self, path: str | Path | None = None) -> int:
        """Load a saved index. Returns number of documents loaded."""
        target = Path(path) if path else self.persist_path
        if target is None or not target.exists():
            return 0
        payload = json.loads(target.read_text(encoding="utf-8"))
        self.clear()
        self.remove_stopwords = payload.get("remove_stopwords", True)
        for d in payload.get("documents", []):
            self.add_document(d.get("text", ""), doc_id=d.get("id"),
                              metadata=d.get("metadata", {}))
        self.persist_path = target
        return len(self.documents)

    # -- chunking helpers ------------------------------------------------------------
    @staticmethod
    def chunk_text(text: str, chunk_size: int = 500,
                   overlap: int = 50) -> list[str]:
        """Split text into overlapping word chunks for indexing."""
        words = (text or "").split()
        if not words:
            return []
        chunks = []
        step = max(1, chunk_size - overlap)
        for i in range(0, len(words), step):
            chunk = " ".join(words[i:i + chunk_size])
            if chunk.strip():
                chunks.append(chunk)
        return chunks

    def index_text(self, text: str, chunk_size: int = 500,
                   overlap: int = 50,
                   base_metadata: dict | None = None) -> list[str]:
        """Chunk a long text and index each chunk. Returns chunk ids."""
        ids = []
        for i, chunk in enumerate(self.chunk_text(text, chunk_size,
                                                  overlap)):
            meta = dict(base_metadata or {})
            meta.update({"chunk_index": i})
            ids.append(self.add_document(chunk, metadata=meta))
        return ids


# ---------------------------------------------------------------------------
# BM25-ish ranking (alternative scorer on the same index)
# ---------------------------------------------------------------------------

class BM25Ranker:
    """Okapi BM25 ranking over a VectorStore's term statistics.

    score(q, d) = sum over query terms of
        idf(t) * (tf*(k1+1)) / (tf + k1*(1-b+b*|d|/avgdl))
    """

    def __init__(self, store: VectorStore, k1: float = 1.5,
                 b: float = 0.75):
        self.store = store
        self.k1 = k1
        self.b = b

    def _bm25_idf(self, df: int, n: int) -> float:
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def score(self, query: str, doc_id: str) -> float:
        n = len(self.store.documents)
        if n == 0 or doc_id not in self.store.documents:
            return 0.0
        tokens = tokenize(query,
                          remove_stopwords=self.store.remove_stopwords)
        tf = self.store._doc_tf.get(doc_id, {})
        doc_len = sum(tf.values()) or 1
        avgdl = self.store.corpus_stats()["avg_doc_length"] or 1.0
        total = 0.0
        for term in set(tokens):
            term_tf = tf.get(term, 0)
            if term_tf == 0:
                continue
            df = self.store._doc_freq.get(term, 0)
            idf = self._bm25_idf(df, n)
            denom = term_tf + self.k1 * (
                1 - self.b + self.b * doc_len / avgdl)
            total += idf * (term_tf * (self.k1 + 1)) / denom
        return total

    def search(self, query: str, top_k: int = 5) -> list[SearchHit]:
        scored = [(doc_id, self.score(query, doc_id))
                  for doc_id in self.store.documents]
        scored = [(d, s) for d, s in scored if s > 0.0]
        scored.sort(key=lambda t: t[1], reverse=True)
        hits = []
        for doc_id, s in scored[:top_k]:
            doc = self.store.documents[doc_id]
            hits.append(SearchHit(doc_id=doc_id, score=round(s, 4),
                                  text=doc.text, metadata=doc.metadata))
        return hits


# ---------------------------------------------------------------------------
# Hybrid memory search — combine vector hits with keyword hits
# ---------------------------------------------------------------------------

def reciprocal_rank_fusion(rankings: list[list[str]], k: float = 60.0,
                           top_k: int = 10) -> list[tuple[str, float]]:
    """Fuse several ranked doc-id lists via reciprocal rank fusion."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    fused = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [(d, round(s, 4)) for d, s in fused[:top_k]]


class HybridSearcher:
    """Combine TF-IDF cosine search and BM25 via RRF."""

    def __init__(self, store: VectorStore):
        self.store = store
        self.bm25 = BM25Ranker(store)

    def search(self, query: str, top_k: int = 5) -> list[SearchHit]:
        tfidf_ids = [h.doc_id for h in self.store.search(query, top_k * 2)]
        bm25_ids = [h.doc_id for h in self.bm25.search(query, top_k * 2)]
        fused = reciprocal_rank_fusion([tfidf_ids, bm25_ids], top_k=top_k)
        hits = []
        for doc_id, score in fused:
            doc = self.store.documents.get(doc_id)
            if doc:
                hits.append(SearchHit(doc_id=doc_id, score=score,
                                      text=doc.text, metadata=doc.metadata))
        return hits


# ---------------------------------------------------------------------------
# Diversified retrieval — Maximal Marginal Relevance (MMR)
# ---------------------------------------------------------------------------

def mmr_rerank(query: str, hits: list[SearchHit], store: VectorStore,
               top_k: int = 5, lambda_: float = 0.7) -> list[SearchHit]:
    """Rerank hits for relevance + diversity.

    MMR = argmax lambda*sim(q,d) - (1-lambda)*max sim(d, selected).
    """
    if not hits:
        return []
    qvec = store._query_vector(query)
    candidates = list(hits)
    selected: list[SearchHit] = []
    while candidates and len(selected) < top_k:
        best, best_score = None, float("-inf")
        for hit in candidates:
            dvec = store._doc_vector(hit.doc_id)
            relevance = VectorStore.cosine_similarity(qvec, dvec)
            redundancy = 0.0
            for sel in selected:
                sim = VectorStore.cosine_similarity(
                    dvec, store._doc_vector(sel.doc_id))
                redundancy = max(redundancy, sim)
            mmr = lambda_ * relevance - (1 - lambda_) * redundancy
            if mmr > best_score:
                best, best_score = hit, mmr
        selected.append(best)  # type: ignore[arg-type]
        candidates.remove(best)
    return selected


# ---------------------------------------------------------------------------
# Near-duplicate detection
# ---------------------------------------------------------------------------

def find_duplicates(store: VectorStore,
                    threshold: float = 0.9) -> list[tuple[str, str, float]]:
    """Pairs of documents with cosine similarity >= threshold."""
    ids = list(store.documents)
    vecs = {d: store._doc_vector(d) for d in ids}
    dups: list[tuple[str, str, float]] = []
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            sim = VectorStore.cosine_similarity(vecs[ids[i]], vecs[ids[j]])
            if sim >= threshold:
                dups.append((ids[i], ids[j], round(sim, 4)))
    dups.sort(key=lambda t: t[2], reverse=True)
    return dups


def dedup_store(store: VectorStore, threshold: float = 0.95) -> int:
    """Remove near-duplicate documents (keeps the first added). Removed count."""
    removed = 0
    for a, b, _ in find_duplicates(store, threshold):
        if a in store.documents and b in store.documents:
            first = min(a, b,
                        key=lambda d: store.documents[d].added_at)
            second = b if first == a else a
            store.remove_document(second)
            removed += 1
    return removed


# ---------------------------------------------------------------------------
# Did-you-mean (edit distance against vocabulary)
# ---------------------------------------------------------------------------

def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance (two-row DP)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1,
                         prev[j - 1] + (0 if ca == cb else 1))
        prev = cur
    return prev[-1]


def did_you_mean(store: VectorStore, query: str,
                 max_distance: int = 2) -> dict[str, str]:
    """Suggest vocabulary corrections for out-of-vocabulary query terms."""
    vocab = set(store.vocabulary())
    suggestions: dict[str, str] = {}
    for term in tokenize(query, remove_stopwords=False):
        if term in vocab or term in STOPWORDS:
            continue
        best, best_d = None, max_distance + 1
        for word in vocab:
            if abs(len(word) - len(term)) > max_distance:
                continue
            d = _edit_distance(term, word)
            if d < best_d:
                best, best_d = word, d
        if best is not None:
            suggestions[term] = best
    return suggestions


# ---------------------------------------------------------------------------
# Simple agglomerative clustering over TF-IDF vectors
# ---------------------------------------------------------------------------

def cluster_documents(store: VectorStore,
                      similarity_threshold: float = 0.25) -> list[list[str]]:
    """Group documents into clusters by average-linkage agglomeration.

    Starts with each doc alone; repeatedly merges the pair of clusters
    with the highest average pairwise similarity above the threshold.
    Returns lists of doc ids.
    """
    ids = list(store.documents)
    if not ids:
        return []
    vecs = {d: store._doc_vector(d) for d in ids}
    clusters: list[set[str]] = [{d} for d in ids]

    def avg_sim(c1: set[str], c2: set[str]) -> float:
        total, n = 0.0, 0
        for a in c1:
            for b in c2:
                total += VectorStore.cosine_similarity(vecs[a], vecs[b])
                n += 1
        return total / n if n else 0.0

    while len(clusters) > 1:
        best_pair: tuple[int, int] | None = None
        best_sim = similarity_threshold
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                s = avg_sim(clusters[i], clusters[j])
                if s > best_sim:
                    best_sim, best_pair = s, (i, j)
        if best_pair is None:
            break
        i, j = best_pair
        clusters[i] = clusters[i] | clusters[j]
        del clusters[j]
    return [sorted(c) for c in clusters]


def cluster_labels(store: VectorStore,
                   clusters: list[list[str]]) -> list[dict]:
    """Label each cluster with its top shared TF-IDF terms."""
    labels = []
    for cluster in clusters:
        term_weights: dict[str, float] = {}
        for doc_id in cluster:
            for term, w in store._doc_vector(doc_id).items():
                term_weights[term] = term_weights.get(term, 0.0) + w
        top = [t for t, _ in sorted(term_weights.items(),
                                    key=lambda kv: kv[1],
                                    reverse=True)[:5]]
        labels.append({"size": len(cluster), "doc_ids": cluster,
                       "label_terms": top})
    return labels


# ---------------------------------------------------------------------------
# Query cache — avoid recomputing identical searches
# ---------------------------------------------------------------------------

class SearchCache:
    """TTL cache for search results (query + top_k -> hits)."""

    def __init__(self, ttl_seconds: float = 300.0, max_entries: int = 200):
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._cache: dict[tuple[str, int], tuple[float, list[SearchHit]]] = {}

    def get(self, query: str, top_k: int) -> list[SearchHit] | None:
        key = (query, top_k)
        entry = self._cache.get(key)
        if not entry:
            return None
        at, hits = entry
        if time.time() - at > self.ttl:
            del self._cache[key]
            return None
        return hits

    def put(self, query: str, top_k: int, hits: list[SearchHit]) -> None:
        if len(self._cache) >= self.max_entries:
            oldest = min(self._cache, key=lambda k: self._cache[k][0])
            del self._cache[oldest]
        self._cache[(query, top_k)] = (time.time(), hits)

    def invalidate(self) -> None:
        self._cache.clear()

    def cached_search(self, store: VectorStore, query: str,
                      top_k: int = 5) -> list[SearchHit]:
        """Search through the cache; populates on miss."""
        hit = self.get(query, top_k)
        if hit is not None:
            return hit
        results = store.search(query, top_k)
        self.put(query, top_k, results)
        return results


# ---------------------------------------------------------------------------
# Extractive summarization — top sentences by TF-IDF weight
# ---------------------------------------------------------------------------

def summarize_document(store: VectorStore, doc_id: str,
                       max_sentences: int = 3) -> str:
    """Extractive summary: highest-scoring sentences by term TF-IDF."""
    doc = store.documents.get(doc_id)
    if not doc:
        return ""
    sentences = [s.strip() for s in
                 re.split(r"(?<=[.!?])\s+", doc.text) if s.strip()]
    if len(sentences) <= max_sentences:
        return doc.text
    vec = store._doc_vector(doc_id)
    scored = []
    for i, sent in enumerate(sentences):
        tokens = tokenize(sent, remove_stopwords=store.remove_stopwords)
        if not tokens:
            continue
        score = sum(vec.get(t, 0.0) for t in tokens) / math.sqrt(len(tokens))
        # Slight preference for earlier sentences.
        score *= 1.0 / (1.0 + 0.05 * i)
        scored.append((score, i, sent))
    scored.sort(key=lambda t: t[0], reverse=True)
    chosen = sorted(scored[:max_sentences], key=lambda t: t[1])
    return " ".join(s for _, _, s in chosen)


def keyword_in_context(store: VectorStore, doc_id: str, term: str,
                       window: int = 40) -> list[str]:
    """KWIC snippets: term shown with `window` chars of context."""
    doc = store.documents.get(doc_id)
    if not doc:
        return []
    snippets = []
    for m in re.finditer(re.escape(term), doc.text, re.IGNORECASE):
        start = max(0, m.start() - window)
        end = min(len(doc.text), m.end() + window)
        snippet = doc.text[start:end].replace("\n", " ").strip()
        snippets.append(f"...{snippet}...")
        if len(snippets) >= 5:
            break
    return snippets


# ---------------------------------------------------------------------------
# MemoryIndex — unified facade: vector search + recency + metadata filters
# ---------------------------------------------------------------------------

class MemoryIndex:
    """High-level memory search combining a VectorStore with recency
    weighting and metadata filtering.

    This is what the agent's control loop calls: remember() to store,
    recall() to retrieve context for the current task.
    """

    def __init__(self, persist_path: str | Path | None = None):
        self.store = VectorStore(persist_path=persist_path)
        self.hybrid = HybridSearcher(self.store)
        self.cache = SearchCache()

    # -- write ---------------------------------------------------------------
    def remember(self, text: str, kind: str = "note",
                 importance: float = 0.5, **metadata) -> str:
        """Store one memory with kind/importance metadata."""
        meta = {"kind": kind, "importance": float(importance)}
        meta.update(metadata)
        doc_id = self.store.add_document(text, metadata=meta)
        self.cache.invalidate()
        return doc_id

    def remember_chunked(self, text: str, kind: str = "note",
                         chunk_size: int = 400, **metadata) -> list[str]:
        """Store a long text as overlapping chunks sharing metadata."""
        meta = {"kind": kind}
        meta.update(metadata)
        ids = self.store.index_text(text, chunk_size=chunk_size,
                                    base_metadata=meta)
        self.cache.invalidate()
        return ids

    def forget(self, doc_id: str) -> bool:
        ok = self.store.remove_document(doc_id)
        if ok:
            self.cache.invalidate()
        return ok

    # -- read ------------------------------------------------------------------
    def recall(self, query: str, top_k: int = 5,
               kind: str | None = None,
               min_importance: float = 0.0,
               diversify: bool = True,
               use_cache: bool = True) -> list[SearchHit]:
        """Retrieve relevant memories.

        Pipeline: hybrid search (TF-IDF + BM25, RRF) -> metadata filter ->
        recency+importance reweight -> optional MMR diversification.
        """
        if use_cache and kind is None and min_importance == 0.0 \
                and not diversify:
            hits = self.cache.cached_search(self.store, query, top_k * 2)
        else:
            hits = self.hybrid.search(query, top_k * 2)

        # Metadata filter.
        filtered = []
        for h in hits:
            meta = h.metadata or {}
            if kind and meta.get("kind") != kind:
                continue
            if float(meta.get("importance", 0.0)) < min_importance:
                continue
            filtered.append(h)

        # Recency + importance reweight (multiplicative, gentle).
        now = time.time()
        rescored = []
        for h in filtered:
            doc = self.store.documents[h.doc_id]
            age_days = max(0.0, (now - doc.added_at) / 86400.0)
            recency = 1.0 / (1.0 + age_days / 30.0)
            importance = 0.5 + 0.5 * float(
                (h.metadata or {}).get("importance", 0.5))
            new_score = h.score * (0.6 + 0.4 * recency) * importance
            rescored.append(SearchHit(doc_id=h.doc_id,
                                      score=round(new_score, 4),
                                      text=h.text, metadata=h.metadata))
        rescored.sort(key=lambda h: h.score, reverse=True)

        if diversify:
            return mmr_rerank(query, rescored, self.store, top_k=top_k)
        return rescored[:top_k]

    def recall_as_context(self, query: str, top_k: int = 3,
                          max_chars: int = 2000) -> str:
        """Memories formatted as a context block for a prompt."""
        hits = self.recall(query, top_k=top_k)
        if not hits:
            return "(no relevant memories)"
        parts = []
        total = 0
        for h in hits:
            chunk = h.text[:800]
            if total + len(chunk) > max_chars:
                break
            parts.append(f"[memory score={h.score:.2f}] {chunk}")
            total += len(chunk)
        return "\n".join(parts)

    # -- maintenance -------------------------------------------------------------
    def save(self) -> Path | None:
        if self.store.persist_path is None:
            return None
        return self.store.save()

    def stats(self) -> dict:
        stats = self.store.corpus_stats()
        kinds: dict[str, int] = {}
        for doc in self.store.documents.values():
            k = (doc.metadata or {}).get("kind", "note")
            kinds[k] = kinds.get(k, 0) + 1
        stats["kinds"] = kinds
        return stats

    def __len__(self) -> int:
        return len(self.store)
