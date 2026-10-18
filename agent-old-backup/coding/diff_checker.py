"""Diff checker — compares code versions, analyzes changes.

Per ChatGPT spec: agent/coding/diff_checker.py
Unified diffs, change classification, risk assessment.
"""
from __future__ import annotations

import ast
import difflib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple


class ChangeType(Enum):
    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"
    RENAMED = "renamed"


class ChangeRisk(Enum):
    LOW = "low"        # comments, whitespace, docs
    MEDIUM = "medium"  # logic changes in one function
    HIGH = "high"      # API changes, new deps, security-sensitive


@dataclass
class FileDiff:
    path: str
    change_type: ChangeType
    additions: int = 0
    deletions: int = 0
    hunks: List[str] = field(default_factory=list)
    risk: ChangeRisk = ChangeRisk.LOW

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "change_type": self.change_type.value,
            "additions": self.additions,
            "deletions": self.deletions,
            "risk": self.risk.value,
            "hunks": self.hunks[:5],
        }


@dataclass
class DiffSummary:
    files_changed: int
    additions: int
    deletions: int
    files: List[FileDiff]
    overall_risk: ChangeRisk

    def to_dict(self) -> Dict[str, Any]:
        return {
            "files_changed": self.files_changed,
            "additions": self.additions,
            "deletions": self.deletions,
            "overall_risk": self.overall_risk.value,
            "files": [f.to_dict() for f in self.files],
        }


class DiffChecker:
    """Analyzes diffs between code versions."""

    def diff_text(self, old: str, new: str,
                  path: str = "file") -> FileDiff:
        """Diff two text versions."""
        old_lines = old.splitlines()
        new_lines = new.splitlines()
        diff = list(difflib.unified_diff(old_lines, new_lines, n=3))
        additions = sum(1 for l in diff if l.startswith("+")
                        and not l.startswith("+++"))
        deletions = sum(1 for l in diff if l.startswith("-")
                        and not l.startswith("---"))
        hunks = []
        current: List[str] = []
        for line in diff:
            if line.startswith("@@"):
                if current:
                    hunks.append("\n".join(current))
                current = [line]
            elif current:
                current.append(line)
        if current:
            hunks.append("\n".join(current))
        if not old.strip():
            ctype = ChangeType.ADDED
        elif not new.strip():
            ctype = ChangeType.REMOVED
        else:
            ctype = ChangeType.MODIFIED
        fd = FileDiff(path=path, change_type=ctype,
                      additions=additions, deletions=deletions,
                      hunks=hunks)
        fd.risk = self._assess_risk(old, new, path)
        return fd

    def diff_files(self, old_path: str, new_path: str) -> FileDiff:
        with open(old_path, encoding="utf-8", errors="ignore") as f:
            old = f.read()
        with open(new_path, encoding="utf-8", errors="ignore") as f:
            new = f.read()
        return self.diff_text(old, new, path=new_path)

    def summarize(self, diffs: List[FileDiff]) -> DiffSummary:
        files_changed = len(diffs)
        additions = sum(d.additions for d in diffs)
        deletions = sum(d.deletions for d in diffs)
        risks = [d.risk for d in diffs]
        overall = ChangeRisk.LOW
        if ChangeRisk.HIGH in risks:
            overall = ChangeRisk.HIGH
        elif ChangeRisk.MEDIUM in risks:
            overall = ChangeRisk.MEDIUM
        return DiffSummary(files_changed, additions, deletions,
                           diffs, overall)

    def _assess_risk(self, old: str, new: str, path: str) -> ChangeRisk:
        # High-risk indicators
        high_patterns = [
            r"\beval\s*\(", r"\bexec\s*\(",
            r"subprocess.*shell\s*=\s*True",
            r"os\.system\s*\(",
            r"__import__\s*\(",
            r"import\s+socket.*connect",
        ]
        for pat in high_patterns:
            if re.search(pat, new) and not re.search(pat, old):
                return ChangeRisk.HIGH
        # API changes: function signatures
        old_sigs = self._signatures(old)
        new_sigs = self._signatures(new)
        for name, sig in new_sigs.items():
            if name in old_sigs and old_sigs[name] != sig:
                return ChangeRisk.HIGH
        # Removed functions = high
        if set(old_sigs) - set(new_sigs):
            return ChangeRisk.HIGH
        # Medium: logic changes
        if self._logic_changed(old, new):
            return ChangeRisk.MEDIUM
        return ChangeRisk.LOW

    def _signatures(self, code: str) -> Dict[str, str]:
        sigs = {}
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return sigs
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = [a.arg for a in node.args.args]
                sigs[node.name] = f"({', '.join(args)})"
        return sigs

    def _logic_changed(self, old: str, new: str) -> bool:
        # Compare non-comment, non-blank lines
        def logic_lines(code: str) -> List[str]:
            out = []
            for line in code.splitlines():
                s = line.strip()
                if s and not s.startswith("#"):
                    out.append(s)
            return out
        return logic_lines(old) != logic_lines(new)

    def changed_functions(self, old: str, new: str) -> List[str]:
        """Names of functions whose bodies changed."""
        def func_bodies(code: str) -> Dict[str, str]:
            bodies = {}
            try:
                tree = ast.parse(code)
            except SyntaxError:
                return bodies
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    bodies[node.name] = ast.dump(node)
            return bodies
        old_b = func_bodies(old)
        new_b = func_bodies(new)
        changed = []
        for name in set(old_b) | set(new_b):
            if old_b.get(name) != new_b.get(name):
                changed.append(name)
        return sorted(changed)

    def git_diff(self, repo: str = ".", ref: str = "HEAD") -> DiffSummary:
        """Parse git diff into structured summary."""
        import subprocess
        try:
            p = subprocess.run(
                ["git", "-C", repo, "diff", ref, "--numstat"],
                capture_output=True, text=True, timeout=30)
        except Exception:
            return DiffSummary(0, 0, 0, [], ChangeRisk.LOW)
        diffs = []
        for line in p.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                try:
                    adds, dels = int(parts[0]), int(parts[1])
                except ValueError:
                    adds, dels = 0, 0
                diffs.append(FileDiff(
                    path=parts[2], change_type=ChangeType.MODIFIED,
                    additions=adds, deletions=dels))
        return self.summarize(diffs)
