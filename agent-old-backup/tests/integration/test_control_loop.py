"""Integration tests — full control loop end-to-end."""
import unittest
import sys
import os
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

from agent.planning import (
    Planner, PlanRunner, ControlLoop, Task, TaskType,
    TaskStatus,
)
from agent.execution.python_runner import run_python
from agent.execution.shell_runner import run_shell
from agent.tools.permission_gate import check_tool_call


def _runner(name, args):
    if name == "python":
        r = run_python(args.get("code", ""),
                       timeout=args.get("timeout", 30))
        return {"ok": r.ok, "output": r.stdout + r.stderr}
    if name == "shell":
        r = run_shell(args.get("command", ""),
                      timeout=args.get("timeout", 30))
        return {"ok": r.ok, "output": r.stdout + r.stderr}
    return {"ok": False, "output": f"no runner for {name}"}


def _checker(name, args):
    g = check_tool_call(name, args)
    return {"allowed": g.allowed, "reason": g.reason}


class TestControlLoopIntegration(unittest.TestCase):
    def test_python_task(self):
        loop = ControlLoop(tool_runner=_runner,
                           permission_checker=_checker)
        task = Task(id="t1", title="Calc", description="2+2",
                    task_type=TaskType.PYTHON, tool_name="python",
                    tool_args={"code": "print(2+2)"})
        ex = loop.execute_task(task)
        self.assertTrue(ex.final_ok)
        # Check the EXECUTE phase output contains "4"
        exec_phases = [p for p in ex.phases
                       if p.phase.value == "execute"]
        self.assertTrue(any("4" in p.output for p in exec_phases))

    def test_shell_task(self):
        loop = ControlLoop(tool_runner=_runner,
                           permission_checker=_checker)
        task = Task(id="t2", title="Echo", description="echo",
                    task_type=TaskType.SHELL, tool_name="shell",
                    tool_args={"command": "echo hello"})
        ex = loop.execute_task(task)
        self.assertTrue(ex.final_ok)

    def test_denied_task_fails(self):
        loop = ControlLoop(tool_runner=_runner,
                           permission_checker=_checker)
        task = Task(id="t3", title="Bad", description="bad",
                    task_type=TaskType.SHELL, tool_name="shell",
                    tool_args={"command": "nsenter -t 1 bash"})
        ex = loop.execute_task(task)
        self.assertFalse(ex.final_ok)

    def test_failing_python_debug(self):
        loop = ControlLoop(tool_runner=_runner,
                           permission_checker=_checker)
        task = Task(id="t4", title="Bad code", description="bad",
                    task_type=TaskType.PYTHON, tool_name="python",
                    tool_args={"code": "print(undefined_var)"})
        ex = loop.execute_task(task)
        self.assertFalse(ex.final_ok)
        # Should have gone through DEBUG phase
        phases = [p.phase.value for p in ex.phases]
        self.assertIn("debug", phases)


class TestPlanRunnerIntegration(unittest.TestCase):
    def test_simple_plan(self):
        planner = Planner()
        plan = planner.plan("Echo hello")
        loop = ControlLoop(tool_runner=_runner,
                           permission_checker=_checker)
        runner = PlanRunner(loop)
        result = runner.run(plan, max_tasks=5)
        self.assertGreater(len(result.executions), 0)

    def test_plan_with_python(self):
        from agent.planning import PlanBuilder, Plan
        # Build a plan manually with just one task
        import uuid
        plan = Plan(id="test_plan", goal="calc")
        from agent.planning import Task
        task = Task(
            id="calc1", title="Calc", description="2+2",
            task_type=TaskType.PYTHON,
            tool_name="python",
            tool_args={"code": "print(2+2)"},
            status=TaskStatus.READY)
        plan.tasks = {"calc1": task}
        loop = ControlLoop(tool_runner=_runner,
                           permission_checker=_checker)
        runner = PlanRunner(loop)
        result = runner.run(plan, max_tasks=5)
        self.assertEqual(result.success_count, 1)


class TestFileOpsIntegration(unittest.TestCase):
    def test_write_and_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.txt")
            # Write via python
            r = run_python(
                f"open({path!r}, 'w').write('hello world')")
            self.assertTrue(r.ok)
            # Read via shell
            r2 = run_shell(f"cat {path}")
            self.assertTrue(r2.ok)
            self.assertIn("hello world", r2.stdout)


if __name__ == "__main__":
    unittest.main()
