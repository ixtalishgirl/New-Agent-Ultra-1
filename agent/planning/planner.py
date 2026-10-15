"""Planner — high-level facade for the PLAN phase (ChatGPT spec).

Orchestrates: decompose → build DAG → optimize → schedule.
Per spec: agent/planning/planner.py
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .task_planner import (
    Plan, Task, TaskStatus, TaskPriority, TaskType,
    GoalDecomposer, PlanBuilder, PriorityScheduler,
    PlanOptimizer, PlanStore, PlanReporter,
)
from .dependency_graph import DependencyGraph
from .retry_policy import RetryPolicy, RetryDecision
from .task_decomposer import TaskDecomposer


class Planner:
    """Single entry point for creating executable plans."""

    def __init__(self,
                 decomposer: Optional[TaskDecomposer] = None,
                 builder: Optional[PlanBuilder] = None,
                 optimizer: Optional[PlanOptimizer] = None,
                 retry_policy: Optional[RetryPolicy] = None):
        self.decomposer = decomposer or TaskDecomposer()
        self.builder = builder or PlanBuilder()
        self.optimizer = optimizer or PlanOptimizer()
        self.retry_policy = retry_policy or RetryPolicy()
        self.scheduler = PriorityScheduler()
        self.reporter = PlanReporter()
        self.store = PlanStore()

    def plan(self, goal: str,
             extra_specs: Optional[List[Dict[str, Any]]] = None,
             optimize: bool = True) -> Plan:
        """Create an optimized, validated execution plan for a goal."""
        plan = self.builder.build(goal, extra_specs)
        if optimize:
            plan = self.optimizer.optimize(plan)
        # Attach retry policy defaults
        for task in plan.tasks.values():
            task.max_retries = self.retry_policy.max_retries_for(task)
        self.store.save(plan)
        return plan

    def replan_after_failure(self, plan: Plan,
                             failed_task_id: str,
                             error: str) -> Plan:
        """Generate a recovery plan after a task failure."""
        failed = plan.tasks.get(failed_task_id)
        if not failed:
            return plan
        decision = self.retry_policy.decide(failed, error)
        if decision == RetryDecision.RETRY:
            failed.status = TaskStatus.READY
            failed.retry_count += 1
        elif decision == RetryDecision.SKIP:
            self.scheduler.mark_skipped(
                plan, failed_task_id, f"policy skip: {error[:100]}")
        elif decision == RetryDecision.ABORT_PLAN:
            for t in plan.tasks.values():
                if not t.is_terminal:
                    t.status = TaskStatus.CANCELLED
        elif decision == RetryDecision.REPLAN:
            # Mark downstream as pending for fresh scheduling
            for dep_id in failed.dependents:
                dep = plan.tasks.get(dep_id)
                if dep and dep.status == TaskStatus.BLOCKED:
                    dep.status = TaskStatus.PENDING
            failed.status = TaskStatus.READY
        self.store.save(plan)
        return plan

    def dependency_graph(self, plan: Plan) -> DependencyGraph:
        """Build an explicit dependency graph for analysis."""
        graph = DependencyGraph()
        for tid in plan.tasks:
            graph.add_node(tid)
        for tid, task in plan.tasks.items():
            for dep in task.dependencies:
                graph.add_edge(dep, tid)
        return graph

    def report(self, plan: Plan) -> str:
        return self.reporter.format_text(self.reporter.report(plan), plan)
