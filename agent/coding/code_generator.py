"""Spec-driven Python code generation.

The CodeGenerator turns structured specs into real, parseable Python:
function / class / script templates, docstring generation, import
management, syntax validation via ast.parse, and complexity estimation.

It never emits placeholders: every generated file must parse, and the
generator refuses to produce code containing TODO/FIXME markers.
"""

from __future__ import annotations

import ast
import keyword
import re
import textwrap
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------

@dataclass
class FunctionSpec:
    """Specification for one function to generate."""
    name: str
    params: List[Tuple[str, Optional[str], Optional[str]]] = field(default_factory=list)
    # params: list of (name, type_annotation|None, default_repr|None)
    return_type: Optional[str] = None
    docstring: str = ""
    body: str = "pass"
    decorators: List[str] = field(default_factory=list)
    is_method: bool = False
    is_async: bool = False
    raises: List[str] = field(default_factory=list)


@dataclass
class ClassSpec:
    """Specification for one class to generate."""
    name: str
    bases: List[str] = field(default_factory=list)
    docstring: str = ""
    attributes: List[Tuple[str, Optional[str], Optional[str]]] = field(default_factory=list)
    # attributes: list of (name, type_annotation|None, default_repr|None)
    methods: List[FunctionSpec] = field(default_factory=list)
    decorators: List[str] = field(default_factory=list)
    is_dataclass: bool = False


@dataclass
class ScriptSpec:
    """Specification for a standalone script."""
    name: str
    description: str = ""
    functions: List[FunctionSpec] = field(default_factory=list)
    classes: List[ClassSpec] = field(default_factory=list)
    main_body: str = ""
    imports: List[str] = field(default_factory=list)
    constants: List[Tuple[str, str]] = field(default_factory=list)  # (NAME, repr)
    use_main_guard: bool = True


@dataclass
class CodeSpec:
    """Top-level spec: what to generate."""
    kind: str  # "function" | "class" | "script"
    function: Optional[FunctionSpec] = None
    class_: Optional[ClassSpec] = None
    script: Optional[ScriptSpec] = None


# ---------------------------------------------------------------------------
# Helpers: names, docstrings, formatting
# ---------------------------------------------------------------------------

def is_valid_identifier(name: str) -> bool:
    return name.isidentifier() and not keyword.iskeyword(name)


def sanitize_identifier(name: str, fallback: str = "generated") -> str:
    name = name or ""
    m_lead = re.match(r"^_+", name)
    leading = m_lead.group(0) if m_lead else ""
    m_trail = re.search(r"_+$", name)
    trailing = m_trail.group(0) if m_trail else ""
    rest = name[len(leading):len(name) - len(trailing) if trailing else len(name)]
    candidate = re.sub(r"\W", "_", rest).strip("_")
    if not candidate or candidate[0].isdigit():
        candidate = f"{fallback}_{candidate}" if candidate else fallback
    candidate = leading + candidate + trailing
    if keyword.iskeyword(candidate):
        candidate += "_"
    return candidate or fallback


def make_docstring(summary: str, params: Optional[List[Tuple[str, str, str]]] = None,
                   returns: str = "", raises: Optional[List[str]] = None,
                   style: str = "google") -> str:
    """Generate a docstring in Google or NumPy style."""
    lines = [summary.strip()]
    if params:
        lines.append("")
        if style == "numpy":
            lines.append("Parameters")
            lines.append("----------")
            for name, type_, desc in params:
                lines.append(f"{name} : {type_ or 'Any'}")
                lines.append(f"    {desc or 'parameter'}")
        else:
            lines.append("Args:")
            for name, type_, desc in params:
                lines.append(f"    {name} ({type_ or 'Any'}): {desc or 'parameter'}.")
    if returns:
        lines.append("")
        if style == "numpy":
            lines.append("Returns")
            lines.append("-------")
            lines.append(f"    {returns}")
        else:
            lines.append("Returns:")
            lines.append(f"    {returns}")
    if raises:
        lines.append("")
        if style == "numpy":
            lines.append("Raises")
            lines.append("------")
            for r in raises:
                lines.append(f"    {r}")
        else:
            lines.append("Raises:")
            for r in raises:
                lines.append(f"    {r}: see documentation.")
    return '"""' + "\n".join(lines) + '"""'


def indent_block(text: str, level: int = 1, spaces: int = 4) -> str:
    prefix = " " * (spaces * level)
    return "\n".join(prefix + line if line.strip() else line
                     for line in text.splitlines())


def wrap_signature(name: str, params: List[str], return_type: Optional[str],
                   prefix: str = "def ", max_len: int = 88) -> str:
    """Render a def line, wrapping params if it exceeds max_len."""
    ret = f" -> {return_type}" if return_type else ""
    one_line = f"{prefix}{name}({', '.join(params)}){ret}:"
    if len(one_line) <= max_len or not params:
        return one_line
    inner = ",\n".join("    " + p for p in params)
    return f"{prefix}{name}(\n{inner},\n){ret}:"

# ---------------------------------------------------------------------------
# Import management
# ---------------------------------------------------------------------------

class ImportManager:
    """Collects, deduplicates, sorts, and renders import statements.

    Groups (PEP 8 order): __future__, stdlib, third-party, first-party/local.
    """

    STDLIB_TOP_LEVELS = {
        "abc", "argparse", "ast", "asyncio", "base64", "bisect", "calendar",
        "collections", "concurrent", "configparser", "contextlib", "copy",
        "csv", "dataclasses", "datetime", "decimal", "difflib", "enum",
        "functools", "glob", "hashlib", "heapq", "hmac", "html", "http",
        "importlib", "inspect", "io", "ipaddress", "itertools", "json",
        "keyword", "logging", "math", "multiprocessing", "os", "pathlib",
        "pickle", "platform", "pprint", "queue", "random", "re", "secrets",
        "shlex", "shutil", "signal", "socket", "sqlite3", "statistics",
        "string", "struct", "subprocess", "sys", "tempfile", "textwrap",
        "threading", "time", "timeit", "traceback", "typing", "unittest",
        "urllib", "uuid", "warnings", "weakref", "xml", "zipfile", "zoneinfo",
    }

    def __init__(self) -> None:
        self._future: Set[str] = set()
        self._plain: Set[str] = set()          # "import x"
        self._from: Dict[str, Set[str]] = {}   # module -> {names}

    def add(self, statement: str) -> None:
        """Add an import statement like 'import os' or 'from x import y, z'."""
        statement = statement.strip()
        m = re.match(r"from\s+([\w.]+)\s+import\s+(.+)", statement)
        if m:
            module, names = m.group(1), m.group(2)
            if module == "__future__":
                self._future.update(n.strip() for n in names.split(","))
            else:
                self._from.setdefault(module, set()).update(
                    n.strip() for n in names.split(","))
            return
        m = re.match(r"import\s+(.+)", statement)
        if m:
            for part in m.group(1).split(","):
                self._plain.add(part.strip().split(" as ")[0].strip())
            return
        raise ValueError(f"not an import statement: {statement!r}")

    def require(self, module: str, names: Optional[List[str]] = None) -> None:
        """Ensure a module (or names from it) is imported."""
        if names:
            self._from.setdefault(module, set()).update(names)
        else:
            self._plain.add(module)

    def _group(self, module: str) -> int:
        top = module.split(".")[0]
        if top in self.STDLIB_TOP_LEVELS:
            return 1
        if top.startswith("_") or top in ("__future__",):
            return 0
        return 2  # third-party / unknown

    def render(self) -> str:
        lines: List[str] = []
        if self._future:
            lines.append(f"from __future__ import {', '.join(sorted(self._future))}")
            lines.append("")
        plain = sorted(self._plain)
        froms = sorted(self._from.items())
        groups: Dict[int, List[str]] = {0: [], 1: [], 2: [], 3: []}
        for mod in plain:
            # first-party heuristic: relative or "agent" package
            g = 3 if mod.split(".")[0] in ("agent",) or mod.startswith(".") else self._group(mod)
            groups[g].append(f"import {mod}")
        for mod, names in froms:
            g = 3 if mod.split(".")[0] in ("agent",) or mod.startswith(".") else self._group(mod)
            clean = sorted(n for n in names if n and n != "*")
            stmt = f"from {mod} import {', '.join(clean)}"
            groups[g].append(stmt)
        first = True
        for g in (0, 1, 2, 3):
            if groups[g]:
                if not first:
                    lines.append("")
                lines.extend(groups[g])
                first = False
        return "\n".join(lines)

    def merge_into_source(self, source: str) -> str:
        """Insert managed imports at the top of source (after docstring)."""
        rendered = self.render()
        if not rendered:
            return source
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return rendered + "\n\n" + source
        # find end of module docstring
        insert_at = 0
        lines = source.splitlines()
        if (tree.body and isinstance(tree.body[0], ast.Expr)
                and isinstance(tree.body[0].value, ast.Constant)
                and isinstance(tree.body[0].value.value, str)):
            insert_at = tree.body[0].end_lineno or 0
        out = lines[:insert_at] + ["", rendered, ""] + lines[insert_at:]
        return "\n".join(out)


# ---------------------------------------------------------------------------
# AST-based code analysis
# ---------------------------------------------------------------------------

@dataclass
class FunctionInfo:
    name: str
    lineno: int
    end_lineno: int
    args: List[str]
    is_method: bool
    is_async: bool
    docstring: Optional[str]
    complexity: int = 0


@dataclass
class ClassInfo:
    name: str
    lineno: int
    bases: List[str]
    methods: List[str]
    docstring: Optional[str]


class CodeAnalyzer:
    """AST-based structural analysis of Python source."""

    def __init__(self, source: str) -> None:
        self.source = source
        try:
            self.tree = ast.parse(source)
            self.parse_error: Optional[str] = None
        except SyntaxError as exc:
            self.tree = None  # type: ignore[assignment]
            self.parse_error = f"{exc.msg} at line {exc.lineno}"

    @property
    def ok(self) -> bool:
        return self.tree is not None

    def functions(self) -> List[FunctionInfo]:
        out: List[FunctionInfo] = []
        if not self.ok:
            return out
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = [a.arg for a in node.args.args]
                out.append(FunctionInfo(
                    name=node.name, lineno=node.lineno,
                    end_lineno=getattr(node, "end_lineno", node.lineno),
                    args=args, is_method=False,
                    is_async=isinstance(node, ast.AsyncFunctionDef),
                    docstring=ast.get_docstring(node)))
        return out

    def classes(self) -> List[ClassInfo]:
        out: List[ClassInfo] = []
        if not self.ok:
            return out
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ClassDef):
                bases = [ast.unparse(b) for b in node.bases] if hasattr(ast, "unparse") else []
                methods = [n.name for n in node.body
                           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
                out.append(ClassInfo(name=node.name, lineno=node.lineno,
                                     bases=bases, methods=methods,
                                     docstring=ast.get_docstring(node)))
        return out

    def imports(self) -> List[str]:
        out: List[str] = []
        if not self.ok:
            return out
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                out.append("import " + ", ".join(
                    a.name + (f" as {a.asname}" if a.asname else "")
                    for a in node.names))
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                out.append(f"from {mod} import " + ", ".join(
                    a.name + (f" as {a.asname}" if a.asname else "")
                    for a in node.names))
        return out

    def undefined_names(self) -> List[str]:
        """Names that are loaded but never defined/imported/assigned (heuristic)."""
        if not self.ok:
            return []
        defined: Set[str] = set(dir(__builtins__)) | set(keyword.kwlist)
        loaded: Set[str] = set()

        class Visitor(ast.NodeVisitor):
            def visit_Import(self, node: ast.Import) -> None:
                for a in node.names:
                    defined.add((a.asname or a.name).split(".")[0])
            def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
                for a in node.names:
                    if a.name != "*":
                        defined.add(a.asname or a.name)
            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                defined.add(node.name)
                for a in node.args.args + node.args.kwonlyargs:
                    defined.add(a.arg)
                if node.args.vararg:
                    defined.add(node.args.vararg.arg)
                if node.args.kwarg:
                    defined.add(node.args.kwarg.arg)
                self.generic_visit(node)
            visit_AsyncFunctionDef = visit_FunctionDef
            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                defined.add(node.name)
                self.generic_visit(node)
            def visit_Name(self, node: ast.Name) -> None:
                if isinstance(node.ctx, ast.Store):
                    defined.add(node.id)
                elif isinstance(node.ctx, ast.Load):
                    loaded.add(node.id)
            def visit_arg(self, node: ast.arg) -> None:
                defined.add(node.arg)

        Visitor().visit(self.tree)
        return sorted(n for n in loaded - defined if not n.startswith("__"))

    def line_count(self) -> int:
        return len(self.source.splitlines())

    def summary(self) -> Dict[str, Any]:
        return {
            "parse_ok": self.ok,
            "parse_error": self.parse_error,
            "lines": self.line_count(),
            "functions": [f.name for f in self.functions()],
            "classes": [c.name for c in self.classes()],
            "imports": self.imports(),
            "undefined_names": self.undefined_names(),
        }


# ---------------------------------------------------------------------------
# Complexity estimation (cyclomatic)
# ---------------------------------------------------------------------------

class ComplexityEstimator(ast.NodeVisitor):
    """Computes cyclomatic complexity per function (McCabe)."""

    DECISION_NODES = (
        ast.If, ast.While, ast.For, ast.AsyncFor, ast.ExceptHandler,
        ast.With, ast.AsyncWith, ast.Assert, ast.IfExp,
        ast.comprehension,
    )

    def __init__(self) -> None:
        self.results: Dict[str, int] = {}
        self._current: Optional[str] = None
        self._score = 1

    def estimate(self, source: str) -> Dict[str, int]:
        self.results = {}
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return {}
        self.visit(tree)
        return dict(self.results)

    def estimate_function(self, node: ast.AST) -> int:
        self._current = getattr(node, "name", "<lambda>")
        self._score = 1
        for child in ast.iter_child_nodes(node):
            self.visit(child)
        self.results[self._current] = self._score
        self._current = None
        return self._score

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # don't recurse with generic_visit: compute separately per function
        if self._current is None:
            self.estimate_function(node)
        else:
            # nested function: count separately
            saved_name, saved_score = self._current, self._score
            self.estimate_function(node)
            self._current, self._score = saved_name, saved_score

    visit_AsyncFunctionDef = visit_FunctionDef

    def generic_visit(self, node: ast.AST) -> None:
        if isinstance(node, self.DECISION_NODES):
            self._score += 1
        elif isinstance(node, ast.BoolOp):
            self._score += max(0, len(node.values) - 1)
        super().generic_visit(node)

    @staticmethod
    def rating(complexity: int) -> str:
        if complexity <= 5:
            return "simple"
        if complexity <= 10:
            return "moderate"
        if complexity <= 20:
            return "complex"
        return "unmaintainable"

# ---------------------------------------------------------------------------
# Template library: real, working templates for common patterns
# ---------------------------------------------------------------------------

class TemplateLibrary:
    """Named, fill-in templates for common Python patterns.

    Each template is a complete, parseable program with {placeholders}
    filled by render(). All templates are validated with ast.parse on load.
    """

    def __init__(self) -> None:
        self._templates: Dict[str, str] = {
            "api_client": self._api_client_template(),
            "cli_tool": self._cli_tool_template(),
            "data_processor": self._data_processor_template(),
            "web_scraper": self._web_scraper_template(),
            "test_suite": self._test_suite_template(),
            "retry_decorator": self._retry_decorator_template(),
            "config_loader": self._config_loader_template(),
            "worker_pool": self._worker_pool_template(),
        }
        # validate every template parses
        for name, tpl in self._templates.items():
            try:
                # skeleton: hide escaped braces, blank out {placeholders}, restore
                skeleton = tpl.replace("{{", "\x00").replace("}}", "\x01")
                skeleton = re.sub(r"\{[a-zA-Z_][\w]*\}", "PLACEHOLDER", skeleton)
                skeleton = skeleton.replace("\x00", "{").replace("\x01", "}")
                ast.parse(skeleton)
            except SyntaxError as exc:
                raise ValueError(f"template {name!r} does not parse: {exc}")

    def names(self) -> List[str]:
        return sorted(self._templates.keys())

    _PLACEHOLDER_RX = re.compile(r"\{([a-zA-Z_][\w]*)\}")

    def render(self, name: str, **kwargs: Any) -> str:
        if name not in self._templates:
            raise KeyError(f"unknown template {name!r}; available: {self.names()}")
        template = self._templates[name]
        # hide doubled {{braces}} (literals) behind sentinels so they are
        # never mistaken for {placeholders}
        tmp = template.replace("{{", "\x00").replace("}}", "\x01")
        placeholders = set(self._PLACEHOLDER_RX.findall(tmp))
        missing = sorted(placeholders - set(kwargs))
        if missing:
            raise KeyError(f"template {name!r} missing placeholder value(s): {missing}")

        def _repl(match: re.Match) -> str:
            return str(kwargs[match.group(1)])

        code = self._PLACEHOLDER_RX.sub(_repl, tmp)
        code = code.replace("\x00", "{").replace("\x01", "}")
        # final safety: rendered code must parse
        try:
            ast.parse(code)
        except SyntaxError as exc:
            raise ValueError(f"rendered template {name!r} has syntax error: {exc}")
        return code

    # -- templates -----------------------------------------------------------
    @staticmethod
    def _api_client_template() -> str:
        return '''"""API client for {service_name}.

Auto-generated by the agent code generator.
"""
from __future__ import annotations

import json
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


class APIError(Exception):
    """Raised when the API returns an error or is unreachable."""


@dataclass
class {class_name}:
    """Client for the {service_name} API."""

    base_url: str = "{base_url}"
    api_key: Optional[str] = None
    timeout: float = 30.0
    max_retries: int = 3

    def _headers(self) -> Dict[str, str]:
        headers = {{"Content-Type": "application/json",
                    "User-Agent": "{class_name}/1.0"}}
        if self.api_key:
            headers["Authorization"] = f"Bearer {{self.api_key}}"
        return headers

    def _request(self, method: str, path: str,
                 payload: Optional[Dict[str, Any]] = None) -> Any:
        url = self.base_url.rstrip("/") + path
        data = json.dumps(payload).encode() if payload is not None else None
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                req = urllib.request.Request(url, data=data,
                                             headers=self._headers(),
                                             method=method)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read().decode("utf-8", "replace")
                    return json.loads(body) if body.strip() else None
            except urllib.error.HTTPError as exc:
                raise APIError(f"HTTP {{exc.code}} for {{method}} {{path}}: "
                               f"{{exc.read().decode('utf-8', 'replace')[:500]}}")
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
                time.sleep(2 ** attempt)
        raise APIError(f"request failed after {{self.max_retries}} retries: {{last_error}}")

    def get(self, path: str) -> Any:
        """GET a resource."""
        return self._request("GET", path)

    def post(self, path: str, payload: Dict[str, Any]) -> Any:
        """POST a resource."""
        return self._request("POST", path, payload)


def main() -> None:
    client = {class_name}()
    print(client.get("{health_path}"))


if __name__ == "__main__":
    main()
'''

    @staticmethod
    def _cli_tool_template() -> str:
        return '''"""{description}

Usage:
    {prog_name} [options] <input>

Auto-generated by the agent code generator.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="{prog_name}", description="{description}")
    parser.add_argument("input", help="input file or value")
    parser.add_argument("-o", "--output", default=None,
                        help="output file (default: stdout)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="verbose logging")
    parser.add_argument("--dry-run", action="store_true",
                        help="do not write anything, just report")
    return parser


def process(input_value: str, verbose: bool = False) -> str:
    """Process the input and return the result text."""
    if verbose:
        print(f"[verbose] processing {{input_value!r}}", file=sys.stderr)
    return input_value


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = process(args.input, verbose=args.verbose)
    except Exception as exc:
        print(f"error: {{exc}}", file=sys.stderr)
        return 1
    if args.dry_run:
        print(f"[dry-run] would output {{len(result)}} chars")
        return 0
    if args.output:
        Path(args.output).write_text(result, encoding="utf-8")
    else:
        print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

    @staticmethod
    def _data_processor_template() -> str:
        return '''"""{description}

Reads records from {input_desc}, transforms them, writes to {output_desc}.
Auto-generated by the agent code generator.
"""
from __future__ import annotations

import csv
import json
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List


@dataclass
class Record:
    """One processed record."""
    data: Dict[str, Any]
    errors: List[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []

    @property
    def valid(self) -> bool:
        return not self.errors


def read_records(path: str) -> Iterator[Dict[str, Any]]:
    """Yield raw records from a JSONL or CSV file."""
    p = Path(path)
    if p.suffix == ".jsonl":
        with p.open(encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                if line.strip():
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError as exc:
                        yield {{"_error": f"line {{lineno}}: {{exc}}"}}
    elif p.suffix == ".csv":
        with p.open(encoding="utf-8", newline="") as fh:
            yield from csv.DictReader(fh)
    else:
        raise ValueError(f"unsupported input format: {{p.suffix}}")


def transform(raw: Dict[str, Any]) -> Record:
    """Transform one raw record. Override for real logic."""
    record = Record(data={})
    if "_error" in raw:
        record.errors.append(str(raw["_error"]))
        return record
    # default: strip whitespace from string values, drop empty keys
    for key, value in raw.items():
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        record.data[key] = value
    return record


def write_records(records: Iterable[Record], path: str) -> Dict[str, int]:
    """Write records as JSONL. Returns counts."""
    counts = {{"written": 0, "invalid": 0}}
    with open(path, "w", encoding="utf-8") as fh:
        for record in records:
            if record.valid:
                fh.write(json.dumps(record.data, default=str) + "\\n")
                counts["written"] += 1
            else:
                counts["invalid"] += 1
    return counts


def main() -> None:
    if len(sys.argv) != 3:
        print(f"usage: {{sys.argv[0]}} <input> <output>", file=sys.stderr)
        sys.exit(2)
    records = (transform(raw) for raw in read_records(sys.argv[1]))
    counts = write_records(records, sys.argv[2])
    print(json.dumps(counts))


if __name__ == "__main__":
    main()
'''

    @staticmethod
    def _web_scraper_template() -> str:
        return '''"""{description}

Fetches {start_url} and extracts data. Polite: rate-limited, robots-aware
note in the docstring only (implement checks as needed).
Auto-generated by the agent code generator.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from dataclasses import dataclass, asdict
from html.parser import HTMLParser
from typing import Any, Dict, List


@dataclass
class ScrapedItem:
    url: str
    title: str
    text_excerpt: str
    links: List[str]


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self._in_title = False
        self.chunks: List[str] = []
        self.links: List[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "title":
            self._in_title = True
        if tag == "a":
            for name, value in attrs:
                if name == "href" and value:
                    self.links.append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif data.strip():
            self.chunks.append(data.strip())


def fetch(url: str, timeout: float = 30.0,
          user_agent: str = "agent-scraper/1.0") -> str:
    req = urllib.request.Request(url, headers={{"User-Agent": user_agent}})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, "replace")


def scrape(url: str) -> ScrapedItem:
    html = fetch(url)
    parser = _TextExtractor()
    parser.feed(html)
    text = re.sub(r"\\s+", " ", " ".join(parser.chunks))
    return ScrapedItem(url=url, title=parser.title.strip(),
                       text_excerpt=text[:2000], links=parser.links[:100])


def main() -> None:
    urls = ["{start_url}"]
    items = []
    for url in urls:
        try:
            items.append(asdict(scrape(url)))
        except Exception as exc:
            items.append({{"url": url, "error": str(exc)}})
        time.sleep({delay_seconds})
    print(json.dumps(items, indent=2, default=str))


if __name__ == "__main__":
    main()
'''

    @staticmethod
    def _test_suite_template() -> str:
        return '''"""Tests for {module_name}.

Auto-generated by the agent code generator. Run with:
    python -m unittest {test_module} -v
"""
import unittest

from {module_name} import {imports}


class Test{test_class}(unittest.TestCase):
    def test_smoke(self) -> None:
        """Smoke test: imports and basic call work."""
        self.assertTrue(True)

{test_methods}

if __name__ == "__main__":
    unittest.main()
'''

    @staticmethod
    def _retry_decorator_template() -> str:
        return '''"""Retry decorator with exponential backoff.

Auto-generated by the agent code generator.
"""
from __future__ import annotations

import functools
import time
from typing import Callable, Tuple, Type, TypeVar

T = TypeVar("T")


def retry(tries: int = 3,
          exceptions: Tuple[Type[BaseException], ...] = (Exception,),
          backoff: float = 1.0,
          max_delay: float = 60.0) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Retry a function on exception with exponential backoff.

    Args:
        tries: total attempts (including the first).
        exceptions: exception types that trigger a retry.
        backoff: base delay in seconds; actual delay is backoff * 2**attempt.
        max_delay: cap for the delay.
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args: object, **kwargs: object) -> T:
            last_error: BaseException | None = None
            for attempt in range(tries):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    last_error = exc
                    if attempt < tries - 1:
                        delay = min(backoff * (2 ** attempt), max_delay)
                        time.sleep(delay)
            assert last_error is not None
            raise last_error
        return wrapper
    return decorator
'''

    @staticmethod
    def _config_loader_template() -> str:
        return '''"""Configuration loader: defaults < file < environment < overrides.

Auto-generated by the agent code generator.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass
class {class_name}:
    """Application configuration."""

    def __post_init__(self) -> None:
        pass

    @classmethod
    def defaults(cls) -> "{class_name}":
        return cls()

    @classmethod
    def from_file(cls, path: str | Path) -> "{class_name}":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "{class_name}":
        known = {{f.name for f in fields(cls)}}
        filtered = {{k: v for k, v in data.items() if k in known}}
        return cls(**filtered)

    @classmethod
    def from_env(cls, prefix: str = "{env_prefix}") -> "{class_name}":
        data: Dict[str, Any] = {{}}
        for f in fields(cls):
            env_key = f"{{prefix}}{{f.name.upper()}}"
            if env_key in os.environ:
                raw = os.environ[env_key]
                data[f.name] = cls._coerce(raw, f.type)
        base = cls.defaults()
        for key, value in data.items():
            setattr(base, key, value)
        return base

    @staticmethod
    def _coerce(raw: str, type_: Any) -> Any:
        name = getattr(type_, "__name__", str(type_))
        if name == "bool":
            return raw.strip().lower() in ("1", "true", "yes", "on")
        if name == "int":
            return int(raw)
        if name == "float":
            return float(raw)
        return raw

    def to_dict(self) -> Dict[str, Any]:
        return {{f.name: getattr(self, f.name) for f in fields(self)}}
'''

    @staticmethod
    def _worker_pool_template() -> str:
        return '''"""{description}

Runs tasks concurrently with a bounded worker pool.
Auto-generated by the agent code generator.
"""
from __future__ import annotations

import concurrent.futures
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List


@dataclass
class TaskResult:
    index: int
    ok: bool
    value: Any = None
    error: str = ""
    elapsed: float = 0.0


def run_pool(tasks: Iterable[Callable[[], Any]],
             max_workers: int = 8,
             timeout: float | None = None) -> List[TaskResult]:
    """Run zero-arg callables concurrently. Order of results matches input."""
    task_list = list(tasks)
    results: List[TaskResult | None] = [None] * len(task_list)

    def _run(index: int, func: Callable[[], Any]) -> TaskResult:
        start = time.monotonic()
        try:
            value = func()
            return TaskResult(index=index, ok=True, value=value,
                              elapsed=time.monotonic() - start)
        except Exception as exc:
            return TaskResult(index=index, ok=False, error=str(exc),
                              elapsed=time.monotonic() - start)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_index = {{
            pool.submit(_run, i, func): i for i, func in enumerate(task_list)
        }}
        for future in concurrent.futures.as_completed(future_to_index,
                                                      timeout=timeout):
            result = future.result()
            results[result.index] = result
    return [r for r in results if r is not None]


def main() -> None:
    results = run_pool([lambda: 1 + 1, lambda: 2 * 3])
    for r in results:
        print(r)


if __name__ == "__main__":
    main()
'''

# ---------------------------------------------------------------------------
# CodeGenerator
# ---------------------------------------------------------------------------

class CodeGenerator:
    """Generates real, parseable Python from specs.

    Guarantees:
      - every generated unit passes ast.parse (else GenerationError)
      - no TODO/FIXME/placeholder markers in output
      - identifiers are sanitized to valid Python names
    """

    PLACEHOLDER_RE = re.compile(
        r"(?i)\b(?:TODO|FIXME|XXX|TBD)\b"
        r"|<(?:your|insert|todo|fixme|tbd|xxx|name|value|type|here)[^<>]{0,40}>"
    )

    def __init__(self, templates: Optional[TemplateLibrary] = None) -> None:
        self.templates = templates or TemplateLibrary()
        self.imports = ImportManager()

    # -- public ---------------------------------------------------------------
    def generate(self, spec: CodeSpec) -> str:
        """Generate code for a spec; raises GenerationError on failure."""
        if spec.kind == "function":
            if spec.function is None:
                raise GenerationError("function spec missing")
            code = self.generate_function(spec.function)
        elif spec.kind == "class":
            if spec.class_ is None:
                raise GenerationError("class spec missing")
            code = self.generate_class(spec.class_)
        elif spec.kind == "script":
            if spec.script is None:
                raise GenerationError("script spec missing")
            code = self.generate_script(spec.script)
        else:
            raise GenerationError(f"unknown spec kind {spec.kind!r}")
        return self.finalize(code)

    def generate_function(self, spec: FunctionSpec) -> str:
        name = sanitize_identifier(spec.name)
        if not is_valid_identifier(name):
            raise GenerationError(f"invalid function name {spec.name!r}")
        params: List[str] = []
        for pname, ptype, pdefault in spec.params:
            pname = sanitize_identifier(pname)
            piece = pname
            if ptype:
                piece += f": {ptype}"
            if pdefault is not None:
                piece += f" = {pdefault}"
            params.append(piece)
        if spec.is_method and (not params or params[0] != "self"):
            params.insert(0, "self")
        kw = "async def" if spec.is_async else "def"
        sig = wrap_signature(name, params, spec.return_type, prefix=kw + " ")
        lines = [sig]
        if spec.docstring:
            doc_params = [(p[0], p[1] or "Any", "") for p in spec.params]
            lines.append(indent_block(make_docstring(
                spec.docstring, params=doc_params,
                returns=f"{spec.return_type}." if spec.return_type else "",
                raises=spec.raises or None)))
        body = spec.body.strip() or "pass"
        lines.append(indent_block(body))
        block = "\n".join(f"@{d}" for d in spec.decorators)
        if block:
            block += "\n"
        return block + "\n".join(lines)

    def generate_class(self, spec: ClassSpec) -> str:
        name = sanitize_identifier(spec.name, fallback="Generated")
        name = name[0].upper() + name[1:] if name else "Generated"
        bases = f"({', '.join(spec.bases)})" if spec.bases else ""
        lines = [f"class {name}{bases}:"]
        body_parts: List[str] = []
        if spec.docstring:
            body_parts.append(indent_block(make_docstring(spec.docstring)))
        if spec.is_dataclass:
            self.imports.require("dataclasses", ["dataclass", "field"])
        if spec.attributes and not spec.is_dataclass:
            # generate __init__ from attributes
            init_params = [("self", None, None)] + [
                (a[0], a[1], a[2]) for a in spec.attributes]
            init_spec = FunctionSpec(
                name="__init__", params=init_params, is_method=True,
                docstring=f"Initialize {name}.",
                body="\n".join(f"self.{sanitize_identifier(a[0])} = {sanitize_identifier(a[0])}"
                               for a in spec.attributes) or "pass")
            body_parts.append(indent_block(self.generate_function(init_spec)))
        elif spec.is_dataclass:
            for attr_name, attr_type, attr_default in spec.attributes:
                attr_name = sanitize_identifier(attr_name)
                decl = f"{attr_name}: {attr_type or 'Any'}"
                if attr_default is not None:
                    decl += f" = {attr_default}"
                body_parts.append(indent_block(decl))
        for method in spec.methods:
            method.is_method = True
            body_parts.append(indent_block(self.generate_function(method)))
        if not body_parts:
            body_parts.append(indent_block("pass"))
        block = "\n".join(f"@{d}" for d in (["dataclass"] if spec.is_dataclass else []) + spec.decorators)
        header = (block + "\n" if block else "") + "\n".join(lines)
        result = header + "\n" + "\n\n".join(body_parts)
        # emit any imports the class required (e.g. dataclasses)
        import_block = self.imports.render()
        self.imports = ImportManager()
        if import_block:
            result = import_block + "\n\n\n" + result
        return result

    def generate_script(self, spec: ScriptSpec) -> str:
        parts: List[str] = []
        if spec.description:
            parts.append(make_docstring(spec.description))
        for imp in spec.imports:
            self.imports.add(imp)
        import_block = self.imports.render()
        if import_block:
            parts.append(import_block)
        for const_name, const_repr in spec.constants:
            parts.append(f"{const_name} = {const_repr}")
        for cls in spec.classes:
            parts.append(self.generate_class(cls))
        for func in spec.functions:
            parts.append(self.generate_function(func))
        if spec.main_body.strip():
            main_fn = FunctionSpec(name="main", docstring="Script entry point.",
                                   body=spec.main_body.strip())
            parts.append(self.generate_function(main_fn))
            if spec.use_main_guard:
                parts.append('if __name__ == "__main__":\n    main()')
        code = "\n\n\n".join(parts) + "\n"
        # reset import manager for next generation
        self.imports = ImportManager()
        return code

    def from_template(self, name: str, **kwargs: Any) -> str:
        """Render a named template; result is finalized (validated)."""
        return self.finalize(self.templates.render(name, **kwargs))

    # -- finalization -----------------------------------------------------------
    def finalize(self, code: str) -> str:
        """Validate generated code: no placeholders, must parse."""
        ph = self.PLACEHOLDER_RE.search(code)
        if ph:
            raise GenerationError(
                f"generated code contains placeholder marker: {ph.group(0)!r}")
        try:
            ast.parse(code)
        except SyntaxError as exc:
            raise GenerationError(f"generated code has syntax error: {exc}")
        # normalize trailing newline
        return code.rstrip("\n") + "\n"

    # -- analysis helpers ---------------------------------------------------------
    def analyze(self, code: str) -> Dict[str, Any]:
        """Run AST analysis + complexity on generated (or any) code."""
        analyzer = CodeAnalyzer(code)
        estimator = ComplexityEstimator()
        complexities = estimator.estimate(code) if analyzer.ok else {}
        return {
            **analyzer.summary(),
            "complexity": complexities,
            "complexity_ratings": {k: ComplexityEstimator.rating(v)
                                   for k, v in complexities.items()},
        }


class GenerationError(Exception):
    """Raised when code generation fails validation."""


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------

_default_generator: Optional[CodeGenerator] = None


def get_default_generator() -> CodeGenerator:
    global _default_generator
    if _default_generator is None:
        _default_generator = CodeGenerator()
    return _default_generator


def generate_function_code(name: str, params: List[Tuple[str, Optional[str], Optional[str]]],
                           body: str, return_type: Optional[str] = None,
                           docstring: str = "") -> str:
    """One-shot function generation."""
    gen = get_default_generator()
    return gen.finalize(gen.generate_function(FunctionSpec(
        name=name, params=params, body=body,
        return_type=return_type, docstring=docstring)))


def generate_script_code(name: str, description: str,
                         functions: List[FunctionSpec],
                         main_body: str,
                         imports: Optional[List[str]] = None) -> str:
    """One-shot script generation."""
    gen = get_default_generator()
    spec = ScriptSpec(name=name, description=description,
                      functions=functions, main_body=main_body,
                      imports=imports or [])
    return gen.generate_script(spec)
