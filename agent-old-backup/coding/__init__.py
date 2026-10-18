"""Code generation, review, and refactoring for the agent.

These modules power the agent's ability to write, check, and improve its
own Python code as part of the control loop (EXECUTE -> DEBUG -> TEST ->
IMPROVE). All output is real, parseable Python validated with ast.parse;
nothing is a stub.

Modules:
    code_generator:  spec-driven code generation, templates, AST analysis,
                     import management, complexity estimation
    code_reviewer:   AST-based linting (unused imports, undefined names,
                     bare except, mutable defaults, security issues, complexity)
    refactor_engine: AST transformations (extract function, rename, inline,
                     simplify conditionals, dead-code removal)
"""

from .code_generator import (
    CodeGenerator,
    CodeSpec,
    FunctionSpec,
    ClassSpec,
    ScriptSpec,
    TemplateLibrary,
    ImportManager,
    ComplexityEstimator,
    CodeAnalyzer,
)
from .code_reviewer import (
    CodeReviewer,
    ReviewFinding,
    ReviewReport,
    FindingSeverity,
    FindingCategory,
)
from .refactor_engine import (
    RefactorEngine,
    RefactorResult,
    ScopeAnalyzer,
    RenameEngine,
)
from .repository_reader import RepositoryReader, RepoMap, ModuleInfo
from .patch_manager import PatchManager, Patch, PatchStatus
from .diff_checker import (
    DiffChecker, FileDiff, DiffSummary, ChangeType, ChangeRisk,
)
from .test_runner import (
    TestRunner, TestRunResult, TestCaseResult, TestStatus,
)
from .lint_runner import (
    LintRunner, LintResult, LintIssue, LintSeverity,
)

__all__ = [
    # code_generator
    "CodeGenerator",
    "CodeSpec",
    "FunctionSpec",
    "ClassSpec",
    "ScriptSpec",
    "TemplateLibrary",
    "ImportManager",
    "ComplexityEstimator",
    "CodeAnalyzer",
    # code_reviewer
    "CodeReviewer",
    "ReviewFinding",
    "ReviewReport",
    "FindingSeverity",
    "FindingCategory",
    # refactor_engine
    "RefactorEngine",
    "RefactorResult",
    "ScopeAnalyzer",
    "RenameEngine",
    # repository_reader
    "RepositoryReader",
    "RepoMap",
    "ModuleInfo",
    # patch_manager
    "PatchManager",
    "Patch",
    "PatchStatus",
    # diff_checker
    "DiffChecker",
    "FileDiff",
    "DiffSummary",
    "ChangeType",
    "ChangeRisk",
    # test_runner
    "TestRunner",
    "TestRunResult",
    "TestCaseResult",
    "TestStatus",
    # lint_runner
    "LintRunner",
    "LintResult",
    "LintIssue",
    "LintSeverity",
]
