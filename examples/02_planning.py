#!/usr/bin/env python3
"""Example 02: Planning — decompose a goal into tasks.

Demonstrates the Planner, dependency graph, and plan execution.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.planning import (
    Planner, PlanBuilder, PlanRunner, ControlLoop,
    Task, TaskType, TaskStatus,
)
from agent.execution.python_runner import run_python
from agent.execution.shell_runner import run_shell
from agent.tools.permission_gate import check_tool_call


def _runner(name: str, args: dict) -> dict:
    """Route tool calls to real executors."""
    if name == "python":
        r = run_python(args.get("code", ""), timeout=30)
        return {"ok": r.ok, "output": r.stdout + r.stderr}
    if name == "shell":
        r = run_shell(args.get("command", ""), timeout=30)
        return {"ok": r.ok, "output": r.stdout + r.stderr}
    return {"ok": False, "output": f"No runner for {name}"}


def _checker(name: str, args: dict) -> dict:
    g = check_tool_call(name, args)
    return {"allowed": g.allowed, "reason": g.reason}


def main():
    planner = Planner()

    # 1. Create a plan from a goal
    goal = "Analyze Python files and count lines"
    print(f"Goal: {goal}\n")
    plan = planner.plan(goal)

    print(f"Plan ID: {plan.id}")
    print(f"Tasks: {len(plan.tasks)}\n")
    for tid, task in plan.tasks.items():
        print(f"  [{task.status.value:8s}] {task.title}")
        print(f"           type={task.task_type.value}, "
              f"tool={task.tool_name}")
        if task.depends_on:
            print(f"           depends_on={task.depends_on}")

    # 2. Build a custom plan with dependencies
    print("\n" + "=" * 50)
    print("Custom plan with dependencies:\n")

    builder = PlanBuilder()
    custom = builder.build(
        "Process data",
        extra_specs=[
            {
                "title": "Generate data",
                "description": "Create test data file",
                "task_type": TaskType.PYTHON,
                "tool_name": "python",
                "tool_args": {
                    "code": "open('/tmp/data.txt','w').write('1\\n2\\n3\\n')"
                },
            },
            {
                "title": "Count lines",
                "description": "Count lines in data file",
                "task_type": TaskType.SHELL,
                "tool_name": "shell",
                "tool_args": {"command": "wc -l /tmp/data.txt"},
            },
        ])

    print(f"Custom plan: {len(custom.tasks)} tasks")
    for tid, task in custom.tasks.items():
        deps = f" (depends on {task.depends_on})" if task.depends_on else ""
        print(f"  - {task.title}{deps}")

    # 3. Execute the custom plan
    print("\n" + "=" * 50)
    print("Executing custom plan:\n")

    loop = ControlLoop(tool_runner=_runner,
                       permission_checker=_checker)
    runner = PlanRunner(loop)
    result = runner.run(custom, max_tasks=10)

    print(f"\nResults: {result.success_count} succeeded, "
          f"{result.failure_count} failed")
    for ex in result.executions:
        status = "OK" if ex.final_ok else "FAIL"
        print(f"  [{status}] {ex.task_id}: "
              f"{ex.final_output[:100]}")


if __name__ == "__main__":
    main()
