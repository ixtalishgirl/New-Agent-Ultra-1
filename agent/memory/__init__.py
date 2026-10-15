"""Memory module — persistent memory systems."""
from .stores import ConversationStore as LegacyConversationStore
from .conversation_store import ConversationStore, Turn
from .project_memory import ProjectMemory, MemoryEntry
from .vector_index import VectorIndex, IndexedDoc, tokenize
from .knowledge_retriever import KnowledgeRetriever, Retrieval

# Backwards compat: stores.py may define these
try:
    from .stores import MemoryStore, SQLiteStore
    _extra = ["MemoryStore", "SQLiteStore"]
except ImportError:
    _extra = []

__all__ = [
    "LegacyConversationStore",
    "ConversationStore", "Turn",
    "ProjectMemory", "MemoryEntry",
    "VectorIndex", "IndexedDoc", "tokenize",
    "KnowledgeRetriever", "Retrieval",
] + _extra
