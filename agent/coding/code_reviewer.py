"""AST-based Python code reviewer.

Reviews Python source for real issues without executing it:
  - unused imports / unused variables (AST scope analysis)
  - undefined names
  - bare `except:` / `except Exception:` that swallows everything
  - mutable default arguments
  - security issues: eval/exec/compile, subprocess/popen with shell=True,
    os.system, pickle.loads on untrusted data, hardcoded secrets, weak hashes
  - complexity warnings (cyclomatic), long functions, long lines
  - style: missing docstrings, shadowing builtins, comparison to None/True

Every finding carries severity, line number, message, and a suggestion.
The review() entry point returns a ReviewReport with a 0-100 score.
"""

from __future__ import annotations

import ast
import builtins
import keyword
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple


class FindingSeverity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FindingCategory(str, Enum):
    CORRECTNESS = "correctness"
    SECURITY = "security"
    COMPLEXITY = "complexity"
    STYLE = "style"
    PERFORMANCE = "performance"


@dataclass
class ReviewFinding:
    severity: FindingSeverity
    category: FindingCategory
    line: int
    message: str
    suggestion: str = ""
    rule: str = ""
    col: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {"severity": self.severity.value, "category": self.category.value,
                "line": self.line, "col": self.col, "message": self.message,
                "suggestion": self.suggestion, "rule": self.rule}

    def __str__(self) -> str:
        loc = f"line {self.line}" + (f":{self.col}" if self.col else "")
        return f"[{self.severity.value.upper()}/{self.category.value}] {loc}: {self.message}"


@dataclass
class ReviewReport:
    filename: str
    findings: List[ReviewFinding] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)

    @property
    def score(self) -> int:
        """0-100 code health score."""
        penalty = {FindingSeverity.INFO: 0, FindingSeverity.LOW: 1,
                   FindingSeverity.MEDIUM: 4, FindingSeverity.HIGH: 10,
                   FindingSeverity.CRITICAL: 25}
        total = sum(penalty[f.severity] for f in self.findings)
        return max(0, 100 - total)

    @property
    def grade(self) -> str:
        s = self.score
        if s >= 90:
            return "A"
        if s >= 75:
            return "B"
        if s >= 60:
            return "C"
        if s >= 40:
            return "D"
        return "F"

    def by_severity(self, severity: FindingSeverity) -> List[ReviewFinding]:
        return [f for f in self.findings if f.severity == severity]

    def by_category(self, category: FindingCategory) -> List[ReviewFinding]:
        return [f for f in self.findings if f.category == category]

    def has_blockers(self) -> bool:
        return any(f.severity in (FindingSeverity.HIGH, FindingSeverity.CRITICAL)
                   for f in self.findings)

    def summary(self) -> str:
        counts = {s.value: len(self.by_severity(s)) for s in FindingSeverity}
        return (f"{self.filename}: score {self.score}/100 (grade {self.grade}), "
                f"{len(self.findings)} finding(s): " +
                ", ".join(f"{v} {k}" for k, v in counts.items() if v))

    def to_dict(self) -> Dict[str, Any]:
        return {"filename": self.filename, "score": self.score,
                "grade": self.grade, "metrics": self.metrics,
                "findings": [f.to_dict() for f in self.findings]}


# ---------------------------------------------------------------------------
# Scope-aware usage analysis
# ---------------------------------------------------------------------------

class _Scope:
    """One lexical scope: tracks definitions and uses of names."""

    def __init__(self, name: str, parent: Optional["_Scope"] = None) -> None:
        self.name = name
        self.parent = parent
        self.defined: Dict[str, int] = {}   # name -> line defined
        self.used: Set[str] = set()
        self.imported: Dict[str, int] = {}  # name -> line imported
        self.params: Set[str] = set()
        self.children: List["_Scope"] = []

    def define(self, name: str, lineno: int) -> None:
        self.defined.setdefault(name, lineno)

    def use(self, name: str) -> None:
        scope: Optional[_Scope] = self
        while scope is not None:
            if name in scope.defined or name in scope.imported or name in scope.params:
                scope.used.add(name)
                return
            scope = scope.parent
        # use of a name defined nowhere in scope chain: record at this scope
        # (the undefined-name check handles builtins separately)
        self.used.add(name)

    def unused_imports(self) -> List[Tuple[str, int]]:
        return [(n, ln) for n, ln in self.imported.items() if n not in self.used]

    def unused_locals(self) -> List[Tuple[str, int]]:
        return [(n, ln) for n, ln in self.defined.items()
                if n not in self.used and not n.startswith("_")
                and n not in self.params]


class _UsageVisitor(ast.NodeVisitor):
    """Builds scope tree, recording definitions and uses."""

    def __init__(self) -> None:
        self.root = _Scope("module")
        self.scope = self.root
        self.undefined: List[Tuple[str, int]] = []
        self.builtin_names = set(dir(builtins)) | set(keyword.kwlist)

    # -- scope management ------------------------------------------------------
    def _push(self, name: str) -> None:
        child = _Scope(name, parent=self.scope)
        self.scope.children.append(child)
        self.scope = child

    def _pop(self) -> None:
        if self.scope.parent is not None:
            self.scope = self.scope.parent

    def _is_defined_anywhere(self, name: str) -> bool:
        scope: Optional[_Scope] = self.scope
        while scope is not None:
            if (name in scope.defined or name in scope.imported
                    or name in scope.params):
                return True
            scope = scope.parent
        return name in self.builtin_names

    # -- definitions -------------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            self.scope.imported[(a.asname or a.name).split(".")[0]] = node.lineno

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for a in node.names:
            if a.name == "*":
                continue
            self.scope.imported[a.asname or a.name] = node.lineno

    def _define_args(self, args: ast.arguments) -> None:
        for a in list(args.args) + list(args.kwonlyargs):
            self.scope.params.add(a.arg)
            self.scope.defined[a.arg] = a.lineno
        if args.vararg:
            self.scope.params.add(args.vararg.arg)
        if args.kwarg:
            self.scope.params.add(args.kwarg.arg)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope.define(node.name, node.lineno)
        for d in node.decorator_list:
            self.visit(d)
        self._push(f"function:{node.name}")
        self._define_args(node.args)
        for stmt in node.body:
            self.visit(stmt)
        self._pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.define(node.name, node.lineno)
        for base in node.bases:
            self.visit(base)
        for d in node.decorator_list:
            self.visit(d)
        self._push(f"class:{node.name}")
        for stmt in node.body:
            self.visit(stmt)
        self._pop()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            self.scope.define(node.id, node.lineno)
        elif isinstance(node.ctx, (ast.Load, ast.Del)):
            self.scope.use(node.id)
            if (isinstance(node.ctx, ast.Load)
                    and not self._is_defined_anywhere(node.id)
                    and not node.id.startswith("__")):
                self.undefined.append((node.id, node.lineno))

    def visit_arg(self, node: ast.arg) -> None:
        self.scope.params.add(node.arg)
        self.scope.defined[node.arg] = node.lineno

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.scope.define(node.target.id, node.lineno)
        self.visit(node.value)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        # comprehension targets live in an implicit scope; approximate
        self._push("comprehension")
        self.visit(node.iter)
        for gen in node.generators:
            self.visit(gen)
        self._pop()

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._push("lambda")
        self._define_args(node.args)
        self.visit(node.body)
        self._pop()

    # -- collection ----------------------------------------------------------------
    def all_scopes(self) -> List[_Scope]:
        out: List[_Scope] = []
        stack = [self.root]
        while stack:
            s = stack.pop()
            out.append(s)
            stack.extend(s.children)
        return out

# ---------------------------------------------------------------------------
# CodeReviewer
# ---------------------------------------------------------------------------

class CodeReviewer:
    """Runs a battery of AST-based checks over Python source."""

    # complexity / size thresholds
    MAX_COMPLEXITY = 10
    MAX_FUNCTION_LINES = 60
    MAX_LINE_LENGTH = 100
    MAX_PARAMS = 6
    MAX_NESTING = 4

    # names that look like hardcoded secrets
    SECRET_NAME_RE = re.compile(
        r"(?i)(api[_-]?key|secret|password|passwd|pwd|token|private[_-]?key|aws[_-]?)")

    # weak hashes
    WEAK_HASHES = {"md5", "sha1"}

    def __init__(self, filename: str = "<string>") -> None:
        self.filename = filename
        self.findings: List[ReviewFinding] = []
        self._source_lines: List[str] = []

    # -- entry point --------------------------------------------------------------
    def review(self, source: str) -> ReviewReport:
        self.findings = []
        self._source_lines = source.splitlines()
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            self._add(FindingSeverity.CRITICAL, FindingCategory.CORRECTNESS,
                      exc.lineno or 1, f"syntax error: {exc.msg}",
                      "Fix the syntax error; nothing else can be checked until it parses.",
                      rule="syntax")
            return ReviewReport(self.filename, self.findings,
                                {"parse_ok": False, "error": str(exc)})
        visitor = _UsageVisitor()
        visitor.visit(tree)
        self._check_unused_imports(visitor)
        self._check_undefined_names(visitor)
        self._check_unused_locals(visitor)
        self._check_security(tree)
        self._check_correctness(tree)
        self._check_complexity(tree)
        self._check_style(tree)
        self._check_line_length()
        metrics = self._metrics(tree, visitor)
        findings = sorted(self.findings, key=lambda f: (f.line, f.col))
        return ReviewReport(self.filename, findings, metrics)

    def review_file(self, path: str) -> ReviewReport:
        self.filename = path
        with open(path, "r", encoding="utf-8") as fh:
            return self.review(fh.read())

    # -- finding helper ---------------------------------------------------------------
    def _add(self, severity: FindingSeverity, category: FindingCategory,
             line: int, message: str, suggestion: str = "",
             rule: str = "", col: int = 0) -> None:
        self.findings.append(ReviewFinding(severity, category, line,
                                           message, suggestion, rule, col))

    # -- usage checks -------------------------------------------------------------------
    def _check_unused_imports(self, visitor: _UsageVisitor) -> None:
        for scope in visitor.all_scopes():
            for name, lineno in scope.unused_imports():
                self._add(FindingSeverity.LOW, FindingCategory.STYLE, lineno,
                          f"unused import: {name!r}",
                          f"Remove 'import {name}' or use it.",
                          rule="unused-import")

    def _check_undefined_names(self, visitor: _UsageVisitor) -> None:
        seen: Set[Tuple[str, int]] = set()
        for name, lineno in visitor.undefined:
            if (name, lineno) in seen:
                continue
            seen.add((name, lineno))
            self._add(FindingSeverity.HIGH, FindingCategory.CORRECTNESS, lineno,
                      f"undefined name: {name!r} (possible NameError at runtime)",
                      f"Define or import {name!r} before use.",
                      rule="undefined-name")

    def _check_unused_locals(self, visitor: _UsageVisitor) -> None:
        for scope in visitor.all_scopes():
            if not scope.name.startswith("function:"):
                continue
            for name, lineno in scope.unused_locals():
                self._add(FindingSeverity.LOW, FindingCategory.STYLE, lineno,
                          f"unused variable {name!r} in {scope.name}",
                          "Remove it or prefix with _ if intentionally unused.",
                          rule="unused-variable")

    # -- security checks ------------------------------------------------------------------
    def _check_security(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                self._check_dangerous_call(node)
            elif isinstance(node, ast.Assign):
                self._check_hardcoded_secret(node)

    def _func_name(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return ""

    def _check_dangerous_call(self, node: ast.Call) -> None:
        name = self._func_name(node.func)
        full = ast.unparse(node.func) if hasattr(ast, "unparse") else name
        line = node.lineno

        if name in ("eval", "exec"):
            self._add(FindingSeverity.CRITICAL, FindingCategory.SECURITY, line,
                      f"use of {name}() executes arbitrary code",
                      "Avoid eval/exec; use ast.literal_eval for data or dispatch tables for logic.",
                      rule="no-eval-exec")
        elif name == "compile" and len(node.args) >= 2:
            self._add(FindingSeverity.HIGH, FindingCategory.SECURITY, line,
                      "compile() on dynamic source can execute arbitrary code",
                      "Only compile trusted, static source.",
                      rule="dynamic-compile")
        elif full in ("os.system",) or name == "system" and "os" in full:
            self._add(FindingSeverity.HIGH, FindingCategory.SECURITY, line,
                      "os.system() invokes a shell",
                      "Use subprocess.run([...], shell=False) with an argument list.",
                      rule="os-system")
        elif name in ("popen", "Popen", "call", "run", "check_output", "check_call"):
            for kw in node.keywords:
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    self._add(FindingSeverity.HIGH, FindingCategory.SECURITY, line,
                              f"{full}() with shell=True",
                              "Pass an argument list with shell=False; never interpolate user input into a shell string.",
                              rule="shell-true")
        elif full in ("pickle.loads", "pickle.load", "cPickle.loads"):
            self._add(FindingSeverity.HIGH, FindingCategory.SECURITY, line,
                      "pickle deserialization of untrusted data is arbitrary code execution",
                      "Use json or another safe format for untrusted data.",
                      rule="pickle-load")
        elif full in ("yaml.load",):
            has_loader = any(kw.arg == "Loader" for kw in node.keywords)
            if not has_loader:
                self._add(FindingSeverity.HIGH, FindingCategory.SECURITY, line,
                          "yaml.load() without a Loader is unsafe",
                          "Use yaml.safe_load().",
                          rule="yaml-load")
        elif name in ("md5", "sha1") or full.endswith((".md5", ".sha1")):
            if "hashlib" in full or name in self.WEAK_HASHES:
                self._add(FindingSeverity.MEDIUM, FindingCategory.SECURITY, line,
                          f"weak hash {name}()",
                          "Use sha256 or better for security purposes.",
                          rule="weak-hash")
        elif full in ("random.random", "random.randint", "random.choice", "random.randrange"):
            self._add(FindingSeverity.LOW, FindingCategory.SECURITY, line,
                      f"{full}() is not cryptographically secure",
                      "Use secrets module for tokens, passwords, or security decisions.",
                      rule="weak-random")
        elif name == "input" and not node.keywords:
            # input() itself is fine; flag only when fed to dangerous sinks is out of scope
            pass

    def _check_hardcoded_secret(self, node: ast.Assign) -> None:
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if not self.SECRET_NAME_RE.search(target.id):
                continue
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                val = node.value.value
                if val and not val.startswith(("os.environ", "{")) and len(val) > 3:
                    self._add(FindingSeverity.CRITICAL, FindingCategory.SECURITY,
                              node.lineno,
                              f"possible hardcoded secret in {target.id!r}",
                              "Load secrets from environment variables or a secret manager; never commit them.",
                              rule="hardcoded-secret")

    # -- correctness checks -----------------------------------------------------------------
    def _check_correctness(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._check_function_correctness(node)
            elif isinstance(node, ast.ExceptHandler):
                self._check_except_handler(node)
            elif isinstance(node, ast.Compare):
                self._check_comparison(node)

    def _check_function_correctness(self, node: ast.AST) -> None:
        assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        # mutable default arguments
        for default, arg in zip(node.args.defaults,
                                node.args.args[-len(node.args.defaults):] if node.args.defaults else []):
            if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                self._add(FindingSeverity.MEDIUM, FindingCategory.CORRECTNESS,
                          node.lineno,
                          f"mutable default argument {arg.arg!r}",
                          f"Use None as the default and create the {type(default).__name__.lower()} inside the function.",
                          rule="mutable-default")
        for default, arg in zip(node.args.kw_defaults, node.args.kwonlyargs):
            if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                self._add(FindingSeverity.MEDIUM, FindingCategory.CORRECTNESS,
                          node.lineno,
                          f"mutable kw-only default {arg.arg!r}",
                          "Use None as the default and create the object inside the function.",
                          rule="mutable-default")
        # builtin shadowing by parameters
        for arg in list(node.args.args) + list(node.args.kwonlyargs):
            if arg.arg in dir(builtins) and arg.arg not in ("self", "cls"):
                self._add(FindingSeverity.LOW, FindingCategory.STYLE, arg.lineno,
                          f"parameter {arg.arg!r} shadows a builtin",
                          f"Rename {arg.arg!r} to avoid confusion.",
                          rule="builtin-shadow")

    def _check_except_handler(self, node: ast.ExceptHandler) -> None:
        if node.type is None:
            self._add(FindingSeverity.MEDIUM, FindingCategory.CORRECTNESS,
                      node.lineno, "bare except: clause catches everything including "
                                   "KeyboardInterrupt and SystemExit",
                      "Catch specific exceptions (e.g. except ValueError).",
                      rule="bare-except")
        elif (isinstance(node.type, ast.Name) and node.type.id == "Exception"
                and len(node.body) <= 2):
            # `except Exception: pass` / tiny handler swallows errors silently
            is_silent = all(isinstance(s, ast.Pass) for s in node.body)
            if is_silent:
                self._add(FindingSeverity.MEDIUM, FindingCategory.CORRECTNESS,
                          node.lineno, "except Exception: pass silently swallows all errors",
                          "Log the exception or handle specific cases.",
                          rule="silent-except")
        # raise without from inside except (loses context) — informational
        for child in ast.walk(node):
            if isinstance(child, ast.Raise) and child.exc is not None and child.cause is None:
                # only flag if it's a bare `raise X` inside an except block (not bare `raise`)
                self._add(FindingSeverity.INFO, FindingCategory.STYLE, child.lineno,
                          "consider 'raise ... from ...' to preserve exception context",
                          "Use 'raise NewError(...) from exc'.",
                          rule="raise-from")
                break

    def _check_comparison(self, node: ast.Compare) -> None:
        for op, comparator in zip(node.ops, node.comparators):
            if isinstance(op, (ast.Eq, ast.NotEq)) and isinstance(comparator, ast.Constant):
                if comparator.value is True or comparator.value is False:
                    self._add(FindingSeverity.LOW, FindingCategory.STYLE, node.lineno,
                              f"comparison to {comparator.value!r}; use truthiness instead",
                              f"Write 'if {ast.unparse(node.left)}:' instead." if hasattr(ast, "unparse") else "Use truthiness.",
                              rule="compare-to-bool")
                elif comparator.value is None:
                    self._add(FindingSeverity.LOW, FindingCategory.STYLE, node.lineno,
                              "comparison to None should use 'is' / 'is not'",
                              "Write 'is None' / 'is not None'.",
                              rule="compare-to-none")

    # -- complexity checks ----------------------------------------------------------------------
    def _check_complexity(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                complexity = self._cyclomatic(node)
                if complexity > self.MAX_COMPLEXITY:
                    self._add(FindingSeverity.MEDIUM, FindingCategory.COMPLEXITY,
                              node.lineno,
                              f"{node.name}() cyclomatic complexity {complexity} "
                              f"(> {self.MAX_COMPLEXITY})",
                              "Split into smaller functions.",
                              rule="high-complexity")
                length = (getattr(node, "end_lineno", node.lineno) or node.lineno) - node.lineno + 1
                if length > self.MAX_FUNCTION_LINES:
                    self._add(FindingSeverity.LOW, FindingCategory.COMPLEXITY,
                              node.lineno,
                              f"{node.name}() is {length} lines (> {self.MAX_FUNCTION_LINES})",
                              "Extract helper functions.",
                              rule="long-function")
                n_params = len(node.args.args) + len(node.args.kwonlyargs)
                if n_params > self.MAX_PARAMS:
                    self._add(FindingSeverity.LOW, FindingCategory.STYLE,
                              node.lineno,
                              f"{node.name}() has {n_params} parameters (> {self.MAX_PARAMS})",
                              "Group parameters into a dataclass or config object.",
                              rule="too-many-params")
                nesting = self._max_nesting(node)
                if nesting > self.MAX_NESTING:
                    self._add(FindingSeverity.LOW, FindingCategory.COMPLEXITY,
                              node.lineno,
                              f"{node.name}() nests {nesting} levels deep (> {self.MAX_NESTING})",
                              "Use early returns to flatten.",
                              rule="deep-nesting")

    @staticmethod
    def _cyclomatic(node: ast.AST) -> int:
        score = 1
        for child in ast.walk(node):
            if isinstance(child, (ast.If, ast.While, ast.For, ast.AsyncFor,
                                  ast.ExceptHandler, ast.With, ast.AsyncWith,
                                  ast.Assert, ast.IfExp, ast.comprehension)):
                score += 1
            elif isinstance(child, ast.BoolOp):
                score += max(0, len(child.values) - 1)
        # don't double count the function def itself
        return score

    @staticmethod
    def _max_nesting(node: ast.AST) -> int:
        def depth(n: ast.AST, current: int) -> int:
            best = current
            for child in ast.iter_child_nodes(n):
                inc = 1 if isinstance(child, (ast.If, ast.For, ast.AsyncFor,
                                              ast.While, ast.With, ast.AsyncWith,
                                              ast.Try)) else 0
                best = max(best, depth(child, current + inc))
            return best
        return depth(node, 0)

    # -- style checks -------------------------------------------------------------------------------
    def _check_style(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("_") and node.name != "__init__":
                    continue
                if ast.get_docstring(node) is None and not node.name.startswith("__"):
                    self._add(FindingSeverity.INFO, FindingCategory.STYLE, node.lineno,
                              f"{node.name}() has no docstring",
                              "Add a docstring describing purpose, args, and returns.",
                              rule="missing-docstring")
            elif isinstance(node, ast.ClassDef):
                if ast.get_docstring(node) is None:
                    self._add(FindingSeverity.INFO, FindingCategory.STYLE, node.lineno,
                              f"class {node.name} has no docstring",
                              "Add a class docstring.",
                              rule="missing-docstring")
            elif isinstance(node, ast.Assert):
                self._add(FindingSeverity.LOW, FindingCategory.STYLE, node.lineno,
                          "assert used outside tests (disabled with -O)",
                          "Use explicit checks that raise exceptions for production validation.",
                          rule="assert-outside-test")

    def _check_line_length(self) -> None:
        for i, line in enumerate(self._source_lines, 1):
            if len(line) > self.MAX_LINE_LENGTH:
                # allow long URLs / noqa
                if "noqa" in line or "http://" in line or "https://" in line:
                    continue
                self._add(FindingSeverity.INFO, FindingCategory.STYLE, i,
                          f"line too long ({len(line)} > {self.MAX_LINE_LENGTH})",
                          "Break the line.",
                          rule="line-too-long")

    # -- metrics ----------------------------------------------------------------------------------------
    def _metrics(self, tree: ast.AST, visitor: _UsageVisitor) -> Dict[str, Any]:
        n_functions = sum(1 for _ in ast.walk(tree)
                          if isinstance(_, (ast.FunctionDef, ast.AsyncFunctionDef)))
        n_classes = sum(1 for _ in ast.walk(tree) if isinstance(_, ast.ClassDef))
        complexities = {n.name: self._cyclomatic(n) for n in ast.walk(tree)
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        avg_complexity = (sum(complexities.values()) / len(complexities)
                          if complexities else 0)
        return {
            "parse_ok": True,
            "lines": len(self._source_lines),
            "functions": n_functions,
            "classes": n_classes,
            "avg_complexity": round(avg_complexity, 2),
            "max_complexity": max(complexities.values(), default=0),
            "undefined_names": sorted({n for n, _ in visitor.undefined}),
        }


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------

def review_code(source: str, filename: str = "<string>") -> ReviewReport:
    """One-shot review of source code."""
    return CodeReviewer(filename).review(source)


def review_file(path: str) -> ReviewReport:
    """One-shot review of a file on disk."""
    return CodeReviewer(path).review_file(path)

# ---------------------------------------------------------------------------
# Additional check passes (mixed into CodeReviewer via subclass hook)
# ---------------------------------------------------------------------------

class ExtendedCodeReviewer(CodeReviewer):
    """CodeReviewer plus extra passes: resource handling, duplication, etc."""

    def review(self, source: str) -> ReviewReport:
        report = super().review(source)
        if not report.metrics.get("parse_ok", True):
            return report
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return report
        self._check_resource_handling(tree)
        self._check_wildcard_imports(tree)
        self._check_print_usage(tree)
        self._check_global_statements(tree)
        self._check_fstring_placeholders(tree)
        self._check_duplicate_code(tree)
        report.findings = sorted(self.findings, key=lambda f: (f.line, f.col))
        return ReviewReport(self.filename, report.findings, report.metrics)

    def _check_resource_handling(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and self._func_name(node.func) == "open":
                # open() outside a with statement -> resource leak risk
                parent = getattr(node, "_parent", None)
                # we didn't set parents; approximate via walking statements
                pass
        # simpler robust approach: find open() calls not inside With
        with_bodies: Set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.With, ast.AsyncWith)):
                for item in ast.walk(node):
                    with_bodies.add(id(item))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and self._func_name(node.func) == "open"
                    and id(node) not in with_bodies):
                # check it's assigned/used in a with via contextlib? keep simple
                self._add(FindingSeverity.LOW, FindingCategory.CORRECTNESS,
                          node.lineno, "open() without a context manager",
                          "Use 'with open(...) as f:' so the file is always closed.",
                          rule="open-without-with")

    def _check_wildcard_imports(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if any(a.name == "*" for a in node.names):
                    self._add(FindingSeverity.MEDIUM, FindingCategory.STYLE,
                              node.lineno,
                              f"wildcard import from {node.module or '?'}",
                              "Import explicit names instead.",
                              rule="wildcard-import")

    def _check_print_usage(self, tree: ast.AST) -> None:
        in_main_guard = False
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                test = ast.unparse(node.test) if hasattr(ast, "unparse") else ""
                if '__name__' in test and '__main__' in test:
                    in_main_guard = True
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and self._func_name(node.func) == "print"):
                # informational only: print is fine for CLIs, noisy in libraries
                self._add(FindingSeverity.INFO, FindingCategory.STYLE,
                          node.lineno, "print() call (consider logging for libraries)",
                          "Use the logging module for library code.",
                          rule="print-usage")

    def _check_global_statements(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Global):
                self._add(FindingSeverity.LOW, FindingCategory.STYLE,
                          node.lineno,
                          f"global statement for {', '.join(node.names)}",
                          "Prefer returning values or encapsulating state in a class.",
                          rule="global-statement")

    def _check_fstring_placeholders(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                has_expr = any(isinstance(v, ast.FormattedValue) for v in node.values)
                if not has_expr:
                    self._add(FindingSeverity.LOW, FindingCategory.STYLE,
                              node.lineno, "f-string without any placeholder",
                              "Remove the 'f' prefix.",
                              rule="fstring-no-placeholder")

    def _check_duplicate_code(self, tree: ast.AST) -> None:
        detector = DuplicateCodeDetector(min_statements=4)
        for dup in detector.find(tree):
            self._add(FindingSeverity.LOW, FindingCategory.COMPLEXITY,
                      dup["first_line"],
                      f"duplicated code block ({dup['statements']} statements, "
                      f"{dup['occurrences']} occurrences)",
                      "Extract the block into a helper function.",
                      rule="duplicate-code")


# ---------------------------------------------------------------------------
# Duplicate code detection
# ---------------------------------------------------------------------------

class DuplicateCodeDetector:
    """Finds duplicated statement blocks via normalized hashing.

    Normalization: names -> NAME, constants -> CONST, so clones that differ
    only in identifiers are still caught.
    """

    def __init__(self, min_statements: int = 4) -> None:
        self.min_statements = max(2, min_statements)

    @staticmethod
    def _normalize(node: ast.AST) -> str:
        class Norm(ast.NodeTransformer):
            def visit_Name(self, n: ast.Name) -> ast.AST:
                return ast.copy_location(ast.Name(id="NAME", ctx=n.ctx), n)
            def visit_Constant(self, n: ast.Constant) -> ast.AST:
                return ast.copy_location(ast.Constant(value="CONST"), n)
            def visit_arg(self, n: ast.arg) -> ast.AST:
                n2 = ast.arg(arg="ARG")
                return ast.copy_location(n2, n)
        new = Norm().visit(node)
        ast.fix_missing_locations(new)
        return ast.dump(new)

    def find(self, tree: ast.AST) -> List[Dict[str, Any]]:
        # collect statement sequences per function
        blocks: List[Tuple[str, int, int, int]] = []  # (hash, lineno, n_stmts, func_line)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            stmts = [s for s in node.body
                     if not (isinstance(s, ast.Expr)
                             and isinstance(s.value, ast.Constant)
                             and isinstance(s.value.value, str))]
            if len(stmts) < self.min_statements:
                continue
            # sliding window of min_statements
            for i in range(len(stmts) - self.min_statements + 1):
                window = stmts[i:i + self.min_statements]
                key = "|".join(self._normalize(s) for s in window)
                import hashlib as _hl
                digest = _hl.sha256(key.encode()).hexdigest()[:16]
                blocks.append((digest, window[0].lineno, self.min_statements, node.lineno))
        # group by digest
        from collections import defaultdict as _dd
        groups: Dict[str, List[Tuple[int, int, int]]] = _dd(list)
        for digest, lineno, n, func_line in blocks:
            groups[digest].append((lineno, n, func_line))
        out = []
        for digest, occs in groups.items():
            funcs = {o[2] for o in occs}
            if len(occs) >= 2 and len(funcs) >= 2:
                out.append({"hash": digest,
                            "occurrences": len(occs),
                            "first_line": min(o[0] for o in occs),
                            "statements": occs[0][1]})
        return out


# ---------------------------------------------------------------------------
# Report comparison: track improvement over time
# ---------------------------------------------------------------------------

def compare_reports(old: ReviewReport, new: ReviewReport) -> Dict[str, Any]:
    """Compare two review reports (e.g. before/after a refactor)."""
    def key(f: ReviewFinding) -> Tuple[str, int, str]:
        return (f.rule or f.message[:60], f.line, f.severity.value)
    old_keys = {key(f) for f in old.findings}
    new_keys = {key(f) for f in new.findings}
    fixed = old_keys - new_keys
    introduced = new_keys - old_keys
    return {
        "old_score": old.score, "new_score": new.score,
        "score_delta": new.score - old.score,
        "old_grade": old.grade, "new_grade": new.grade,
        "fixed_count": len(fixed), "introduced_count": len(introduced),
        "fixed": sorted(f[0] for f in fixed)[:20],
        "introduced": sorted(f[0] for f in introduced)[:20],
        "improved": new.score > old.score,
    }


# ---------------------------------------------------------------------------
# Batch review
# ---------------------------------------------------------------------------

def review_many(sources: Dict[str, str],
                reviewer_cls: type = CodeReviewer) -> Dict[str, ReviewReport]:
    """Review many named sources. Returns filename -> report."""
    return {name: reviewer_cls(name).review(src) for name, src in sources.items()}


def worst_reports(reports: Dict[str, ReviewReport],
                  limit: int = 5) -> List[ReviewReport]:
    """Return the lowest-scoring reports first."""
    return sorted(reports.values(), key=lambda r: r.score)[:limit]
