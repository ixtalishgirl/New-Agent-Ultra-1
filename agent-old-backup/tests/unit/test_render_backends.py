"""Render backend wiring: Supabase executor, Playwright detection, screenshots.

Supabase tests use mocks/fakes — never the real database, never real creds.
"""
import base64
import json
import os

import pytest

from agent.tools import supabase_tools as st


class _FakeCursor:
    def __init__(self, rows, rowcount):
        self._rows = rows
        self.rowcount = rowcount

    def execute(self, sql):
        self.sql = sql

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    instances = []

    def __init__(self, rows=None, rowcount=0, fail=None):
        self._rows = rows or []
        self._rowcount = rowcount
        self._fail = fail
        _FakeConn.instances.append(self)

    def cursor(self, cursor_factory=None):
        if self._fail:
            raise self._fail
        return _FakeCursor(self._rows, self._rowcount)

    def close(self):
        pass


def _patch_psycopg2(monkeypatch, rows=None, rowcount=0, fail=None):
    import sys
    import types

    fake_mod = types.ModuleType("psycopg2")
    fake_extras = types.ModuleType("psycopg2.extras")

    class RealDictCursor:
        pass

    fake_extras.RealDictCursor = RealDictCursor

    def fake_connect(dsn):
        assert "fake-host" in dsn  # the fake DSN reached the driver
        if fail:
            raise fail
        return _FakeConn(rows=rows, rowcount=rowcount)

    fake_mod.connect = fake_connect
    fake_mod.extras = fake_extras
    monkeypatch.setitem(sys.modules, "psycopg2", fake_mod)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", fake_extras)


def test_supabase_select_wiring(monkeypatch):
    monkeypatch.setenv("SUPABASE_DB_URL",
                       "postgresql://u:SUPERSECRETPW@fake-host/db")
    _patch_psycopg2(monkeypatch, rows=[{"id": 1}], rowcount=1)
    r = st.supabase_query({"sql": "SELECT 1"})
    assert r.ok, r.error
    assert r.parsed["rows"] == [{"id": 1}]


def test_supabase_ddl_allowed(monkeypatch):
    """CREATE TABLE must work — the user owns the DB (no read-only guard)."""
    monkeypatch.setenv("SUPABASE_DB_URL",
                       "postgresql://u:SUPERSECRETPW@fake-host/db")

    class NoRowsCursor(_FakeCursor):
        def fetchall(self):
            raise Exception("no results to fetch")

    import sys
    import types
    fake_mod = types.ModuleType("psycopg2")
    fake_extras = types.ModuleType("psycopg2.extras")
    fake_extras.RealDictCursor = type("RealDictCursor", (), {})

    class Conn(_FakeConn):
        def cursor(self, cursor_factory=None):
            return NoRowsCursor([], 0)

    fake_mod.connect = lambda dsn: Conn()
    fake_mod.extras = fake_extras
    monkeypatch.setitem(sys.modules, "psycopg2", fake_mod)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", fake_extras)

    r = st.supabase_query({"sql": "CREATE TABLE t (id INT)"})
    assert r.ok, r.error


def test_supabase_error_redacts_credentials(monkeypatch):
    monkeypatch.setenv("SUPABASE_DB_URL",
                       "postgresql://u:SUPERSECRETPW@fake-host/db")
    _patch_psycopg2(monkeypatch, fail=Exception("auth failed for u:SUPERSECRETPW"))
    r = st.supabase_query({"sql": "SELECT 1"})
    assert not r.ok
    assert "SUPERSECRETPW" not in r.error


def test_supabase_unconfigured(monkeypatch):
    for k in ("SUPABASE_DB_URL", "SUPABASE_URL", "SUPABASE_KEY",
              "SUPABASE_ANON_KEY"):
        monkeypatch.delenv(k, raising=False)
    r = st.supabase_query({"sql": "SELECT 1"})
    assert not r.ok
    assert "not configured" in r.error


def test_playwright_detection_never_raises():
    from agent.tools import browser_tools as bt
    assert isinstance(bt._playwright_available(), bool)


def test_screenshot_base64_shape(tmp_path):
    """screen_shot output contract: parsed['data_uri'] is a PNG data URI."""
    from agent.tools import vision_tools as vt
    png = (tmp_path / "t.png")
    # minimal 1x1 PNG
    raw = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
    png.write_bytes(raw)
    uri = vt._data_uri(str(png))
    assert uri.startswith("data:image/png;base64,")
    assert base64.b64decode(uri.split(",", 1)[1]) == raw


def test_chat_renders_screenshot_markdown():
    """Chat.tsx regex contract: ![alt](data:image/...;base64,...) is detected."""
    import re
    pattern = r"!\[([^\]]*)\]\((data:image\/[a-zA-Z+]+;base64,[A-Za-z0-9+/=]+)\)"
    text = "here ![screenshot](data:image/png;base64,QUJD) done"
    m = re.search(pattern, text)
    assert m and m.group(2) == "data:image/png;base64,QUJD"
