"""Supabase REST fallback: SQL->PostgREST translation + route chain.

All HTTP is mocked — never touches the real database or network.
"""
import pytest

from agent.tools import supabase_tools as st


@pytest.fixture()
def rest_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_DB_URL", "")
    monkeypatch.setenv("SUPABASE_URL", "https://xyz.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")
    st._ROUTE["mode"] = None
    yield
    st._ROUTE["mode"] = None


class _Harness:
    def __init__(self, monkeypatch, rows=None):
        self.calls = []
        self.rows = rows if rows is not None else [{"id": 1}]
        monkeypatch.setattr(st, "_rest_call", self._fake)

    def _fake(self, url, key, method, path, params=None, body=None):
        self.calls.append((method, path, params or {}, body))
        return list(self.rows), 200


def test_select_star(rest_env, monkeypatch):
    h = _Harness(monkeypatch)
    res = st.supabase_query({"sql": "SELECT * FROM todos"})
    assert res.ok is True, res.error
    method, path, params, body = h.calls[0]
    assert (method, path) == ("GET", "/rest/v1/todos")
    assert params.get("select") == "*"
    assert body is None
    assert res.parsed["route"] == "rest"


def test_select_cols_where_order_limit(rest_env, monkeypatch):
    h = _Harness(monkeypatch)
    res = st.supabase_query(
        {"sql": "SELECT id, title FROM todos WHERE done = false "
                "AND priority > 2 ORDER BY id DESC LIMIT 5"})
    assert res.ok is True, res.error
    _, _, params, _ = h.calls[0]
    assert params["select"] == "id,title"
    assert params["done"] == "eq.false"
    assert params["priority"] == "gt.2"
    assert params["order"] == "id.desc"
    assert params["limit"] == "5"


def test_insert(rest_env, monkeypatch):
    h = _Harness(monkeypatch, rows=[{"id": 7, "title": "hi"}])
    res = st.supabase_query(
        {"sql": "INSERT INTO todos (title, done) VALUES ('hi', false), "
                "('yo', true)"})
    assert res.ok is True, res.error
    method, path, params, body = h.calls[0]
    assert method == "POST" and path == "/rest/v1/todos"
    assert body == [{"title": "hi", "done": False},
                    {"title": "yo", "done": True}]


def test_update_requires_where(rest_env, monkeypatch):
    h = _Harness(monkeypatch)
    res = st.supabase_query(
        {"sql": "UPDATE todos SET done = true WHERE id = 3"})
    assert res.ok is True, res.error
    method, path, params, body = h.calls[0]
    assert method == "PATCH"
    assert body == {"done": True}
    assert params["id"] == "eq.3"

    res2 = st.supabase_query({"sql": "UPDATE todos SET done = true"})
    assert res2.ok is False
    assert "WHERE" in res2.error


def test_delete(rest_env, monkeypatch):
    h = _Harness(monkeypatch, rows=[])
    res = st.supabase_query(
        {"sql": "DELETE FROM todos WHERE id IN (1, 2) AND title IS NOT NULL"})
    assert res.ok is True, res.error
    method, path, params, body = h.calls[0]
    assert method == "DELETE"
    assert params["id"] == "in.(1,2)"
    assert params["title"] == "not.is.null"


def test_ddl_honest_error(rest_env, monkeypatch):
    h = _Harness(monkeypatch)
    res = st.supabase_query(
        {"sql": "CREATE TABLE t (id bigint primary key)"})
    assert res.ok is False
    assert "DDL" in res.error
    assert not h.calls, "DDL must never hit the REST API"


def test_string_escaping(rest_env, monkeypatch):
    h = _Harness(monkeypatch)
    st.supabase_query(
        {"sql": "INSERT INTO todos (title) VALUES ('it''s ok')"})
    _, _, _, body = h.calls[0]
    assert body == [{"title": "it's ok"}]


def test_pg_preferred_then_rest_fallback(monkeypatch):
    """Direct pg fails -> REST used; winning route is remembered."""
    monkeypatch.setenv("SUPABASE_DB_URL",
                       "postgresql://u:pw@db.xyz.supabase.co:5432/postgres")
    monkeypatch.setenv("SUPABASE_URL", "https://xyz.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")
    st._ROUTE["mode"] = None
    try:
        calls = {"pg": 0}

        def fake_pg(sql, dsn):
            calls["pg"] += 1
            return st.SupabaseResult(ok=False, error="pg down")

        h = _Harness(monkeypatch)
        monkeypatch.setattr(st, "_run_psycopg2", fake_pg)
        res = st.supabase_query({"sql": "SELECT * FROM todos"})
        assert res.ok is True
        assert calls["pg"] == 1
        assert st._ROUTE["mode"] == "rest"
        # second query skips pg entirely (cached route)
        st.supabase_query({"sql": "SELECT * FROM todos"})
        assert calls["pg"] == 1
    finally:
        st._ROUTE["mode"] = None


def test_pooler_targets_built_from_dsn():
    kw = st._pooler_targets(
        "postgresql://postgres:s3cret@db.abcdef.supabase.co:5432/postgres"
        "?sslmode=require")
    assert kw, "expected pooler targets"
    first = kw[0]
    assert first["host"] == "aws-0-eu-central-1.pooler.supabase.com"
    assert first["port"] == 6543
    assert first["user"] == "postgres.abcdef"
    assert first["password"] == "s3cret"
    assert first["dbname"] == "postgres"
