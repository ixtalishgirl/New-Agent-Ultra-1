"""Goal manager — tracks user goals, milestones, and progress.

Part of the PLAN phase. Goals persist across sessions via JSON storage.
No personality. No rules.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


class GoalStatus(Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class MilestoneStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    BLOCKED = "blocked"


@dataclass
class Milestone:
    """A checkpoint within a goal."""
    id: str
    title: str
    description: str = ""
    status: MilestoneStatus = MilestoneStatus.PENDING
    plan_id: Optional[str] = None  # linked execution plan
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Milestone":
        d = dict(d)
        d["status"] = MilestoneStatus(d["status"])
        return cls(**d)


@dataclass
class Goal:
    """A durable user goal."""
    id: str
    title: str
    description: str = ""
    status: GoalStatus = GoalStatus.ACTIVE
    milestones: List[Milestone] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["milestones"] = [m.to_dict() for m in self.milestones]
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Goal":
        d = dict(d)
        d["status"] = GoalStatus(d["status"])
        d["milestones"] = [Milestone.from_dict(m)
                           for m in d.get("milestones", [])]
        return cls(**d)

    @property
    def progress(self) -> float:
        if not self.milestones:
            return 0.0
        done = sum(1 for m in self.milestones
                   if m.status == MilestoneStatus.DONE)
        return done / len(self.milestones)

    @property
    def current_milestone(self) -> Optional[Milestone]:
        for m in self.milestones:
            if m.status in (MilestoneStatus.PENDING,
                            MilestoneStatus.IN_PROGRESS,
                            MilestoneStatus.BLOCKED):
                return m
        return None


@dataclass
class GoalEvent:
    """An event in a goal's timeline."""
    id: str
    goal_id: str
    event_type: str  # created, milestone_added, milestone_done, note, etc.
    message: str
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "GoalEvent":
        return cls(**d)


class GoalManager:
    """Manages the full lifecycle of user goals."""

    def __init__(self, store: Optional["GoalStore"] = None):
        self.store = store or GoalStore()
        self._goals: Dict[str, Goal] = {}
        self._events: Dict[str, List[GoalEvent]] = {}
        self._load_all()

    # -- CRUD --------------------------------------------------------

    def create_goal(self, title: str, description: str = "",
                    tags: Optional[List[str]] = None,
                    milestones: Optional[List[str]] = None) -> Goal:
        goal = Goal(
            id=f"goal_{uuid.uuid4().hex[:10]}",
            title=title,
            description=description,
            tags=tags or [],
        )
        for ms_title in milestones or []:
            goal.milestones.append(Milestone(
                id=f"ms_{uuid.uuid4().hex[:8]}",
                title=ms_title,
            ))
        self._goals[goal.id] = goal
        self._events[goal.id] = []
        self._log(goal.id, "created", f"Goal created: {title}")
        self.store.save_goal(goal)
        return goal

    def get_goal(self, goal_id: str) -> Optional[Goal]:
        return self._goals.get(goal_id)

    def list_goals(self, status: Optional[GoalStatus] = None,
                   tag: Optional[str] = None) -> List[Goal]:
        out = list(self._goals.values())
        if status:
            out = [g for g in out if g.status == status]
        if tag:
            out = [g for g in out if tag in g.tags]
        out.sort(key=lambda g: -g.updated_at)
        return out

    def update_goal(self, goal_id: str, **kwargs) -> Optional[Goal]:
        goal = self._goals.get(goal_id)
        if not goal:
            return None
        for k, v in kwargs.items():
            if hasattr(goal, k):
                setattr(goal, k, v)
        goal.updated_at = time.time()
        self.store.save_goal(goal)
        return goal

    def complete_goal(self, goal_id: str) -> bool:
        goal = self._goals.get(goal_id)
        if not goal:
            return False
        goal.status = GoalStatus.COMPLETED
        goal.completed_at = time.time()
        goal.updated_at = time.time()
        for m in goal.milestones:
            if m.status != MilestoneStatus.DONE:
                m.status = MilestoneStatus.DONE
                m.completed_at = time.time()
        self._log(goal_id, "completed", "Goal marked complete")
        self.store.save_goal(goal)
        return True

    def abandon_goal(self, goal_id: str, reason: str = "") -> bool:
        goal = self._goals.get(goal_id)
        if not goal:
            return False
        goal.status = GoalStatus.ABANDONED
        goal.updated_at = time.time()
        self._log(goal_id, "abandoned", f"Abandoned: {reason}")
        self.store.save_goal(goal)
        return True

    # -- Milestones ----------------------------------------------------

    def add_milestone(self, goal_id: str, title: str,
                      description: str = "") -> Optional[Milestone]:
        goal = self._goals.get(goal_id)
        if not goal:
            return None
        ms = Milestone(id=f"ms_{uuid.uuid4().hex[:8]}",
                       title=title, description=description)
        goal.milestones.append(ms)
        goal.updated_at = time.time()
        self._log(goal_id, "milestone_added", f"Milestone: {title}")
        self.store.save_goal(goal)
        return ms

    def start_milestone(self, goal_id: str, milestone_id: str) -> bool:
        ms = self._find_milestone(goal_id, milestone_id)
        if not ms:
            return False
        ms.status = MilestoneStatus.IN_PROGRESS
        self._touch(goal_id)
        self._log(goal_id, "milestone_started", f"Started: {ms.title}")
        return True

    def complete_milestone(self, goal_id: str, milestone_id: str,
                           note: str = "") -> bool:
        ms = self._find_milestone(goal_id, milestone_id)
        if not ms:
            return False
        ms.status = MilestoneStatus.DONE
        ms.completed_at = time.time()
        self._touch(goal_id)
        self._log(goal_id, "milestone_done",
                  f"Done: {ms.title}. {note}".strip())
        # Auto-complete goal if all milestones done
        goal = self._goals[goal_id]
        if all(m.status == MilestoneStatus.DONE
               for m in goal.milestones) and goal.milestones:
            self.complete_goal(goal_id)
        else:
            self.store.save_goal(goal)
        return True

    def block_milestone(self, goal_id: str, milestone_id: str,
                        reason: str) -> bool:
        ms = self._find_milestone(goal_id, milestone_id)
        if not ms:
            return False
        ms.status = MilestoneStatus.BLOCKED
        ms.metadata["block_reason"] = reason
        self._touch(goal_id)
        self._log(goal_id, "milestone_blocked",
                  f"Blocked: {ms.title} — {reason}")
        return True

    def link_plan(self, goal_id: str, milestone_id: str,
                  plan_id: str) -> bool:
        ms = self._find_milestone(goal_id, milestone_id)
        if not ms:
            return False
        ms.plan_id = plan_id
        self._touch(goal_id)
        return True

    def _find_milestone(self, goal_id: str,
                        milestone_id: str) -> Optional[Milestone]:
        goal = self._goals.get(goal_id)
        if not goal:
            return None
        for m in goal.milestones:
            if m.id == milestone_id:
                return m
        return None

    # -- Timeline ------------------------------------------------------

    def add_note(self, goal_id: str, message: str) -> bool:
        if goal_id not in self._goals:
            return False
        self._log(goal_id, "note", message)
        self._touch(goal_id)
        return True

    def timeline(self, goal_id: str,
                 limit: int = 50) -> List[GoalEvent]:
        events = self._events.get(goal_id, [])
        events += self.store.load_events(goal_id)
        # Dedupe by id
        seen: Dict[str, GoalEvent] = {}
        for e in events:
            seen[e.id] = e
        out = sorted(seen.values(), key=lambda e: e.timestamp)
        return out[-limit:]

    def _log(self, goal_id: str, event_type: str, message: str) -> None:
        ev = GoalEvent(
            id=f"ev_{uuid.uuid4().hex[:8]}",
            goal_id=goal_id,
            event_type=event_type,
            message=message,
        )
        self._events.setdefault(goal_id, []).append(ev)
        self.store.save_event(ev)

    def _touch(self, goal_id: str) -> None:
        goal = self._goals.get(goal_id)
        if goal:
            goal.updated_at = time.time()
            self.store.save_goal(goal)

    def _load_all(self) -> None:
        for gid in self.store.list_goals():
            try:
                self._goals[gid] = self.store.load_goal(gid)
                self._events[gid] = []
            except Exception:
                continue

    # -- Reporting -----------------------------------------------------

    def summary(self) -> Dict[str, Any]:
        goals = list(self._goals.values())
        return {
            "total": len(goals),
            "active": sum(1 for g in goals
                          if g.status == GoalStatus.ACTIVE),
            "completed": sum(1 for g in goals
                             if g.status == GoalStatus.COMPLETED),
            "abandoned": sum(1 for g in goals
                             if g.status == GoalStatus.ABANDONED),
            "avg_progress": (sum(g.progress for g in goals) / len(goals)
                             if goals else 0.0),
        }

    def next_actions(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Next milestones across all active goals."""
        out = []
        for g in self.list_goals(status=GoalStatus.ACTIVE):
            ms = g.current_milestone
            if ms:
                out.append({
                    "goal_id": g.id,
                    "goal_title": g.title,
                    "milestone_id": ms.id,
                    "milestone_title": ms.title,
                    "status": ms.status.value,
                    "plan_id": ms.plan_id,
                })
        return out[:limit]


class GoalStore:
    """JSON file persistence for goals and events."""

    def __init__(self, directory: str = "/tmp/haley_goals"):
        import os
        self.directory = directory
        self.events_dir = os.path.join(directory, "events")
        os.makedirs(self.events_dir, exist_ok=True)

    def _goal_path(self, goal_id: str) -> str:
        import os
        safe = "".join(c for c in goal_id if c.isalnum() or c in "_-")
        return os.path.join(self.directory, f"{safe}.json")

    def save_goal(self, goal: Goal) -> str:
        path = self._goal_path(goal.id)
        with open(path, "w") as f:
            json.dump(goal.to_dict(), f, indent=2, default=str)
        return path

    def load_goal(self, goal_id: str) -> Goal:
        with open(self._goal_path(goal_id)) as f:
            return Goal.from_dict(json.load(f))

    def list_goals(self) -> List[str]:
        import os
        return [f[:-5] for f in os.listdir(self.directory)
                if f.endswith(".json")]

    def save_event(self, event: GoalEvent) -> None:
        import os
        path = os.path.join(self.events_dir, f"{event.goal_id}.jsonl")
        with open(path, "a") as f:
            f.write(json.dumps(event.to_dict(), default=str) + "\n")

    def load_events(self, goal_id: str) -> List[GoalEvent]:
        import os
        path = os.path.join(self.events_dir, f"{goal_id}.jsonl")
        events = []
        if not os.path.exists(path):
            return events
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(GoalEvent.from_dict(json.loads(line)))
                    except Exception:
                        continue
        return events


# ---------------------------------------------------------------------------
# Goal decomposition — break goals into milestones automatically
# ---------------------------------------------------------------------------

class GoalDecomposer:
    """Suggests milestones for a goal using heuristics."""

    # Phase templates for common goal types
    _TEMPLATES: Dict[str, List[str]] = {
        "build": ["Requirements gathered", "Design complete",
                  "Implementation done", "Tests passing",
                  "Deployed and verified"],
        "research": ["Questions defined", "Sources gathered",
                     "Facts triangulated", "Summary written"],
        "learn": ["Basics covered", "Practice completed",
                  "Project built", "Knowledge verified"],
        "fix": ["Issue reproduced", "Root cause found",
                "Fix implemented", "Regression tested"],
        "deploy": ["Build succeeds", "Staging verified",
                   "Production deployed", "Health checks green"],
    }

    def suggest_milestones(self, goal_title: str,
                           goal_description: str = "") -> List[str]:
        """Return suggested milestone titles."""
        text = (goal_title + " " + goal_description).lower()
        for key, template in self._TEMPLATES.items():
            if key in text:
                return list(template)
        # Generic fallback
        return ["Plan created", "Work in progress",
                "Review completed", "Goal achieved"]

    def decompose_goal(self, manager: GoalManager, goal_id: str) -> int:
        """Auto-populate milestones for a goal. Returns count added."""
        goal = manager.get_goal(goal_id)
        if not goal or goal.milestones:
            return 0
        suggestions = self.suggest_milestones(goal.title, goal.description)
        for s in suggestions:
            manager.add_milestone(goal_id, s)
        return len(suggestions)
