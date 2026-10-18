"""Self-replication, browser login, element inspect, self-modification.

No network, no real browser, no real model — providers and the browser
op-layer are stubbed/mocked. Proves the machinery, not the backends.
"""
import threading

import pytest

from agent.model.provider import ModelProvider, ModelResponse
from agent.tools import replicate_tools as rt
from agent.tools.dispatcher import Dispatcher
from agent.tools.registry import build_full_registry


class StubProvider(ModelProvider):
    def __init__(self, script=None):
        self._script = list(script or [])

    def generate(self, system_prompt, user_prompt, max_tokens=2048):
        text = (self._script.pop(0) if self._script
                else "<final_answer>stub ok</final_answer>")
        return ModelResponse(text=text)

    def health_check(self):
        return True


@pytest.fixture()
def disp():
    reg = build_full_registry()
    d = Dispatcher(reg, workspace="/tmp")
    _ = d.cc_handlers  # bind set_executor for all modules (incl. replicate)
    yield d
    # stop any lingering children
    for cid, rec in list(rt._CHILDREN.items()):
        if rec.status == "running":
            rt.agent_stop({"child_id": cid})


@pytest.fixture(autouse=True)
def _clean_provider_factory():
    yield
    rt.set_provider_factory(None)


def test_spawn_status_result_roundtrip(disp):
    rt.set_provider_factory(lambda: StubProvider())
    r = rt.spawn_agent({"task": "say hi", "timeout": 60})
    assert r["ok"], r
    cid = r["child_id"]
    st = rt.agent_status({"child_id": cid})
    assert st["ok"] and st["child_id"] == cid
    got = rt.agent_result({"child_id": cid, "timeout": 60})
    assert got["ok"], got
    assert got["result"]["final_text"] == "stub ok"


def test_child_can_use_tools(disp):
    rt.set_provider_factory(lambda: StubProvider([
        '<tool_call>{"name": "sysinfo", "args": {}}</tool_call>',
        "<final_answer>tools work</final_answer>",
    ]))
    r = rt.spawn_agent({"task": "run sysinfo", "timeout": 60})
    got = rt.agent_result({"child_id": r["child_id"], "timeout": 60})
    assert got["ok"], got
    assert got["result"]["tool_calls"] >= 1


def test_max_depth_enforced(disp):
    rt._local.depth = 3
    try:
        r = rt.spawn_agent({"task": "too deep"})
        assert not r["ok"] and "depth" in r["error"]
    finally:
        rt._local.depth = 0


def test_unknown_child(disp):
    assert not rt.agent_status({"child_id": "nope"})["ok"]
    assert not rt.agent_result({"child_id": "nope"})["ok"]


def test_children_get_isolated_browser_contexts(disp):
    seen = {}

    class CtxSpy(StubProvider):
        def generate(self, system_prompt, user_prompt, max_tokens=2048):
            from agent.tools import browser_tools as bt
            seen[threading.get_ident()] = bt._current_browser_context()
            return super().generate(system_prompt, user_prompt, max_tokens)

    rt.set_provider_factory(lambda: CtxSpy())
    c1 = rt.spawn_agent({"task": "t1", "timeout": 60})
    c2 = rt.spawn_agent({"task": "t2", "timeout": 60})
    assert c1["ok"] and c2["ok"]
    rt.agent_result({"child_id": c1["child_id"], "timeout": 60})
    rt.agent_result({"child_id": c2["child_id"], "timeout": 60})
    ctxs = list(seen.values())
    assert len(ctxs) == 2, seen
    assert ctxs[0] != ctxs[1], "children must not share a browser context"
    assert all(c.startswith("child-") for c in ctxs)


def test_browser_tool_routes_per_thread_context():
    from agent.tools import browser_tools as bt
    captured = {}

    def fake_run_op(op, params, timeout=90):
        captured[op] = params.get("browser_context")
        return {"tabs": []}

    orig = bt._run_op
    bt._run_op = fake_run_op
    try:
        bt._thread_context.ctx = "child-aaa"
        bt.browser_tab_list({})
        assert captured["tab_list"] == "child-aaa"
        bt._thread_context.ctx = "child-bbb"
        bt.browser_tab_list({})
        assert captured["tab_list"] == "child-bbb"
        bt._thread_context.ctx = ""
        bt.browser_tab_list({})
        assert captured["tab_list"] == ""
    finally:
        bt._run_op = orig
        bt._thread_context.ctx = ""


def test_browser_login_requires_args():
    from agent.tools import browser_tools as bt
    r = bt.browser_login({"url": "https://example.com"})
    assert not r.ok and "required" in r.error


def test_browser_login_scrubs_password():
    from agent.tools import browser_tools as bt
    from agent.tools.browser_tools import BrowserResult
    secret = "s3cr3t-pw-xyz"

    def fake_res(op, params, timeout=90):
        assert params["password"] == secret  # reaches the browser op
        return BrowserResult(ok=True, output=f"welcome {secret} user",
                             parsed={"detail": f"used {secret}"})

    orig = bt._res
    bt._res = fake_res
    try:
        r = bt.browser_login({"url": "https://example.com",
                              "username": "u", "password": secret})
        assert secret not in r.output
        assert secret not in r.error
        assert "***" in r.output
    finally:
        bt._res = orig


def test_browser_inspect_element_requires_selector():
    from agent.tools import browser_tools as bt
    r = bt.browser_inspect_element({})
    assert not r.ok and "selector" in r.error


def test_selfmod_protects_permission_gate(disp):
    r = disp.dispatch("self_modify", {
        "file": "agent/tools/permission_gate.py",
        "old": "x", "new": "y", "reason": "test"})
    assert not r.ok and "refused" in r.output.lower()


def test_scheduled_selfmod_rotates_and_verifies(disp):
    from agent.tools import selfmod_tools as sm
    before = sm._current_accent()
    r = disp.dispatch("scheduled_selfmod", {})
    assert r.ok, r.output
    after = sm._current_accent()
    assert after != before
    assert after in sm.ACCENT_PALETTE


def test_replicate_verify_tool(disp):
    # uses its own echo provider; works without the model
    from agent.tools import replicate_tools as rt2
    res = rt2.replicate_verify({})
    assert res["verdict"] == "REPLICATION WORKING", res
    assert res["browser_contexts_isolated"] is True
    assert len(res["per_child"]) == 2
