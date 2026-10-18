"""Result verification for the agent control loop.

This module implements the VERIFY stage: every tool result is validated,
sanity-checked, cross-referenced, and scored with a confidence value in
[0.0, 1.0] before the agent may act on it. The agent MUST use tools and
MUST NOT fabricate output; verification is the enforcement mechanism.

Architecture:
    ResultVerifier            orchestrator; runs strategy chain, scores confidence
    VerificationStrategy      base class for per-tool-type strategies
    ShellOutputVerifier       shell command output (exit codes, error patterns)
    PythonOutputVerifier      python execution output (tracebacks, JSON, return values)
    WebContentVerifier        web fetches (HTTP errors, page-content sanity)
    FileOperationVerifier     file writes/reads (existence, size, checksum)
    SchemaValidator           JSON-schema-like validation with detailed errors
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from ipaddress import ip_address
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Regex catalog: patterns that indicate an error / failure in tool output
# ---------------------------------------------------------------------------

ERROR_PATTERNS: List[Tuple[str, re.Pattern, float]] = [
    # (name, compiled pattern, severity weight 0..1)
    ("python_traceback", re.compile(r"^Traceback \(most recent call last\):", re.M), 1.0),
    ("python_exception", re.compile(r"^\w*(?:Error|Exception|Warning)\s*:", re.M), 0.9),
    ("generic_error_prefix", re.compile(r"(?i)^(?:error|fatal|failed|failure)\s*[:\-]", re.M), 0.9),
    ("command_not_found", re.compile(r"(?i)(?:command not found|not recognized as an internal|no such file or directory|cannot find the file)", re.M), 0.9),
    ("permission_denied", re.compile(r"(?i)(?:permission denied|access is denied|eacces|operation not permitted)", re.M), 0.9),
    ("http_error", re.compile(r"(?i)\b(?:404|500|502|503|504)\b\s*(?:not found|internal server error|bad gateway|service unavailable|gateway timeout)?", re.M), 0.8),
    ("http_error_prefix", re.compile(r"(?i)^HTTP/\d\.\d\s+[45]\d\d\b", re.M), 0.95),
    ("timeout", re.compile(r"(?i)(?:timed out|timeout expired|deadline exceeded|operation timed out)", re.M), 0.8),
    ("segfault", re.compile(r"(?i)(?:segmentation fault|core dumped|bus error|abort(?:ed)?)", re.M), 0.95),
    ("oom", re.compile(r"(?i)(?:out of memory|memoryerror|killed\b.*\b(?:9|oom))", re.M), 0.85),
    ("network_unreachable", re.compile(r"(?i)(?:network is unreachable|connection refused|connection reset|name resolution|dns|getaddrinfo failed|no route to host)", re.M), 0.85),
    ("null_undefined", re.compile(r"(?i)\b(?:undefined|null|none)\b.*\b(?:returned|received|got)\b", re.M), 0.6),
    ("empty_result_marker", re.compile(r"(?i)^(?:no results?|not found|empty|nil|null)\s*$", re.M), 0.5),
    ("stack_overflow", re.compile(r"(?i)(?:stack overflow|recursionerror|maximum recursion)", re.M), 0.9),
    ("syntax_error", re.compile(r"(?i)(?:syntaxerror|invalid syntax|unexpected token|parse error)", re.M), 0.9),
    ("assertion_failed", re.compile(r"(?i)(?:assertionerror|assert.*failed|test failed|\bFAILED\b)", re.M), 0.85),
    ("panic", re.compile(r"(?i)(?:^panic:|\brustc\b.*panicked|goroutine.*panic)", re.M), 0.95),
]

# Suspect patterns: output that smells like fabrication or a placeholder.
FABRICATION_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("lorem_ipsum", re.compile(r"(?i)lorem ipsum")),
    ("placeholder_brackets", re.compile(r"\[(?:TODO|FIXME|TBD|XXX)[^\]\n]{0,80}\]", re.I)),
    ("placeholder_braces", re.compile(r"\{\{(?:todo|fixme|tbd|xxx)[^}\n]{0,80}\}\}", re.I)),
    ("ellipsis_truncation", re.compile(r"\.\.\.\s*$", re.M)),
    ("fake_timestamp_1970", re.compile(r"1970-01-01[ T]00:00:0?0")),
    ("example_domain", re.compile(r"(?i)\bexample\.(?:com|org|net)\b")),
]

# Output that is exactly empty / whitespace-only.
EMPTY_OUTPUT_MARKERS = {"", " ", "null", "none", "undefined", "{}", "[]", '""', "''"}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass
class CheckResult:
    """Outcome of a single verification check."""
    name: str
    passed: bool
    confidence: float
    message: str
    severity: Severity = Severity.INFO
    details: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.confidence = max(0.0, min(1.0, float(self.confidence)))


@dataclass
class VerificationReport:
    """Full verification report for one tool result."""
    passed: bool
    confidence: float
    checks: List[CheckResult] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    evidence: Dict[str, Any] = field(default_factory=dict)
    tool_name: str = ""
    verified_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")

    def __post_init__(self) -> None:
        self.confidence = max(0.0, min(1.0, float(self.confidence)))

    @property
    def failed_checks(self) -> List[CheckResult]:
        return [c for c in self.checks if not c.passed]

    @property
    def critical_failures(self) -> List[CheckResult]:
        return [c for c in self.checks if not c.passed and c.severity == Severity.CRITICAL]

    def summary(self) -> str:
        total = len(self.checks)
        ok = sum(1 for c in self.checks if c.passed)
        return (
            f"Verification {'PASSED' if self.passed else 'FAILED'} "
            f"(confidence {self.confidence:.2f}): {ok}/{total} checks passed, "
            f"{len(self.warnings)} warning(s)."
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "confidence": self.confidence,
            "tool_name": self.tool_name,
            "verified_at": self.verified_at,
            "checks": [
                {
                    "name": c.name,
                    "passed": c.passed,
                    "confidence": c.confidence,
                    "message": c.message,
                    "severity": c.severity.value,
                    "details": c.details,
                }
                for c in self.checks
            ],
            "warnings": self.warnings,
            "evidence": self.evidence,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

SchemaType = Union[str, Dict[str, Any], List[Any]]


class SchemaValidator:
    """Validates values against a declarative schema with detailed errors.

    Schema format (dict):
        {"type": "object", "required": [...], "properties": {...},
         "additionalProperties": bool}
    Types: "string", "integer", "number", "boolean", "null", "array",
           "object", "any". Supports "enum", "min"/"max"/"minLength"/
           "maxLength", "pattern", "format" (url/email/ip/date/datetime/uuid),
           "items" (for arrays), "minItems"/"maxItems", "coerce" (bool:
           attempt safe type coercion before validation).
    """

    def __init__(self, schema: Dict[str, Any]) -> None:
        if not isinstance(schema, dict):
            raise TypeError("schema must be a dict")
        self.schema = schema
        self.errors: List[str] = []

    # -- public API ---------------------------------------------------------
    def validate(self, value: Any, path: str = "$") -> Tuple[bool, List[str]]:
        """Validate value against the schema. Returns (ok, errors)."""
        self.errors = []
        self._validate(value, self.schema, path)
        return (len(self.errors) == 0), list(self.errors)

    def validate_or_raise(self, value: Any, path: str = "$") -> Any:
        ok, errors = self.validate(value, path)
        if not ok:
            raise ValueError("Schema validation failed:\n" + "\n".join(f"  - {e}" for e in errors))
        return value

    # -- internals ----------------------------------------------------------
    def _fail(self, path: str, message: str) -> None:
        self.errors.append(f"{path}: {message}")

    def _validate(self, value: Any, schema: Dict[str, Any], path: str) -> Any:
        expected = schema.get("type", "any")
        if schema.get("coerce"):
            value = self._coerce(value, expected, path)
        checker = {
            "any": lambda v, p, s, e: v,
            "string": self._check_string,
            "integer": self._check_integer,
            "number": self._check_number,
            "boolean": self._check_boolean,
            "null": self._check_null,
            "array": self._check_array,
            "object": self._check_object,
        }.get(expected)
        if checker is None:
            self._fail(path, f"unknown schema type {expected!r}")
            return value
        value = checker(value, path, schema, expected)
        self._check_common(value, schema, path)
        return value

    def _coerce(self, value: Any, expected: str, path: str) -> Any:
        """Safe type coercion: strings -> numbers/bools, single -> list."""
        try:
            if expected == "integer" and isinstance(value, str):
                v = value.strip()
                if re.fullmatch(r"[+-]?\d+", v):
                    return int(v)
            elif expected == "number" and isinstance(value, str):
                v = value.strip()
                if re.fullmatch(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?", v):
                    return float(v)
            elif expected == "boolean" and isinstance(value, str):
                v = value.strip().lower()
                if v in ("true", "yes", "1", "on"):
                    return True
                if v in ("false", "no", "0", "off"):
                    return False
            elif expected == "array" and not isinstance(value, (list, tuple)):
                return [value]
            elif expected == "string" and isinstance(value, (int, float, bool)):
                return str(value)
        except (ValueError, TypeError):
            pass
        return value

    def _check_common(self, value: Any, schema: Dict[str, Any], path: str) -> None:
        if "enum" in schema and value not in schema["enum"]:
            self._fail(path, f"value {value!r} not in enum {schema['enum']!r}")
        fmt = schema.get("format")
        if fmt and isinstance(value, str):
            ok, why = validate_format(value, fmt)
            if not ok:
                self._fail(path, f"format {fmt!r} invalid: {why}")
        for key, bound, cmp in (
            ("minimum", schema.get("minimum"), lambda v, b: v >= b),
            ("maximum", schema.get("maximum"), lambda v, b: v <= b),
            ("exclusiveMinimum", schema.get("exclusiveMinimum"), lambda v, b: v > b),
            ("exclusiveMaximum", schema.get("exclusiveMaximum"), lambda v, b: v < b),
            ("minLength", len(value) if isinstance(value, str) else None, lambda v, b: v >= b),
            ("maxLength", len(value) if isinstance(value, str) else None, lambda v, b: v <= b),
            ("minItems", len(value) if isinstance(value, (list, tuple)) else None, lambda v, b: v >= b),
            ("maxItems", len(value) if isinstance(value, (list, tuple)) else None, lambda v, b: v <= b),
        ):
            if bound is None or key not in schema:
                continue
            actual = len(value) if key in ("minLength", "maxLength", "minItems", "maxItems") else value
            if isinstance(value, (int, float, str, list, tuple)) and not cmp(actual, schema[key]):
                self._fail(path, f"{key} violated: {actual!r} vs bound {schema[key]!r}")
        pattern = schema.get("pattern")
        if pattern and isinstance(value, str):
            try:
                if not re.search(pattern, value):
                    self._fail(path, f"value does not match pattern {pattern!r}")
            except re.error as exc:
                self._fail(path, f"bad schema pattern {pattern!r}: {exc}")

    def _check_string(self, value: Any, path: str, schema: Dict[str, Any], expected: str) -> Any:
        if not isinstance(value, str):
            self._fail(path, f"expected string, got {type(value).__name__}")
        return value

    def _check_integer(self, value: Any, path: str, schema: Dict[str, Any], expected: str) -> Any:
        if isinstance(value, bool) or not isinstance(value, int):
            self._fail(path, f"expected integer, got {type(value).__name__}")
        return value

    def _check_number(self, value: Any, path: str, schema: Dict[str, Any], expected: str) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self._fail(path, f"expected number, got {type(value).__name__}")
        elif isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            self._fail(path, "number must be finite")
        return value

    def _check_boolean(self, value: Any, path: str, schema: Dict[str, Any], expected: str) -> Any:
        if not isinstance(value, bool):
            self._fail(path, f"expected boolean, got {type(value).__name__}")
        return value

    def _check_null(self, value: Any, path: str, schema: Dict[str, Any], expected: str) -> Any:
        if value is not None:
            self._fail(path, f"expected null, got {type(value).__name__}")
        return value

    def _check_array(self, value: Any, path: str, schema: Dict[str, Any], expected: str) -> Any:
        if not isinstance(value, (list, tuple)):
            self._fail(path, f"expected array, got {type(value).__name__}")
            return value
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(value):
                self._validate(item, item_schema, f"{path}[{i}]")
        if schema.get("uniqueItems"):
            seen = set()
            for i, item in enumerate(value):
                key = json.dumps(item, sort_keys=True, default=str)
                if key in seen:
                    self._fail(f"{path}[{i}]", "duplicate item violates uniqueItems")
                seen.add(key)
        return value

    def _check_object(self, value: Any, path: str, schema: Dict[str, Any], expected: str) -> Any:
        if not isinstance(value, dict):
            self._fail(path, f"expected object, got {type(value).__name__}")
            return value
        for req in schema.get("required", []):
            if req not in value:
                self._fail(path, f"missing required property {req!r}")
        props = schema.get("properties", {})
        for key, sub in props.items():
            if key in value:
                self._validate(value[key], sub, f"{path}.{key}")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    self._fail(path, f"additional property {key!r} not allowed")
        return value


# ---------------------------------------------------------------------------
# Format validators (URL, email, IP, date, datetime, uuid, ... )
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


def validate_format(value: str, fmt: str) -> Tuple[bool, str]:
    """Validate a string against a named format. Returns (ok, reason)."""
    fmt = fmt.lower()
    if fmt == "url":
        try:
            parts = urlparse(value)
            if parts.scheme not in ("http", "https", "ftp", "ws", "wss"):
                return False, f"unsupported scheme {parts.scheme!r}"
            if not parts.netloc:
                return False, "missing host"
            return True, ""
        except Exception as exc:
            return False, str(exc)
    if fmt == "email":
        return (True, "") if _EMAIL_RE.match(value) else (False, "not an email address")
    if fmt in ("ipv4", "ipv6", "ip"):
        try:
            addr = ip_address(value)
            if fmt == "ipv4" and addr.version != 4:
                return False, "not an IPv4 address"
            if fmt == "ipv6" and addr.version != 6:
                return False, "not an IPv6 address"
            return True, ""
        except ValueError:
            return False, "not a valid IP address"
    if fmt == "date":
        try:
            datetime.strptime(value, "%Y-%m-%d")
            return True, ""
        except ValueError:
            return False, "expected YYYY-MM-DD"
    if fmt == "datetime":
        candidate = value.replace("Z", "+00:00")
        try:
            datetime.fromisoformat(candidate)
            return True, ""
        except ValueError:
            return False, "expected ISO-8601 datetime"
    if fmt == "uuid":
        return (True, "") if _UUID_RE.match(value) else (False, "not a UUID")
    if fmt == "slug":
        return (True, "") if _SLUG_RE.match(value) else (False, "not a url slug")
    if fmt == "hex":
        return (True, "") if _HEX_RE.match(value) else (False, "not hex")
    if fmt == "sha256":
        ok = len(value) == 64 and bool(_HEX_RE.match(value))
        return (ok, "" if ok else "not a sha256 hex digest")
    if fmt == "semver":
        ok = bool(re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", value))
        return (ok, "" if ok else "not semantic versioning")
    if fmt == "domain":
        ok = bool(re.fullmatch(r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,}", value))
        return (ok, "" if ok else "not a domain name")
    return False, f"unknown format {fmt!r}"

# ---------------------------------------------------------------------------
# Output sanity checks (shared by strategies)
# ---------------------------------------------------------------------------

def detect_error_patterns(text: str) -> List[Dict[str, Any]]:
    """Scan text for known error patterns. Returns list of matches."""
    hits: List[Dict[str, Any]] = []
    if not text:
        return hits
    for name, pattern, severity in ERROR_PATTERNS:
        for match in pattern.finditer(text):
            hits.append({
                "pattern": name,
                "severity": severity,
                "line": match.group(0).strip()[:200],
            })
            if len(hits) >= 25:  # cap the noise
                return hits
    return hits


def detect_fabrication_markers(text: str) -> List[Dict[str, Any]]:
    """Detect placeholder / fabrication markers in claimed tool output."""
    hits: List[Dict[str, Any]] = []
    if not text:
        return hits
    for name, pattern in FABRICATION_PATTERNS:
        for match in pattern.finditer(text):
            hits.append({"marker": name, "match": match.group(0)[:120]})
    return hits


def is_empty_output(value: Any) -> Tuple[bool, str]:
    """Detect empty / missing output. Returns (is_empty, reason)."""
    if value is None:
        return True, "output is None"
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lower() in EMPTY_OUTPUT_MARKERS:
            return True, f"output is empty marker {stripped[:40]!r}"
        if not stripped:
            return True, "output is blank/whitespace only"
        return False, ""
    if isinstance(value, (list, tuple, dict, set)):
        return (True, f"output is empty {type(value).__name__}") if len(value) == 0 else (False, "")
    return False, ""


def text_stats(text: str) -> Dict[str, Any]:
    """Basic statistics used by sanity checks."""
    if not isinstance(text, str):
        text = str(text)
    lines = text.splitlines()
    words = text.split()
    printable_ratio = (
        sum(1 for ch in text if ch.isprintable() or ch in "\n\t\r") / max(1, len(text))
    )
    return {
        "chars": len(text),
        "lines": len(lines),
        "words": len(words),
        "printable_ratio": round(printable_ratio, 4),
        "max_line_len": max((len(l) for l in lines), default=0),
        "avg_line_len": round(sum(len(l) for l in lines) / max(1, len(lines)), 2),
    }


def check_json_parseable(text: str) -> Tuple[bool, Any, str]:
    """Try to parse text as JSON. Returns (ok, value, error_message)."""
    try:
        return True, json.loads(text), ""
    except json.JSONDecodeError as exc:
        return False, None, f"JSON parse error at line {exc.lineno}, col {exc.colno}: {exc.msg}"


# ---------------------------------------------------------------------------
# Verification strategies
# ---------------------------------------------------------------------------

class VerificationStrategy:
    """Base class: one verifier per tool-result type."""

    tool_types: Tuple[str, ...] = ()

    def handles(self, tool_name: str) -> bool:
        return tool_name in self.tool_types

    def verify(self, result: Dict[str, Any]) -> List[CheckResult]:
        """Return a list of check results for the tool result dict."""
        raise NotImplementedError

    # -- shared helper checks ------------------------------------------------
    def check_not_empty(self, value: Any, what: str = "output") -> CheckResult:
        empty, reason = is_empty_output(value)
        return CheckResult(
            name=f"non_empty_{what}",
            passed=not empty,
            confidence=0.95 if not empty else 0.05,
            message=f"{what} is present" if not empty else f"{what} missing: {reason}",
            severity=Severity.ERROR if empty else Severity.INFO,
        )

    def check_no_error_patterns(self, text: str) -> CheckResult:
        hits = detect_error_patterns(text or "")
        worst = max((h["severity"] for h in hits), default=0.0)
        passed = worst < 0.85
        return CheckResult(
            name="no_error_patterns",
            passed=passed,
            confidence=round(1.0 - worst, 3) if hits else 0.97,
            message=("no error patterns detected" if not hits
                     else f"{len(hits)} error pattern(s), worst: {hits[0]['pattern']}"),
            severity=Severity.ERROR if worst >= 0.9 else (Severity.WARNING if hits else Severity.INFO),
            details={"hits": hits[:10]},
        )

    def check_no_fabrication(self, text: str) -> CheckResult:
        hits = detect_fabrication_markers(text or "")
        return CheckResult(
            name="no_fabrication_markers",
            passed=len(hits) == 0,
            confidence=0.2 if hits else 0.95,
            message=("no fabrication markers" if not hits
                     else f"possible fabricated output: {hits[0]['marker']}"),
            severity=Severity.CRITICAL if hits else Severity.INFO,
            details={"hits": hits},
        )


class ShellOutputVerifier(VerificationStrategy):
    """Verifies shell command results: exit code, stderr, output sanity."""

    tool_types = ("shell", "bash", "terminal", "exec", "subprocess")

    def verify(self, result: Dict[str, Any]) -> List[CheckResult]:
        checks: List[CheckResult] = []
        stdout = result.get("stdout", result.get("output", ""))
        stderr = result.get("stderr", "")
        exit_code = result.get("exit_code", result.get("returncode", result.get("code", 0)))
        command = result.get("command", "")

        # 1. exit code
        try:
            code = int(exit_code)
        except (TypeError, ValueError):
            checks.append(CheckResult(
                name="exit_code_valid", passed=False, confidence=0.3,
                message=f"exit code {exit_code!r} is not an integer",
                severity=Severity.WARNING))
            code = 0
        else:
            checks.append(CheckResult(
                name="exit_code_zero", passed=code == 0, confidence=0.95 if code == 0 else 0.1,
                message=f"exit code {code} ({'success' if code == 0 else 'failure'})",
                severity=Severity.ERROR if code != 0 else Severity.INFO,
                details={"exit_code": code}))

        # 2. stderr should not contain fatal errors even on exit 0
        if stderr:
            stderr_hits = detect_error_patterns(stderr)
            severe = [h for h in stderr_hits if h["severity"] >= 0.85]
            checks.append(CheckResult(
                name="stderr_clean", passed=len(severe) == 0,
                confidence=0.9 if not severe else 0.3,
                message=("stderr clean" if not severe
                         else f"stderr contains {len(severe)} severe error pattern(s)"),
                severity=Severity.WARNING if severe else Severity.INFO,
                details={"stderr": stderr[:500], "hits": severe[:5]}))
        else:
            checks.append(CheckResult(
                name="stderr_clean", passed=True, confidence=0.9,
                message="stderr empty", severity=Severity.INFO))

        # 3. stdout sanity
        checks.append(self.check_not_empty(stdout, what="stdout"))
        checks.append(self.check_no_error_patterns(stdout))
        checks.append(self.check_no_fabrication(stdout))

        # 4. output statistics: guard against binary garbage / absurd sizes
        stats = text_stats(stdout or "")
        checks.append(CheckResult(
            name="output_text_sane",
            passed=stats["printable_ratio"] >= 0.7 and stats["chars"] <= 5_000_000,
            confidence=0.9 if stats["printable_ratio"] >= 0.95 else 0.5,
            message=(f"{stats['chars']} chars, {stats['lines']} lines, "
                     f"printable ratio {stats['printable_ratio']}"),
            severity=Severity.WARNING if stats["printable_ratio"] < 0.7 else Severity.INFO,
            details=stats))

        # 5. command echo: if result echoes the command, confirm it matches
        if command and stdout:
            first_line = (stdout or "").splitlines()[0] if (stdout or "").splitlines() else ""
            echoed = command.strip()[:60] in first_line or first_line.strip() == "$"
            checks.append(CheckResult(
                name="command_echo", passed=True, confidence=0.6,
                message=f"command echo {'detected' if echoed else 'not detected'} (informational)",
                severity=Severity.INFO))
        return checks


class PythonOutputVerifier(VerificationStrategy):
    """Verifies python execution results: tracebacks, JSON, return values."""

    tool_types = ("python", "python_exec", "eval_python", "jupyter", "notebook")

    def verify(self, result: Dict[str, Any]) -> List[CheckResult]:
        checks: List[CheckResult] = []
        stdout = result.get("stdout", result.get("output", ""))
        stderr = result.get("stderr", "")
        exit_code = result.get("exit_code", result.get("returncode", 0))
        return_value = result.get("return_value", result.get("result", None))
        expect_json = result.get("expect_json", False)

        # 1. traceback detection (strongest signal)
        combined = f"{stdout or ''}\n{stderr or ''}"
        tb_hits = [h for h in detect_error_patterns(combined)
                   if h["pattern"] in ("python_traceback", "python_exception",
                                       "syntax_error", "assertion_failed")]
        checks.append(CheckResult(
            name="no_traceback", passed=len(tb_hits) == 0,
            confidence=0.05 if tb_hits else 0.97,
            message=("traceback/exception detected" if tb_hits else "no traceback"),
            severity=Severity.CRITICAL if tb_hits else Severity.INFO,
            details={"hits": tb_hits[:5]}))

        # 2. exit code
        try:
            code = int(exit_code)
        except (TypeError, ValueError):
            code = 0 if not tb_hits else 1
        checks.append(CheckResult(
            name="exit_code_zero", passed=code == 0 and not tb_hits,
            confidence=0.95 if code == 0 and not tb_hits else 0.1,
            message=f"exit code {code}",
            severity=Severity.ERROR if (code != 0 or tb_hits) else Severity.INFO))

        # 3. JSON expectation
        if expect_json or (stdout and stdout.strip().startswith(("{", "["))):
            ok, value, err = check_json_parseable((stdout or "").strip())
            checks.append(CheckResult(
                name="json_parseable", passed=ok, confidence=0.95 if ok else 0.1,
                message="stdout parses as JSON" if ok else err,
                severity=Severity.ERROR if (expect_json and not ok) else Severity.WARNING,
                details={"parsed_type": type(value).__name__ if ok else None}))

        # 4. return value sanity
        if return_value is not None:
            checks.append(CheckResult(
                name="return_value_present", passed=True, confidence=0.8,
                message=f"return value of type {type(return_value).__name__} present",
                severity=Severity.INFO,
                details={"type": type(return_value).__name__}))
            if isinstance(return_value, str):
                checks.append(self.check_no_error_patterns(return_value))
        else:
            checks.append(CheckResult(
                name="return_value_present", passed=True, confidence=0.5,
                message="no return value (informational)",
                severity=Severity.INFO))

        # 5. syntax of any embedded code blocks
        code_blocks = re.findall(r"```python\n(.*?)```", combined, re.S)
        for i, block in enumerate(code_blocks[:5]):
            try:
                ast.parse(block)
                checks.append(CheckResult(
                    name=f"code_block_{i}_syntax", passed=True, confidence=0.9,
                    message=f"embedded code block {i} parses", severity=Severity.INFO))
            except SyntaxError as exc:
                checks.append(CheckResult(
                    name=f"code_block_{i}_syntax", passed=False, confidence=0.3,
                    message=f"embedded code block {i} has syntax error: {exc}",
                    severity=Severity.WARNING))
        checks.append(self.check_no_fabrication(stdout or ""))
        return checks


class WebContentVerifier(VerificationStrategy):
    """Verifies web fetch results: HTTP status, content presence, page sanity."""

    tool_types = ("web_fetch", "browser_open", "http_get", "curl", "wget",
                  "playwright", "web_search")

    def verify(self, result: Dict[str, Any]) -> List[CheckResult]:
        checks: List[CheckResult] = []
        content = result.get("content", result.get("text", result.get("body", "")))
        status = result.get("status_code", result.get("status", 200))
        url = result.get("url", "")
        title = result.get("title", "")

        # 1. HTTP status
        try:
            code = int(status)
        except (TypeError, ValueError):
            code = 200
        ok_status = 200 <= code < 400
        checks.append(CheckResult(
            name="http_status_ok", passed=ok_status,
            confidence=0.95 if ok_status else 0.15,
            message=f"HTTP status {code}",
            severity=Severity.ERROR if not ok_status else Severity.INFO,
            details={"status_code": code}))

        # 2. URL format
        if url:
            ok, why = validate_format(str(url), "url")
            checks.append(CheckResult(
                name="url_valid", passed=ok, confidence=0.9 if ok else 0.2,
                message="URL valid" if ok else f"URL invalid: {why}",
                severity=Severity.WARNING if not ok else Severity.INFO))

        # 3. content present
        checks.append(self.check_not_empty(content, what="page content"))

        # 4. error page detection (soft 404s, captcha walls, access denied)
        text = content or ""
        soft_error_res = [
            (r"(?i)404[\s\-:]*page not found", "soft_404"),
            (r"(?i)access denied|forbidden", "access_denied_page"),
            (r"(?i)captcha|verify you are (a )?human|are you a robot", "bot_challenge_page"),
            (r"(?i)please enable javascript", "js_required_page"),
            (r"(?i)account (suspended|terminated|disabled)", "account_dead_page"),
        ]
        found = [name for rx, name in soft_error_res if re.search(rx, text)]
        checks.append(CheckResult(
            name="no_soft_error_page", passed=len(found) == 0,
            confidence=0.3 if found else 0.9,
            message=("soft error page detected: " + ", ".join(found)) if found else "no soft error page",
            severity=Severity.WARNING if found else Severity.INFO,
            details={"detected": found}))

        # 5. content sanity stats
        stats = text_stats(text)
        checks.append(CheckResult(
            name="content_sane",
            passed=stats["chars"] >= 50 and stats["printable_ratio"] >= 0.6,
            confidence=0.85 if stats["chars"] >= 200 else 0.5,
            message=f"{stats['chars']} chars, title={title[:60]!r}" if title else f"{stats['chars']} chars",
            severity=Severity.WARNING if stats["chars"] < 50 else Severity.INFO,
            details=stats))

        checks.append(self.check_no_fabrication(text))
        return checks


class FileOperationVerifier(VerificationStrategy):
    """Verifies file operations: existence, size, content hash, read-back."""

    tool_types = ("write_file", "read_file", "edit_file", "delete_file",
                  "copy_file", "move_file", "file_write", "file_read")

    def verify(self, result: Dict[str, Any]) -> List[CheckResult]:
        checks: List[CheckResult] = []
        operation = result.get("operation", result.get("op", ""))
        path = result.get("path", result.get("file", ""))
        content = result.get("content", result.get("data", None))
        expected_hash = result.get("expected_sha256", result.get("sha256", None))
        expected_size = result.get("expected_size", result.get("size", None))

        # 1. path present
        checks.append(CheckResult(
            name="path_present", passed=bool(path),
            confidence=0.95 if path else 0.1,
            message=f"path={path!r}" if path else "no path in result",
            severity=Severity.ERROR if not path else Severity.INFO))

        # 2. content hash check
        if content is not None and expected_hash:
            actual = hashlib.sha256(
                content.encode("utf-8") if isinstance(content, str) else bytes(content)
            ).hexdigest()
            match = actual.lower() == str(expected_hash).lower()
            checks.append(CheckResult(
                name="content_hash_match", passed=match,
                confidence=0.98 if match else 0.05,
                message="sha256 matches" if match else f"sha256 mismatch: {actual[:16]}... vs {str(expected_hash)[:16]}...",
                severity=Severity.CRITICAL if not match else Severity.INFO,
                details={"actual": actual, "expected": str(expected_hash)}))

        # 3. size check
        if content is not None and expected_size is not None:
            actual_size = len(content.encode("utf-8") if isinstance(content, str) else bytes(content))
            try:
                exp = int(expected_size)
            except (TypeError, ValueError):
                exp = None
            if exp is not None:
                match = actual_size == exp
                checks.append(CheckResult(
                    name="size_match", passed=match,
                    confidence=0.9 if match else 0.3,
                    message=f"size {actual_size} vs expected {exp}",
                    severity=Severity.WARNING if not match else Severity.INFO,
                    details={"actual": actual_size, "expected": exp}))

        # 4. operation-specific sanity
        op = str(operation).lower()
        if op in ("write", "write_file", "create"):
            checks.append(self.check_not_empty(content, what="written content"))
            if isinstance(content, str) and content:
                try:
                    compile(content, str(path or "<string>"), "exec")
                    syntax_ok = True
                    syntax_err = ""
                except SyntaxError as exc:
                    syntax_ok = False
                    syntax_err = str(exc)
                if str(path or "").endswith(".py"):
                    checks.append(CheckResult(
                        name="written_python_syntax", passed=syntax_ok,
                        confidence=0.95 if syntax_ok else 0.1,
                        message="written python parses" if syntax_ok else f"syntax error: {syntax_err}",
                        severity=Severity.ERROR if not syntax_ok else Severity.INFO))
        elif op in ("read", "read_file"):
            checks.append(self.check_not_empty(content, what="read content"))
        elif op in ("delete", "delete_file"):
            declared = result.get("deleted", result.get("success", True))
            checks.append(CheckResult(
                name="delete_confirmed", passed=bool(declared),
                confidence=0.8 if declared else 0.2,
                message="delete confirmed" if declared else "delete not confirmed",
                severity=Severity.WARNING if not declared else Severity.INFO))

        checks.append(self.check_no_error_patterns(str(content or "")[:2000]))
        return checks

# ---------------------------------------------------------------------------
# Cross-referencing helpers
# ---------------------------------------------------------------------------

def normalize_for_comparison(text: str) -> str:
    """Normalize text so two independent sources can be compared."""
    text = (text or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[\"'`]", "", text)
    return text


def values_agree(a: Any, b: Any, numeric_tolerance: float = 0.01) -> Tuple[bool, str]:
    """Decide whether two independently obtained values agree."""
    if a is None or b is None:
        return False, "one value is missing"
    if isinstance(a, bool) or isinstance(b, bool):
        return (a is b, "booleans match" if a is b else "booleans differ")
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if a == b:
            return True, "numbers exactly equal"
        denom = max(abs(a), abs(b), 1e-12)
        rel = abs(a - b) / denom
        return (rel <= numeric_tolerance,
                f"relative difference {rel:.4f} within tolerance" if rel <= numeric_tolerance
                else f"relative difference {rel:.4f} exceeds tolerance")
    sa, sb = normalize_for_comparison(str(a)), normalize_for_comparison(str(b))
    if sa == sb:
        return True, "normalized strings equal"
    # token overlap heuristic
    ta, tb = set(sa.split()), set(sb.split())
    if ta and tb:
        jaccard = len(ta & tb) / len(ta | tb)
        if jaccard >= 0.7:
            return True, f"token overlap {jaccard:.2f} high enough to agree"
        return False, f"token overlap {jaccard:.2f} too low"
    return False, "strings differ"


class CrossReference:
    """Verifies a result against one or more independent sources.

    A cross-reference source is a callable returning the independently
    obtained value for the same fact, e.g. a second tool run or a cached
    verified fact from memory.
    """

    def __init__(self, name: str, source: Callable[[], Any],
                 tolerance: float = 0.01) -> None:
        self.name = name
        self.source = source
        self.tolerance = tolerance

    def check(self, claimed: Any) -> CheckResult:
        try:
            independent = self.source()
        except Exception as exc:
            return CheckResult(
                name=f"xref_{self.name}", passed=True, confidence=0.5,
                message=f"independent source {self.name!r} unavailable: {exc}",
                severity=Severity.WARNING)
        agree, reason = values_agree(claimed, independent, self.tolerance)
        return CheckResult(
            name=f"xref_{self.name}", passed=agree,
            confidence=0.92 if agree else 0.25,
            message=f"cross-reference {self.name!r}: {reason}",
            severity=Severity.ERROR if not agree else Severity.INFO,
            details={"claimed": str(claimed)[:200], "independent": str(independent)[:200]})


# ---------------------------------------------------------------------------
# ResultVerifier: orchestrator
# ---------------------------------------------------------------------------

class ResultVerifier:
    """Orchestrates verification of a tool result.

    Pipeline:
        1. Select strategy for tool_name (fallback: generic checks).
        2. Run strategy checks.
        3. Run schema validation if a schema is registered for the tool.
        4. Run cross-references if provided.
        5. Aggregate into a confidence score with a detailed breakdown.
        6. Decide pass/fail.

    Confidence aggregation: weighted mean of check confidences where the
    weight of a check = 2.0 for CRITICAL, 1.5 for ERROR, 1.0 for WARNING,
    0.5 for INFO. Any CRITICAL failure forces the report to fail.
    Any failed ERROR check caps confidence at 0.49.
    """

    SEVERITY_WEIGHTS = {
        Severity.CRITICAL: 2.0,
        Severity.ERROR: 1.5,
        Severity.WARNING: 1.0,
        Severity.INFO: 0.5,
    }

    def __init__(self,
                 strategies: Optional[List[VerificationStrategy]] = None,
                 schemas: Optional[Dict[str, Dict[str, Any]]] = None,
                 min_confidence: float = 0.6) -> None:
        self.strategies: List[VerificationStrategy] = strategies or [
            ShellOutputVerifier(),
            PythonOutputVerifier(),
            WebContentVerifier(),
            FileOperationVerifier(),
        ]
        self.schemas: Dict[str, Dict[str, Any]] = schemas or {}
        self.min_confidence = float(min_confidence)

    # -- registration -------------------------------------------------------
    def register_strategy(self, strategy: VerificationStrategy) -> None:
        self.strategies.append(strategy)

    def register_schema(self, tool_name: str, schema: Dict[str, Any]) -> None:
        self.schemas[tool_name] = schema

    def strategy_for(self, tool_name: str) -> Optional[VerificationStrategy]:
        for strategy in self.strategies:
            if strategy.handles(tool_name):
                return strategy
        return None

    # -- verification -------------------------------------------------------
    def verify(self,
               tool_name: str,
               result: Dict[str, Any],
               cross_references: Optional[List[CrossReference]] = None,
               expected: Any = None) -> VerificationReport:
        """Verify one tool result and return a full report."""
        checks: List[CheckResult] = []
        warnings: List[str] = []
        evidence: Dict[str, Any] = {
            "tool_name": tool_name,
            "result_keys": sorted(result.keys()),
            "result_hash": self._hash_result(result),
        }

        strategy = self.strategy_for(tool_name)
        if strategy is not None:
            try:
                checks.extend(strategy.verify(result))
            except Exception as exc:
                checks.append(CheckResult(
                    name="strategy_crashed", passed=False, confidence=0.1,
                    message=f"strategy {type(strategy).__name__} crashed: {exc}",
                    severity=Severity.ERROR))
        else:
            checks.extend(self._generic_checks(tool_name, result))

        # schema validation
        schema = self.schemas.get(tool_name)
        if schema is not None:
            validator = SchemaValidator(schema)
            ok, errors = validator.validate(result)
            checks.append(CheckResult(
                name="schema_valid", passed=ok,
                confidence=0.95 if ok else 0.15,
                message="result matches schema" if ok else f"schema errors: {len(errors)}",
                severity=Severity.ERROR if not ok else Severity.INFO,
                details={"errors": errors[:10]}))

        # cross-references
        for xref in cross_references or []:
            claimed = self._extract_claimed_value(result, expected)
            try:
                checks.append(xref.check(claimed))
            except Exception as exc:
                checks.append(CheckResult(
                    name=f"xref_{xref.name}", passed=True, confidence=0.5,
                    message=f"cross-reference crashed: {exc}",
                    severity=Severity.WARNING))

        # expected-value direct comparison
        if expected is not None:
            claimed = self._extract_claimed_value(result, expected)
            agree, reason = values_agree(claimed, expected)
            checks.append(CheckResult(
                name="matches_expected", passed=agree,
                confidence=0.93 if agree else 0.2,
                message=f"expected-value comparison: {reason}",
                severity=Severity.ERROR if not agree else Severity.INFO))

        confidence, breakdown = self._score(checks)
        evidence["confidence_breakdown"] = breakdown

        critical = [c for c in checks if not c.passed and c.severity == Severity.CRITICAL]
        error_failed = [c for c in checks if not c.passed and c.severity == Severity.ERROR]
        passed = (len(critical) == 0 and len(error_failed) == 0
                  and confidence >= self.min_confidence)
        for c in checks:
            if c.severity == Severity.WARNING and not c.passed:
                warnings.append(f"[{c.name}] {c.message}")

        return VerificationReport(
            passed=passed,
            confidence=confidence,
            checks=checks,
            warnings=warnings,
            evidence=evidence,
            tool_name=tool_name,
        )

    # -- internals ----------------------------------------------------------
    def _generic_checks(self, tool_name: str, result: Dict[str, Any]) -> List[CheckResult]:
        base = VerificationStrategy()
        checks = [base.check_not_empty(result, what="result")]
        text = json.dumps(result, default=str)[:20000]
        checks.append(base.check_no_error_patterns(text))
        checks.append(base.check_no_fabrication(text))
        return checks

    @staticmethod
    def _extract_claimed_value(result: Dict[str, Any], expected: Any) -> Any:
        """Pull the most likely 'claimed value' out of a result dict."""
        for key in ("value", "result", "output", "stdout", "content", "data"):
            if key in result:
                return result[key]
        return result

    @staticmethod
    def _hash_result(result: Dict[str, Any]) -> str:
        try:
            canonical = json.dumps(result, sort_keys=True, default=str)
        except Exception:
            canonical = repr(result)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _score(self, checks: List[CheckResult]) -> Tuple[float, Dict[str, Any]]:
        """Weighted confidence aggregation with a per-check breakdown."""
        if not checks:
            return 0.5, {"method": "no checks ran", "weights": {}}
        total_w = 0.0
        weighted = 0.0
        per_check: Dict[str, Any] = {}
        for c in checks:
            w = self.SEVERITY_WEIGHTS.get(c.severity, 1.0)
            # failed checks drag harder than passed checks lift
            effective_w = w * (1.5 if not c.passed else 1.0)
            total_w += effective_w
            weighted += c.confidence * effective_w
            per_check[c.name] = {
                "confidence": round(c.confidence, 3),
                "weight": round(effective_w, 2),
                "passed": c.passed,
                "severity": c.severity.value,
            }
        score = weighted / total_w if total_w else 0.5
        # hard rules
        if any(not c.passed and c.severity == Severity.CRITICAL for c in checks):
            score = min(score, 0.19)
        elif any(not c.passed and c.severity == Severity.ERROR for c in checks):
            score = min(score, 0.49)
        score = max(0.0, min(1.0, round(score, 4)))
        breakdown = {
            "method": "severity-weighted mean with failure amplification",
            "n_checks": len(checks),
            "n_passed": sum(1 for c in checks if c.passed),
            "checks": per_check,
        }
        return score, breakdown

    # -- convenience --------------------------------------------------------
    def verify_shell(self, command: str, stdout: str, stderr: str = "",
                     exit_code: int = 0) -> VerificationReport:
        return self.verify("shell", {
            "command": command, "stdout": stdout,
            "stderr": stderr, "exit_code": exit_code,
        })

    def verify_python(self, stdout: str, stderr: str = "",
                      exit_code: int = 0,
                      return_value: Any = None,
                      expect_json: bool = False) -> VerificationReport:
        return self.verify("python", {
            "stdout": stdout, "stderr": stderr, "exit_code": exit_code,
            "return_value": return_value, "expect_json": expect_json,
        })

    def batch_verify(self,
                     items: List[Tuple[str, Dict[str, Any]]]) -> List[VerificationReport]:
        """Verify many tool results; returns one report per item."""
        return [self.verify(tool_name, result) for tool_name, result in items]

    def gate(self, report: VerificationReport) -> Tuple[bool, str]:
        """Decide whether the agent may act on this result.

        Returns (allowed, reason). This is the enforcement point of
        'the agent cannot fabricate output': unverified or low-confidence
        results are not actionable.
        """
        if report.critical_failures:
            names = ", ".join(c.name for c in report.critical_failures)
            return False, f"BLOCKED: critical verification failures: {names}"
        if not report.passed:
            return False, (
                f"BLOCKED: verification failed (confidence {report.confidence:.2f} "
                f"< {self.min_confidence:.2f}); {report.summary()}"
            )
        return True, (
            f"ALLOWED: verification passed with confidence {report.confidence:.2f}"
        )

# ---------------------------------------------------------------------------
# Output contracts: per-tool expectations beyond generic strategy checks
# ---------------------------------------------------------------------------

@dataclass
class OutputContract:
    """Declares what a tool's result must contain to be actionable.

    Attributes:
        tool_name:        tool this contract applies to
        required_keys:    keys that must exist in the result dict
        forbidden_keys:   keys that must NOT exist (e.g. raw secrets echoed back)
        value_rules:      mapping of key -> SchemaValidator-compatible rule dict
        min_output_chars: minimum length of the primary textual output
        must_be_json:     primary output must parse as JSON
    """
    tool_name: str
    required_keys: List[str] = field(default_factory=list)
    forbidden_keys: List[str] = field(default_factory=list)
    value_rules: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    min_output_chars: int = 0
    must_be_json: bool = False

    def check(self, result: Dict[str, Any]) -> List[CheckResult]:
        checks: List[CheckResult] = []
        for key in self.required_keys:
            present = key in result and result[key] not in (None, "", [], {})
            checks.append(CheckResult(
                name=f"contract_key_{key}", passed=bool(present),
                confidence=0.9 if present else 0.1,
                message=f"required key {key!r} {'present' if present else 'MISSING'}",
                severity=Severity.ERROR if not present else Severity.INFO))
        for key in self.forbidden_keys:
            absent = key not in result
            checks.append(CheckResult(
                name=f"contract_no_{key}", passed=absent,
                confidence=0.9 if absent else 0.2,
                message=(f"forbidden key {key!r} absent" if absent
                         else f"forbidden key {key!r} PRESENT in result"),
                severity=Severity.WARNING if not absent else Severity.INFO))
        for key, rule in self.value_rules.items():
            if key in result:
                validator = SchemaValidator(rule)
                ok, errors = validator.validate(result[key], path=f"$.{key}")
                checks.append(CheckResult(
                    name=f"contract_value_{key}", passed=ok,
                    confidence=0.92 if ok else 0.2,
                    message=("value rule satisfied" if ok
                             else f"value rule failed: {errors[0] if errors else 'unknown'}"),
                    severity=Severity.ERROR if not ok else Severity.INFO,
                    details={"errors": errors[:5]}))
        if self.min_output_chars:
            text = str(result.get("output", result.get("stdout", result.get("content", ""))))
            ok = len(text.strip()) >= self.min_output_chars
            checks.append(CheckResult(
                name="contract_min_output", passed=ok,
                confidence=0.85 if ok else 0.25,
                message=(f"output {len(text.strip())} chars >= {self.min_output_chars}"
                         if ok else f"output only {len(text.strip())} chars, need {self.min_output_chars}"),
                severity=Severity.WARNING if not ok else Severity.INFO))
        if self.must_be_json:
            text = str(result.get("output", result.get("stdout", result.get("content", "")))).strip()
            ok, _, err = check_json_parseable(text)
            checks.append(CheckResult(
                name="contract_json", passed=ok,
                confidence=0.9 if ok else 0.15,
                message="primary output is valid JSON" if ok else f"not JSON: {err}",
                severity=Severity.ERROR if not ok else Severity.INFO))
        return checks


# Predefined contracts for the agent's core toolset.
DEFAULT_CONTRACTS: Dict[str, OutputContract] = {
    "shell": OutputContract(
        tool_name="shell",
        required_keys=["stdout", "exit_code"],
        value_rules={"exit_code": {"type": "integer", "minimum": 0, "maximum": 255}},
    ),
    "python": OutputContract(
        tool_name="python",
        required_keys=["stdout", "exit_code"],
        value_rules={"exit_code": {"type": "integer", "minimum": 0}},
    ),
    "web_fetch": OutputContract(
        tool_name="web_fetch",
        required_keys=["content", "status_code", "url"],
        value_rules={
            "status_code": {"type": "integer", "minimum": 100, "maximum": 599},
            "url": {"type": "string", "format": "url"},
        },
        min_output_chars=1,
    ),
    "write_file": OutputContract(
        tool_name="write_file",
        required_keys=["path"],
        value_rules={"path": {"type": "string", "minLength": 1}},
    ),
    "read_file": OutputContract(
        tool_name="read_file",
        required_keys=["path", "content"],
        value_rules={"path": {"type": "string", "minLength": 1}},
    ),
}


# Predefined JSON schemas for common tool results.
COMMON_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "shell": {
        "type": "object",
        "required": ["stdout", "exit_code"],
        "properties": {
            "command": {"type": "string"},
            "stdout": {"type": "string"},
            "stderr": {"type": "string"},
            "exit_code": {"type": "integer", "minimum": 0, "maximum": 255},
        },
    },
    "python": {
        "type": "object",
        "required": ["stdout", "exit_code"],
        "properties": {
            "stdout": {"type": "string"},
            "stderr": {"type": "string"},
            "exit_code": {"type": "integer", "minimum": 0},
        },
    },
    "web_fetch": {
        "type": "object",
        "required": ["content", "status_code", "url"],
        "properties": {
            "content": {"type": "string"},
            "status_code": {"type": "integer", "minimum": 100, "maximum": 599},
            "url": {"type": "string", "format": "url"},
            "title": {"type": "string"},
        },
    },
}


# ---------------------------------------------------------------------------
# Retry advisor: turns a failed report into actionable next steps
# ---------------------------------------------------------------------------

class RetryAdvisor:
    """Suggests what to do when verification fails.

    Maps failure signatures to concrete remediation advice for the control
    loop (RETRY with adjusted parameters, SWITCH tool, ASK for missing
    input, or ABORT).
    """

    def advise(self, report: VerificationReport) -> Dict[str, Any]:
        failed = report.failed_checks
        names = {c.name for c in failed}
        suggestions: List[str] = []
        action = "retry"

        if any(c.severity == Severity.CRITICAL for c in failed):
            action = "abort"
            suggestions.append(
                "Critical verification failure (traceback / hash mismatch / fabrication marker). "
                "Do NOT act on this result. Debug the tool call itself before retrying.")
        if "no_traceback" in names or "exit_code_zero" in names:
            action = "retry" if action != "abort" else action
            suggestions.append(
                "Tool raised an error. Read the stderr/traceback, fix the invocation "
                "(arguments, paths, imports), then retry.")
        if "non_empty_stdout" in names or "non_empty_output" in names or "non_empty_result" in names:
            suggestions.append(
                "Tool returned empty output. Check that the command actually produces "
                "output (add print/echo, remove output suppression, verify the target exists).")
        if "no_soft_error_page" in names:
            suggestions.append(
                "Fetched page looks like an error/bot wall. Retry with different "
                "headers, wait and retry, or switch to an alternative source.")
        if "json_parseable" in names or "contract_json" in names:
            suggestions.append(
                "Expected JSON but got something else. Strip log prefixes, request "
                "JSON explicitly from the tool, or parse a substring.")
        if "schema_valid" in names:
            suggestions.append(
                "Result shape does not match the tool schema. Map the actual keys "
                "to the expected schema or update the schema.")
        if any(n.startswith("xref_") for n in names):
            suggestions.append(
                "Independent source disagrees. Run a third source / re-run the tool; "
                "trust the majority or the higher-confidence source.")
        if "content_hash_match" in names:
            action = "abort"
            suggestions.append(
                "Content hash mismatch: the bytes are not what was expected. "
                "Re-fetch/re-read; do not trust the current bytes.")
        if not suggestions:
            suggestions.append(
                "Generic verification failure. Inspect the failed checks, adjust "
                "the tool call, and retry once before escalating.")

        return {
            "action": action,  # retry | abort | switch_tool | escalate
            "suggestions": suggestions,
            "failed_checks": [c.name for c in failed],
            "confidence": report.confidence,
            "retry_worthwhile": action == "retry" and report.confidence >= 0.25,
        }


# ---------------------------------------------------------------------------
# Convenience: one-shot verification entry point
# ---------------------------------------------------------------------------

_default_verifier: Optional[ResultVerifier] = None


def get_default_verifier() -> ResultVerifier:
    """Return a shared ResultVerifier with contracts and schemas wired in."""
    global _default_verifier
    if _default_verifier is None:
        verifier = ResultVerifier(schemas=dict(COMMON_SCHEMAS))
        _default_verifier = verifier
    return _default_verifier


def verify_result(tool_name: str,
                  result: Dict[str, Any],
                  expected: Any = None,
                  cross_references: Optional[List[CrossReference]] = None
                  ) -> VerificationReport:
    """One-shot verification using the shared default verifier."""
    verifier = get_default_verifier()
    contract = DEFAULT_CONTRACTS.get(tool_name)
    report = verifier.verify(tool_name, result, cross_references=cross_references,
                             expected=expected)
    if contract is not None:
        extra = contract.check(result)
        report.checks.extend(extra)
        # re-score with the contract checks included
        confidence, breakdown = verifier._score(report.checks)
        report.confidence = confidence
        report.evidence["confidence_breakdown"] = breakdown
        critical = any(not c.passed and c.severity == Severity.CRITICAL for c in report.checks)
        error_failed = any(not c.passed and c.severity == Severity.ERROR for c in report.checks)
        report.passed = (not critical and not error_failed
                         and confidence >= verifier.min_confidence)
    return report
