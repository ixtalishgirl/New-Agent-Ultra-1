"""tool_create produces REAL tools the agent itself uses (not code dumps).

Covers: create -> immediate dispatch round-trip, manifest persistence,
fresh-registry reload (restart simulation), denial enforcement on generated
code, and Supabase-unconfigured graceful fallback.
"""
import json
import shutil

import pytest

from agent.tools import custom_tools as ct
from agent.tools.dispatcher import Dispatcher
from agent.tools.registry import build_full_registry

CODE = '''
def run(args):
    n = int(args.get("n", 1))
    return {"ok": True, "output": f"triple:{n*3}", "parsed": {"v": n*3}}
'''


@pytest.fixture()
def disp():
    reg = build_full_registry()
    d = Dispatcher(reg, workspace="/tmp")
    yield d
    # cleanup: remove any custom tools created by the test
    for p in ct.CUSTOM_DIR.glob("*.py"):
        if p.name != "__init__.py":
            p.unlink()
    if ct.MANIFEST.exists():
        ct.MANIFEST.unlink()


def test_create_dispatch_roundtrip(disp):
    r = disp.dispatch("tool_create", {
        "name": "tripler_test",
        "description": "Triples a number (test tool)",
        "code": CODE,
    })
    assert r.ok, r.output
    info = json.loads(r.output)
    assert info["active"] is True
    assert info["name"] == "tripler_test"

    # usable IMMEDIATELY in the same session, like any built-in tool
    r2 = disp.dispatch("tripler_test", {"n": 5})
    assert r2.ok, r2.output
    assert "triple:15" in r2.output

    # manifest records it
    manifest = json.loads(ct.MANIFEST.read_text())
    assert any(e["name"] == "tripler_test" for e in manifest)


def test_reload_from_disk(disp):
    disp.dispatch("tool_create", {"name": "reloader_test", "code": CODE})
    # simulate a restart: brand-new registry loads custom/ via manifest
    reg2 = build_full_registry()
    assert reg2.get("reloader_test") is not None
    d2 = Dispatcher(reg2, workspace="/tmp")
    r = d2.dispatch("reloader_test", {"n": 2})
    assert r.ok and "triple:6" in r.output


def test_denial_enforced_on_generated_code(disp):
    evil = 'def run(args):\n import subprocess\n subprocess.run("nsenter -m -t 1 /bin/sh", shell=True)\n return {"ok": True}\n'
    r = disp.dispatch("tool_create", {"name": "evil_test", "code": evil})
    assert not r.ok
    assert "DENIED" in r.output


def test_code_must_define_run(disp):
    r = disp.dispatch("tool_create", {"name": "norun_test",
                                     "code": "x = 1\n"})
    assert not r.ok
    assert "run(args" in r.output


def test_supabase_backup_graceful_without_config(disp, monkeypatch):
    # no SUPABASE_DB_URL / SUPABASE_URL in test env -> best-effort warning
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    ok, note = ct.backup_to_supabase("x", "def run(a): return {}", {}, "d")
    assert ok is False
    assert "not configured" in note


def test_playwright_detection_logic():
    from agent.tools import browser_tools as bt
    # must return a bool and never raise, with or without playwright installed
    assert isinstance(bt._playwright_available(), bool)
