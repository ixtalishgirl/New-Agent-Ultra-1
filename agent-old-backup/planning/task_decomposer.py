"""Task decomposer — spec-aligned decomposition (ChatGPT spec).

Per spec: agent/planning/task_decomposer.py
Wraps GoalDecomposer with the spec's naming and adds subtask division
with verification checkpoints.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .task_planner import GoalDecomposer, TaskType, TaskPriority


@dataclass
class Subtask:
    """A decomposed subtask with its verification criterion."""
    title: str
    description: str
    tool_name: str
    tool_args: Dict[str, Any] = field(default_factory=dict)
    verify_criterion: str = ""  # how to verify this subtask succeeded
    priority: int = 2
    estimated_seconds: float = 30.0
    tags: List[str] = field(default_factory=list)

    def to_spec(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "task_type": TaskType.COMPOUND,
            "tool_name": self.tool_name,
            "tool_args": self.tool_args,
            "priority": TaskPriority(self.priority),
            "estimated_seconds": self.estimated_seconds,
            "tags": self.tags,
            "metadata": {"verify_criterion": self.verify_criterion},
        }


class TaskDecomposer:
    """Decomposes goals into verified subtasks."""

    def __init__(self):
        self._base = GoalDecomposer()

    def decompose(self, goal: str) -> List[Dict[str, Any]]:
        """Return task specs (compatible with PlanBuilder)."""
        return self._base.decompose(goal)

    def decompose_with_verification(self, goal: str) -> List[Subtask]:
        """Decompose into subtasks, each with a verification criterion."""
        specs = self._base.decompose(goal)
        out: List[Subtask] = []
        for spec in specs:
            out.append(Subtask(
                title=spec["title"],
                description=spec["description"],
                tool_name=spec["tool_name"],
                tool_args=spec.get("tool_args", {}),
                verify_criterion=self._criterion_for(spec),
                priority=spec["priority"].value
                if hasattr(spec["priority"], "value")
                else int(spec["priority"]),
                estimated_seconds=spec.get("estimated_seconds", 30.0),
                tags=spec.get("tags", []),
            ))
        return out

    def _criterion_for(self, spec: Dict[str, Any]) -> str:
        tool = spec.get("tool_name", "")
        if tool == "shell":
            return "command exits 0 and output is non-empty"
        if tool == "python":
            return "script runs without traceback"
        if tool == "web_search":
            return "at least 3 sources returned"
        if tool.startswith("browser_"):
            return "page loads and expected content present"
        if tool == "file_write":
            return "file exists and is non-empty"
        if tool == "file_read":
            return "content returned without error"
        return "tool reports ok=true"

    def split_compound(self, subtask: Subtask,
                       max_parts: int = 4) -> List[Subtask]:
        """Split a large subtask into smaller parts."""
        # Heuristic: split on sentence boundaries of the description
        import re
        sentences = [s.strip() for s in
                     re.split(r"(?<=[.!?])\s+", subtask.description)
                     if s.strip()]
        if len(sentences) <= 1 or len(sentences) > max_parts * 2:
            return [subtask]
        parts: List[Subtask] = []
        for i, sent in enumerate(sentences[:max_parts]):
            parts.append(Subtask(
                title=f"{subtask.title} (part {i+1})",
                description=sent,
                tool_name=subtask.tool_name,
                tool_args=dict(subtask.tool_args),
                verify_criterion=subtask.verify_criterion,
                priority=subtask.priority,
                estimated_seconds=subtask.estimated_seconds / len(sentences),
                tags=subtask.tags + ["split"],
            ))
        return parts
