"""Error analyzer — DEBUG phase: classify errors, find root causes.

Analyzes Python tracebacks, shell errors, and tool failures.
No personality. No rules. Direct execution.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class ErrorCategory(Enum):
    SYNTAX = "syntax"
    IMPORT = "import"
    NAME = "name"
    TYPE = "type"
    VALUE = "value"
    ATTRIBUTE = "attribute"
    INDEX = "index"
    KEY = "key"
    FILE_NOT_FOUND = "file_not_found"
    PERMISSION = "permission"
    NETWORK = "network"
    TIMEOUT = "timeout"
    POLICY_DENIED = "policy_denied"
    TOOL_UNKNOWN = "tool_unknown"
    SCHEMA = "schema"
    GIT = "git"
    UNKNOWN = "unknown"


@dataclass
class StackFrame:
    filename: str
    lineno: int
    function: str
    code: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"filename": self.filename, "lineno": self.lineno,
                "function": self.function, "code": self.code}


@dataclass
class ErrorAnalysis:
    """Structured analysis of an error."""
    category: ErrorCategory
    exception_type: str
    message: str
    frames: List[StackFrame] = field(default_factory=list)
    root_cause: str = ""
    suspect_lines: List[Tuple[str, int]] = field(default_factory=list)
    suggested_fix: str = ""
    confidence: float = 0.5
    retriable: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category.value,
            "exception_type": self.exception_type,
            "message": self.message,
            "frames": [f.to_dict() for f in self.frames],
            "root_cause": self.root_cause,
            "suspect_lines": self.suspect_lines,
            "suggested_fix": self.suggested_fix,
            "confidence": self.confidence,
            "retriable": self.retriable,
        }


# Map exception names to categories
_EXCEPTION_CATEGORIES = {
    "SyntaxError": ErrorCategory.SYNTAX,
    "IndentationError": ErrorCategory.SYNTAX,
    "TabError": ErrorCategory.SYNTAX,
    "ImportError": ErrorCategory.IMPORT,
    "ModuleNotFoundError": ErrorCategory.IMPORT,
    "NameError": ErrorCategory.NAME,
    "TypeError": ErrorCategory.TYPE,
    "ValueError": ErrorCategory.VALUE,
    "AttributeError": ErrorCategory.ATTRIBUTE,
    "IndexError": ErrorCategory.INDEX,
    "KeyError": ErrorCategory.KEY,
    "FileNotFoundError": ErrorCategory.FILE_NOT_FOUND,
    "PermissionError": ErrorCategory.PERMISSION,
    "TimeoutError": ErrorCategory.TIMEOUT,
    "ConnectionError": ErrorCategory.NETWORK,
}


class ErrorAnalyzer:
    """Analyzes error output into structured ErrorAnalysis."""

    # Traceback frame pattern
    _FRAME_RE = re.compile(
        r'^\s*File "([^"]+)", line (\d+), in (\S+)\s*$')
    _EXC_RE = re.compile(r"^(\w+(?:\.\w+)*(?:Error|Exception|Warning)):\s*(.*)$")

    def analyze(self, output: str,
                context: Optional[Dict[str, Any]] = None) -> ErrorAnalysis:
        """Analyze tool output (stderr or combined) for errors."""
        context = context or {}
        # 1. Policy denial?
        if "denied by policy" in output.lower():
            return ErrorAnalysis(
                category=ErrorCategory.POLICY_DENIED,
                exception_type="PolicyDenied",
                message=self._first_line(output),
                root_cause="Tool call matched a hard denial pattern",
                suggested_fix="Use a different approach that does not "
                              "trigger the denial",
                confidence=0.95, retriable=False)
        # 2. Python traceback?
        tb = self._parse_traceback(output)
        if not tb:
            # Try bare exception line (no traceback header)
            tb = self._parse_bare_exception(output)
        if tb:
            exc_type, message, frames = tb
            category = _EXCEPTION_CATEGORIES.get(
                exc_type.split(".")[-1], ErrorCategory.UNKNOWN)
            analysis = ErrorAnalysis(
                category=category, exception_type=exc_type,
                message=message, frames=frames)
            self._enrich_python(analysis)
            return analysis
        # 3. Shell errors
        shell = self._analyze_shell(output)
        if shell:
            return shell
        # 4. Tool errors
        if "unknown or disabled tool" in output.lower():
            return ErrorAnalysis(
                category=ErrorCategory.TOOL_UNKNOWN,
                exception_type="ToolUnknown",
                message=self._first_line(output),
                root_cause="Tool name not in registry",
                suggested_fix="Check tool name spelling or generate the tool",
                confidence=0.9, retriable=False)
        # 5. Timeout
        if "timeout" in output.lower() or "timed out" in output.lower():
            return ErrorAnalysis(
                category=ErrorCategory.TIMEOUT,
                exception_type="Timeout",
                message=self._first_line(output),
                root_cause="Operation exceeded time budget",
                suggested_fix="Increase timeout or split into smaller steps",
                confidence=0.85, retriable=True)
        # Fallback
        return ErrorAnalysis(
            category=ErrorCategory.UNKNOWN,
            exception_type="Unknown",
            message=self._first_line(output)[:300],
            root_cause="Could not classify error",
            suggested_fix="Inspect full output manually",
            confidence=0.2, retriable=True)

    def _first_line(self, text: str) -> str:
        for line in text.splitlines():
            if line.strip():
                return line.strip()[:300]
        return ""

    def _parse_traceback(self, output: str
                         ) -> Optional[Tuple[str, str, List[StackFrame]]]:
        lines = output.splitlines()
        frames: List[StackFrame] = []
        exc_type = ""
        message = ""
        in_tb = False
        pending_frame: Optional[Dict[str, Any]] = None
        for i, line in enumerate(lines):
            if "Traceback (most recent call last):" in line:
                in_tb = True
                continue
            if in_tb:
                m = self._FRAME_RE.match(line)
                if m:
                    if pending_frame:
                        frames.append(StackFrame(**pending_frame))
                    pending_frame = {
                        "filename": m.group(1),
                        "lineno": int(m.group(2)),
                        "function": m.group(3),
                        "code": "",
                    }
                    continue
                if pending_frame and line.strip() and not line.startswith(" " * 4):
                    # Code line follows frame header (indented)
                    pass
                m2 = self._EXC_RE.match(line.strip())
                if m2 and not line.startswith(" "):
                    if pending_frame:
                        frames.append(StackFrame(**pending_frame))
                        pending_frame = None
                    exc_type = m2.group(1)
                    message = m2.group(2)
                    in_tb = False
        if exc_type:
            return exc_type, message, frames
        return None

    def _parse_bare_exception(self, output: str
                                ) -> Optional[Tuple[str, str, List[StackFrame]]]:
        """Parse a bare 'ExceptionType: message' line without traceback."""
        for line in output.splitlines():
            m = self._EXC_RE.match(line.strip())
            if m:
                return m.group(1), m.group(2), []
        return None

    def _enrich_python(self, analysis: ErrorAnalysis) -> None:
        """Add root cause and fix suggestions for Python errors."""
        msg = analysis.message
        exc = analysis.exception_type
        if "ModuleNotFoundError" in exc or "ImportError" in exc:
            mod = self._extract_quoted(msg) or "module"
            analysis.root_cause = f"Missing module: {mod}"
            analysis.suggested_fix = f"pip install {mod} (or check name)"
            analysis.confidence = 0.9
        elif "NameError" in exc:
            name = self._extract_quoted(msg) or "name"
            analysis.root_cause = f"Undefined name: {name}"
            analysis.suggested_fix = (
                f"Define '{name}' before use; check for typos or "
                f"missing imports")
            analysis.confidence = 0.85
            if analysis.frames:
                f = analysis.frames[-1]
                analysis.suspect_lines = [(f.filename, f.lineno)]
        elif "SyntaxError" in exc:
            analysis.root_cause = "Invalid Python syntax"
            analysis.suggested_fix = "Check brackets, colons, indentation"
            analysis.confidence = 0.9
            if analysis.frames:
                f = analysis.frames[-1]
                analysis.suspect_lines = [(f.filename, f.lineno)]
        elif "TypeError" in exc:
            analysis.root_cause = f"Type mismatch: {msg[:120]}"
            analysis.suggested_fix = "Check argument types and count"
            analysis.confidence = 0.7
        elif "FileNotFoundError" in exc:
            path = self._extract_quoted(msg) or "file"
            analysis.root_cause = f"Missing file: {path}"
            analysis.suggested_fix = "Check path spelling and cwd"
            analysis.confidence = 0.9
        elif "AttributeError" in exc:
            analysis.root_cause = f"Bad attribute: {msg[:120]}"
            analysis.suggested_fix = "Check object type and attribute name"
            analysis.confidence = 0.7
        elif "KeyError" in exc:
            key = self._extract_quoted(msg) or "key"
            analysis.root_cause = f"Missing dict key: {key}"
            analysis.suggested_fix = "Use .get() or check key exists"
            analysis.confidence = 0.85
        elif "IndexError" in exc:
            analysis.root_cause = "Index out of range"
            analysis.suggested_fix = "Check list length before indexing"
            analysis.confidence = 0.85

    def _extract_quoted(self, text: str) -> Optional[str]:
        m = re.search(r"'([^']+)'", text)
        return m.group(1) if m else None

    def _analyze_shell(self, output: str) -> Optional[ErrorAnalysis]:
        low = output.lower()
        # Command not found
        m = re.search(r"([\w-]+): command not found", output)
        if m:
            return ErrorAnalysis(
                category=ErrorCategory.VALUE,
                exception_type="CommandNotFound",
                message=f"command not found: {m.group(1)}",
                root_cause=f"Missing executable: {m.group(1)}",
                suggested_fix=f"Install {m.group(1)} or check PATH",
                confidence=0.9, retriable=False)
        # Permission denied (filesystem)
        if "permission denied" in low and "policy" not in low:
            return ErrorAnalysis(
                category=ErrorCategory.PERMISSION,
                exception_type="PermissionError",
                message=self._first_line(output),
                root_cause="Filesystem permission denied",
                suggested_fix="Check file ownership/permissions",
                confidence=0.8, retriable=False)
        # No such file
        m = re.search(r"(?:no such file|cannot stat)[^\n]*", output,
                      re.IGNORECASE)
        if m:
            return ErrorAnalysis(
                category=ErrorCategory.FILE_NOT_FOUND,
                exception_type="FileNotFound",
                message=m.group(0)[:200],
                root_cause="Path does not exist",
                suggested_fix="Verify the path",
                confidence=0.85, retriable=False)
        # Git errors
        if "fatal:" in low and "git" in low:
            return ErrorAnalysis(
                category=ErrorCategory.GIT,
                exception_type="GitError",
                message=self._first_line(output),
                root_cause="Git operation failed",
                suggested_fix="Check git status and repo state",
                confidence=0.7, retriable=True)
        return None


class ErrorClassifier:
    """Groups similar errors for learning."""

    def __init__(self):
        self._analyzer = ErrorAnalyzer()

    def fingerprint(self, output: str) -> str:
        """Stable fingerprint for an error (groups duplicates)."""
        import hashlib
        analysis = self._analyzer.analyze(output)
        # Normalize: category + exception type + message skeleton
        skeleton = re.sub(r"'[^']*'", "'X'", analysis.message)
        skeleton = re.sub(r"\d+", "N", skeleton)
        key = f"{analysis.category.value}:{analysis.exception_type}:{skeleton}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    def group(self, outputs: List[str]) -> Dict[str, List[int]]:
        """Group error output indices by fingerprint."""
        groups: Dict[str, List[int]] = {}
        for i, out in enumerate(outputs):
            fp = self.fingerprint(out)
            groups.setdefault(fp, []).append(i)
        return groups
