"""Lint runner — static analysis for Python code (stdlib-based).

Per ChatGPT spec: agent/coding/lint_runner.py
Checks: syntax, unused imports, complexity, style, security patterns.
No external dependencies.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set


class LintSeverity(Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class LintIssue:
    """A single lint finding."""
    code: str  # e.g. "E001", "W201"
    severity: LintSeverity
    line: int
    message: str
    suggestion: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "severity": self.severity.value,
                "line": self.line, "message": self.message,
                "suggestion": self.suggestion}


@dataclass
class LintResult:
    path: str
    issues: List[LintIssue] = field(default_factory=list)
    lines: int = 0

    @property
    def errors(self) -> int:
        return sum(1 for i in self.issues
                   if i.severity == LintSeverity.ERROR)

    @property
    def warnings(self) -> int:
        return sum(1 for i in self.issues
                   if i.severity == LintSeverity.WARNING)

    @property
    def ok(self) -> bool:
        return self.errors == 0

    def to_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "lines": self.lines,
                "errors": self.errors, "warnings": self.warnings,
                "ok": self.ok,
                "issues": [i.to_dict() for i in self.issues]}


class LintRunner:
    """Runs all lint checks on Python source."""

    # Security patterns: (regex, message)
    _SECURITY_PATTERNS = [
        (re.compile(r"\beval\s*\("), "Use of eval() — code injection risk"),
        (re.compile(r"\bexec\s*\("), "Use of exec() — code injection risk"),
        (re.compile(r"subprocess\.\w+\(.*shell\s*=\s*True"),
         "subprocess with shell=True — injection risk"),
        (re.compile(r"os\.system\s*\("), "os.system() — prefer subprocess"),
        (re.compile(r"pickle\.loads?\s*\("),
         "pickle deserialization — arbitrary code risk"),
        (re.compile(r"input\s*\(\)"), "input() in non-interactive code"),
    ]

    def lint(self, path: str) -> LintResult:
        with open(path, encoding="utf-8", errors="ignore") as f:
            source = f.read()
        return self.lint_source(source, path)

    def lint_source(self, source: str, path: str = "<string>") -> LintResult:
        result = LintResult(path=path, lines=source.count("\n") + 1)
        # 1. Syntax
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            result.issues.append(LintIssue(
                code="E001", severity=LintSeverity.ERROR,
                line=e.lineno or 0,
                message=f"SyntaxError: {e.msg}",
                suggestion="Fix the syntax error"))
            return result
        # 2. All AST-based checks
        checker = _AstChecker(source, tree)
        result.issues.extend(checker.check())
        # 3. Line-based checks
        result.issues.extend(self._check_lines(source))
        # 4. Security patterns
        result.issues.extend(self._check_security(source))
        result.issues.sort(key=lambda i: (i.line, i.code))
        return result

    def _check_lines(self, source: str) -> List[LintIssue]:
        issues = []
        for i, line in enumerate(source.splitlines(), 1):
            if len(line) > 120:
                issues.append(LintIssue(
                    code="W101", severity=LintSeverity.WARNING, line=i,
                    message=f"Line too long ({len(line)} > 120)",
                    suggestion="Break into multiple lines"))
            if line.rstrip() != line:
                issues.append(LintIssue(
                    code="W102", severity=LintSeverity.INFO, line=i,
                    message="Trailing whitespace",
                    suggestion="Remove trailing whitespace"))
            if "\t" in line:
                issues.append(LintIssue(
                    code="W103", severity=LintSeverity.WARNING, line=i,
                    message="Tab indentation",
                    suggestion="Use 4 spaces"))
        return issues

    def _check_security(self, source: str) -> List[LintIssue]:
        issues = []
        for i, line in enumerate(source.splitlines(), 1):
            for rx, msg in self._SECURITY_PATTERNS:
                if rx.search(line):
                    issues.append(LintIssue(
                        code="S201", severity=LintSeverity.WARNING,
                        line=i, message=msg,
                        suggestion="Review for security implications"))
        return issues

    def lint_directory(self, root: str,
                       max_files: int = 200) -> List[LintResult]:
        import os
        results = []
        count = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in {".git", "__pycache__", ".venv"}]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                results.append(self.lint(os.path.join(dirpath, fn)))
                count += 1
                if count >= max_files:
                    return results
        return results


class _AstChecker(ast.NodeVisitor):
    """AST-based lint checks."""

    def __init__(self, source: str, tree: ast.AST):
        self.source = source
        self.tree = tree
        self.issues: List[LintIssue] = []
        self._imported: Dict[str, int] = {}  # name -> line
        self._used: Set[str] = set()
        self._defined: Set[str] = set()

    def check(self) -> List[LintIssue]:
        self.visit(self.tree)
        self._check_unused_imports()
        return self.issues

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            name = (a.asname or a.name).split(".")[0]
            self._imported[name] = node.lineno
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for a in node.names:
            if a.name == "*":
                continue
            name = a.asname or a.name
            self._imported[name] = node.lineno
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self._used.add(node.id)
        elif isinstance(node.ctx, (ast.Store, ast.Del)):
            self._defined.add(node.id)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # Mutable default args
        for d in node.args.defaults:
            if isinstance(d, (ast.List, ast.Dict, ast.Set)):
                self.issues.append(LintIssue(
                    code="W201", severity=LintSeverity.WARNING,
                    line=node.lineno,
                    message=f"Mutable default argument in {node.name}()",
                    suggestion="Use None and initialize inside"))
        # Complexity
        complexity = self._complexity(node)
        if complexity > 10:
            self.issues.append(LintIssue(
                code="W202", severity=LintSeverity.WARNING,
                line=node.lineno,
                message=f"High complexity ({complexity}) in {node.name}()",
                suggestion="Split into smaller functions"))
        # Missing docstring
        if not ast.get_docstring(node) and not node.name.startswith("_"):
            self.issues.append(LintIssue(
                code="W203", severity=LintSeverity.INFO,
                line=node.lineno,
                message=f"Missing docstring in {node.name}()",
                suggestion="Add a docstring"))
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is None:
            self.issues.append(LintIssue(
                code="W204", severity=LintSeverity.WARNING,
                line=node.lineno,
                message="Bare except clause",
                suggestion="Catch specific exceptions"))
        self.generic_visit(node)

    def _check_unused_imports(self) -> None:
        for name, line in self._imported.items():
            if name not in self._used and name not in self._defined:
                self.issues.append(LintIssue(
                    code="W205", severity=LintSeverity.WARNING,
                    line=line,
                    message=f"Unused import: {name}",
                    suggestion="Remove the import"))

    def _complexity(self, node: ast.FunctionDef) -> int:
        count = 1
        for child in ast.walk(node):
            if isinstance(child, (ast.If, ast.While, ast.For,
                                  ast.AsyncFor, ast.ExceptHandler,
                                  ast.With, ast.AsyncWith,
                                  ast.Assert)):
                count += 1
            elif isinstance(child, ast.BoolOp):
                count += len(child.values) - 1
        return count
