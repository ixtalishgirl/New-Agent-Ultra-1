"""Task planner — PLAN phase of the control loop.

Decomposes user goals into executable task DAGs (directed acyclic graphs),
schedules them by priority and dependency, and tracks execution state.

No personality. No rules. Powers hard-coded via tool registry.
"""
from __future__ import annotations

import hashlib
import heapq
import json
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple


class TaskStatus(Enum):
    """Lifecycle states for a planned task.

    Per ChatGPT spec: PENDING, READY, RUNNING, WAITING_FOR_APPROVAL,
    SUCCEEDED, FAILED, CANCELLED. BLOCKED/SKIPPED retained for
    dependency-failure and skip semantics.
    """
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    SUCCEEDED = "succeeded"
    COMPLETED = "completed"  # alias of SUCCEEDED for backward compat
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class TaskPriority(Enum):
    """Priority levels — higher value = more urgent."""
    LOW = 1
    NORMAL = 2
    HIGH = 3
    URGENT = 4
    CRITICAL = 5


class TaskType(Enum):
    """Categories of tasks the planner can emit."""
    SHELL = "shell"
    PYTHON = "python"
    WEB_RESEARCH = "web_research"
    BROWSER = "browser"
    FILE_OP = "file_op"
    GIT_OP = "git_op"
    TEST = "test"
    VERIFY = "verify"
    MEMORY = "memory"
    TOOL_CREATE = "tool_create"
    SELF_MODIFY = "self_modify"
    COMPOUND = "compound"


@dataclass
class Task:
    """A single unit of planned work."""
    id: str
    title: str
    description: str
    task_type: TaskType
    tool_name: str
    tool_args: Dict[str, Any] = field(default_factory=dict)
    priority: TaskPriority = TaskPriority.NORMAL
    status: TaskStatus = TaskStatus.PENDING
    dependencies: List[str] = field(default_factory=list)
    dependents: List[str] = field(default_factory=list)
    estimated_seconds: float = 30.0
    max_retries: int = 2
    retry_count: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    result: Optional[str] = None
    error: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["task_type"] = self.task_type.value
        d["priority"] = self.priority.value
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Task":
        d = dict(d)
        d["task_type"] = TaskType(d["task_type"])
        d["priority"] = TaskPriority(d["priority"])
        d["status"] = TaskStatus(d["status"])
        return cls(**d)

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.started_at and self.completed_at:
            return self.completed_at - self.started_at
        return None

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            TaskStatus.SUCCEEDED, TaskStatus.COMPLETED, TaskStatus.FAILED,
            TaskStatus.SKIPPED, TaskStatus.CANCELLED,
        )

    @property
    def is_succeeded(self) -> bool:
        return self.status in (TaskStatus.SUCCEEDED, TaskStatus.COMPLETED)


@dataclass
class Plan:
    """A complete execution plan — a DAG of tasks for one user goal."""
    id: str
    goal: str
    tasks: Dict[str, Task] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "goal": self.goal,
            "tasks": {tid: t.to_dict() for tid, t in self.tasks.items()},
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Plan":
        return cls(
            id=d["id"],
            goal=d["goal"],
            tasks={tid: Task.from_dict(td) for tid, td in d["tasks"].items()},
            created_at=d.get("created_at", time.time()),
            updated_at=d.get("updated_at", time.time()),
            metadata=d.get("metadata", {}),
        )

    @property
    def completion_ratio(self) -> float:
        if not self.tasks:
            return 1.0
        done = sum(1 for t in self.tasks.values() if t.is_terminal)
        return done / len(self.tasks)

    @property
    def failed_tasks(self) -> List[Task]:
        return [t for t in self.tasks.values() if t.status == TaskStatus.FAILED]

    @property
    def pending_tasks(self) -> List[Task]:
        return [t for t in self.tasks.values()
                if t.status in (TaskStatus.PENDING, TaskStatus.READY)]


# ---------------------------------------------------------------------------
# Goal decomposition — turn natural-language goals into task lists
# ---------------------------------------------------------------------------

# Keyword → task template mapping. Each entry: (keywords, task_type, tool,
# title_template, args_builder_hint, priority, estimated_seconds)
_DECOMPOSITION_RULES: List[Dict[str, Any]] = [
    {
        "keywords": ["search", "find", "look up", "research", "investigate"],
        "task_type": TaskType.WEB_RESEARCH,
        "tool": "web_search",
        "title": "Web research: {subject}",
        "priority": TaskPriority.NORMAL,
        "estimated": 45.0,
        "tags": ["research"],
    },
    {
        "keywords": ["open", "visit", "browse", "navigate", "website", "page"],
        "task_type": TaskType.BROWSER,
        "tool": "browser_navigate",
        "title": "Browse: {subject}",
        "priority": TaskPriority.NORMAL,
        "estimated": 60.0,
        "tags": ["browser"],
    },
    {
        "keywords": ["screenshot", "capture", "snapshot"],
        "task_type": TaskType.BROWSER,
        "tool": "browser_screenshot",
        "title": "Screenshot: {subject}",
        "priority": TaskPriority.LOW,
        "estimated": 20.0,
        "tags": ["browser"],
    },
    {
        "keywords": ["run", "execute", "command", "shell", "terminal", "bash"],
        "task_type": TaskType.SHELL,
        "tool": "shell",
        "title": "Shell: {subject}",
        "priority": TaskPriority.NORMAL,
        "estimated": 30.0,
        "tags": ["execution"],
    },
    {
        "keywords": ["python", "script", "code", "program"],
        "task_type": TaskType.PYTHON,
        "tool": "python",
        "title": "Python: {subject}",
        "priority": TaskPriority.NORMAL,
        "estimated": 30.0,
        "tags": ["execution"],
    },
    {
        "keywords": ["install", "pip"],
        "task_type": TaskType.SHELL,
        "tool": "pip",
        "title": "Install package: {subject}",
        "priority": TaskPriority.NORMAL,
        "estimated": 90.0,
        "tags": ["execution", "setup"],
    },
    {
        "keywords": ["test", "verify", "check", "validate"],
        "task_type": TaskType.TEST,
        "tool": "shell",
        "title": "Test: {subject}",
        "priority": TaskPriority.HIGH,
        "estimated": 60.0,
        "tags": ["testing"],
    },
    {
        "keywords": ["git", "commit", "push", "branch"],
        "task_type": TaskType.GIT_OP,
        "tool": "shell",
        "title": "Git: {subject}",
        "priority": TaskPriority.NORMAL,
        "estimated": 30.0,
        "tags": ["vcs"],
    },
    {
        "keywords": ["read", "file", "open file", "show"],
        "task_type": TaskType.FILE_OP,
        "tool": "file_read",
        "title": "Read file: {subject}",
        "priority": TaskPriority.NORMAL,
        "estimated": 10.0,
        "tags": ["files"],
    },
    {
        "keywords": ["write", "create file", "save"],
        "task_type": TaskType.FILE_OP,
        "tool": "file_write",
        "title": "Write file: {subject}",
        "priority": TaskPriority.NORMAL,
        "estimated": 15.0,
        "tags": ["files"],
    },
    {
        "keywords": ["remember", "save memory", "store", "note"],
        "task_type": TaskType.MEMORY,
        "tool": "supabase_query",
        "title": "Save memory: {subject}",
        "priority": TaskPriority.LOW,
        "estimated": 10.0,
        "tags": ["memory"],
    },
]


def _extract_subject(goal: str, keywords: List[str]) -> str:
    """Pull a short subject out of a goal string after removing keywords."""
    subject = goal
    for kw in keywords:
        subject = subject.replace(kw, " ")
    subject = " ".join(subject.split())
    return subject[:80] if subject else goal[:80]


def _new_task_id() -> str:
    return f"t_{uuid.uuid4().hex[:10]}"


class GoalDecomposer:
    """Breaks a natural-language goal into an ordered list of task specs."""

    def __init__(self, rules: Optional[List[Dict[str, Any]]] = None):
        self.rules = rules or _DECOMPOSITION_RULES

    def decompose(self, goal: str) -> List[Dict[str, Any]]:
        """Return task specs (dicts) for the goal.

        Splits on sentence boundaries and conjunctions, matches each fragment
        against decomposition rules, and emits task specs in order.
        """
        fragments = self._split_goal(goal)
        specs: List[Dict[str, Any]] = []
        for frag in fragments:
            spec = self._match_fragment(frag)
            if spec:
                specs.append(spec)
        if not specs:
            # Fallback: single generic research task
            specs.append({
                "title": f"Handle: {goal[:60]}",
                "description": goal,
                "task_type": TaskType.COMPOUND,
                "tool_name": "web_search",
                "tool_args": {"query": goal[:200]},
                "priority": TaskPriority.NORMAL,
                "estimated_seconds": 60.0,
                "tags": ["fallback"],
            })
        return specs

    def _split_goal(self, goal: str) -> List[str]:
        import re
        # Split on sentence ends and coordinating conjunctions used as steps
        parts = re.split(r"[.;!?]\s*|\s+then\s+|\s+and then\s+|\s+after that\s+", goal)
        frags = [p.strip() for p in parts if p.strip()]
        # Further split numbered steps: "1. do X 2. do Y"
        out: List[str] = []
        for f in frags:
            sub = re.split(r"\s*\d+[.)]\s*", f)
            out.extend(s.strip() for s in sub if s.strip())
        return out or [goal]

    def _match_fragment(self, fragment: str) -> Optional[Dict[str, Any]]:
        frag_low = fragment.lower()
        best: Optional[Dict[str, Any]] = None
        best_hits = 0
        for rule in self.rules:
            hits = sum(1 for kw in rule["keywords"] if kw in frag_low)
            if hits > best_hits:
                best_hits = hits
                best = rule
        if not best:
            return None
        subject = _extract_subject(fragment, best["keywords"])
        return {
            "title": best["title"].format(subject=subject or fragment[:40]),
            "description": fragment,
            "task_type": best["task_type"],
            "tool_name": best["tool"],
            "tool_args": {},
            "priority": best["priority"],
            "estimated_seconds": best["estimated"],
            "tags": list(best.get("tags", [])),
        }


# ---------------------------------------------------------------------------
# DAG construction and validation
# ---------------------------------------------------------------------------

class PlanBuilder:
    """Assembles task specs into a validated Plan DAG."""

    def __init__(self, decomposer: Optional[GoalDecomposer] = None):
        self.decomposer = decomposer or GoalDecomposer()

    def build(self, goal: str,
              extra_specs: Optional[List[Dict[str, Any]]] = None) -> Plan:
        """Build a Plan from a goal string plus optional extra task specs."""
        specs = self.decomposer.decompose(goal)
        if extra_specs:
            specs.extend(extra_specs)
        plan = Plan(id=f"plan_{uuid.uuid4().hex[:10]}", goal=goal)
        prev_id: Optional[str] = None
        for spec in specs:
            task = Task(
                id=_new_task_id(),
                title=spec.get("title", "Untitled task"),
                description=spec.get("description", ""),
                task_type=spec.get("task_type", TaskType.COMPOUND),
                tool_name=spec.get("tool_name", "shell"),
                tool_args=spec.get("tool_args", {}),
                priority=spec.get("priority", TaskPriority.NORMAL),
                estimated_seconds=spec.get("estimated_seconds", 30.0),
                max_retries=spec.get("max_retries", 2),
                tags=spec.get("tags", []),
                metadata=spec.get("metadata", {}),
            )
            # Chain dependencies: each task depends on the previous one by
            # default (sequential plan). Explicit deps override.
            deps = spec.get("dependencies")
            if deps:
                task.dependencies = list(deps)
            elif prev_id and spec.get("parallel") is not True:
                task.dependencies = [prev_id]
            plan.tasks[task.id] = task
            prev_id = task.id
        self._link_dependents(plan)
        self._validate(plan)
        self._mark_ready(plan)
        return plan

    def _link_dependents(self, plan: Plan) -> None:
        for task in plan.tasks.values():
            task.dependents = []
        for task in plan.tasks.values():
            for dep_id in task.dependencies:
                dep = plan.tasks.get(dep_id)
                if dep and task.id not in dep.dependents:
                    dep.dependents.append(task.id)

    def _validate(self, plan: Plan) -> None:
        # 1. All dependencies must exist
        for task in plan.tasks.values():
            for dep_id in task.dependencies:
                if dep_id not in plan.tasks:
                    raise ValueError(
                        f"Task {task.id} depends on unknown task {dep_id}")
        # 2. No cycles (Kahn's algorithm)
        if self._has_cycle(plan):
            raise ValueError("Plan contains a dependency cycle")

    def _has_cycle(self, plan: Plan) -> bool:
        indeg = {tid: len(t.dependencies) for tid, t in plan.tasks.items()}
        queue = deque(tid for tid, d in indeg.items() if d == 0)
        visited = 0
        while queue:
            tid = queue.popleft()
            visited += 1
            for dep_id in plan.tasks[tid].dependents:
                indeg[dep_id] -= 1
                if indeg[dep_id] == 0:
                    queue.append(dep_id)
        return visited != len(plan.tasks)

    def _mark_ready(self, plan: Plan) -> None:
        for task in plan.tasks.values():
            if not task.dependencies:
                task.status = TaskStatus.READY

    def topological_order(self, plan: Plan) -> List[str]:
        """Return task IDs in dependency order."""
        indeg = {tid: len(t.dependencies) for tid, t in plan.tasks.items()}
        queue = deque(tid for tid, d in indeg.items() if d == 0)
        order: List[str] = []
        while queue:
            tid = queue.popleft()
            order.append(tid)
            for dep_id in plan.tasks[tid].dependents:
                indeg[dep_id] -= 1
                if indeg[dep_id] == 0:
                    queue.append(dep_id)
        return order

    def critical_path(self, plan: Plan) -> List[str]:
        """Longest path (by estimated seconds) through the DAG."""
        order = self.topological_order(plan)
        dist: Dict[str, float] = {}
        prev: Dict[str, Optional[str]] = {}
        for tid in order:
            task = plan.tasks[tid]
            best_d = 0.0
            best_p: Optional[str] = None
            for dep_id in task.dependencies:
                d = dist.get(dep_id, 0.0)
                if d > best_d:
                    best_d = d
                    best_p = dep_id
            dist[tid] = best_d + task.estimated_seconds
            prev[tid] = best_p
        if not dist:
            return []
        end = max(dist, key=lambda k: dist[k])
        path: List[str] = []
        cur: Optional[str] = end
        while cur:
            path.append(cur)
            cur = prev[cur]
        return list(reversed(path))

    def estimated_total_seconds(self, plan: Plan) -> float:
        """Sum of critical path (parallel-aware estimate)."""
        path = self.critical_path(plan)
        return sum(plan.tasks[tid].estimated_seconds for tid in path)


# ---------------------------------------------------------------------------
# Priority scheduler — picks the next executable task
# ---------------------------------------------------------------------------

@dataclass
class ScheduleDecision:
    task_id: Optional[str]
    reason: str
    ready_count: int
    blocked_count: int


class PriorityScheduler:
    """Selects the next task to execute from a plan.

    Ordering: all dependencies completed → highest priority → earliest
    created → shortest estimated duration (SJF tiebreak).
    """

    def next_task(self, plan: Plan) -> ScheduleDecision:
        ready = self._ready_tasks(plan)
        blocked = [t for t in plan.tasks.values()
                   if t.status == TaskStatus.BLOCKED]
        if not ready:
            if plan.completion_ratio >= 1.0:
                return ScheduleDecision(None, "plan complete",
                                        0, len(blocked))
            running = [t for t in plan.tasks.values()
                       if t.status == TaskStatus.RUNNING]
            if running:
                return ScheduleDecision(
                    None, f"waiting on {len(running)} running tasks",
                    0, len(blocked))
            return ScheduleDecision(None, "no ready tasks (deadlock?)",
                                    0, len(blocked))
        # Heap: (-priority, created_at, estimated_seconds, task_id)
        heap: List[Tuple[int, float, float, str]] = []
        for t in ready:
            heapq.heappush(heap, (-t.priority.value, t.created_at,
                                  t.estimated_seconds, t.id))
        _, _, _, tid = heapq.heappop(heap)
        return ScheduleDecision(tid, "highest priority ready task",
                                len(ready), len(blocked))

    def _ready_tasks(self, plan: Plan) -> List[Task]:
        out = []
        for task in plan.tasks.values():
            if task.status not in (TaskStatus.READY, TaskStatus.PENDING):
                continue
            if self._deps_satisfied(plan, task):
                out.append(task)
        return out

    def _deps_satisfied(self, plan: Plan, task: Task) -> bool:
        for dep_id in task.dependencies:
            dep = plan.tasks.get(dep_id)
            if not dep or dep.status != TaskStatus.COMPLETED:
                return False
        return True

    def mark_running(self, plan: Plan, task_id: str) -> None:
        task = plan.tasks[task_id]
        task.status = TaskStatus.RUNNING
        task.started_at = time.time()
        plan.updated_at = time.time()

    def mark_completed(self, plan: Plan, task_id: str,
                       result: str = "") -> None:
        task = plan.tasks[task_id]
        task.status = TaskStatus.COMPLETED
        task.completed_at = time.time()
        task.result = result
        plan.updated_at = time.time()
        self._unblock_dependents(plan, task)

    def mark_failed(self, plan: Plan, task_id: str, error: str = "") -> None:
        task = plan.tasks[task_id]
        task.error = error
        if task.retry_count < task.max_retries:
            task.retry_count += 1
            task.status = TaskStatus.READY
            task.started_at = None
        else:
            task.status = TaskStatus.FAILED
            task.completed_at = time.time()
            # Dependents become blocked
            for dep_id in task.dependents:
                dep = plan.tasks.get(dep_id)
                if dep and dep.status in (TaskStatus.PENDING, TaskStatus.READY):
                    dep.status = TaskStatus.BLOCKED
        plan.updated_at = time.time()

    def mark_skipped(self, plan: Plan, task_id: str, reason: str = "") -> None:
        task = plan.tasks[task_id]
        task.status = TaskStatus.SKIPPED
        task.completed_at = time.time()
        task.result = f"skipped: {reason}"
        plan.updated_at = time.time()
        self._unblock_dependents(plan, task, skipped=True)

    def _unblock_dependents(self, plan: Plan, task: Task,
                            skipped: bool = False) -> None:
        for dep_id in task.dependents:
            dep = plan.tasks.get(dep_id)
            if not dep:
                continue
            if dep.status == TaskStatus.BLOCKED and not skipped:
                # Re-evaluate: blocked only if a dep failed
                if all(plan.tasks[d].status == TaskStatus.COMPLETED
                       for d in dep.dependencies if d in plan.tasks):
                    dep.status = TaskStatus.READY
            elif dep.status == TaskStatus.PENDING:
                if self._deps_satisfied(plan, dep):
                    dep.status = TaskStatus.READY

    def parallel_groups(self, plan: Plan) -> List[List[str]]:
        """Group tasks into waves that can run in parallel.

        Each wave contains tasks whose dependencies are all in earlier waves.
        """
        builder = PlanBuilder()
        order = builder.topological_order(plan)
        level: Dict[str, int] = {}
        for tid in order:
            task = plan.tasks[tid]
            if not task.dependencies:
                level[tid] = 0
            else:
                level[tid] = 1 + max(level.get(d, 0)
                                     for d in task.dependencies)
        waves: Dict[int, List[str]] = defaultdict(list)
        for tid, lv in level.items():
            waves[lv].append(tid)
        return [waves[k] for k in sorted(waves)]


# ---------------------------------------------------------------------------
# Plan optimizer — merge, split, and reorder for efficiency
# ---------------------------------------------------------------------------

class PlanOptimizer:
    """Rewrites plans for faster / cheaper execution."""

    def optimize(self, plan: Plan) -> Plan:
        plan = self._merge_trivial_sequences(plan)
        plan = self._hoist_independent(plan)
        plan = self._deduplicate(plan)
        return plan

    def _merge_trivial_sequences(self, plan: Plan) -> Plan:
        """Merge adjacent tiny file_read tasks on the same file pattern."""
        # Conservative: only merge READY/PENDING tasks with same tool and
        # estimated_seconds < 15 into their successor.
        builder = PlanBuilder()
        order = builder.topological_order(plan)
        merged: Set[str] = set()
        for tid in order:
            if tid in merged:
                continue
            task = plan.tasks[tid]
            if (task.estimated_seconds >= 15 or task.status not in
                    (TaskStatus.PENDING, TaskStatus.READY)):
                continue
            # Find single dependent with single dependency (chain link)
            if len(task.dependents) != 1:
                continue
            nxt = plan.tasks.get(task.dependents[0])
            if not nxt or len(nxt.dependencies) != 1:
                continue
            if nxt.tool_name != task.tool_name:
                continue
            if nxt.status not in (TaskStatus.PENDING, TaskStatus.READY):
                continue
            # Merge task into nxt
            nxt.title = f"{task.title} + {nxt.title}"
            nxt.description = task.description + "\n" + nxt.description
            nxt.estimated_seconds += task.estimated_seconds
            nxt.dependencies = list(task.dependencies)
            merged.add(tid)
        for tid in merged:
            # Rewire dependents of merged task's dependents
            task = plan.tasks[tid]
            for dep_id in task.dependents:
                dep = plan.tasks.get(dep_id)
                if dep and tid in dep.dependencies:
                    dep.dependencies.remove(tid)
                    for d in task.dependencies:
                        if d not in dep.dependencies:
                            dep.dependencies.append(d)
            del plan.tasks[tid]
        builder._link_dependents(plan)
        return plan

    def _hoist_independent(self, plan: Plan) -> Plan:
        """Remove unnecessary sequential edges between independent tasks."""
        # If A -> B but B doesn't use A's result (no shared tags/files),
        # allow parallel execution.
        for task in plan.tasks.values():
            keep: List[str] = []
            for dep_id in task.dependencies:
                dep = plan.tasks.get(dep_id)
                if not dep:
                    continue
                if self._shares_context(dep, task):
                    keep.append(dep_id)
                else:
                    # Drop edge; remove reverse link
                    if task.id in dep.dependents:
                        dep.dependents.remove(task.id)
            task.dependencies = keep
        builder = PlanBuilder()
        builder._mark_ready(plan)
        return plan

    def _shares_context(self, a: Task, b: Task) -> bool:
        # Shared tags or overlapping file paths in args => real dependency
        if set(a.tags) & set(b.tags) & {"files", "vcs", "execution"}:
            return True
        a_files = self._files_in_args(a.tool_args)
        b_files = self._files_in_args(b.tool_args)
        if a_files & b_files:
            return True
        # Test/verify tasks depend on what they check
        if b.task_type in (TaskType.TEST, TaskType.VERIFY):
            return True
        return False

    def _files_in_args(self, args: Dict[str, Any]) -> Set[str]:
        out: Set[str] = set()
        for k, v in args.items():
            if isinstance(v, str) and (
                    k in ("path", "file", "filepath") or "/" in v or "." in v):
                out.add(v)
        return out

    def _deduplicate(self, plan: Plan) -> Plan:
        """Drop exact-duplicate tasks (same tool + args)."""
        seen: Dict[str, str] = {}
        dupes: List[str] = []
        for tid, task in plan.tasks.items():
            key = hashlib.sha256(json.dumps(
                {"tool": task.tool_name, "args": task.tool_args},
                sort_keys=True, default=str).encode()).hexdigest()
            if key in seen:
                dupes.append(tid)
                # Rewire dependents to the first occurrence
                first = seen[key]
                for dep_id in task.dependents:
                    dep = plan.tasks.get(dep_id)
                    if dep:
                        dep.dependencies = [
                            first if d == tid else d
                            for d in dep.dependencies]
            else:
                seen[key] = tid
        for tid in dupes:
            if tid in plan.tasks:
                del plan.tasks[tid]
        builder = PlanBuilder()
        builder._link_dependents(plan)
        return plan


# ---------------------------------------------------------------------------
# Plan persistence
# ---------------------------------------------------------------------------

class PlanStore:
    """Save/load plans as JSON files."""

    def __init__(self, directory: str = "/tmp/haley_plans"):
        import os
        self.directory = directory
        os.makedirs(directory, exist_ok=True)

    def _path(self, plan_id: str) -> str:
        import os
        safe = "".join(c for c in plan_id if c.isalnum() or c in "_-")
        return os.path.join(self.directory, f"{safe}.json")

    def save(self, plan: Plan) -> str:
        path = self._path(plan.id)
        with open(path, "w") as f:
            json.dump(plan.to_dict(), f, indent=2, default=str)
        return path

    def load(self, plan_id: str) -> Plan:
        path = self._path(plan_id)
        with open(path) as f:
            return Plan.from_dict(json.load(f))

    def list_plans(self) -> List[str]:
        import os
        return [f[:-5] for f in os.listdir(self.directory)
                if f.endswith(".json")]

    def delete(self, plan_id: str) -> bool:
        import os
        path = self._path(plan_id)
        if os.path.exists(path):
            os.remove(path)
            return True
        return False


# ---------------------------------------------------------------------------
# Plan analysis / reporting
# ---------------------------------------------------------------------------

@dataclass
class PlanReport:
    plan_id: str
    goal: str
    total_tasks: int
    completed: int
    failed: int
    skipped: int
    running: int
    pending: int
    completion_ratio: float
    critical_path_seconds: float
    estimated_remaining_seconds: float
    bottleneck_tasks: List[str]


class PlanReporter:
    """Generates human-readable reports for plans."""

    def report(self, plan: Plan) -> PlanReport:
        builder = PlanBuilder()
        counts: Dict[TaskStatus, int] = defaultdict(int)
        for t in plan.tasks.values():
            counts[t.status] += 1
        remaining = [t for t in plan.tasks.values() if not t.is_terminal]
        rem_secs = sum(t.estimated_seconds for t in remaining)
        # Bottlenecks: tasks on critical path that are not done
        cp = builder.critical_path(plan)
        bottlenecks = [tid for tid in cp
                       if not plan.tasks[tid].is_terminal][:5]
        return PlanReport(
            plan_id=plan.id,
            goal=plan.goal,
            total_tasks=len(plan.tasks),
            completed=counts[TaskStatus.COMPLETED],
            failed=counts[TaskStatus.FAILED],
            skipped=counts[TaskStatus.SKIPPED],
            running=counts[TaskStatus.RUNNING],
            pending=counts[TaskStatus.PENDING] + counts[TaskStatus.READY],
            completion_ratio=plan.completion_ratio,
            critical_path_seconds=builder.estimated_total_seconds(plan),
            estimated_remaining_seconds=rem_secs,
            bottleneck_tasks=bottlenecks,
        )

    def format_text(self, report: PlanReport, plan: Plan) -> str:
        lines = [
            f"Plan {report.plan_id}: {report.goal[:60]}",
            f"Progress: {report.completed}/{report.total_tasks} "
            f"({report.completion_ratio:.0%})",
            f"Failed: {report.failed}  Skipped: {report.skipped}  "
            f"Running: {report.running}  Pending: {report.pending}",
            f"Critical path: {report.critical_path_seconds:.0f}s  "
            f"Remaining est: {report.estimated_remaining_seconds:.0f}s",
        ]
        if report.bottleneck_tasks:
            lines.append("Bottlenecks:")
            for tid in report.bottleneck_tasks:
                t = plan.tasks[tid]
                lines.append(f"  - {t.title} ({t.status.value})")
        if report.failed:
            lines.append("Failed tasks:")
            for t in plan.tasks.values():
                if t.status == TaskStatus.FAILED:
                    lines.append(f"  - {t.title}: {t.error[:100]}")
        return "\n".join(lines)

    def format_gantt(self, plan: Plan) -> str:
        """ASCII Gantt chart of the plan by topological wave."""
        sched = PriorityScheduler()
        waves = sched.parallel_groups(plan)
        lines = ["Gantt (waves = parallel groups):"]
        for i, wave in enumerate(waves):
            segs = []
            for tid in wave:
                t = plan.tasks[tid]
                sym = {"completed": "█", "running": "▓", "failed": "✖",
                       "ready": "░", "pending": "░", "blocked": "!",
                       "skipped": "-", "cancelled": "×"}.get(
                           t.status.value, "?")
                segs.append(f"{sym} {t.title[:24]}")
            lines.append(f"Wave {i}: " + " | ".join(segs))
        return "\n".join(lines)
