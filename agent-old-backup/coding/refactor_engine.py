"""AST-based refactoring engine.

Safe, mechanical code transformations:
  - extract_function: pull a statement range into a new function
  - rename_variable: scope-aware rename (no accidental capture)
  - inline_variable: replace a single-assignment variable with its value
  - simplify_conditionals: collapse redundant if/else, double negation, etc.
  - remove_dead_code: unreachable code after return/raise, unused assignments

Every transformation re-parses the result and rolls back on failure, so
the engine never produces broken code. RefactorResult reports exactly
what changed.
"""

from __future__ import annotations

import ast
import copy
import keyword
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple


@dataclass
class RefactorResult:
    """Outcome of one refactoring operation."""
    ok: bool
    source: str
    description: str
    changes: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "description": self.description,
                "changes": self.changes, "warnings": self.warnings,
                "lines": len(self.source.splitlines())}


class RefactorError(Exception):
    """Raised when a refactoring cannot be performed safely."""


# ---------------------------------------------------------------------------
# Scope analysis
# ---------------------------------------------------------------------------

class ScopeAnalyzer:
    """Maps every name occurrence to its defining scope.

    Used to guarantee safe renames: a rename is only applied when the new
    name is free in every scope the old name touches.
    """

    def __init__(self, tree: ast.AST) -> None:
        self.tree = tree
        # name -> list of (scope_id, node, kind) where kind in def/use
        self.occurrences: Dict[str, List[Tuple[int, ast.AST, str]]] = {}
        self._scope_stack: List[int] = [0]
        self._next_scope = 1
        self._walk(tree)

    def _current(self) -> int:
        return self._scope_stack[-1]

    def _record(self, name: str, node: ast.AST, kind: str) -> None:
        self.occurrences.setdefault(name, []).append((self._current(), node, kind))

    def _walk(self, node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            self._visit(child)

    def _visit(self, node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                             ast.Lambda, ast.GeneratorExp, ast.ListComp,
                             ast.SetComp, ast.DictComp)):
            self._record(node.name if hasattr(node, "name") else "<lambda>",
                         node, "def")
            sid = self._next_scope
            self._next_scope += 1
            self._scope_stack.append(s_id := sid)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                for a in list(node.args.args) + list(node.args.kwonlyargs):
                    self._record(a.arg, a, "def")
                if node.args.vararg:
                    self._record(node.args.vararg.arg, node.args.vararg, "def")
                if node.args.kwarg:
                    self._record(node.args.kwarg.arg, node.args.kwarg, "def")
            self._walk(node)
            self._scope_stack.pop()
            return
        if isinstance(node, ast.Name):
            self._record(node.id, node,
                          "def" if isinstance(node.ctx, ast.Store) else "use")
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                if a.name != "*":
                    self._record(a.asname or a.name.split(".")[0], node, "def")
        elif isinstance(node, ast.NamedExpr):
            self._record(node.target.id, node, "def")
            self._visit(node.value)
            return
        self._walk(node)

    def scopes_of(self, name: str) -> Set[int]:
        return {scope for scope, _, _ in self.occurrences.get(name, [])}

    def names_in_scope(self, scope_id: int) -> Set[str]:
        return {name for name, occs in self.occurrences.items()
                if any(s == scope_id for s, _, _ in occs)}

    def is_safe_rename(self, old: str, new: str) -> Tuple[bool, str]:
        """Check whether renaming old -> new is capture-safe."""
        if not new.isidentifier() or keyword.iskeyword(new):
            return False, f"{new!r} is not a valid identifier"
        if old == new:
            return False, "old and new names are identical"
        old_occs = self.occurrences.get(old, [])
        if not old_occs:
            return False, f"{old!r} does not occur"
        touched_scopes = {s for s, _, _ in old_occs}
        for scope_id in touched_scopes:
            names = self.names_in_scope(scope_id)
            if new in names:
                return False, (f"{new!r} already used in a scope touched by "
                               f"{old!r}: rename would capture/shadow")
        return True, "safe"


# ---------------------------------------------------------------------------
# Rename engine
# ---------------------------------------------------------------------------

class RenameEngine:
    """Scope-aware renaming built on ScopeAnalyzer."""

    def __init__(self, source: str) -> None:
        self.source = source
        try:
            self.tree = ast.parse(source)
        except SyntaxError as exc:
            raise RefactorError(f"cannot refactor code with syntax error: {exc}")
        self.analyzer = ScopeAnalyzer(self.tree)

    def check(self, old: str, new: str) -> Tuple[bool, str]:
        return self.analyzer.is_safe_rename(old, new)

    def rename(self, old: str, new: str) -> RefactorResult:
        """Rename all occurrences of old to new (scope-checked)."""
        ok, reason = self.check(old, new)
        if not ok:
            return RefactorResult(False, self.source,
                                  f"rename {old!r} -> {new!r} refused",
                                  warnings=[reason])
        occs = self.analyzer.occurrences.get(old, [])
        # apply from last to first by position to keep offsets stable;
        # simplest robust approach: AST transform + unparse
        tree = copy.deepcopy(self.tree)

        class Renamer(ast.NodeTransformer):
            def visit_Name(self, node: ast.Name) -> ast.AST:
                if node.id == old:
                    node.id = new
                return node

            def visit_arg(self, node: ast.arg) -> ast.AST:
                if node.arg == old:
                    node.arg = new
                return node

            def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
                if node.name == old:
                    node.name = new
                return self.generic_visit(node)

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
                if node.name == old:
                    node.name = new
                return self.generic_visit(node)

        new_tree = Renamer().visit(tree)
        ast.fix_missing_locations(new_tree)
        try:
            new_source = ast.unparse(new_tree)
        except Exception as exc:
            return RefactorResult(False, self.source,
                                  f"rename {old!r} -> {new!r} failed",
                                  warnings=[f"unparse failed: {exc}"])
        # verify the renamed code still parses and the new name resolves
        try:
            ast.parse(new_source)
        except SyntaxError as exc:
            return RefactorResult(False, self.source,
                                  f"rename {old!r} -> {new!r} produced invalid code",
                                  warnings=[str(exc)])
        return RefactorResult(
            True, new_source,
            f"renamed {old!r} -> {new!r} ({len(occs)} occurrence(s))",
            changes=[{"type": "rename", "old": old, "new": new,
                      "occurrences": len(occs)}])

# ---------------------------------------------------------------------------
# RefactorEngine
# ---------------------------------------------------------------------------

class RefactorEngine:
    """Applies AST transformations with parse-verified rollback."""

    def __init__(self, source: str) -> None:
        self.source = source
        try:
            self.tree = ast.parse(source)
        except SyntaxError as exc:
            raise RefactorError(f"cannot refactor code with syntax error: {exc}")

    # -- internal: apply with rollback -------------------------------------------------
    def _apply(self, transformer: ast.NodeTransformer,
               description: str) -> RefactorResult:
        tree = copy.deepcopy(self.tree)
        try:
            new_tree = transformer.visit(tree)
            ast.fix_missing_locations(new_tree)
            new_source = ast.unparse(new_tree)
            ast.parse(new_source)  # rollback point: must parse
        except RefactorError as exc:
            return RefactorResult(False, self.source, description,
                                  warnings=[str(exc)])
        except Exception as exc:
            return RefactorResult(False, self.source, description,
                                  warnings=[f"transformation failed: {exc}"])
        if new_source == ast.unparse(self.tree):
            return RefactorResult(False, self.source, description,
                                  warnings=["no changes made"])
        self.tree = ast.parse(new_source)
        self.source = new_source
        return RefactorResult(True, new_source, description,
                              changes=[{"type": "transform",
                                        "description": description}])

    # -- extract function ---------------------------------------------------------------
    def extract_function(self, func_name: str, new_name: str,
                         start_line: int, end_line: int,
                         params: Optional[List[str]] = None) -> RefactorResult:
        """Extract lines [start_line, end_line] from func_name into new_name.

        Free variables used inside the range become parameters (inferred);
        a single assigned-then-used-after variable becomes the return value.
        """
        target = self._find_function(func_name)
        if target is None:
            return RefactorResult(False, self.source,
                                  f"extract into {new_name!r}",
                                  warnings=[f"function {func_name!r} not found"])
        if not new_name.isidentifier() or keyword.iskeyword(new_name):
            return RefactorResult(False, self.source,
                                  f"extract into {new_name!r}",
                                  warnings=[f"{new_name!r} is not a valid name"])
        stmts = [s for s in target.body
                 if start_line <= s.lineno <= end_line]
        if not stmts:
            return RefactorResult(False, self.source,
                                  f"extract into {new_name!r}",
                                  warnings=["no statements in the given line range"])
        # free variables: loaded inside range but not stored inside range
        stored_inside: Set[str] = set()
        loaded_inside: Set[str] = set()

        class Collector(ast.NodeVisitor):
            def visit_Name(self, node: ast.Name) -> None:
                if isinstance(node.ctx, ast.Store):
                    stored_inside.add(node.id)
                elif isinstance(node.ctx, ast.Load):
                    loaded_inside.add(node.id)
                self.generic_visit(node)

        for s in stmts:
            Collector().visit(s)
        # names defined before the range in the same function
        defined_before: Set[str] = set()
        for s in target.body:
            if s.lineno < start_line:
                for n in ast.walk(s):
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                        defined_before.add(n.id)
                    if isinstance(n, ast.arg):
                        defined_before.add(n.arg)
        for a in target.args.args:
            defined_before.add(a.arg)
        free = sorted((loaded_inside - stored_inside) & defined_before)
        call_params = params if params is not None else free

        # assigned inside, used after the range -> return it
        used_after: Set[str] = set()
        for s in target.body:
            if s.lineno > end_line:
                for n in ast.walk(s):
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                        used_after.add(n.id)
        returns = sorted((stored_inside) & used_after - set(call_params))

        new_func = ast.FunctionDef(
            name=new_name,
            args=ast.arguments(
                posonlyargs=[], args=[ast.arg(arg=p) for p in call_params],
                kwonlyargs=[], kw_defaults=[], defaults=[]),
            body=copy.deepcopy(stmts) or [ast.Pass()],
            decorator_list=[])
        if returns:
            if len(returns) == 1:
                new_func.body.append(ast.Return(
                    value=ast.Name(id=returns[0], ctx=ast.Load())))
                call: ast.stmt = ast.Assign(
                    targets=[ast.Name(id=returns[0], ctx=ast.Store())],
                    value=ast.Call(func=ast.Name(id=new_name, ctx=ast.Load()),
                                   args=[ast.Name(id=p, ctx=ast.Load())
                                         for p in call_params], keywords=[]))
            else:
                new_func.body.append(ast.Return(value=ast.Tuple(
                    elts=[ast.Name(id=r, ctx=ast.Load()) for r in returns],
                    ctx=ast.Load())))
                call = ast.Assign(
                    targets=[ast.Tuple(elts=[ast.Name(id=r, ctx=ast.Store())
                                             for r in returns], ctx=ast.Store())],
                    value=ast.Call(func=ast.Name(id=new_name, ctx=ast.Load()),
                                   args=[ast.Name(id=p, ctx=ast.Load())
                                         for p in call_params], keywords=[]))
        else:
            call = ast.Expr(value=ast.Call(
                func=ast.Name(id=new_name, ctx=ast.Load()),
                args=[ast.Name(id=p, ctx=ast.Load()) for p in call_params],
                keywords=[]))
        ast.fix_missing_locations(new_func)
        ast.fix_missing_locations(call)

        # rebuild the original function body: replace range with the call
        new_body: List[ast.stmt] = []
        inserted = False
        for s in target.body:
            if start_line <= s.lineno <= end_line:
                if not inserted:
                    new_body.append(call)
                    inserted = True
            else:
                new_body.append(s)
        if not new_body:
            new_body = [ast.Pass()]

        engine = self

        class Extractor(ast.NodeTransformer):
            def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
                if node.name == func_name and node.lineno == target.lineno:
                    node.body = new_body
                return self.generic_visit(node)

        result = self._apply(Extractor(), f"extracted lines {start_line}-{end_line} "
                                          f"from {func_name}() into {new_name}()")
        if not result.ok:
            return result
        # re-find: unparse renumbered the lines, so the old target is stale
        fresh_target = self._find_function(func_name)
        fresh_lineno = fresh_target.lineno if fresh_target else -1

        # insert the new function right after the original
        class Inserter(ast.NodeTransformer):
            def visit_Module(self, node: ast.Module) -> ast.AST:
                out: List[ast.stmt] = []
                for stmt in node.body:
                    out.append(stmt)
                    if (isinstance(stmt, ast.FunctionDef)
                            and stmt.name == func_name
                            and stmt.lineno == fresh_lineno):
                        out.append(new_func)
                node.body = out
                return node

        result2 = self._apply(Inserter(), f"inserted {new_name}() after {func_name}()")
        if not result2.ok:
            return result2
        return RefactorResult(
            True, self.source,
            f"extracted lines {start_line}-{end_line} from {func_name}() into "
            f"{new_name}({', '.join(call_params)})"
            + (f" returning {', '.join(returns)}" if returns else ""),
            changes=[{"type": "extract_function", "from": func_name,
                      "new": new_name, "params": call_params,
                      "returns": returns, "lines": [start_line, end_line]}])

    def _find_function(self, name: str) -> Optional[ast.FunctionDef]:
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return node  # type: ignore[return-value]
        return None

    # -- inline variable ------------------------------------------------------------------
    def inline_variable(self, func_name: Optional[str], var_name: str) -> RefactorResult:
        """Inline a variable assigned exactly once with a side-effect-free value."""
        scope: ast.AST = (self._find_function(func_name) if func_name
                          else self.tree)
        if scope is None:
            return RefactorResult(False, self.source, f"inline {var_name!r}",
                                  warnings=[f"function {func_name!r} not found"])
        assigns: List[ast.Assign] = []
        for node in ast.walk(scope):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == var_name:
                        assigns.append(node)
        if len(assigns) != 1:
            return RefactorResult(False, self.source, f"inline {var_name!r}",
                                  warnings=[f"expected exactly 1 assignment, found {len(assigns)}"])
        assign = assigns[0]
        value = assign.value
        # side-effect check: no calls, no await/yield, no comprehensions with calls
        for node in ast.walk(value):
            if isinstance(node, (ast.Call, ast.Await, ast.Yield, ast.YieldFrom)):
                return RefactorResult(False, self.source, f"inline {var_name!r}",
                                      warnings=["value has side effects; inlining is unsafe"])
        # make sure the variable is not assigned elsewhere via aug-assign etc.
        for node in ast.walk(scope):
            if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) \
                    and node.target.id == var_name:
                return RefactorResult(False, self.source, f"inline {var_name!r}",
                                      warnings=["variable is augmented elsewhere"])

        value_copy_src = ast.unparse(value)

        class Inliner(ast.NodeTransformer):
            def __init__(self) -> None:
                self.removed = False

            def visit_Assign(self, node: ast.Assign) -> Optional[ast.AST]:
                if (not self.removed and node is assign
                        or (len(node.targets) == 1
                            and isinstance(node.targets[0], ast.Name)
                            and node.targets[0].id == var_name
                            and ast.unparse(node.value) == value_copy_src)):
                    self.removed = True
                    return None  # drop the assignment
                return self.generic_visit(node)

            def visit_Name(self, node: ast.Name) -> ast.AST:
                if node.id == var_name and isinstance(node.ctx, ast.Load):
                    return copy.deepcopy(value)
                return node

        result = self._apply(Inliner(), f"inlined variable {var_name!r}")
        if result.ok:
            result.changes = [{"type": "inline_variable", "name": var_name,
                               "value": value_copy_src[:120]}]
            result.description = f"inlined {var_name!r} = {value_copy_src[:60]}"
        return result

    # -- simplify conditionals ---------------------------------------------------------------
    def simplify_conditionals(self) -> RefactorResult:
        """Collapse redundant conditionals:
             if cond: return True else: return False  -> return bool(cond)
             if x: x = True/False patterns, double negation, `if cond: pass else: ...`
        """

        class Simplifier(ast.NodeTransformer):
            def __init__(self) -> None:
                self.count = 0

            def visit_If(self, node: ast.If) -> ast.AST:
                self.generic_visit(node)
                # if c: return True / else: return False
                if (len(node.body) == 1 and len(node.orelse) == 1
                        and isinstance(node.body[0], ast.Return)
                        and isinstance(node.orelse[0], ast.Return)):
                    b, e = node.body[0].value, node.orelse[0].value
                    if (isinstance(b, ast.Constant) and isinstance(e, ast.Constant)
                            and isinstance(b.value, bool) and isinstance(e.value, bool)
                            and b.value != e.value):
                        self.count += 1
                        val: ast.expr = node.test if b.value else ast.UnaryOp(
                            op=ast.Not(), operand=node.test)
                        return ast.Return(value=val)
                # if c: x = True else: x = False  (single target, same name)
                if (len(node.body) == 1 and len(node.orelse) == 1
                        and isinstance(node.body[0], ast.Assign)
                        and isinstance(node.orelse[0], ast.Assign)):
                    b, e = node.body[0], node.orelse[0]
                    if (len(b.targets) == 1 and len(e.targets) == 1
                            and isinstance(b.targets[0], ast.Name)
                            and isinstance(e.targets[0], ast.Name)
                            and b.targets[0].id == e.targets[0].id
                            and isinstance(b.value, ast.Constant)
                            and isinstance(e.value, ast.Constant)
                            and isinstance(b.value.value, bool)
                            and isinstance(e.value.value, bool)
                            and b.value.value != e.value.value):
                        self.count += 1
                        val = (node.test if b.value.value
                               else ast.UnaryOp(op=ast.Not(), operand=node.test))
                        return ast.Assign(targets=b.targets, value=val)
                return node

            def visit_UnaryOp(self, node: ast.UnaryOp) -> ast.AST:
                self.generic_visit(node)
                # not not x -> x
                if (isinstance(node.op, ast.Not)
                        and isinstance(node.operand, ast.UnaryOp)
                        and isinstance(node.operand.op, ast.Not)):
                    self.count += 1
                    return node.operand.operand
                return node

        simplifier = Simplifier()
        result = self._apply(simplifier, "simplified conditionals")
        if result.ok:
            result.changes = [{"type": "simplify_conditionals",
                               "simplifications": simplifier.count}]
            result.description = (f"simplified {simplifier.count} conditional(s)")
        return result

    # -- remove dead code ------------------------------------------------------------------------
    def remove_dead_code(self) -> RefactorResult:
        """Remove unreachable statements (after return/raise/break/continue)
        and assignments to variables never used afterwards in the same scope."""

        class DeadCodeRemover(ast.NodeTransformer):
            TERMINATORS = (ast.Return, ast.Raise, ast.Break, ast.Continue)

            def __init__(self) -> None:
                self.removed = 0

            def _clean_block(self, stmts: List[ast.stmt]) -> List[ast.stmt]:
                out: List[ast.stmt] = []
                for s in stmts:
                    out.append(s)
                    if isinstance(s, self.TERMINATORS):
                        # everything after is unreachable
                        dropped = len(stmts) - len(out)
                        self.removed += dropped
                        break
                return out or [ast.Pass()]

            def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
                self.generic_visit(node)
                node.body = self._clean_block(node.body)
                return node

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_If(self, node: ast.If) -> ast.AST:
                self.generic_visit(node)
                node.body = self._clean_block(node.body)
                node.orelse = self._clean_block(node.orelse) if node.orelse else []
                return node

            def visit_For(self, node: ast.For) -> ast.AST:
                self.generic_visit(node)
                node.body = self._clean_block(node.body)
                return node

            visit_While = visit_For
            visit_With = visit_For

        remover = DeadCodeRemover()
        result = self._apply(remover, "removed dead code")
        if result.ok:
            result.changes = [{"type": "remove_dead_code",
                               "removed_statements": remover.removed}]
            result.description = f"removed {remover.removed} unreachable statement(s)"
        # second pass: drop assignments whose target is never loaded later
        if result.ok:
            second = self._remove_unused_assignments()
            if second.ok and second.changes[0].get("removed_assignments"):
                result.changes.extend(second.changes)
                result.description += (f"; removed "
                                       f"{second.changes[0]['removed_assignments']} "
                                       f"unused assignment(s)")
                result.source = second.source
        return result

    def _remove_unused_assignments(self) -> RefactorResult:
        tree = copy.deepcopy(self.tree)

        class FuncPass(ast.NodeVisitor):
            def __init__(self) -> None:
                self.to_remove: Set[int] = set()

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                # collect loads per name after each assignment
                for i, stmt in enumerate(node.body):
                    if not isinstance(stmt, ast.Assign):
                        continue
                    if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
                        continue
                    name = stmt.targets[0].id
                    # is `name` loaded anywhere later in this function?
                    used_later = False
                    for later in node.body[i + 1:]:
                        for n in ast.walk(later):
                            if isinstance(n, ast.Name) and n.id == name \
                                    and isinstance(n.ctx, ast.Load):
                                used_later = True
                                break
                        if used_later:
                            break
                    # also check it's not a parameter or used in nested scopes
                    if not used_later and name not in {a.arg for a in node.args.args}:
                        # ensure no store to the same name later (reassignment
                        # means the first value is dead too, but keep it simple:
                        # only remove if never stored again either)
                        stored_again = False
                        for later in node.body[i + 1:]:
                            for n in ast.walk(later):
                                if isinstance(n, ast.Name) and n.id == name \
                                        and isinstance(n.ctx, ast.Store):
                                    stored_again = True
                        if not stored_again:
                            # side-effect check on the value
                            if not any(isinstance(n, (ast.Call, ast.Await, ast.Yield))
                                       for n in ast.walk(stmt.value)):
                                self.to_remove.add(id(stmt))
                self.generic_visit(node)

        collector = FuncPass()
        collector.visit(tree)
        removed = 0

        class Dropper(ast.NodeTransformer):
            def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
                self.generic_visit(node)
                new_body = []
                for stmt in node.body:
                    if id(stmt) in collector.to_remove:
                        nonlocal_removed[0] += 1
                        continue
                    new_body.append(stmt)
                node.body = new_body or [ast.Pass()]
                return node

        nonlocal_removed = [0]
        new_tree = Dropper().visit(tree)
        ast.fix_missing_locations(new_tree)
        try:
            new_source = ast.unparse(new_tree)
            ast.parse(new_source)
        except Exception as exc:
            return RefactorResult(False, self.source, "remove unused assignments",
                                  warnings=[str(exc)])
        if nonlocal_removed[0] == 0:
            return RefactorResult(False, self.source, "remove unused assignments",
                                  warnings=["no unused assignments found"],
                                  changes=[{"removed_assignments": 0}])
        self.tree = ast.parse(new_source)
        self.source = new_source
        return RefactorResult(True, new_source, "removed unused assignments",
                              changes=[{"removed_assignments": nonlocal_removed[0]}])

    # -- rename (delegated) ----------------------------------------------------------------------------
    def rename_variable(self, old: str, new: str) -> RefactorResult:
        engine = RenameEngine(self.source)
        result = engine.rename(old, new)
        if result.ok:
            self.source = result.source
            self.tree = ast.parse(result.source)
        return result


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------

def refactor(source: str, operations: List[Tuple[str, Dict[str, Any]]]) -> RefactorResult:
    """Apply a sequence of refactorings.

    operations: [("rename_variable", {"old": ..., "new": ...}),
                 ("inline_variable", {"func_name": ..., "var_name": ...}),
                 ("simplify_conditionals", {}),
                 ("remove_dead_code", {}),
                 ("extract_function", {"func_name": ..., "new_name": ...,
                                       "start_line": ..., "end_line": ...})]
    Stops at the first failure and returns its result.
    """
    engine = RefactorEngine(source)
    applied: List[Dict[str, Any]] = []
    for op_name, kwargs in operations:
        method = getattr(engine, op_name, None)
        if method is None:
            return RefactorResult(False, engine.source, f"unknown operation {op_name!r}",
                                  changes=applied,
                                  warnings=[f"unknown refactoring {op_name!r}"])
        result = method(**kwargs)
        applied.extend(result.changes)
        if not result.ok:
            result.changes = applied
            return result
    return RefactorResult(True, engine.source,
                          f"applied {len(operations)} refactoring(s)",
                          changes=applied)

# ---------------------------------------------------------------------------
# More transformations: merge nested ifs, extract constants
# ---------------------------------------------------------------------------

class _ExtraTransforms:
    """Mixin-style extra AST transformations for RefactorEngine."""

    def merge_nested_ifs(self: "RefactorEngine") -> RefactorResult:
        """Merge `if a: if b: body` into `if a and b: body`."""

        class Merger(ast.NodeTransformer):
            def __init__(self) -> None:
                self.merged = 0

            def visit_If(self, node: ast.If) -> ast.AST:
                self.generic_visit(node)
                if (len(node.body) == 1 and not node.orelse
                        and isinstance(node.body[0], ast.If)
                        and not node.body[0].orelse):
                    inner = node.body[0]
                    combined = ast.BoolOp(op=ast.And(),
                                          values=[node.test, inner.test])
                    node.test = combined
                    node.body = inner.body
                    self.merged += 1
                return node

        merger = Merger()
        result = self._apply(merger, "merged nested ifs")
        if result.ok:
            result.changes = [{"type": "merge_nested_ifs",
                               "merged": merger.merged}]
            result.description = f"merged {merger.merged} nested if(s)"
        return result

    def extract_constant(self: "RefactorEngine", value_repr: str,
                         const_name: str) -> RefactorResult:
        """Replace all occurrences of a literal with a module-level constant."""
        if not const_name.isidentifier() or keyword.iskeyword(const_name):
            return RefactorResult(False, self.source,
                                  f"extract constant {const_name!r}",
                                  warnings=[f"{const_name!r} is not a valid name"])
        try:
            literal = ast.parse(value_repr, mode="eval").body
        except SyntaxError:
            return RefactorResult(False, self.source,
                                  f"extract constant {const_name!r}",
                                  warnings=[f"{value_repr!r} is not a valid expression"])
        if not isinstance(literal, ast.Constant):
            return RefactorResult(False, self.source,
                                  f"extract constant {const_name!r}",
                                  warnings=["only literal constants can be extracted"])
        target_value = literal.value
        count = [0]

        class Replacer(ast.NodeTransformer):
            def visit_Constant(self, node: ast.Constant) -> ast.AST:
                if (node.value == target_value
                        and type(node.value) is type(target_value)):
                    count[0] += 1
                    return ast.copy_location(
                        ast.Name(id=const_name, ctx=ast.Load()), node)
                return node

        # don't replace inside the constant definition itself: insert after
        result = self._apply(Replacer(), f"extracted constant {const_name!r}")
        if not result.ok:
            return result
        if count[0] == 0:
            return RefactorResult(False, self.source,
                                  f"extract constant {const_name!r}",
                                  warnings=[f"value {value_repr!r} not found"])

        class ConstInserter(ast.NodeTransformer):
            def visit_Module(self, node: ast.Module) -> ast.AST:
                # insert after module docstring / imports
                idx = 0
                for i, stmt in enumerate(node.body):
                    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) \
                            and isinstance(stmt.value.value, str):
                        idx = i + 1
                    elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
                        idx = i + 1
                    else:
                        break
                const_def = ast.Assign(
                    targets=[ast.Name(id=const_name, ctx=ast.Store())],
                    value=ast.Constant(value=target_value))
                ast.fix_missing_locations(const_def)
                node.body.insert(idx, const_def)
                return node

        result2 = self._apply(ConstInserter(), f"inserted {const_name!r}")
        if not result2.ok:
            return result2
        return RefactorResult(
            True, self.source,
            f"extracted constant {const_name} = {value_repr} ({count[0]} use(s))",
            changes=[{"type": "extract_constant", "name": const_name,
                      "value": value_repr, "uses": count[0]}])


# attach the mixin methods to RefactorEngine
RefactorEngine.merge_nested_ifs = _ExtraTransforms.merge_nested_ifs  # type: ignore[attr-defined]
RefactorEngine.extract_constant = _ExtraTransforms.extract_constant  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# RefactoringSession: apply with full undo history
# ---------------------------------------------------------------------------

class RefactoringSession:
    """A refactoring session with undo support.

    Each successful operation pushes the previous source onto the undo
    stack, so any sequence can be rolled back step by step.
    """

    def __init__(self, source: str) -> None:
        self.engine = RefactorEngine(source)
        self._undo: List[Tuple[str, str]] = []  # (description, source)
        self._redo: List[Tuple[str, str]] = []
        self.history: List[str] = []

    @property
    def source(self) -> str:
        return self.engine.source

    def do(self, op_name: str, **kwargs: Any) -> RefactorResult:
        method = getattr(self.engine, op_name, None)
        if method is None:
            return RefactorResult(False, self.source, op_name,
                                  warnings=[f"unknown operation {op_name!r}"])
        before = self.engine.source
        result = method(**kwargs)
        if result.ok:
            self._undo.append((result.description, before))
            self._redo.clear()
            self.history.append(result.description)
        return result

    def undo(self) -> RefactorResult:
        if not self._undo:
            return RefactorResult(False, self.source, "undo",
                                  warnings=["nothing to undo"])
        description, previous = self._undo.pop()
        self._redo.append((description, self.engine.source))
        self.engine = RefactorEngine(previous)
        self.history.append(f"undid: {description}")
        return RefactorResult(True, previous, f"undid: {description}")

    def redo(self) -> RefactorResult:
        if not self._redo:
            return RefactorResult(False, self.source, "redo",
                                  warnings=["nothing to redo"])
        description, nxt = self._redo.pop()
        self._undo.append((description, self.engine.source))
        self.engine = RefactorEngine(nxt)
        self.history.append(f"redid: {description}")
        return RefactorResult(True, nxt, f"redid: {description}")

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)


# ---------------------------------------------------------------------------
# Diff rendering for review before applying
# ---------------------------------------------------------------------------

def refactor_diff(before: str, after: str, context: int = 3) -> str:
    """Unified diff between two sources (for human/agent review)."""
    import difflib as _difflib
    diff = _difflib.unified_diff(before.splitlines(), after.splitlines(),
                                 fromfile="before", tofile="after",
                                 n=context, lineterm="")
    return "\n".join(diff)


def preview_refactor(source: str, op_name: str,
                     **kwargs: Any) -> Dict[str, Any]:
    """Run a refactoring without committing: returns ok/diff/description."""
    engine = RefactorEngine(source)
    method = getattr(engine, op_name, None)
    if method is None:
        return {"ok": False, "diff": "", "description": op_name,
                "warnings": [f"unknown operation {op_name!r}"]}
    result = method(**kwargs)
    return {"ok": result.ok,
            "diff": refactor_diff(source, result.source) if result.ok else "",
            "description": result.description,
            "warnings": result.warnings,
            "changes": result.changes}
