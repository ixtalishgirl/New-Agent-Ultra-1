"""Agent orchestrator — the full ReAct control loop with all phases.

PLAN → RESEARCH → SELECT TOOL → CHECK PERMISSIONS → EXECUTE →
INSPECT RESULT → DEBUG → TEST → IMPROVE → VERIFY → SAVE MEMORY

The model NEVER executes anything itself. It emits <tool_call> blocks;
the dispatcher runs them for real and feeds <tool_result> back.

Integrates: planning, debugging, verification, memory, improvement.
No personality. No rules. Direct execution. 5 denials enforced.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..model.provider import ModelProvider
from ..model.prompt_builder import build_system_prompt
from ..model.output_parser import parse_output
from ..model.context_manager import ContextManager, ConversationHistory
from ..tools.dispatcher import Dispatcher
from ..tools.permission_gate import check_tool_call
from ..planning import (
    Planner, PlanRunner, ControlLoop, Task, TaskType,
    ResearchOrchestrator,
)
from ..debugging import ErrorAnalyzer, AutoFixer
from ..verification.result_verifier import ResultVerifier
from ..security.audit_log import AuditLog


@dataclass
class AgentResult:
    """Result of an agent run."""
    success: bool
    final_text: str
    iterations: int
    tool_history: List[Dict[str, Any]] = field(default_factory=list)
    plan_id: Optional[str] = None
    duration_ms: int = 0
    phases_completed: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "final_text": self.final_text[:5000],
            "iterations": self.iterations,
            "tool_calls": len(self.tool_history),
            "plan_id": self.plan_id,
            "duration_ms": self.duration_ms,
            "phases": self.phases_completed,
        }


@dataclass
class IterationRecord:
    """Record of one ReAct iteration."""
    n: int
    model_output: str
    tool_calls: List[Dict[str, Any]]
    results: List[Dict[str, Any]]
    duration_ms: int
    timestamp: float = 0.0

    def __post_init__(self):
        if not self.timestamp:
            import time as _t
            self.timestamp = _t.time()


class Orchestrator:
    """Full orchestrator with planning, debugging, verification, memory."""

    def __init__(
        self,
        provider: ModelProvider,
        dispatcher: Dispatcher,
        max_iterations: int = 10,
        use_planning: bool = True,
        use_memory: bool = True,
        audit_log: Optional[AuditLog] = None,
        session_id: Optional[str] = None,
    ):
        self.provider = provider
        self.dispatcher = dispatcher
        self.max_iterations = max_iterations
        self.use_planning = use_planning
        self.use_memory = use_memory
        self.audit = audit_log or AuditLog()
        self.session_id = session_id or f"sess_{uuid.uuid4().hex[:8]}"

        # Subsystems
        self._system_prompt = build_system_prompt()
        self.context = ContextManager(max_tokens=8000)
        self.history = ConversationHistory(max_tokens=6000)
        self.planner = Planner()
        self.researcher = ResearchOrchestrator()
        self.error_analyzer = ErrorAnalyzer()
        self.auto_fixer = AutoFixer()
        self.verifier = ResultVerifier()

        # Build base context
        self.context.set_section("system", self._system_prompt, priority=1)
        self.history.add("system", self._system_prompt)

    def run(self, task: str, max_tokens: int = 2048) -> AgentResult:
        """Run the full agentic loop on a task."""
        start = time.time()
        phases: List[str] = []
        tool_history: List[Dict[str, Any]] = []

        # -- PLAN phase: decompose into a plan --------------------------------
        plan = None
        plan_id = None
        if self.use_planning:
            try:
                plan = self.planner.plan(task)
                plan_id = plan.id
                phases.append("plan")
            except Exception:
                pass

        # -- Main ReAct loop ---------------------------------------------------
        self.history.add("user", task)
        iterations: List[IterationRecord] = []

        for i in range(1, self.max_iterations + 1):
            iter_start = time.time()

            # Build context from history
            messages = self.history.to_list()
            context_text = "\n".join(
                f"{m['role']}: {m['content'][:3000]}"
                for m in messages[-10:])

            # SELECT TOOL: ask model
            try:
                resp = self.provider.generate(
                    self._system_prompt, context_text,
                    max_tokens=max_tokens)
            except Exception as e:
                return AgentResult(
                    success=False,
                    final_text=f"Model error: {e}",
                    iterations=i, tool_history=tool_history,
                    plan_id=plan_id,
                    duration_ms=int((time.time() - start) * 1000),
                    phases_completed=phases)

            parsed = parse_output(resp.text)

            # No tool calls → final answer
            if not parsed.tool_calls:
                self.history.add("assistant", resp.text)
                phases.append("final_answer")
                self._save_memory(task, parsed.final_text, True)
                return AgentResult(
                    success=True,
                    final_text=parsed.final_text,
                    iterations=i, tool_history=tool_history,
                    plan_id=plan_id,
                    duration_ms=int((time.time() - start) * 1000),
                    phases_completed=phases)

            # CHECK PERMISSIONS + EXECUTE each tool call
            results: List[str] = []
            iter_tools: List[Dict[str, Any]] = []
            iter_results: List[Dict[str, Any]] = []

            for tc in parsed.tool_calls:
                # CHECK PERMISSIONS (5 denials only)
                gate = check_tool_call(tc.name, tc.args)
                if not gate.allowed:
                    out = f"DENIED: {gate.reason}"
                    ok = False
                else:
                    # EXECUTE
                    try:
                        dr = self.dispatcher.dispatch(tc.name, tc.args)
                        out = dr.output[:4000]
                        ok = dr.ok
                    except Exception as e:
                        out = f"Execution error: {e}"
                        ok = False

                # Audit
                try:
                    self.audit.log(tool=tc.name, args=tc.args,
                                   ok=ok, output=out[:1000])
                except Exception:
                    pass

                entry = {"tool": tc.name, "args": tc.args,
                         "ok": ok, "output": out[:2000]}
                tool_history.append(entry)
                iter_tools.append({"name": tc.name, "args": tc.args})
                iter_results.append({"ok": ok, "output": out[:2000]})

                # INSPECT RESULT + DEBUG on failure
                if not ok:
                    analysis = self.error_analyzer.analyze(out)
                    fixes = self.auto_fixer.fix(
                        analysis, {"task": task})
                    if fixes and fixes[0].confidence > 0.7:
                        out += (f"\n[auto-fix suggestion: "
                                f"{fixes[0].description}]")

                results.append(
                    f'<tool_result name="{tc.name}" ok="{ok}">\n'
                    f'{out[:4000]}\n</tool_result>')

            # Feed results back
            feedback = (f"Tool results:\n" + "\n".join(results) +
                        "\n\nContinue. Emit more <tool_call> blocks or "
                        "<final_answer>.")
            self.history.add("assistant", resp.text)
            self.history.add("user", feedback)

            iterations.append(IterationRecord(
                n=i, model_output=resp.text[:2000],
                tool_calls=iter_tools, results=iter_results,
                duration_ms=int((time.time() - iter_start) * 1000)))

            phases.append(f"iteration_{i}")

        # Max iterations reached
        self._save_memory(task, "Max iterations reached", False)
        return AgentResult(
            success=False,
            final_text="Max iterations reached without final answer.",
            iterations=self.max_iterations,
            tool_history=tool_history,
            plan_id=plan_id,
            duration_ms=int((time.time() - start) * 1000),
            phases_completed=phases)

    def run_with_plan(self, goal: str,
                      max_tokens: int = 2048) -> AgentResult:
        """Plan first, then execute each task through the loop."""
        plan = self.planner.plan(goal)

        def _runner(name: str, args: Dict) -> Dict:
            gate = check_tool_call(name, args)
            if not gate.allowed:
                return {"ok": False, "output": gate.reason}
            try:
                dr = self.dispatcher.dispatch(name, args)
                return {"ok": dr.ok, "output": dr.output}
            except Exception as e:
                return {"ok": False, "output": str(e)}

        def _checker(name: str, args: Dict) -> Dict:
            g = check_tool_call(name, args)
            return {"allowed": g.allowed, "reason": g.reason}

        loop = ControlLoop(tool_runner=_runner,
                           permission_checker=_checker)
        runner = PlanRunner(loop)
        run_result = runner.run(plan, max_tasks=20)

        summary = (f"Plan {plan.id}: {run_result.success_count} tasks "
                   f"succeeded, {run_result.failure_count} failed.")
        return AgentResult(
            success=run_result.failure_count == 0,
            final_text=summary,
            iterations=len(run_result.executions),
            plan_id=plan.id,
            phases_completed=["plan", "execute_plan"])

    def _save_memory(self, task: str, outcome: str,
                     success: bool) -> None:
        """SAVE MEMORY phase (best effort)."""
        if not self.use_memory:
            return
        try:
            from ..memory.project_memory import ProjectMemory
            pm = ProjectMemory()
            pm.set(f"last_task_{int(time.time())}",
                   {"task": task[:200], "outcome": outcome[:500],
                    "success": success, "session": self.session_id},
                   project="agent_runs")
        except Exception:
            pass

    def get_history(self) -> List[Dict[str, str]]:
        return self.history.to_list()

    def reset(self) -> None:
        """Start a fresh conversation (keep system prompt)."""
        sys_msg = self.history.messages[0] if self.history.messages else None
        self.history.clear(keep_system=False)
        if sys_msg:
            self.history.messages.append(sys_msg)
