"""Firebase Firestore — the agent's primary memory/data store.

Why Firestore: it speaks plain HTTPS (IPv4), so it works from hosts with
no IPv6 egress where direct Postgres is unreachable. Collections and
documents are created implicitly on first write — no DDL step needed.

Used for: agent memory (key/value + documents), custom-tool backup,
and any structured data the agent must keep.

Auth: the FIREBASE_SERVICE_ACCOUNT env var holds the service-account
JSON. The secret is NEVER logged, printed, or returned in outputs.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class FirestoreResult:
    ok: bool
    output: str = ""
    parsed: dict = field(default_factory=dict)
    error: str = ""


_COLLECTION_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_DOC_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,256}$")
_QUERY_OPS = {"==", "!=", ">", "<", ">=", "<=", "in", "not-in",
              "array-contains", "array-contains-any"}

_client: Any = None


def _redact_key(text: str) -> str:
    # Never leak fragments of the service account.
    return re.sub(r"(?i)(private[_-]?key.{0,40})", r"private_key=***", text)


def _db() -> Any:
    """Firestore client, initialized once per process. Never raises secrets."""
    global _client
    if _client is not None:
        return _client
    try:
        import firebase_admin  # type: ignore
        from firebase_admin import credentials, firestore  # type: ignore
    except ImportError:
        raise RuntimeError("firebase_admin not installed "
                           "(pip install firebase-admin)")
    raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "").strip()
    if not raw:
        raise RuntimeError("FIREBASE_SERVICE_ACCOUNT env var is not set")
    try:
        info = json.loads(raw)
    except Exception:
        raise RuntimeError("FIREBASE_SERVICE_ACCOUNT is not valid JSON")
    cred = credentials.Certificate(info)
    try:
        app = firebase_admin.get_app()
    except ValueError:
        app = firebase_admin.initialize_app(cred)
    _client = firestore.client(app)
    return _client


def _check_names(collection: str, doc_id: str | None = None) -> tuple[str, str]:
    collection = (collection or "").strip()
    if not _COLLECTION_RE.match(collection):
        raise ValueError(f"bad collection name {collection!r}")
    if doc_id is not None:
        doc_id = (doc_id or "").strip()
        if not _DOC_RE.match(doc_id):
            raise ValueError(f"bad doc id {doc_id!r}")
        return collection, doc_id
    return collection, ""


def _doc_to_dict(doc) -> dict:
    d = doc.to_dict() or {}
    d["_id"] = doc.id
    return d


def firestore_set(args: dict) -> FirestoreResult:
    """Create/update a document: {collection, doc_id, data: dict, merge?}."""
    try:
        collection, doc_id = _check_names(args.get("collection"),
                                          args.get("doc_id"))
        data = args.get("data")
        if not isinstance(data, dict):
            return FirestoreResult(ok=False,
                                   error="data must be a JSON object")
        merge = bool(args.get("merge", True))
        _db().collection(collection).document(doc_id).set(data, merge=merge)
        return FirestoreResult(ok=True,
                               output=f"set {collection}/{doc_id}",
                               parsed={"id": doc_id})
    except Exception as e:
        return FirestoreResult(ok=False, error=_redact_key(str(e))[:500])


def firestore_get(args: dict) -> FirestoreResult:
    """Read one document: {collection, doc_id}."""
    try:
        collection, doc_id = _check_names(args.get("collection"),
                                          args.get("doc_id"))
        doc = _db().collection(collection).document(doc_id).get()
        if not doc.exists:
            return FirestoreResult(ok=False,
                                   error=f"{collection}/{doc_id} not found")
        data = _doc_to_dict(doc)
        return FirestoreResult(ok=True,
                               output=json.dumps(data)[:4000],
                               parsed={"doc": data})
    except Exception as e:
        return FirestoreResult(ok=False, error=_redact_key(str(e))[:500])


def firestore_delete(args: dict) -> FirestoreResult:
    """Delete one document: {collection, doc_id}."""
    try:
        collection, doc_id = _check_names(args.get("collection"),
                                          args.get("doc_id"))
        _db().collection(collection).document(doc_id).delete()
        return FirestoreResult(ok=True,
                               output=f"deleted {collection}/{doc_id}")
    except Exception as e:
        return FirestoreResult(ok=False, error=_redact_key(str(e))[:500])


def firestore_query(args: dict) -> FirestoreResult:
    """Query documents: {collection, filters?: [{field, op, value}],
    order_by?, order_dir? (asc|desc), limit?}."""
    try:
        collection, _ = _check_names(args.get("collection"))
        q = _db().collection(collection)
        for f in args.get("filters") or []:
            field, op, value = f.get("field"), f.get("op"), f.get("value")
            if not field or op not in _QUERY_OPS:
                return FirestoreResult(
                    ok=False,
                    error=f"bad filter {f!r}; ops: {sorted(_QUERY_OPS)}")
            q = q.where(field, op, value)
        order_by = args.get("order_by")
        if order_by:
            from firebase_admin import firestore as _fs  # type: ignore
            direction = (_fs.Query.DESCENDING
                         if str(args.get("order_dir", "asc")).lower() == "desc"
                         else _fs.Query.ASCENDING)
            q = q.order_by(order_by, direction=direction)
        limit = args.get("limit")
        if limit:
            q = q.limit(int(limit))
        docs = [_doc_to_dict(d) for d in q.stream()]
        return FirestoreResult(ok=True,
                               output=f"{len(docs)} doc(s) from {collection}",
                               parsed={"docs": docs})
    except Exception as e:
        return FirestoreResult(ok=False, error=_redact_key(str(e))[:500])


def firestore_list(args: dict) -> FirestoreResult:
    """List documents in a collection: {collection, limit? (default 50)}."""
    try:
        collection, _ = _check_names(args.get("collection"))
        limit = int(args.get("limit", 50))
        docs = [_doc_to_dict(d)
                for d in _db().collection(collection).limit(limit).stream()]
        return FirestoreResult(ok=True,
                               output=f"{len(docs)} doc(s) in {collection}",
                               parsed={"docs": docs})
    except Exception as e:
        return FirestoreResult(ok=False, error=_redact_key(str(e))[:500])


def _S(arg_type: str, required: bool, desc: str) -> dict:
    return {"type": arg_type, "required": required, "description": desc}


TOOL_DEFS: list[tuple] = [
    ("firestore_set", "Write a document to Firestore (agent memory/data store)",
     {"collection": _S("string", True, "Collection name"),
      "doc_id": _S("string", True, "Document id"),
      "data": _S("object", True, "JSON object to store"),
      "merge": _S("boolean", False, "Merge with existing (default true)")},
     "data", 60),
    ("firestore_get", "Read one Firestore document",
     {"collection": _S("string", True, "Collection name"),
      "doc_id": _S("string", True, "Document id")},
     "data", 60),
    ("firestore_delete", "Delete one Firestore document",
     {"collection": _S("string", True, "Collection name"),
      "doc_id": _S("string", True, "Document id")},
     "data", 60),
    ("firestore_query", "Query Firestore with filters",
     {"collection": _S("string", True, "Collection name"),
      "filters": _S("array", False, "[{field, op, value}] ops: ==,!=,>,<,>=,<=,in,not-in,array-contains"),
      "order_by": _S("string", False, "Field to sort by"),
      "order_dir": _S("string", False, "asc|desc"),
      "limit": _S("integer", False, "Max docs")},
     "data", 60),
    ("firestore_list", "List documents in a Firestore collection",
     {"collection": _S("string", True, "Collection name"),
      "limit": _S("integer", False, "Max docs (default 50)")},
     "data", 60),
]

HANDLERS: dict[str, Any] = {
    "firestore_set": firestore_set,
    "firestore_get": firestore_get,
    "firestore_delete": firestore_delete,
    "firestore_query": firestore_query,
    "firestore_list": firestore_list,
}
