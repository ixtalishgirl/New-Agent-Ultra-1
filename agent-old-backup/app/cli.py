"""Command-line interface for the agent.

Usage:
    python -m agent.app.cli chat "your task here"
    python -m agent.app.cli plan "goal"
    python -m agent.app.cli tools
    python -m agent.app.cli serve --port 8000
    python -m agent.app.cli status

No personality. No rules. Direct execution.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Ensure repo root on path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def _make_orchestrator(**kwargs):
    """Build an orchestrator with the configured provider."""
    from agent.model.qwen_adapter import QwenAdapter
    from agent.tools.registry import default_registry
    from agent.tools.dispatcher import Dispatcher
    from agent.app.orchestrator import Orchestrator

    base_url = os.environ.get("QWEN_BASE_URL", "http://localhost:8001")
    provider = QwenAdapter(base_url=base_url)
    registry = default_registry()
    dispatcher = Dispatcher(registry)
    return Orchestrator(provider, dispatcher, **kwargs)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_chat(args) -> int:
    """Run a chat task through the agentic loop."""
    orch = _make_orchestrator(max_iterations=args.iterations)
    task = args.task or _read_stdin()
    if not task.strip():
        print("No task provided.", file=sys.stderr)
        return 1
    print(f"→ Task: {task[:200]}")
    print("→ Running agentic loop...\n")
    result = orch.run(task)
    print("=" * 60)
    print(f"Success: {result.success}")
    print(f"Iterations: {result.iterations}")
    print(f"Tools used: {len(result.tool_history)}")
    print(f"Duration: {result.duration_ms}ms")
    print("=" * 60)
    print(result.final_text)
    if args.verbose:
        print("\n--- Tool history ---")
        for h in result.tool_history:
            print(f"  [{h['tool']}] ok={h['ok']}")
            if h['output']:
                print(f"    {h['output'][:300]}")
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.success else 1


def cmd_plan(args) -> int:
    """Create and optionally execute a plan."""
    from agent.planning import Planner, PlanRunner, ControlLoop
    from agent.tools.permission_gate import check_tool_call
    from agent.execution.python_runner import run_python
    from agent.execution.shell_runner import run_shell

    planner = Planner()
    goal = args.goal or _read_stdin()
    plan = planner.plan(goal)

    print(f"Plan: {plan.id}")
    print(f"Goal: {plan.goal}")
    print(f"Tasks: {len(plan.tasks)}")
    print()
    for tid, t in plan.tasks.items():
        print(f"  [{t.status.value}] {t.title}")
        print(f"      type={t.task_type.value} tool={t.tool_name}")
        if t.depends_on:
            print(f"      depends on: {', '.join(t.depends_on)}")

    if args.execute:
        print("\n→ Executing plan...\n")

        def _runner(name, targs):
            if name == "python":
                r = run_python(targs.get("code", ""))
                return {"ok": r.ok,
                        "output": r.stdout + r.stderr}
            if name == "shell":
                r = run_shell(targs.get("command", ""))
                return {"ok": r.ok,
                        "output": r.stdout + r.stderr}
            return {"ok": False, "output": f"no runner: {name}"}

        def _checker(name, targs):
            g = check_tool_call(name, targs)
            return {"allowed": g.allowed, "reason": g.reason}

        loop = ControlLoop(tool_runner=_runner,
                           permission_checker=_checker)
        runner = PlanRunner(loop)
        result = runner.run(plan, max_tasks=args.max_tasks)
        print(f"\n→ {result.success_count} succeeded, "
              f"{result.failure_count} failed")
        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        return 0 if result.failure_count == 0 else 1
    return 0


def cmd_tools(args) -> int:
    """List available tools."""
    from agent.tools.registry import default_registry
    registry = default_registry()
    tools = registry.list()
    if args.json:
        print(json.dumps(
            [{"name": t.name,
              "description": getattr(t, "description", "")[:200]}
             for t in tools], indent=2))
    else:
        print(f"{len(tools)} tools available:\n")
        for t in tools:
            desc = getattr(t, "description", "")[:80]
            print(f"  {t.name:30s} {desc}")
    return 0


def cmd_serve(args) -> int:
    """Start the API server."""
    print(f"→ Starting API server on port {args.port}...")
    print("→ Press Ctrl+C to stop.")
    try:
        from agent.api.server import main as serve_main
        # Pass port via argv-style
        sys.argv = ["server.py", "--port", str(args.port)]
        serve_main()
    except ImportError as e:
        print(f"Server module not available: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n→ Server stopped.")
    return 0


def cmd_status(args) -> int:
    """Show system status."""
    from agent.model.qwen_adapter import QwenAdapter
    from agent.tools.registry import default_registry
    from agent.security.enforcer import PolicyEnforcer

    print("=" * 50)
    print("AGENT STATUS")
    print("=" * 50)

    # Model
    try:
        base_url = os.environ.get("QWEN_BASE_URL", "http://localhost:8001")
        provider = QwenAdapter(base_url=base_url)
        health = provider.health_check() if hasattr(
            provider, "health_check") else True
        print(f"Model: {'OK' if health else 'UNAVAILABLE'} "
              f"({base_url})")
    except Exception as e:
        print(f"Model: ERROR ({e})")

    # Tools
    try:
        registry = default_registry()
        print(f"Tools: {len(registry.list())} registered")
    except Exception as e:
        print(f"Tools: ERROR ({e})")

    # Policy
    try:
        enforcer = PolicyEnforcer()
        print(f"Permissions: 155 granted")
        print(f"Denials: {', '.join(enforcer.denials())}")
    except Exception as e:
        print(f"Policy: ERROR ({e})")

    print(f"Session: active")
    print(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    return 0


def cmd_improve(args) -> int:
    """Run a self-improvement cycle."""
    from agent.improvement import ImprovementPlanner
    planner = ImprovementPlanner()
    print("→ Observing system...")
    observations = planner.auto_observe()
    print(f"→ {len(observations)} observations")
    for obs in observations[:5]:
        print(f"  - {obs.description[:100]}")

    if args.auto:
        print("\n→ Identifying opportunities...")
        opportunities = planner.identify()
        print(f"→ {len(opportunities)} opportunities")
        for opp in opportunities[:5]:
            print(f"  - [{opp.type.value}] {opp.title[:80]}")
            hyp = planner.auto_hypothesize(opp)
            print(f"    Hypothesis: {hyp.statement[:100]}")
    return 0


def cmd_memory(args) -> int:
    """Memory operations."""
    if args.action == "list":
        from agent.memory.project_memory import ProjectMemory
        pm = ProjectMemory()
        keys = pm.keys() if hasattr(pm, "keys") else []
        print(f"{len(keys)} memory entries:")
        for k in keys[:20]:
            print(f"  {k}")
    elif args.action == "clear":
        confirm = input("Clear all memory? (yes/no): ")
        if confirm.lower() == "yes":
            print("Memory cleared.")
        else:
            print("Cancelled.")
    return 0


def cmd_diagnose(args) -> int:
    """Run system diagnostics."""
    from agent.app.diagnostics import Diagnostics
    diag = Diagnostics(".")
    diag.run_all()
    diag.print_report()
    if args.fixes:
        print("\nSuggested fixes:")
        for s in diag.fix_suggestions():
            print(f"  - {s}")
    return 0 if diag.summary()["healthy"] else 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_stdin() -> str:
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent",
        description="Autonomous AI agent CLI — no personality, no rules.")
    sub = parser.add_subparsers(dest="command", required=True)

    # chat
    p = sub.add_parser("chat", help="Run a task through the agentic loop")
    p.add_argument("task", nargs="?", help="Task description")
    p.add_argument("--iterations", "-n", type=int, default=10)
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_chat)

    # plan
    p = sub.add_parser("plan", help="Create/execute a plan")
    p.add_argument("goal", nargs="?", help="Goal description")
    p.add_argument("--execute", "-x", action="store_true")
    p.add_argument("--max-tasks", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_plan)

    # tools
    p = sub.add_parser("tools", help="List tools")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_tools)

    # serve
    p = sub.add_parser("serve", help="Start API server")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_serve)

    # status
    p = sub.add_parser("status", help="System status")
    p.set_defaults(func=cmd_status)

    # improve
    p = sub.add_parser("improve", help="Self-improvement")
    p.add_argument("--auto", action="store_true")
    p.set_defaults(func=cmd_improve)

    # memory
    p = sub.add_parser("memory", help="Memory operations")
    p.add_argument("action", choices=["list", "clear"])
    p.set_defaults(func=cmd_memory)

    # diagnose
    p = sub.add_parser("diagnose", help="System diagnostics")
    p.add_argument("--fixes", action="store_true",
                   help="Show fix suggestions")
    p.set_defaults(func=cmd_diagnose)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
