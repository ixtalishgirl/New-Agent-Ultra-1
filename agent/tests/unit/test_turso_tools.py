"""Turso tools: arg conversion, pipeline format, result parsing, no leaks.

urllib is fully mocked — no network, no credentials.
"""
import io
import json

import pytest

from agent.tools import turso_tools as tt


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ok_payload(cols, rows):
    return {"results": [
        {"type": "ok",
         "response": {"type": "execute",
                      "result": {
                          "cols": [{"name": c} for c in cols],
                          "rows": [[{"type": "text", "value": str(v)}
                                    if not isinstance(v, int)
                                    else {"type": "integer",
                                          "value": str(v)}
                                    for v in row]
                                   for row in rows]}}},
        {"type": "ok", "response": {"type": "close"}},
    ]}


@pytest.fixture()
def turso_env(monkeypatch):
    monkeypatch.setenv("TURSO_URL", "https://mydb-org.turso.io")
    monkeypatch.setenv("TURSO_TOKEN", "tok123")


def _patch(monkeypatch, payload):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["auth"] = req.headers.get("Authorization")
        seen["body"] = json.loads(req.data.decode())
        return _FakeResp(payload)

    monkeypatch.setattr(tt.urllib.request, "urlopen", fake_urlopen)
    return seen


def test_select_parses_rows(turso_env, monkeypatch):
    seen = _patch(monkeypatch, _ok_payload(["name", "n"], [["a", 1]]))
    r = tt.turso_query({"sql": "SELECT * FROM t"})
    assert r.ok is True, r.error
    assert r.parsed["rows"] == [{"name": "a", "n": 1}]
    assert seen["url"] == "https://mydb-org.turso.io/v2/pipeline"
    assert seen["auth"] == "Bearer tok123"
    stmt = seen["body"]["requests"][0]["stmt"]
    assert stmt["sql"] == "SELECT * FROM t"


def test_params_converted(turso_env, monkeypatch):
    seen = _patch(monkeypatch, _ok_payload([], []))
    r = tt.turso_query({"sql": "INSERT INTO t VALUES (?, ?, ?, ?)",
                        "params": ["x", 5, 2.5, None]})
    assert r.ok is True
    args = seen["body"]["requests"][0]["stmt"]["args"]
    assert args[0] == {"type": "text", "value": "x"}
    assert args[1] == {"type": "integer", "value": "5"}
    assert args[2]["type"] == "float"
    assert args[3] == {"type": "null"}


def test_create_table_ok(turso_env, monkeypatch):
    _patch(monkeypatch, _ok_payload([], []))
    r = tt.turso_query({"sql": "CREATE TABLE t (id INTEGER PRIMARY KEY)"})
    assert r.ok is True
    assert "ok" in r.output


def test_turso_error_reported(turso_env, monkeypatch):
    _patch(monkeypatch, {"results": [
        {"type": "error",
         "error": {"message": "no such table: nope"}}]})
    r = tt.turso_query({"sql": "SELECT * FROM nope"})
    assert r.ok is False
    assert "no such table" in r.error


def test_missing_env(monkeypatch):
    monkeypatch.delenv("TURSO_URL", raising=False)
    monkeypatch.delenv("TURSO_TOKEN", raising=False)
    r = tt.turso_query({"sql": "SELECT 1"})
    assert r.ok is False
    assert "TURSO_URL" in r.error


def test_token_never_in_error(turso_env, monkeypatch):
    def boom(req, timeout=None):
        raise OSError("conn failed, token was Bearer tok123456789")

    monkeypatch.setattr(tt.urllib.request, "urlopen", boom)
    r = tt.turso_query({"sql": "SELECT 1"})
    assert r.ok is False
    assert "tok123456789" not in r.error


def test_empty_sql_rejected(turso_env):
    assert tt.turso_query({"sql": "  "}).ok is False


def test_tool_defs_registered():
    assert tt.TOOL_DEFS[0][0] == "turso_query"
    assert "turso_query" in tt.HANDLERS
