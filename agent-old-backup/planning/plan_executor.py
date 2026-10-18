"""Plan executor — runs plans through the full control loop.

Control loop per task:
PLAN → RESEARCH → SELECT TOOL → CHECK PERMISSIONS → EXECUTE →
INSPECT RESULT → DEBUG → TEST → IMPROVE → VERIFY → SAVE MEMORY

No personality. No rules. Powers hard-coded.
"""
from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from .task_planner import (
    Plan, Task, TaskStatus, PriorityScheduler, PlanReporter,
)


class LoopPhase(Enum):
    PLAN = "plan"
    RESEARCH = "research"
    SELECT_TOOL = "select_tool"
    CHECK_PERMISSIONS = "check_permissions"
    EXECUTE = "execute"
    INSPECT_RESULT = "inspect_result"
    DEBUG = "debug"
    TEST = "test"
    IMPROVE = "improve"
    VERIFY = "verify"
    SAVE_MEMORY = "save_memory"


@dataclass
class PhaseResult:
    phase: LoopPhase
    ok: bool
    output: str = ""
    duration_seconds: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskExecution:
    task_id: str
    phases: List[PhaseResult] = field(default_factory=list)
    final_ok: bool = False
    final_output: str = ""
    started_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.completed_at:
            return self.completed_at - self.started_at
        return None


# Type aliases for injected capabilities
ToolRunner = Callable[[str, Dict[str, Any]], Dict[str, Any]]
# Returns {"ok": bool, "output": str}
PermissionChecker = Callable[[str, Dict[str, Any]], Dict[str, Any]]
# Returns {"allowed": bool, "reason": str}
Researcher = Callable[[str], Dict[str, Any]]
# Returns {"summary": str, "sources": list}
Debugger = Callable[[str, str], Dict[str, Any]]
# (task_desc, error) -> {"fixed": bool, "suggestion": str}
Verifier = Callable[[str, str], Dict[str, Any]]
# (task_desc, output) -> {"passed": bool, "confidence": float}
MemorySaver = Callable[[str, Dict[str, Any]], None]


class ControlLoop:
    """Executes a single task through all control-loop phases."""

    PHASES = list(LoopPhase)

    def __init__(
        self,
        tool_runner: ToolRunner,
        permission_checker: Optional[PermissionChecker] = None,
        researcher: Optional[Researcher] = None,
        debugger: Optional[Debugger] = None,
        verifier: Optional[Verifier] = None,
        memory_saver: Optional[MemorySaver] = None,
        max_debug_attempts: int = 2,
    ):
        self.tool_runner = tool_runner
        self.permission_checker = permission_checker or (
            lambda name, args: {"allowed": True, "reason": "no gate"})
        self.researcher = researcher
        self.debugger = debugger
        self.verifier = verifier
        self.memory_saver = memory_saver
        self.max_debug_attempts = max_debug_attempts

    def execute_task(self, task: Task) -> TaskExecution:
        """Run one task through the full loop."""
        execution = TaskExecution(task_id=task.id)
        context: Dict[str, Any] = {
            "task": task,
            "research": None,
            "tool_output": None,
            "debug_attempts": 0,
        }
        try:
            for phase in self.PHASES:
                result = self._run_phase(phase, context)
                execution.phases.append(result)
                if not result.ok and phase in (
                        LoopPhase.SELECT_TOOL, LoopPhase.CHECK_PERMISSIONS):
                    # Fatal: cannot proceed
                    break
                if not result.ok and phase == LoopPhase.EXECUTE:
                    # Try debug loop
                    if not self._debug_loop(context, execution):
                        break
            execution.final_ok = all(
                p.ok for p in execution.phases
                if p.phase in (LoopPhase.EXECUTE, LoopPhase.VERIFY))
            # If we never reached EXECUTE (e.g. denied at permission check),
            # that's a failure
            if not any(p.phase == LoopPhase.EXECUTE
                       for p in execution.phases):
                execution.final_ok = False
            execution.final_output = self._summarize(execution)
        except Exception as e:
            execution.phases.append(PhaseResult(
                phase=LoopPhase.EXECUTE, ok=False,
                output=f"Loop crashed: {e}\n{traceback.format_exc()}"))
            execution.final_ok = False
        execution.completed_at = time.time()
        return execution

    def _run_phase(self, phase: LoopPhase,
                   ctx: Dict[str, Any]) -> PhaseResult:
        start = time.time()
        task: Task = ctx["task"]
        try:
            if phase == LoopPhase.PLAN:
                return self._phase_plan(task, start)
            elif phase == LoopPhase.RESEARCH:
                return self._phase_research(task, ctx, start)
            elif phase == LoopPhase.SELECT_TOOL:
                return self._phase_select_tool(task, ctx, start)
            elif phase == LoopPhase.CHECK_PERMISSIONS:
                return self._phase_check_permissions(task, ctx, start)
            elif phase == LoopPhase.EXECUTE:
                return self._phase_execute(task, ctx, start)
            elif phase == LoopPhase.INSPECT_RESULT:
                return self._phase_inspect(task, ctx, start)
            elif phase == LoopPhase.DEBUG:
                return self._phase_debug(task, ctx, start)
            elif phase == LoopPhase.TEST:
                return self._phase_test(task, ctx, start)
            elif phase == LoopPhase.IMPROVE:
                return self._phase_improve(task, ctx, start)
            elif phase == LoopPhase.VERIFY:
                return self._phase_verify(task, ctx, start)
            elif phase == LoopPhase.SAVE_MEMORY:
                return self._phase_save_memory(task, ctx, start)
        except Exception as e:
            return PhaseResult(phase=phase, ok=False,
                               output=f"{phase.value} failed: {e}",
                               duration_seconds=time.time() - start)
        return PhaseResult(phase=phase, ok=True,
                           duration_seconds=time.time() - start)

    # -- Individual phases --------------------------------------------

    def _phase_plan(self, task: Task, start: float) -> PhaseResult:
        return PhaseResult(
            phase=LoopPhase.PLAN, ok=True,
            output=f"Task planned: {task.title} "
                   f"(type={task.task_type.value}, "
                   f"priority={task.priority.name})",
            duration_seconds=time.time() - start,
            metadata={"task_id": task.id})

    def _phase_research(self, task: Task, ctx: Dict[str, Any],
                        start: float) -> PhaseResult:
        if not self.researcher or task.task_type.value != "web_research":
            return PhaseResult(
                phase=LoopPhase.RESEARCH, ok=True,
                output="Research skipped (not a research task)",
                duration_seconds=time.time() - start)
        try:
            res = self.researcher(task.description)
            ctx["research"] = res
            return PhaseResult(
                phase=LoopPhase.RESEARCH, ok=True,
                output=f"Research: {str(res.get('summary', ''))[:300]}",
                duration_seconds=time.time() - start,
                metadata={"sources": len(res.get("sources", []))})
        except Exception as e:
            return PhaseResult(
                phase=LoopPhase.RESEARCH, ok=False,
                output=f"Research failed: {e}",
                duration_seconds=time.time() - start)

    def _phase_select_tool(self, task: Task, ctx: Dict[str, Any],
                           start: float) -> PhaseResult:
        if not task.tool_name:
            return PhaseResult(
                phase=LoopPhase.SELECT_TOOL, ok=False,
                output="No tool selected for task",
                duration_seconds=time.time() - start)
        ctx["selected_tool"] = task.tool_name
        return PhaseResult(
            phase=LoopPhase.SELECT_TOOL, ok=True,
            output=f"Selected tool: {task.tool_name}",
            duration_seconds=time.time() - start)

    def _phase_check_permissions(self, task: Task, ctx: Dict[str, Any],
                                 start: float) -> PhaseResult:
        try:
            gate = self.permission_checker(task.tool_name, task.tool_args)
        except Exception as e:
            return PhaseResult(
                phase=LoopPhase.CHECK_PERMISSIONS, ok=False,
                output=f"Permission check crashed: {e}",
                duration_seconds=time.time() - start)
        if not gate.get("allowed", False):
            return PhaseResult(
                phase=LoopPhase.CHECK_PERMISSIONS, ok=False,
                output=f"Denied: {gate.get('reason', 'no reason')}",
                duration_seconds=time.time() - start)
        return PhaseResult(
            phase=LoopPhase.CHECK_PERMISSIONS, ok=True,
            output=f"Allowed: {gate.get('reason', 'ok')}",
            duration_seconds=time.time() - start)

    def _phase_execute(self, task: Task, ctx: Dict[str, Any],
                       start: float) -> PhaseResult:
        try:
            res = self.tool_runner(task.tool_name, task.tool_args)
        except Exception as e:
            return PhaseResult(
                phase=LoopPhase.EXECUTE, ok=False,
                output=f"Execution crashed: {e}\n{traceback.format_exc()}",
                duration_seconds=time.time() - start)
        ok = bool(res.get("ok", False))
        output = str(res.get("output", ""))[:4000]
        ctx["tool_output"] = output
        ctx["tool_ok"] = ok
        return PhaseResult(
            phase=LoopPhase.EXECUTE, ok=ok,
            output=output,
            duration_seconds=time.time() - start)

    def _phase_inspect(self, task: Task, ctx: Dict[str, Any],
                       start: float) -> PhaseResult:
        output = ctx.get("tool_output", "")
        if not output.strip():
            return PhaseResult(
                phase=LoopPhase.INSPECT_RESULT, ok=False,
                output="Empty tool output — nothing to inspect",
                duration_seconds=time.time() - start)
        # Heuristic error detection
        low = output.lower()
        err_markers = ["traceback", "error:", "failed", "exception",
                       "not found", "denied", "timeout"]
        found = [m for m in err_markers if m in low]
        ctx["inspect_errors"] = found
        return PhaseResult(
            phase=LoopPhase.INSPECT_RESULT, ok=True,
            output=f"Inspected {len(output)} chars; "
                   f"error markers: {found or 'none'}",
            duration_seconds=time.time() - start,
            metadata={"error_markers": found})

    def _phase_debug(self, task: Task, ctx: Dict[str, Any],
                     start: float) -> PhaseResult:
        if ctx.get("tool_ok", True):
            return PhaseResult(
                phase=LoopPhase.DEBUG, ok=True,
                output="No errors — debug skipped",
                duration_seconds=time.time() - start)
        if not self.debugger:
            return PhaseResult(
                phase=LoopPhase.DEBUG, ok=True,
                output="No debugger available — skipping",
                duration_seconds=time.time() - start)
        try:
            res = self.debugger(task.description,
                                ctx.get("tool_output", ""))
            ctx["debug_suggestion"] = res.get("suggestion", "")
            return PhaseResult(
                phase=LoopPhase.DEBUG, ok=True,
                output=f"Debug suggestion: "
                       f"{str(res.get('suggestion', ''))[:300]}",
                duration_seconds=time.time() - start)
        except Exception as e:
            return PhaseResult(
                phase=LoopPhase.DEBUG, ok=False,
                output=f"Debug failed: {e}",
                duration_seconds=time.time() - start)

    def _phase_test(self, task: Task, ctx: Dict[str, Any],
                    start: float) -> PhaseResult:
        # TEST phase: for test-type tasks, the execution IS the test.
        # For others, do a lightweight sanity check on output.
        if task.task_type.value == "test":
            ok = ctx.get("tool_ok", False)
            return PhaseResult(
                phase=LoopPhase.TEST, ok=ok,
                output=f"Test task {'passed' if ok else 'failed'}",
                duration_seconds=time.time() - start)
        output = ctx.get("tool_output", "")
        sane = len(output.strip()) > 0
        return PhaseResult(
            phase=LoopPhase.TEST, ok=sane,
            output="Output sanity check passed" if sane
            else "Output sanity check failed (empty)",
            duration_seconds=time.time() - start)

    def _phase_improve(self, task: Task, ctx: Dict[str, Any],
                       start: float) -> PhaseResult:
        # IMPROVE: record lessons for future runs
        suggestion = ctx.get("debug_suggestion", "")
        improved = bool(suggestion) or ctx.get("tool_ok", False)
        return PhaseResult(
            phase=LoopPhase.IMPROVE, ok=True,
            output=f"Improvement noted: "
                   f"{suggestion[:200] if suggestion else 'none needed'}",
            duration_seconds=time.time() - start)

    def _phase_verify(self, task: Task, ctx: Dict[str, Any],
                      start: float) -> PhaseResult:
        if not self.verifier:
            # Default: trust tool_ok
            ok = ctx.get("tool_ok", False)
            return PhaseResult(
                phase=LoopPhase.VERIFY, ok=ok,
                output=f"Default verify: {'passed' if ok else 'failed'}",
                duration_seconds=time.time() - start)
        try:
            res = self.verifier(task.description,
                                ctx.get("tool_output", ""))
            ok = bool(res.get("passed", False))
            return PhaseResult(
                phase=LoopPhase.VERIFY, ok=ok,
                output=f"Verified (confidence "
                       f"{res.get('confidence', 0):.0%})",
                duration_seconds=time.time() - start,
                metadata={"confidence": res.get("confidence", 0)})
        except Exception as e:
            return PhaseResult(
                phase=LoopPhase.VERIFY, ok=False,
                output=f"Verify failed: {e}",
                duration_seconds=time.time() - start)

    def _phase_save_memory(self, task: Task, ctx: Dict[str, Any],
                           start: float) -> PhaseResult:
        if not self.memory_saver:
            return PhaseResult(
                phase=LoopPhase.SAVE_MEMORY, ok=True,
                output="No memory saver — skipped",
                duration_seconds=time.time() - start)
        try:
            self.memory_saver(task.title, {
                "task_id": task.id,
                "ok": ctx.get("tool_ok", False),
                "output": ctx.get("tool_output", "")[:1000],
            })
            return PhaseResult(
                phase=LoopPhase.SAVE_MEMORY, ok=True,
                output="Memory saved",
                duration_seconds=time.time() - start)
        except Exception as e:
            return PhaseResult(
                phase=LoopPhase.SAVE_MEMORY, ok=False,
                output=f"Memory save failed: {e}",
                duration_seconds=time.time() - start)

    # -- Debug retry loop ----------------------------------------------

    def _debug_loop(self, ctx: Dict[str, Any],
                    execution: TaskExecution) -> bool:
        """Try to fix a failed execution. Returns True if fixed."""
        task: Task = ctx["task"]
        while ctx["debug_attempts"] < self.max_debug_attempts:
            ctx["debug_attempts"] += 1
            dbg = self._phase_debug(task, ctx, time.time())
            execution.phases.append(dbg)
            suggestion = ctx.get("debug_suggestion", "")
            if not suggestion:
                return False
            # Retry execution with the suggestion noted
            retry = self._phase_execute(task, ctx, time.time())
            execution.phases.append(retry)
            if retry.ok:
                return True
        return False

    def _summarize(self, execution: TaskExecution) -> str:
        ok_phases = sum(1 for p in execution.phases if p.ok)
        return (f"Task {execution.task_id}: "
                f"{ok_phases}/{len(execution.phases)} phases ok, "
                f"final={'OK' if execution.final_ok else 'FAILED'}")


# ---------------------------------------------------------------------------
# Plan runner — executes a whole plan task by task
# ---------------------------------------------------------------------------

@dataclass
class PlanRunResult:
    plan_id: str
    executions: List[TaskExecution] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None

    @property
    def success_count(self) -> int:
        return sum(1 for e in self.executions if e.final_ok)

    @property
    def failure_count(self) -> int:
        return sum(1 for e in self.executions if not e.final_ok)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "success": self.success_count,
            "failed": self.failure_count,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


class PlanRunner:
    """Runs a Plan to completion using ControlLoop + PriorityScheduler."""

    def __init__(self, loop: ControlLoop,
                 scheduler: Optional[PriorityScheduler] = None,
                 reporter: Optional[PlanReporter] = None):
        self.loop = loop
        self.scheduler = scheduler or PriorityScheduler()
        self.reporter = reporter or PlanReporter()

    def run(self, plan: Plan,
            max_tasks: Optional[int] = None) -> PlanRunResult:
        """Execute tasks until plan completes or max_tasks reached."""
        result = PlanRunResult(plan_id=plan.id)
        count = 0
        while True:
            if max_tasks and count >= max_tasks:
                break
            decision = self.scheduler.next_task(plan)
            if not decision.task_id:
                break
            task = plan.tasks[decision.task_id]
            self.scheduler.mark_running(plan, task.id)
            try:
                execution = self.loop.execute_task(task)
            except Exception as e:
                execution = TaskExecution(task_id=task.id)
                execution.final_ok = False
                execution.final_output = f"Runner crash: {e}"
                execution.completed_at = time.time()
            result.executions.append(execution)
            if execution.final_ok:
                self.scheduler.mark_completed(
                    plan, task.id, execution.final_output)
            else:
                self.scheduler.mark_failed(
                    plan, task.id, execution.final_output)
            count += 1
        result.completed_at = time.time()
        return result

    def run_report(self, plan: Plan,
                   run: PlanRunResult) -> str:
        """Human-readable report of a plan run."""
        rep = self.reporter.report(plan)
        lines = [self.reporter.format_text(rep, plan), "",
                 f"Executed {len(run.executions)} tasks: "
                 f"{run.success_count} ok, {run.failure_count} failed"]
        for e in run.executions:
            status = "OK " if e.final_ok else "FAIL"
            dur = e.duration_seconds or 0
            lines.append(f"  [{status}] {e.task_id} ({dur:.1f}s)")
        return "\n".join(lines)
