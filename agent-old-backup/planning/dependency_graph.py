"""Dependency graph — DAG utilities for task planning (ChatGPT spec).

Per spec: agent/planning/dependency_graph.py
Standalone graph with cycle detection, topological sort, levels, ancestors.
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Dict, List, Optional, Set


class DependencyGraph:
    """Directed acyclic graph of task dependencies."""

    def __init__(self):
        self._succ: Dict[str, Set[str]] = defaultdict(set)  # node -> dependents
        self._pred: Dict[str, Set[str]] = defaultdict(set)  # node -> dependencies
        self._nodes: Set[str] = set()

    def add_node(self, node: str) -> None:
        self._nodes.add(node)
        self._succ.setdefault(node, set())
        self._pred.setdefault(node, set())

    def add_edge(self, frm: str, to: str) -> None:
        """frm must complete before to."""
        self.add_node(frm)
        self.add_node(to)
        self._succ[frm].add(to)
        self._pred[to].add(frm)

    def remove_edge(self, frm: str, to: str) -> None:
        self._succ.get(frm, set()).discard(to)
        self._pred.get(to, set()).discard(frm)

    def remove_node(self, node: str) -> None:
        for s in list(self._succ.get(node, [])):
            self._pred[s].discard(node)
        for p in list(self._pred.get(node, [])):
            self._succ[p].discard(node)
        self._succ.pop(node, None)
        self._pred.pop(node, None)
        self._nodes.discard(node)

    @property
    def nodes(self) -> Set[str]:
        return set(self._nodes)

    def dependencies(self, node: str) -> Set[str]:
        return set(self._pred.get(node, set()))

    def dependents(self, node: str) -> Set[str]:
        return set(self._succ.get(node, set()))

    def roots(self) -> Set[str]:
        return {n for n in self._nodes if not self._pred[n]}

    def leaves(self) -> Set[str]:
        return {n for n in self._nodes if not self._succ[n]}

    def has_cycle(self) -> bool:
        return len(self.topological_sort()) != len(self._nodes)

    def topological_sort(self) -> List[str]:
        indeg = {n: len(self._pred[n]) for n in self._nodes}
        queue = deque(n for n, d in indeg.items() if d == 0)
        order: List[str] = []
        while queue:
            n = queue.popleft()
            order.append(n)
            for s in self._succ[n]:
                indeg[s] -= 1
                if indeg[s] == 0:
                    queue.append(s)
        return order

    def levels(self) -> Dict[str, int]:
        """Longest-path level for each node (0 = roots)."""
        order = self.topological_sort()
        level: Dict[str, int] = {}
        for n in order:
            if not self._pred[n]:
                level[n] = 0
            else:
                level[n] = 1 + max(level[p] for p in self._pred[n])
        return level

    def ancestors(self, node: str) -> Set[str]:
        """All transitive dependencies."""
        out: Set[str] = set()
        stack = list(self._pred.get(node, []))
        while stack:
            n = stack.pop()
            if n not in out:
                out.add(n)
                stack.extend(self._pred.get(n, []))
        return out

    def descendants(self, node: str) -> Set[str]:
        """All transitive dependents."""
        out: Set[str] = set()
        stack = list(self._succ.get(node, []))
        while stack:
            n = stack.pop()
            if n not in out:
                out.add(n)
                stack.extend(self._succ.get(n, []))
        return out

    def find_cycle(self) -> Optional[List[str]]:
        """Return one cycle if present, else None."""
        WHITE, GRAY, BLACK = 0, 1, 2
        color: Dict[str, int] = {n: WHITE for n in self._nodes}
        stack: List[str] = []

        def dfs(n: str) -> Optional[List[str]]:
            color[n] = GRAY
            stack.append(n)
            for s in self._succ[n]:
                if color[s] == GRAY:
                    return stack[stack.index(s):] + [s]
                if color[s] == WHITE:
                    r = dfs(s)
                    if r:
                        return r
            stack.pop()
            color[n] = BLACK
            return None

        for n in self._nodes:
            if color[n] == WHITE:
                r = dfs(n)
                if r:
                    return r
        return None

    def subgraph(self, nodes: Set[str]) -> "DependencyGraph":
        g = DependencyGraph()
        for n in nodes:
            g.add_node(n)
        for n in nodes:
            for s in self._succ.get(n, set()):
                if s in nodes:
                    g.add_edge(n, s)
        return g

    def to_dot(self) -> str:
        """Graphviz DOT representation."""
        lines = ["digraph plan {"]
        for n in sorted(self._nodes):
            lines.append(f'  "{n}";')
        for frm, tos in self._succ.items():
            for to in sorted(tos):
                lines.append(f'  "{frm}" -> "{to}";')
        lines.append("}")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, List[str]]:
        return {n: sorted(self._succ[n]) for n in self._nodes}

    @classmethod
    def from_dict(cls, d: Dict[str, List[str]]) -> "DependencyGraph":
        g = cls()
        for frm, tos in d.items():
            g.add_node(frm)
            for to in tos:
                g.add_edge(frm, to)
        return g
