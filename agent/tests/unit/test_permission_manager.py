"""Tests for PermissionManager + central registry discovery + dispatcher wiring.

All permissions default ON except the 5 locked denials (ahsan's standing order).
"""
import json
import pytest

from agent.tools.registry import (
    ToolRegistry, default_registry, build_full_registry, collect_handlers,
)
from agent.tools.permission_manager import PermissionManager, CheckResult
from agent.security.policy import DENIALS, ALL_GRANTED


@pytest.fixture()
def reg():
    return build_full_registry()


@pytest.fixture()
def mgr(reg, tmp_path):
    return PermissionManager(registry=reg, state_path=tmp_path / "state.json")


def test_denials_locked_off(mgr):
    for d in DENIALS:
        assert not mgr.is_allowed(f"denial:{d}"), d
        perm = mgr.get(f"denial:{d}")
        assert perm is not None and perm.locked


def test_all_policy_grants_on_by_default(mgr):
    assert len(ALL_GRANTED) == 155
    for item in ALL_GRANTED:
        assert mgr.is_allowed(f"policy:{item[0]}"), item


def test_every_tool_permission_on_by_default(mgr, reg):
    for tool in reg.list():
        assert mgr.is_allowed(f"tool:{tool.name}"), tool.name
        # bare tool name also resolves
        assert mgr.is_allowed(tool.name), tool.name


def test_safety_toggles_default_on(mgr):
    assert mgr.is_allowed("safety:destructive_confirmation")
    assert mgr.is_allowed("safety:audit")


def test_enable_disable_roundtrip(mgr):
    assert mgr.is_allowed("tool:shell")
    r = mgr.disable("tool:shell")
    assert r.allowed and not mgr.is_allowed("tool:shell")
    r = mgr.enable("tool:shell")
    assert r.allowed and mgr.is_allowed("tool:shell")


def test_cannot_enable_locked_denial(mgr):
    r = mgr.enable("denial:container_escape")
    assert not r.allowed
    assert not mgr.is_allowed("denial:container_escape")
    # bare denial name also refused
    r = mgr.enable("container_escape")
    assert not r.allowed


def test_state_persists(tmp_path, reg):
    p = tmp_path / "state.json"
    m1 = PermissionManager(registry=reg, state_path=p)
    m1.disable("tool:shell")
    assert p.exists()
    m2 = PermissionManager(registry=reg, state_path=p)
    assert not m2.is_allowed("tool:shell")
    assert m2.is_allowed("tool:python")


def test_denial_blocks_tool_call(mgr):
    r = mgr.check("shell", {"command": "nsenter -m -p -n /proc/1/root /bin/sh"})
    assert not r.allowed
    assert "container_escape" in r.reason


def test_destructive_confirmation_gate(mgr):
    # file_delete without confirm -> needs confirmation
    r = mgr.check("file_delete", {"path": "/tmp/whatever"})
    assert not r.allowed and r.needs_confirmation
    # with confirm -> allowed (permission itself is ON)
    r = mgr.check("file_delete", {"path": "/tmp/whatever", "confirm": True})
    assert r.allowed
    # toggle OFF -> no confirmation needed
    mgr.disable("safety:destructive_confirmation")
    r = mgr.check("file_delete", {"path": "/tmp/whatever"})
    assert r.allowed


def test_service_control_stop_needs_confirm(mgr):
    r = mgr.check("service_control", {"service": "x", "action": "stop"})
    assert not r.allowed and r.needs_confirmation
    r = mgr.check("service_control", {"service": "x", "action": "status"})
    assert r.allowed


def test_unknown_tool_denied_closed(mgr):
    assert not mgr.is_allowed("tool:no_such_tool_xyz")
    r = mgr.check("no_such_tool_xyz", {})
    assert not r.allowed


def test_discover_returns_all_tools(reg, mgr):
    disc = reg.discover(mgr)
    names = {d["name"] for d in disc}
    for tool in reg.list():
        assert tool.name in names
    for d in disc:
        assert {"name", "description", "schema", "enabled",
                "permission_profile", "version", "status"} <= set(d)
    # spot check: core capabilities discoverable and enabled
    by_name = {d["name"]: d for d in disc}
    for core in ("shell", "python", "pip", "file_read", "file_write",
                 "browser_navigate", "web_search", "tool_create",
                 "git_status", "git_commit"):
        assert core in by_name, core
        assert by_name[core]["enabled"], core


def test_collect_handlers_shape():
    handlers = collect_handlers()
    assert isinstance(handlers, dict)
    for name, fn in handlers.items():
        assert callable(fn), name


def test_dispatcher_uses_permission_manager(reg, tmp_path):
    from agent.tools.dispatcher import Dispatcher
    mgr = PermissionManager(registry=reg, state_path=tmp_path / "s.json")
    disp = Dispatcher(reg, workspace="/tmp", permission_manager=mgr)
    # denial enforced through dispatch path
    r = disp.dispatch("shell", {"command": "nsenter --all sh"})
    assert not r.ok and "container_escape" in r.output
    # disabled permission enforced through dispatch path
    mgr.disable("tool:python")
    r = disp.dispatch("python", {"code": "print(1)"})
    assert not r.ok
    mgr.enable("tool:python")
    # destructive without confirm -> CONFIRMATION_REQUIRED marker
    r = disp.dispatch("file_delete", {"path": "/tmp/xyz_nope"})
    assert not r.ok and "CONFIRMATION_REQUIRED" in r.output


def test_computer_control_modules_registered(reg):
    """If the computer-control modules exist, their tools are registered."""
    import importlib
    mods = ["agent.tools.vision_tools", "agent.tools.input_tools",
            "agent.tools.browser_tools", "agent.tools.filemgmt_tools",
            "agent.tools.app_tools", "agent.tools.code_tools",
            "agent.tools.memory_tools", "agent.tools.safety_tools"]
    present = 0
    for m in mods:
        try:
            importlib.import_module(m)
            present += 1
        except Exception:
            pass
    if present == 0:
        pytest.skip("computer-control modules not yet installed")
    names = {t.name for t in reg.list()}
    # category spot-checks, tolerant of exact naming choices
    assert any(n.startswith("screen_") for n in names), "vision tools"
    assert any(n.startswith("file_") or n.startswith("dir_")
               or n.startswith("trash_") for n in names), "file mgmt tools"
    assert any(n.startswith("app_") or n.startswith("process_")
               or n.startswith("service_") for n in names), "app tools"
    assert any(n.startswith("memory_") or n.startswith("macro_")
               or n.startswith("sched_") or n.startswith("workflow_")
               or n in ("branch_if", "loop_repeat", "parallel_run",
                        "retry_backoff", "event_emit")
               for n in names), "memory/automation tools"
    try:
        importlib.import_module("agent.tools.input_tools")
        assert any(n.startswith("mouse_") for n in names), "mouse tools"
        assert any(n.startswith("key_") or n.startswith("clipboard_")
                   for n in names), "keyboard tools"
    except Exception:
        pass
    try:
        importlib.import_module("agent.tools.browser_tools")
        assert any(n.startswith("browser_") and n not in
                   ("browser_navigate", "browser_screenshot", "browser_extract")
                   for n in names), "new browser tools"
    except Exception:
        pass
    try:
        importlib.import_module("agent.tools.safety_tools")
        assert any(n.startswith("perm_") or n in
                   ("audit_log", "session_info", "health_check",
                    "safe_mode", "backup_create") for n in names), "safety tools"
    except Exception:
        pass
