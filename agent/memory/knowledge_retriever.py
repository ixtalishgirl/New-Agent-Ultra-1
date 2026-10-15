"""Knowledge retriever — unified retrieval across memory stores.

Per ChatGPT spec: agent/memory/knowledge_retriever.py
Combines vector search, project memory, and conversation history.
Ranks and merges results.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .vector_index import VectorIndex
from .project_memory import ProjectMemory
from .conversation_store import ConversationStore


@dataclass
class Retrieval:
    """A single retrieved knowledge item."""
    source: str  # "vector", "project_kv", "project_doc", "conversation"
    content: str
    score: float
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"source": self.source, "content": self.content[:1000],
                "score": round(self.score, 3), "metadata": self.metadata}


class KnowledgeRetriever:
    """Unified retrieval across all memory systems."""

    def __init__(self,
                 vector_index: Optional[VectorIndex] = None,
                 project_memory: Optional[ProjectMemory] = None,
                 conversation_store: Optional[ConversationStore] = None):
        self.vector = vector_index or VectorIndex()
        self.projects = project_memory or ProjectMemory()
        self.conversations = conversation_store or ConversationStore()

    def retrieve(self, query: str, top_k: int = 10,
                 sources: Optional[List[str]] = None,
                 project: str = "default") -> List[Retrieval]:
        """Retrieve relevant knowledge from all (or selected) sources."""
        sources = sources or ["vector", "project_kv", "project_doc",
                              "conversation"]
        out: List[Retrieval] = []
        if "vector" in sources:
            out.extend(self._from_vector(query, top_k))
        if "project_kv" in sources:
            out.extend(self._from_kv(query, project))
        if "project_doc" in sources:
            out.extend(self._from_docs(query, project, top_k))
        if "conversation" in sources:
            out.extend(self._from_conversations(query, top_k))
        # Sort by score, dedupe by content hash
        out.sort(key=lambda r: -r.score)
        seen: set = set()
        deduped = []
        for r in out:
            h = hash(r.content[:200])
            if h not in seen:
                seen.add(h)
                deduped.append(r)
        return deduped[:top_k]

    def _from_vector(self, query: str,
                     top_k: int) -> List[Retrieval]:
        results = []
        for doc, score in self.vector.search(query, top_k=top_k):
            results.append(Retrieval(
                source="vector", content=doc.text, score=score,
                metadata={"doc_id": doc.id, **doc.metadata}))
        return results

    def _from_kv(self, query: str,
                 project: str) -> List[Retrieval]:
        results = []
        q = query.lower()
        for key, value in self.projects.all(project).items():
            text = f"{key}: {value}"
            score = 0.9 if q in key.lower() else (
                0.5 if q in str(value).lower() else 0.0)
            if score > 0:
                results.append(Retrieval(
                    source="project_kv", content=text, score=score,
                    metadata={"key": key, "project": project}))
        return results

    def _from_docs(self, query: str, project: str,
                   top_k: int) -> List[Retrieval]:
        results = []
        for d in self.projects.search_documents(query, project, top_k):
            results.append(Retrieval(
                source="project_doc",
                content=f"{d['title']}\n{d.get('snippet', '')}",
                score=0.7,
                metadata={"doc_id": d["id"]}))
        return results

    def _from_conversations(self, query: str,
                            top_k: int) -> List[Retrieval]:
        results = []
        for turn in self.conversations.search(query, top_k):
            results.append(Retrieval(
                source="conversation",
                content=f"[{turn.role}] {turn.content[:500]}",
                score=0.6,
                metadata={"session_id": turn.session_id,
                          "turn_id": turn.id}))
        return results

    def retrieve_context(self, query: str, max_chars: int = 4000,
                         project: str = "default") -> str:
        """Build a context block for the model prompt."""
        items = self.retrieve(query, top_k=8, project=project)
        parts = []
        chars = 0
        for item in items:
            block = f"[{item.source} score={item.score:.2f}]\n{item.content[:800]}"
            if chars + len(block) > max_chars:
                break
            parts.append(block)
            chars += len(block)
        return "\n\n".join(parts)

    def index_text(self, text: str,
                   metadata: Optional[Dict[str, Any]] = None) -> str:
        """Add text to the vector index for future retrieval."""
        return self.vector.add(text, metadata)
