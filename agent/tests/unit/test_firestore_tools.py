"""Firestore tools: validation, CRUD, query translation, backup wiring.

The firebase_admin SDK is fully mocked — no network, no credentials.
"""
import pytest

from agent.tools import firestore_tools as ft


class _FakeDoc:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data) if self._data else None


class _FakeDocRef:
    def __init__(self, store, doc_id):
        self._store = store
        self._id = doc_id

    def set(self, data, merge=False):
        if merge and self._id in self._store:
            self._store[self._id].update(data)
        else:
            self._store[self._id] = dict(data)

    def get(self):
        return _FakeDoc(self._id, self._store.get(self._id))

    def delete(self):
        self._store.pop(self._id, None)


class _FakeQuery:
    def __init__(self, store):
        self._store = store
        self._filters = []
        self._limit = None

    def where(self, field, op, value):
        self._filters.append((field, op, value))
        return self

    def order_by(self, *a, **k):
        return self

    def limit(self, n):
        self._limit = n
        return self

    def _match(self, doc_id, data):
        for field, op, value in self._filters:
            v = data.get(field)
            if op == "==":
                ok = v == value
            elif op == "!=":
                ok = v != value
            elif op == ">":
                ok = v is not None and v > value
            elif op == "<":
                ok = v is not None and v < value
            elif op == ">=":
                ok = v is not None and v >= value
            elif op == "<=":
                ok = v is not None and v <= value
            elif op == "in":
                ok = v in value
            else:
                ok = False
            if not ok:
                return False
        return True

    def stream(self):
        out = [_FakeDoc(k, v) for k, v in self._store.items()
               if self._match(k, v)]
        return out[: self._limit] if self._limit else out


class _FakeCollection:
    def __init__(self, store):
        self._store = store

    def document(self, doc_id):
        return _FakeDocRef(self._store, doc_id)

    def where(self, field, op, value):
        return _FakeQuery(self._store).where(field, op, value)

    def order_by(self, *a, **k):
        return _FakeQuery(self._store)

    def limit(self, n):
        return _FakeQuery(self._store).limit(n)

    def stream(self):
        return _FakeQuery(self._store).stream()


class _FakeDB:
    def __init__(self):
        self.cols = {}

    def collection(self, name):
        return _FakeCollection(self.cols.setdefault(name, {}))


@pytest.fixture()
def fdb(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(ft, "_client", db)
    return db


def test_set_get_roundtrip(fdb):
    r = ft.firestore_set({"collection": "memory", "doc_id": "k1",
                          "data": {"a": 1}})
    assert r.ok is True, r.error
    g = ft.firestore_get({"collection": "memory", "doc_id": "k1"})
    assert g.ok is True
    assert g.parsed["doc"]["a"] == 1
    assert g.parsed["doc"]["_id"] == "k1"


def test_get_missing(fdb):
    r = ft.firestore_get({"collection": "memory", "doc_id": "nope"})
    assert r.ok is False
    assert "not found" in r.error


def test_delete(fdb):
    ft.firestore_set({"collection": "m", "doc_id": "d", "data": {"x": 1}})
    assert ft.firestore_delete({"collection": "m", "doc_id": "d"}).ok is True
    assert ft.firestore_get({"collection": "m", "doc_id": "d"}).ok is False


def test_bad_names_rejected(fdb):
    assert ft.firestore_set(
        {"collection": "bad/name", "doc_id": "d", "data": {}}).ok is False
    assert ft.firestore_get(
        {"collection": "memory", "doc_id": ""}).ok is False
    assert ft.firestore_set(
        {"collection": "memory", "doc_id": "d",
         "data": "not-a-dict"}).ok is False


def test_query_filters(fdb):
    ft.firestore_set({"collection": "t", "doc_id": "a",
                      "data": {"n": 1, "tag": "x"}})
    ft.firestore_set({"collection": "t", "doc_id": "b",
                      "data": {"n": 5, "tag": "y"}})
    r = ft.firestore_query({"collection": "t",
                            "filters": [{"field": "n", "op": ">",
                                         "value": 2}]})
    assert r.ok is True
    assert [d["_id"] for d in r.parsed["docs"]] == ["b"]
    r2 = ft.firestore_query({"collection": "t",
                             "filters": [{"field": "tag", "op": "==",
                                          "value": "x"}],
                             "limit": 10})
    assert [d["_id"] for d in r2.parsed["docs"]] == ["a"]


def test_bad_filter_op_rejected(fdb):
    r = ft.firestore_query({"collection": "t",
                            "filters": [{"field": "n", "op": "LIKE",
                                         "value": 1}]})
    assert r.ok is False
    assert "bad filter" in r.error


def test_list(fdb):
    for i in range(3):
        ft.firestore_set({"collection": "c", "doc_id": f"d{i}",
                          "data": {"i": i}})
    r = ft.firestore_list({"collection": "c", "limit": 2})
    assert r.ok is True
    assert len(r.parsed["docs"]) == 2


def test_no_secret_in_errors(monkeypatch):
    def boom():
        raise RuntimeError("auth failed: private_key=SECRETXYZ leaked?")

    monkeypatch.setattr(ft, "_db", boom)
    r = ft.firestore_get({"collection": "c", "doc_id": "d"})
    assert r.ok is False
    assert "SECRETXYZ" not in r.error
    assert "***" in r.error


def test_custom_tool_backup_prefers_firestore(monkeypatch):
    from agent.tools import custom_tools as ct
    fdb = _FakeDB()
    monkeypatch.setattr(ft, "_client", fdb)
    ok, note = ct.backup_to_firestore("my_tool", "code()", {"type": "object"},
                                      "desc")
    assert ok is True, note
    assert fdb.cols["custom_tools"]["my_tool"]["code"] == "code()"
    # restore writes files; just check the doc round-trips
    g = ft.firestore_get({"collection": "custom_tools", "doc_id": "my_tool"})
    assert g.ok is True
    assert g.parsed["doc"]["description"] == "desc"
    assert g.parsed["doc"]["name"] == "my_tool"


def test_tool_defs_registered():
    names = [t[0] for t in ft.TOOL_DEFS]
    assert {"firestore_set", "firestore_get", "firestore_delete",
            "firestore_query", "firestore_list"} <= set(names)
    assert set(ft.HANDLERS) >= set(names)
