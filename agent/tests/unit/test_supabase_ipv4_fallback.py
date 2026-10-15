"""Supabase IPv4 fallback: when the DB host resolves IPv6-first on a host
without IPv6 egress ("Network is unreachable"), the connector retries with a
forced-IPv4 hostaddr. Mocks psycopg2 and DNS — never touches the real DB.
"""
import socket
import sys
import types

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
    def __init__(self, rows=None, rowcount=1):
        self._rows = rows or [{"one": 1}]
        self._rowcount = rowcount

    def cursor(self, cursor_factory=None):
        return _FakeCursor(self._rows, self._rowcount)

    def close(self):
        pass


def _install_fake_psycopg2(monkeypatch, connect_impl):
    fake_mod = types.ModuleType("psycopg2")
    fake_extras = types.ModuleType("psycopg2.extras")

    class RealDictCursor:
        pass

    fake_extras.RealDictCursor = RealDictCursor
    fake_mod.extras = fake_extras
    fake_mod.connect = connect_impl
    monkeypatch.setitem(sys.modules, "psycopg2", fake_mod)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", fake_extras)


def _fake_dns(monkeypatch, ipv4="203.0.113.7"):
    def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        assert family == socket.AF_INET, "must request IPv4 explicitly"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ipv4, port))]

    monkeypatch.setattr(st.socket, "getaddrinfo", fake_getaddrinfo)


DSN = "postgresql://u:pw@db.example.supabase.co:5432/postgres?sslmode=require"


def test_ipv4_retry_on_network_unreachable(monkeypatch):
    calls = []

    def connect_impl(dsn=None, **kwargs):
        calls.append((dsn, kwargs))
        if "hostaddr" not in kwargs:
            raise Exception(
                'connection to server at "db.example.supabase.co" '
                "(2a05:d014::1), port 5432 failed: Network is unreachable"
            )
        return _FakeConn()

    _install_fake_psycopg2(monkeypatch, connect_impl)
    _fake_dns(monkeypatch)

    res = st._run_psycopg2("SELECT 1", DSN)
    assert res.ok is True, res.error
    assert len(calls) == 2
    assert calls[1][1].get("hostaddr") == "203.0.113.7"
    assert "password" not in res.error and "pw" not in res.output


def test_no_retry_on_auth_error(monkeypatch):
    calls = []

    def connect_impl(dsn=None, **kwargs):
        calls.append(kwargs)
        raise Exception('password authentication failed for user "u"')

    _install_fake_psycopg2(monkeypatch, connect_impl)
    _fake_dns(monkeypatch)

    res = st._run_psycopg2("SELECT 1", DSN)
    assert res.ok is False
    assert len(calls) == 1, "auth errors must not trigger an IPv4 retry"
    assert "authentication failed" in res.error


def test_direct_success_no_dns_lookup(monkeypatch):
    def connect_impl(dsn=None, **kwargs):
        assert "hostaddr" not in kwargs
        return _FakeConn()

    _install_fake_psycopg2(monkeypatch, connect_impl)

    def no_dns(*a, **k):  # pragma: no cover
        raise AssertionError("DNS must not be consulted on direct success")

    monkeypatch.setattr(st.socket, "getaddrinfo", no_dns)

    res = st._run_psycopg2("SELECT 1", DSN)
    assert res.ok is True
