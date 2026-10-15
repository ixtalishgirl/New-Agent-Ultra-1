"""Repository reader — inspects codebases for the self-modification pipeline.

Per ChatGPT spec: agent/coding/repository_reader.py
Maps repo structure, finds entry points, analyzes dependencies.
No personality. No rules. Direct execution.
"""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


@dataclass
class ModuleInfo:
    """Information about a Python module."""
    path: str
    name: str  # dotted name
    size_bytes: int
    lines: int
    imports: List[str] = field(default_factory=list)
    classes: List[str] = field(default_factory=list)
    functions: List[str] = field(default_factory=list)
    has_main: bool = False
    docstring: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path, "name": self.name,
            "size_bytes": self.size_bytes, "lines": self.lines,
            "imports": self.imports, "classes": self.classes,
            "functions": self.functions, "has_main": self.has_main,
            "docstring": self.docstring[:300],
        }


@dataclass
class RepoMap:
    """Full map of a repository."""
    root: str
    modules: List[ModuleInfo] = field(default_factory=list)
    entry_points: List[str] = field(default_factory=list)
    total_lines: int = 0
    total_files: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "root": self.root,
            "total_lines": self.total_lines,
            "total_files": self.total_files,
            "entry_points": self.entry_points,
            "modules": [m.to_dict() for m in self.modules],
        }

    def find_module(self, name: str) -> Optional[ModuleInfo]:
        for m in self.modules:
            if m.name == name or m.path.endswith(name):
                return m
        return None

    def import_graph(self) -> Dict[str, List[str]]:
        """module -> list of imported module names."""
        return {m.name: m.imports for m in self.modules}


class RepositoryReader:
    """Reads and maps Python repositories."""

    SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules",
                 ".tox", ".pytest_cache", "dist", "build", ".eggs"}

    def __init__(self, root: str = "."):
        self.root = os.path.abspath(root)

    def map(self) -> RepoMap:
        """Build a complete map of the repository."""
        repo = RepoMap(root=self.root)
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in self.SKIP_DIRS]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                try:
                    info = self._analyze_file(path)
                except Exception:
                    continue
                repo.modules.append(info)
                repo.total_lines += info.lines
                repo.total_files += 1
                if info.has_main or fn in ("main.py", "__main__.py", "app.py"):
                    repo.entry_points.append(path)
        return repo

    def _analyze_file(self, path: str) -> ModuleInfo:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            source = f.read()
        rel = os.path.relpath(path, self.root)
        name = rel[:-3].replace(os.sep, ".")
        if name.endswith(".__init__"):
            name = name[:-9]
        lines = source.count("\n") + 1
        info = ModuleInfo(
            path=path, name=name,
            size_bytes=os.path.getsize(path), lines=lines)
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return info
        info.docstring = (ast.get_docstring(tree) or "")[:300]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    info.imports.append(a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    info.imports.append(node.module.split(".")[0])
            elif isinstance(node, ast.ClassDef):
                info.classes.append(node.name)
            elif isinstance(node, ast.FunctionDef):
                if node.col_offset == 0:
                    info.functions.append(node.name)
        info.has_main = '__main__' in source
        # Dedupe imports
        info.imports = sorted(set(info.imports))
        return info

    def find_entry_points(self) -> List[str]:
        """Find likely entry points (mains, CLIs, servers)."""
        repo = self.map()
        return repo.entry_points

    def dependency_order(self) -> List[str]:
        """Topological order of modules by imports (best effort)."""
        repo = self.map()
        names = {m.name for m in repo.modules}
        # Only consider intra-repo imports
        deps: Dict[str, Set[str]] = {}
        for m in repo.modules:
            deps[m.name] = {i for i in m.imports if i in names}
        # Kahn's
        order: List[str] = []
        indeg = {n: 0 for n in names}
        succ: Dict[str, Set[str]] = {n: set() for n in names}
        for n, ds in deps.items():
            for d in ds:
                if d != n:
                    succ[d].add(n)
                    indeg[n] += 1
        queue = [n for n, d in indeg.items() if d == 0]
        while queue:
            n = queue.pop(0)
            order.append(n)
            for s in succ[n]:
                indeg[s] -= 1
                if indeg[s] == 0:
                    queue.append(s)
        # Append any remaining (cycles)
        for n in names:
            if n not in order:
                order.append(n)
        return order

    def hotspots(self, top_n: int = 10) -> List[Dict[str, Any]]:
        """Largest/most complex modules (candidates for refactoring)."""
        repo = self.map()
        scored = []
        for m in repo.modules:
            # Complexity proxy: lines * (1 + classes + functions/3)
            score = m.lines * (1 + len(m.classes) + len(m.functions) / 3)
            scored.append((score, m))
        scored.sort(key=lambda x: -x[0])
        return [{
            "path": m.path, "name": m.name, "lines": m.lines,
            "classes": len(m.classes), "functions": len(m.functions),
            "score": round(s, 1),
        } for s, m in scored[:top_n]]

    def search_code(self, pattern: str,
                    max_results: int = 50) -> List[Dict[str, Any]]:
        """Grep for a pattern across Python files."""
        import re
        try:
            rx = re.compile(pattern)
        except re.error:
            rx = re.compile(re.escape(pattern))
        hits = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in self.SKIP_DIRS]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                try:
                    with open(path, encoding="utf-8", errors="ignore") as f:
                        for i, line in enumerate(f, 1):
                            if rx.search(line):
                                hits.append({
                                    "path": path, "line": i,
                                    "text": line.strip()[:200],
                                })
                                if len(hits) >= max_results:
                                    return hits
                except Exception:
                    continue
        return hits
