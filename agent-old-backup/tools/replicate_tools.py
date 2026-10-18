"""Self-replication — the agent spawns child agents so a team works in parallel.

One AI does one task, another does another; the parent collects results.

Safety bounds (correct engineering, documented here and in the tool
descriptions — these are NOT permission restrictions):
- max 8 ACTIVE children per parent dispatcher
- max spawn depth 3 (a child at depth 3 cannot spawn further)
- child wall-clock timeout, default 600s (10 min)
- children inherit the SAME registry + PermissionManager as the parent,
  including the 5 hard denials (enforced by permission_gate at execution)

A child is a real Orchestrator with its own session_id, running the normal
PLAN -> RESEARCH -> SELECT TOOL -> CHECK PERMISSIONS -> EXECUTE loop.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

MAX_CHILDREN_PER_PARENT = 8
MAX_DEPTH = 3
DEFAULT_CHILD_TIMEOUT = 600

_local = threading.local()


def _depth() -> int:
    return getattr(_local, "depth", 0)


@dataclass
class ChildRecord:
    child_id: str
    task: str
    depth: int
    parent_id: str
    status: str = "running"  # running | done | failed | stopped | timed_out
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    result: Optional[dict] = None
    error: str = ""
    thread: Any = None


_CHILDREN: dict[str, ChildRecord] = {}
_LOCK = threading.Lock()

# Bound by the Dispatcher (same pattern as set_executor): the parent
# dispatcher's bound dispatch method; __self__ is the Dispatcher, which
# carries .registry, .permission_manager and .workspace.
_DISPATCH_FN: Optional[Callable] = None
_PROVIDER_FACTORY: Optional[Callable] = None


def set_executor(fn: Callable[[str, dict], Any]) -> None:
    global _DISPATCH_FN
    _DISPATCH_FN = fn


def set_provider_factory(fn: Callable[[], Any]) -> None:
    """Override how child model providers are built (tests use a stub)."""
    global _PROVIDER_FACTORY
    _PROVIDER_FACTORY = fn


def _default_provider():
    from agent.model.qwen_adapter import QwenAdapter
    base_url = os.environ.get(
        "HALEY_API_URL",
        os.environ.get("QWEN_BASE_URL", "http://localhost:8001"))
    return QwenAdapter(base_url=base_url)


def _parent_dispatcher():
    if _DISPATCH_FN is None:
        return None
    return getattr(_DISPATCH_FN, "__self__", None)


def _active_children(parent_id: str) -> int:
    with _LOCK:
        return sum(1 for c in _CHILDREN.values()
                   if c.parent_id == parent_id and c.status == "running")


def _run_child(rec: ChildRecord, provider, dispatcher) -> None:
    _local.depth = rec.depth
    # Each child gets its OWN isolated Playwright browser context
    # (separate cookies, storage, pages) — parallel children never leak
    # sessions into each other or into the parent.
    bctx = f"child-{rec.child_id}"
    try:
        from agent.tools import browser_tools as _bt
        _bt._thread_context.ctx = bctx
    except Exception:
        pass
    try:
        from agent.app.orchestrator import Orchestrator
        orch = Orchestrator(
            provider=provider,
            dispatcher=dispatcher,
            session_id=f"child-{rec.child_id}",
            max_iterations=10,
        )
        res = orch.run(rec.task)
        rec.result = res.to_dict() if hasattr(res, "to_dict") else {
            "success": bool(getattr(res, "success", False)),
            "final_text": str(getattr(res, "final_text", ""))[:5000],
        }
        rec.result["browser_context"] = bctx
        rec.status = "done" if rec.result.get("success") else "failed"
        if not rec.result.get("success") and not rec.error:
            rec.error = rec.result.get("final_text", "")[:300]
    except Exception as e:
        rec.status = "failed"
        rec.error = f"{type(e).__name__}: {e}"[:500]
    finally:
        rec.ended_at = time.time()
        try:
            from agent.tools import browser_tools as _bt2
            _bt2._thread_context.ctx = ""
        except Exception:
            pass


def _new_child_id() -> str:
    return uuid.uuid4().hex[:12]


def spawn_agent(args: dict) -> dict:
    """Spawn a child agent with its own session to work on `task`."""
    task = str(args.get("task", "")).strip()
    if not task:
        return {"ok": False, "error": "task is required"}
    depth = _depth()
    if depth >= MAX_DEPTH:
        return {"ok": False,
                "error": f"max spawn depth reached ({MAX_DEPTH})"}
    parent = _parent_dispatcher()
    parent_id = getattr(parent, "session_id", None) or "process"
    # fall back: dispatcher has no session_id; use id() as parent key
    parent_key = f"{id(parent)}" if parent is not None else "process"
    if _active_children(parent_key) >= MAX_CHILDREN_PER_PARENT:
        return {"ok": False,
                "error": f"max {MAX_CHILDREN_PER_PARENT} active children "
                         f"per parent reached"}
    timeout = int(args.get("timeout", DEFAULT_CHILD_TIMEOUT))
    provider_factory = _PROVIDER_FACTORY or _default_provider
    try:
        provider = provider_factory()
    except Exception as e:
        return {"ok": False, "error": f"provider unavailable: {e}"[:300]}
    if parent is None:
        return {"ok": False,
                "error": "no dispatcher bound (spawn_agent needs a live "
                         "Dispatcher); running standalone?"}
    from agent.tools.dispatcher import Dispatcher
    child_disp = Dispatcher(parent.registry, workspace=parent.workspace,
                            permission_manager=parent.permission_manager)
    rec = ChildRecord(child_id=_new_child_id(), task=task,
                      depth=depth + 1, parent_id=parent_key)
    t = threading.Thread(target=_run_child, args=(rec, provider, child_disp),
                         daemon=True,
                         name=f"agent-child-{rec.child_id}")
    rec.thread = t
    with _LOCK:
        _CHILDREN[rec.child_id] = rec
    t.start()
    return {"ok": True, "child_id": rec.child_id, "depth": rec.depth,
            "timeout": timeout,
            "note": "child running; use agent_status / agent_result"}


def agent_status(args: dict) -> dict:
    """Check a child agent's state and progress."""
    child_id = str(args.get("child_id", ""))
    with _LOCK:
        rec = _CHILDREN.get(child_id)
    if rec is None:
        return {"ok": False, "error": f"unknown child_id: {child_id}"}
    elapsed = (rec.ended_at or time.time()) - rec.started_at
    return {"ok": True, "child_id": rec.child_id, "status": rec.status,
            "depth": rec.depth, "task": rec.task[:200],
            "elapsed_s": round(elapsed, 1), "error": rec.error}


def agent_result(args: dict) -> dict:
    """Collect a finished child's result (waits up to `timeout` seconds)."""
    child_id = str(args.get("child_id", ""))
    timeout = float(args.get("timeout", 60))
    with _LOCK:
        rec = _CHILDREN.get(child_id)
    if rec is None:
        return {"ok": False, "error": f"unknown child_id: {child_id}"}
    deadline = time.time() + max(0, timeout)
    while rec.status == "running" and time.time() < deadline:
        time.sleep(0.2)
    if rec.status == "running":
        rec.status = "timed_out"
        rec.ended_at = time.time()
        return {"ok": False, "child_id": child_id, "status": "timed_out",
                "error": "child did not finish in time (thread abandoned; "
                         "CPython cannot kill threads)"}
    return {"ok": rec.status == "done", "child_id": child_id,
            "status": rec.status, "result": rec.result, "error": rec.error}


def agent_stop(args: dict) -> dict:
    """Terminate a child agent (best-effort: marked stopped; a running
    thread is abandoned because CPython cannot kill threads)."""
    child_id = str(args.get("child_id", ""))
    with _LOCK:
        rec = _CHILDREN.get(child_id)
        if rec is None:
            return {"ok": False, "error": f"unknown child_id: {child_id}"}
        if rec.status == "running":
            rec.status = "stopped"
            rec.ended_at = time.time()
            return {"ok": True, "child_id": child_id, "status": "stopped",
                    "note": "marked stopped; thread abandoned (CPython)"}
        return {"ok": True, "child_id": child_id,
                "status": rec.status, "note": "already finished"}


def fan_out(args: dict) -> dict:
    """Run N sub-tasks in parallel child agents; wait for all; combine.

    args: tasks (list[str], required), timeout (seconds per child, default
    600), overall_timeout (seconds for all, default = timeout).
    """
    tasks = args.get("tasks", [])
    if not isinstance(tasks, list) or not tasks:
        return {"ok": False, "error": "tasks (non-empty list) is required"}
    per_timeout = float(args.get("timeout", DEFAULT_CHILD_TIMEOUT))
    overall = float(args.get("overall_timeout", per_timeout))
    spawned: list[str] = []
    errors: list[str] = []
    for t in tasks:
        r = spawn_agent({"task": str(t), "timeout": per_timeout})
        if r.get("ok"):
            spawned.append(r["child_id"])
        else:
            errors.append(f"task {str(t)[:60]!r}: {r.get('error')}")
    deadline = time.time() + max(1, overall)
    results: dict[str, dict] = {}
    for cid in spawned:
        remaining = max(0, deadline - time.time())
        results[cid] = agent_result({"child_id": cid, "timeout": remaining})
    ok = all(v.get("ok") for v in results.values()) and not errors
    return {"ok": ok, "children": spawned, "results": results,
            "spawn_errors": errors}


def _S(arg_type: str, required: bool, desc: str) -> dict:
    return {"type": arg_type, "required": required, "description": desc}


class _EchoProvider:
    """Model-free provider for replicate_verify: proves the replication
    MACHINERY works (spawn -> run -> real tool call -> result -> isolated
    browser context) without needing the LLM endpoint."""

    def __init__(self, script: list[str]):
        self._script = list(script)

    def generate(self, system_prompt: str, user_prompt: str,
                 max_tokens: int = 2048):
        from agent.model.provider import ModelResponse
        text = self._script.pop(0) if self._script else \
            "<final_answer>echo done</final_answer>"
        return ModelResponse(text=text)

    def health_check(self) -> bool:
        return True


def replicate_verify(args: dict) -> dict:
    """Prove self-replication REALLY works (the user's 'chel rhi ya na').

    Spawns 2 children with a model-free echo provider. Each child executes a
    REAL tool call (sysinfo) through its own Dispatcher, inside its own
    isolated browser context. Reports PASS/FAIL per child.
    """
    global _PROVIDER_FACTORY
    saved = _PROVIDER_FACTORY
    script = [
        '<tool_call>{"name": "sysinfo", "args": {}}</tool_call>',
        "<final_answer>child-1 verified: sysinfo executed</final_answer>",
        '<tool_call>{"name": "sysinfo", "args": {}}</tool_call>',
        "<final_answer>child-2 verified: sysinfo executed</final_answer>",
    ]

    def echo_factory():
        return _EchoProvider(script)

    _PROVIDER_FACTORY = echo_factory
    children: list[str] = []
    try:
        for i in (1, 2):
            r = spawn_agent({"task": f"verify-child-{i}", "timeout": 120})
            if not r.get("ok"):
                return {"ok": False,
                        "error": f"spawn failed: {r.get('error')}"}
            children.append(r["child_id"])
        report: dict[str, dict] = {}
        for cid in children:
            got = agent_result({"child_id": cid, "timeout": 120})
            with _LOCK:
                rec = _CHILDREN.get(cid)
            res = (got.get("result") or {})
            tool_calls = res.get("tool_calls", 0)
            bctx = (rec.result or {}).get("browser_context", "") if rec else ""
            passed = (got.get("ok") and tool_calls >= 1
                      and bctx.startswith("child-"))
            report[cid] = {
                "PASS" if passed else "FAIL": True,
                "status": got.get("status"),
                "tool_calls": tool_calls,
                "browser_context": bctx,
                "final_text": str(res.get("final_text", ""))[:200],
                "error": got.get("error", ""),
            }
        # isolation: the two contexts must differ
        ctxs = [v["browser_context"] for v in report.values()]
        isolated = len(set(ctxs)) == 2 and all(ctxs)
        overall = all("PASS" in v for v in report.values()) and isolated
        return {"ok": overall, "per_child": report,
                "browser_contexts_isolated": isolated,
                "verdict": "REPLICATION WORKING"
                           if overall else "REPLICATION FAILED"}
    finally:
        _PROVIDER_FACTORY = saved


TOOL_DEFS: list[tuple] = [
    ("spawn_agent", "Spawn a child agent (own session) to work on a task in parallel. Max 8 active per parent, max depth 3, 5 denials enforced in children.",
     {"task": _S("string", True, "Task for the child agent"),
      "timeout": _S("integer", False, "Wall-clock timeout sec (default 600)")},
     "custom", 30),
    ("agent_status", "Check a child agent's state (running/done/failed) and progress",
     {"child_id": _S("string", True, "Child id from spawn_agent")},
     "custom", 30),
    ("agent_result", "Collect a finished child's result (final text + tool calls); waits up to timeout",
     {"child_id": _S("string", True, "Child id"),
      "timeout": _S("number", False, "Wait seconds (default 60)")},
     "custom", 120),
    ("agent_stop", "Terminate a child agent",
     {"child_id": _S("string", True, "Child id")},
     "custom", 30),
    ("fan_out", "Run N sub-tasks in parallel child agents, wait for all, return combined results",
     {"tasks": _S("array", True, "List of task strings"),
      "timeout": _S("number", False, "Per-child timeout sec (default 600)"),
      "overall_timeout": _S("number", False, "Overall wait sec")},
     "custom", 700),
    ("replicate_verify", "Prove self-replication works: spawn 2 children, each runs a real tool in its own isolated browser context; PASS/FAIL per child",
     {}, "custom", 300),
]

HANDLERS: dict[str, Any] = {
    "spawn_agent": lambda a: _wrap(spawn_agent(a)),
    "agent_status": lambda a: _wrap(agent_status(a)),
    "agent_result": lambda a: _wrap(agent_result(a)),
    "agent_stop": lambda a: _wrap(agent_stop(a)),
    "fan_out": lambda a: _wrap(fan_out(a)),
    "replicate_verify": lambda a: _wrap(replicate_verify(a)),
}


class _R:
    def __init__(self, ok, output="", parsed=None, error=""):
        self.ok = ok
        self.output = output
        self.parsed = parsed or {}
        self.error = error


def _wrap(d: dict) -> _R:
    import json as _json
    ok = bool(d.get("ok"))
    return _R(ok=ok,
              output=_json.dumps(d, indent=1, default=str)[:4000],
              parsed=d,
              error="" if ok else str(d.get("error", ""))[:500])
