"""Code Development tools — scaffolding, refactoring, translation, review.

Real implementations: template-based project scaffolds (multi-file, runnable),
ast-based symbol indexing/renaming, rule-based code translation (honest scope),
static code review findings, and module dependency analysis.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_TIMEOUT = 60
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass
class DevResult:
    ok: bool
    output: str = ""
    parsed: dict = field(default_factory=dict)
    error: str = ""


def _fail(msg: str) -> DevResult:
    return DevResult(ok=False, error=msg, output=msg)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_files(base: Path, files: dict[str, str]) -> list[str]:
    written = []
    for rel, content in files.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        written.append(rel)
    return written


# ---------------------------------------------------------------------------
# Scaffolds
# ---------------------------------------------------------------------------

def _scaffold_web(name: str) -> dict[str, str]:
    return {
        "index.html": textwrap.dedent(f"""\
            <!DOCTYPE html>
            <html lang="en">
            <head><meta charset="utf-8"/>
            <meta name="viewport" content="width=device-width, initial-scale=1"/>
            <title>{name}</title>
            <link rel="stylesheet" href="styles.css"/></head>
            <body>
            <main id="app"><h1>{name}</h1>
            <p>Scaffolded web app. Edit <code>app.js</code> to build.</p>
            <button id="btn">Click me</button><div id="out"></div></main>
            <script src="app.js"></script>
            </body></html>
            """),
        "styles.css": "body{font-family:system-ui;margin:2rem}#app{max-width:720px;margin:auto}\n",
        "app.js": textwrap.dedent("""\
            document.getElementById('btn').addEventListener('click', () => {
              document.getElementById('out').textContent =
                'Hello from scaffolded app! ' + new Date().toLocaleTimeString();
            });
            """),
        "README.md": f"# {name}\n\nStatic web app scaffold.\n\nServe locally: `python3 -m http.server` in this directory.\n",
    }


def _scaffold_cli(name: str) -> dict[str, str]:
    mod = re.sub(r"[^a-z0-9_]", "_", name.lower())
    return {
        f"{mod}.py": textwrap.dedent(f"""\
            \"\"\"{name} — CLI application scaffold.\"\"\"
            from __future__ import annotations
            import argparse, sys

            def cmd_hello(args: argparse.Namespace) -> int:
                print(f"Hello, {{args.name}}!")
                return 0

            def build_parser() -> argparse.ArgumentParser:
                p = argparse.ArgumentParser(prog="{mod}", description="{name}")
                sub = p.add_subparsers(dest="cmd", required=True)
                h = sub.add_parser("hello", help="Say hello")
                h.add_argument("--name", default="world")
                h.set_defaults(func=cmd_hello)
                return p

            def main(argv=None) -> int:
                args = build_parser().parse_args(argv)
                return args.func(args)

            if __name__ == "__main__":
                raise SystemExit(main())
            """),
        "README.md": f"# {name}\n\nRun: `python3 {mod}.py hello --name Ahsan`\n",
        f"test_{mod}.py": textwrap.dedent(f"""\
            import {mod}
            def test_hello(capsys):
                assert {mod}.main(["hello", "--name", "t"]) == 0
                assert "Hello, t!" in capsys.readouterr().out
            """),
    }


def _scaffold_api(name: str) -> dict[str, str]:
    mod = re.sub(r"[^a-z0-9_]", "_", name.lower())
    return {
        "app.py": textwrap.dedent(f"""\
            \"\"\"{name} — REST API scaffold (stdlib only, no deps).\"\"\"
            from __future__ import annotations
            import json
            from http.server import BaseHTTPRequestHandler, HTTPServer
            from urllib.parse import urlparse

            ITEMS: dict[int, dict] = {{}}
            _next_id = [1]

            class Handler(BaseHTTPRequestHandler):
                def _send(self, code: int, obj) -> None:
                    body = json.dumps(obj).encode()
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def do_GET(self) -> None:
                    path = urlparse(self.path).path
                    if path == "/items":
                        self._send(200, list(ITEMS.values()))
                    elif path.startswith("/items/"):
                        try:
                            i = ITEMS[int(path.rsplit("/", 1)[1])]
                            self._send(200, i)
                        except (KeyError, ValueError):
                            self._send(404, {{"error": "not found"}})
                    elif path == "/health":
                        self._send(200, {{"ok": True}})
                    else:
                        self._send(404, {{"error": "unknown route"}})

                def do_POST(self) -> None:
                    if urlparse(self.path).path != "/items":
                        self._send(404, {{"error": "unknown route"}})
                        return
                    length = int(self.headers.get("Content-Length", 0))
                    data = json.loads(self.rfile.read(length) or b"{{}}")
                    _id = _next_id[0]; _next_id[0] += 1
                    ITEMS[_id] = {{"id": _id, **data}}
                    self._send(201, ITEMS[_id])

                def log_message(self, *a) -> None:  # quiet
                    pass

            def main(port: int = 8000) -> None:
                HTTPServer(("127.0.0.1", port), Handler).serve_forever()

            if __name__ == "__main__":
                main()
            """),
        "README.md": f"# {name}\n\nRun: `python3 app.py` then `curl localhost:8000/health`.\n\nEndpoints: GET /items, GET /items/<id>, POST /items, GET /health.\n",
    }


def _scaffold_saas(name: str) -> dict[str, str]:
    files = _scaffold_api(name + " API")
    files["README.md"] = (
        f"# {name} (SaaS starter)\n\nBackend: stdlib REST API in app.py.\n"
        "Next steps: add auth (JWT), a real database (see db_schema_generate),\n"
        "and a frontend (see web scaffold). Deploy notes in DEPLOY.md.\n")
    files["DEPLOY.md"] = (
        "# Deploy\n\n1. `python3 app.py` behind a reverse proxy.\n"
        "2. Use the `deploy_preview` tool to smoke-test locally.\n"
        "3. Production: run under systemd/supervisor with TLS.\n")
    return files


def _scaffold_lib(name: str) -> dict[str, str]:
    mod = re.sub(r"[^a-z0-9_]", "_", name.lower())
    return {
        "pyproject.toml": textwrap.dedent(f"""\
            [build-system]
            requires = ["setuptools>=61"]
            build-backend = "setuptools.build_meta"
            [project]
            name = "{mod}"
            version = "0.1.0"
            description = "{name} library"
            requires-python = ">=3.9"
            """),
        f"src/{mod}/__init__.py": f'""" {name} library. """\n__version__ = "0.1.0"\n',
        f"src/{mod}/core.py": textwrap.dedent('''\
            """Core logic."""
            def add(a, b):
                """Add two numbers."""
                return a + b
            '''),
        f"tests/test_core.py": textwrap.dedent(f"""\
            from {mod}.core import add
            def test_add():
                assert add(2, 3) == 5
            """),
        "README.md": f"# {name}\n\nInstall (editable): `pip install -e .`\n",
    }


def _scaffold_plugin(name: str) -> dict[str, str]:
    mod = re.sub(r"[^a-z0-9_]", "_", name.lower())
    return {
        f"{mod}_plugin.py": textwrap.dedent(f'''\
            """{name} — plugin scaffold.

            Host contract: the host imports this module and calls
            ``register(host)``; the plugin then hooks events via
            ``host.on(event, handler)``.
            """
            PLUGIN_NAME = "{name}"
            PLUGIN_VERSION = "0.1.0"

            def register(host) -> None:
                host.on("startup", lambda ctx: print(f"[{{PLUGIN_NAME}}] started"))

            def unregister(host) -> None:
                pass
            '''),
        "README.md": f"# {name} plugin\n\nImplement `register(host)` per the host contract above.\n",
    }


def _scaffold_desktop(name: str) -> dict[str, str]:
    mod = re.sub(r"[^a-z0-9_]", "_", name.lower())
    return {
        f"{mod}.py": textwrap.dedent(f'''\
            """{name} — desktop app scaffold (tkinter, stdlib)."""
            import tkinter as tk
            from tkinter import ttk

            class App(tk.Tk):
                def __init__(self) -> None:
                    super().__init__()
                    self.title("{name}")
                    self.geometry("480x320")
                    self.label = ttk.Label(self, text="Hello from {name}!")
                    self.label.pack(pady=20)
                    ttk.Button(self, text="Click",
                               command=self.on_click).pack()

                def on_click(self) -> None:
                    self.label.config(text="Clicked!")

            if __name__ == "__main__":
                App().mainloop()
            '''),
        "README.md": f"# {name}\n\nRun: `python3 {mod}.py` (needs a display).\n",
    }


_SCAFFOLDS = {
    "web": _scaffold_web, "cli": _scaffold_cli, "api": _scaffold_api,
    "saas": _scaffold_saas, "lib": _scaffold_lib, "plugin": _scaffold_plugin,
    "desktop": _scaffold_desktop,
}


def _repo_scaffold(args: dict) -> DevResult:
    kind = str(args.get("kind", "cli")).lower()
    name = str(args.get("name", "myapp"))
    dest = Path(str(args.get("dest", f"./{name}"))).expanduser()
    if kind not in _SCAFFOLDS:
        return _fail(f"unknown kind '{kind}' (web|cli|api|saas|lib|plugin|desktop)")
    if dest.exists() and not args.get("overwrite"):
        return _fail(f"destination exists: {dest} (pass overwrite=true)")
    files = _SCAFFOLDS[kind](name)
    written = _write_files(dest, files)
    return DevResult(ok=True,
                    output=f"scaffolded {kind} project '{name}' in {dest} ({len(written)} files)",
                    parsed={"kind": kind, "dest": str(dest), "files": written})


# ---------------------------------------------------------------------------
# Code completion (heuristic, honest scope)
# ---------------------------------------------------------------------------

def _code_complete(args: dict) -> DevResult:
    path = str(args.get("path", ""))
    line = int(args.get("line", 0))
    prefix = str(args.get("prefix", ""))
    suggestions: list[str] = []
    if path:
        p = Path(path)
        if not p.exists():
            return _fail(f"file not found: {path}")
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError as e:
            return _fail(f"syntax error in {path}: {e}")
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith(prefix):
                    argspec = ", ".join(a.arg for a in node.args.args)
                    suggestions.append(f"{node.name}({argspec})")
            elif isinstance(node, ast.ClassDef):
                if node.name.startswith(prefix):
                    suggestions.append(node.name)
        # imported names
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for a in node.names:
                    nm = a.asname or a.name
                    if nm.startswith(prefix) and nm not in suggestions:
                        suggestions.append(nm)
    else:
        for kw in ("def ", "class ", "import ", "from ", "return ", "for ", "while ",
                   "if ", "elif ", "else:", "try:", "with "):
            if kw.startswith(prefix):
                suggestions.append(kw)
    note = ("Heuristic AST-based completions (symbols defined in file). "
            "Not an LLM — for semantic completion use the model endpoint.")
    return DevResult(ok=True, output="\n".join(suggestions) or "(no suggestions)",
                    parsed={"suggestions": suggestions[:50], "note": note})


# ---------------------------------------------------------------------------
# Multi-file edit
# ---------------------------------------------------------------------------

def _multi_file_edit(args: dict) -> DevResult:
    root = Path(str(args.get("root", "."))).expanduser()
    pattern = str(args.get("pattern", ""))
    repl = str(args.get("replacement", ""))
    glob = str(args.get("glob", "*.py"))
    dry = bool(args.get("dry_run", True))
    if not pattern:
        return _fail("pattern is required")
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return _fail(f"bad regex: {e}")
    changed = []
    for p in root.rglob(glob):
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        new, n = rx.subn(repl, text)
        if n:
            changed.append({"file": str(p), "replacements": n})
            if not dry:
                p.write_text(new, encoding="utf-8")
    return DevResult(
        ok=True,
        output=f"{'would change' if dry else 'changed'} {len(changed)} files"
               + (" (dry_run — pass dry_run=false to apply)" if dry else ""),
        parsed={"dry_run": dry, "files": changed[:100]})


# ---------------------------------------------------------------------------
# Repo understanding: symbol index + dependency graph
# ---------------------------------------------------------------------------

def _repo_understand(args: dict) -> DevResult:
    root = Path(str(args.get("root", "."))).expanduser()
    files = [p for p in root.rglob("*.py")
             if ".git" not in p.parts and "__pycache__" not in p.parts]
    files = files[:500]
    index: dict[str, dict] = {}
    dep_graph: dict[str, list[str]] = {}
    for p in files:
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        rel = str(p.relative_to(root))
        syms = {"classes": [], "functions": [], "imports": []}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                syms["classes"].append(node.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if isinstance(getattr(node, "parent", None), type(None)):
                    syms["functions"].append(node.name)
            elif isinstance(node, ast.Import):
                syms["imports"].extend(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                syms["imports"].append(node.module.split(".")[0])
        index[rel] = syms
        dep_graph[rel] = sorted(set(syms["imports"]))
    return DevResult(
        ok=True,
        output=f"indexed {len(index)} python files under {root}",
        parsed={"files": len(index), "index": index, "dependencies": dep_graph})


# ---------------------------------------------------------------------------
# Rule-based translation (honest scope)
# ---------------------------------------------------------------------------

_PY_TO_JS_RULES = [
    (r"\bdef (\w+)\(([^)]*)\):", r"function \1(\2) {"),
    (r"\bprint\((.*)\)", r"console.log(\1)"),
    (r"\bTrue\b", "true"), (r"\bFalse\b", "false"), (r"\bNone\b", "null"),
    (r"\bself\b", "this"),
    (r"elif ", "else if "),
    (r"#(.*)$", r"//\1"),
]


def _code_translate(args: dict) -> DevResult:
    src = str(args.get("from", "python")).lower()
    dst = str(args.get("to", "javascript")).lower()
    code = str(args.get("code", ""))
    path = str(args.get("path", ""))
    if path:
        code = Path(path).read_text(encoding="utf-8")
    if not code:
        return _fail("no code given (code or path)")
    if src == "python" and dst in ("javascript", "js"):
        out = code
        for pat, rep in _PY_TO_JS_RULES:
            out = re.sub(pat, rep, out, flags=re.MULTILINE)
        note = ("Rule-based translation (syntax patterns only). Review by hand: "
                "indentation blocks, list/dict comprehensions, and stdlib calls "
                "need manual porting.")
        return DevResult(ok=True, output=out, parsed={"note": note})
    return DevResult(ok=False, output="",
                    error=f"translation {src}->{dst} not supported by rule engine "
                          f"(only python->javascript). Use the model for others.")


# ---------------------------------------------------------------------------
# Refactor: rename symbol / extract function
# ---------------------------------------------------------------------------

def _code_refactor(args: dict) -> DevResult:
    action = str(args.get("action", "rename")).lower()
    root = Path(str(args.get("root", "."))).expanduser()
    dry = bool(args.get("dry_run", True))
    if action == "rename":
        old, new = str(args.get("old", "")), str(args.get("new", ""))
        if not old or not new or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", new):
            return _fail("rename needs valid 'old' and 'new' identifiers")
        rx = re.compile(rf"\b{re.escape(old)}\b")
        changed = []
        for p in root.rglob("*.py"):
            if "__pycache__" in p.parts or ".git" in p.parts:
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            new_text, n = rx.subn(new, text)
            if n:
                try:
                    ast.parse(new_text)  # keep it parseable
                except SyntaxError:
                    continue
                changed.append({"file": str(p), "replacements": n})
                if not dry:
                    p.write_text(new_text, encoding="utf-8")
        return DevResult(ok=True,
                        output=f"renamed {old}->{new} in {len(changed)} files"
                               + (" (dry_run)" if dry else ""),
                        parsed={"dry_run": dry, "files": changed[:100]})
    if action == "extract":
        path = str(args.get("path", ""))
        func = str(args.get("function", ""))
        start = int(args.get("start_line", 0))
        end = int(args.get("end_line", 0))
        new_name = str(args.get("new_name", "extracted"))
        if not path or not func or not start or not end:
            return _fail("extract needs path, function, start_line, end_line")
        p = Path(path)
        lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
        if not (0 < start <= end <= len(lines)):
            return _fail("line range out of bounds")
        block = lines[start - 1:end]
        indent = re.match(r"\s*", block[0]).group(0)
        body = textwrap.indent("".join(block), "    ")
        new_def = f"\n{indent}def {new_name}():\n{body}\n"
        call_line = f"{indent}{new_name}()\n"
        new_lines = lines[:start - 1] + [call_line] + lines[end:]
        # insert new def before the enclosing function
        src = "".join(new_lines)
        m = re.search(rf"^(\s*)def {re.escape(func)}\b", src, re.MULTILINE)
        if m:
            ins = m.start()
            src = src[:ins] + textwrap.dedent(new_def) + "\n" + src[ins:]
        try:
            ast.parse(src)
        except SyntaxError as e:
            return _fail(f"extract would break syntax: {e} (no change made)")
        if not dry:
            p.write_text(src, encoding="utf-8")
        return DevResult(ok=True,
                        output=f"extracted lines {start}-{end} into {new_name}()"
                               + (" (dry_run)" if dry else ""),
                        parsed={"dry_run": dry})
    return _fail(f"unknown refactor action '{action}' (rename|extract)")


# ---------------------------------------------------------------------------
# API/SDK scaffolds, schema gen, docs gen, review, arch
# ---------------------------------------------------------------------------

def _api_scaffold(args: dict) -> DevResult:
    name = str(args.get("name", "myapi"))
    endpoints = args.get("endpoints") or ["GET /health", "GET /items", "POST /items"]
    dest = Path(str(args.get("dest", f"./{name}"))).expanduser()
    routes = []
    for ep in endpoints:
        m = re.match(r"\s*(GET|POST|PUT|DELETE|PATCH)\s+(\S+)", str(ep).upper())
        if not m:
            continue
        method, route = m.groups()
        fn = "handle_" + re.sub(r"[^a-z0-9]+", "_", route.strip("/").lower() or "root") \
             + "_" + method.lower()
        routes.append((method, route, fn))
    body = []
    for method, route, fn in routes:
        body.append(textwrap.dedent(f"""\
            def {fn}(request):
                \"\"\"{method} {route}\"\"\"
                return {{"ok": True, "route": "{route}"}}
            """))
    code = ('"""%s — endpoint handlers. Wire into your framework. """\n\n' % name
            + "\n".join(body)
            + "\nROUTES = [\n"
            + "".join(f'    ("{m}", "{r}", {f}),\n' for m, r, f in routes)
            + "]\n")
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "endpoints.py").write_text(code, encoding="utf-8")
    (dest / "README.md").write_text(
        f"# {name}\n\nEndpoints:\n" + "".join(f"- {m} {r}\n" for m, r, f in routes),
        encoding="utf-8")
    return DevResult(ok=True, output=f"wrote {len(routes)} endpoint handlers to {dest}",
                    parsed={"dest": str(dest), "routes": [f"{m} {r}" for m, r, _ in routes]})


def _sdk_scaffold(args: dict) -> DevResult:
    name = str(args.get("name", "myapi"))
    base_url = str(args.get("base_url", "http://localhost:8000"))
    endpoints = args.get("endpoints") or ["GET /items", "POST /items"]
    dest = Path(str(args.get("dest", f"./{name}_sdk"))).expanduser()
    methods = []
    for ep in endpoints:
        m = re.match(r"\s*(GET|POST|PUT|DELETE|PATCH)\s+(\S+)", str(ep).upper())
        if not m:
            continue
        method, route = m.groups()
        fn = re.sub(r"[^a-z0-9]+", "_", route.strip("/").lower() or "root")
        methods.append(textwrap.dedent(f"""\
            def {fn}(self, payload=None):
                return self._req("{method}", "{route}", payload)
            """))
    code = textwrap.dedent(f'''\
        """{name} client SDK (urllib, no deps)."""
        import json, urllib.request

        class {name.title().replace("_", "")}Client:
            def __init__(self, base_url="{base_url}", timeout=30):
                self.base_url = base_url.rstrip("/")
                self.timeout = timeout

            def _req(self, method, route, payload=None):
                data = json.dumps(payload).encode() if payload is not None else None
                req = urllib.request.Request(self.base_url + route, data=data,
                                             method=method,
                                             headers={{"Content-Type": "application/json"}})
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read() or b"null")

        ''') + "\n".join(methods)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "client.py").write_text(code, encoding="utf-8")
    return DevResult(ok=True, output=f"SDK written to {dest}/client.py",
                    parsed={"dest": str(dest)})


def _db_schema_generate(args: dict) -> DevResult:
    table = re.sub(r"[^A-Za-z0-9_]", "", str(args.get("table", "items")))
    fields = args.get("fields") or [{"name": "id", "type": "INTEGER PRIMARY KEY"},
                                    {"name": "name", "type": "TEXT NOT NULL"}]
    cols = []
    for f in fields:
        nm = re.sub(r"[^A-Za-z0-9_]", "", str(f.get("name", "col")))
        typ = str(f.get("type", "TEXT")).upper()
        cols.append(f"    {nm} {typ}")
    ddl = f"CREATE TABLE IF NOT EXISTS {table} (\n" + ",\n".join(cols) + "\n);"
    out = str(args.get("output", ""))
    if out:
        Path(out).write_text(ddl + "\n", encoding="utf-8")
    return DevResult(ok=True, output=ddl, parsed={"table": table, "output": out})


def _docs_generate(args: dict) -> DevResult:
    path = str(args.get("path", "."))
    p = Path(path)
    if p.is_file():
        files = [p]
        root = p.parent
    else:
        root = p
        files = [f for f in root.rglob("*.py")
                 if "__pycache__" not in f.parts and ".git" not in f.parts][:200]
    doc_parts = []
    for f in files:
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        mod_doc = ast.get_docstring(tree) or ""
        doc_parts.append(f"## {f.name}\n\n{mod_doc}\n")
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                d = ast.get_docstring(node) or ""
                doc_parts.append(f"### {node.name}\n\n{d}\n")
    md = "# API docs (generated)\n\n" + "\n".join(doc_parts)
    out = str(args.get("output", ""))
    if out:
        Path(out).write_text(md, encoding="utf-8")
    return DevResult(ok=True, output=f"generated docs for {len(files)} files",
                    parsed={"files": len(files), "output": out,
                            "preview": md[:2000]})


def _code_review(args: dict) -> DevResult:
    path = str(args.get("path", "."))
    p = Path(path)
    files = [p] if p.is_file() else [f for f in p.rglob("*.py")
                                    if "__pycache__" not in f.parts
                                    and ".git" not in f.parts][:300]
    findings = []
    for f in files:
        try:
            src = f.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except SyntaxError as e:
            findings.append({"file": str(f), "severity": "error",
                            "msg": f"syntax error: {e}"})
            continue
        lines = src.splitlines()
        for i, ln in enumerate(lines, 1):
            if "TODO" in ln or "FIXME" in ln:
                findings.append({"file": str(f), "line": i,
                                "severity": "info", "msg": ln.strip()[:120]})
            if re.search(r"except\s*:", ln):
                findings.append({"file": str(f), "line": i,
                                "severity": "warning", "msg": "bare except:"})
            if "eval(" in ln or "exec(" in ln:
                findings.append({"file": str(f), "line": i,
                                "severity": "warning",
                                "msg": "eval/exec usage — review carefully"})
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                loc = getattr(node, "end_lineno", node.lineno) - node.lineno
                if loc > 80:
                    findings.append({"file": str(f), "line": node.lineno,
                                    "severity": "info",
                                    "msg": f"long function {node.name} ({loc} lines)"})
                # complexity: count branches
                branches = sum(isinstance(n, (ast.If, ast.For, ast.While,
                                             ast.ExceptHandler, ast.With))
                               for n in ast.walk(node))
                if branches > 10:
                    findings.append({"file": str(f), "line": node.lineno,
                                    "severity": "warning",
                                    "msg": f"high branch count in {node.name} ({branches})"})
    return DevResult(ok=True,
                    output=f"reviewed {len(files)} files, {len(findings)} findings",
                    parsed={"files": len(files), "findings": findings[:200]})


def _arch_analyze(args: dict) -> DevResult:
    root = Path(str(args.get("root", "."))).expanduser()
    mods: dict[str, set[str]] = {}
    for p in root.rglob("*.py"):
        if "__pycache__" in p.parts or ".git" in p.parts:
            continue
        rel = str(p.relative_to(root))
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
        mods[rel] = imports
    # local edges: import of a sibling top-level package dir name
    local_pkgs = {str(p.relative_to(root)).split("/")[0]
                  for p in root.rglob("*.py")
                  if "__pycache__" not in p.parts and ".git" not in p.parts}
    edges = []
    for rel, imps in mods.items():
        for imp in imps:
            if imp in local_pkgs:
                edges.append({"from": rel, "to": imp})
    return DevResult(ok=True,
                    output=f"{len(mods)} modules, {len(edges)} internal edges",
                    parsed={"modules": len(mods), "edges": edges[:300]})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def _sch(t: str, req: bool, desc: str) -> dict:
    return {"type": t, "required": req, "description": desc}


HANDLERS: dict = {
    "repo_scaffold": _repo_scaffold,
    "code_complete": _code_complete,
    "multi_file_edit": _multi_file_edit,
    "repo_understand": _repo_understand,
    "code_translate": _code_translate,
    "code_refactor": _code_refactor,
    "api_scaffold": _api_scaffold,
    "sdk_scaffold": _sdk_scaffold,
    "db_schema_generate": _db_schema_generate,
    "docs_generate": _docs_generate,
    "code_review": _code_review,
    "arch_analyze": _arch_analyze,
}

TOOL_DEFS: list[tuple] = [
    ("repo_scaffold", "Scaffold multi-file project (web|cli|api|saas|lib|plugin|desktop)",
     {"kind": _sch("string", False, "web|cli|api|saas|lib|plugin|desktop"),
      "name": _sch("string", False, "Project name"),
      "dest": _sch("string", False, "Destination dir"),
      "overwrite": _sch("boolean", False, "Replace existing dir")},
     "computer", 60),
    ("code_complete", "Heuristic AST-based code completions for a file position",
     {"path": _sch("string", False, "Python file (omit for keywords)"),
      "line": _sch("integer", False, "Line number"),
      "prefix": _sch("string", False, "Identifier prefix")},
     "computer", 30),
    ("multi_file_edit", "Regex replace across files (dry-run default)",
     {"root": _sch("string", False, "Root dir"),
      "pattern": _sch("string", True, "Regex pattern"),
      "replacement": _sch("string", False, "Replacement"),
      "glob": _sch("string", False, "File glob (default *.py)"),
      "dry_run": _sch("boolean", False, "Preview only (default true)")},
     "computer", 120),
    ("repo_understand", "Index symbols + import graph across a repo",
     {"root": _sch("string", False, "Repo root")},
     "computer", 120),
    ("code_translate", "Rule-based python->javascript translation (honest scope)",
     {"from": _sch("string", False, "Source lang (python)"),
      "to": _sch("string", False, "Target lang (javascript)"),
      "code": _sch("string", False, "Code text"),
      "path": _sch("string", False, "Or file path")},
     "computer", 30),
    ("code_refactor", "rename symbol across files | extract lines into function",
     {"action": _sch("string", False, "rename|extract"),
      "root": _sch("string", False, "Root for rename"),
      "old": _sch("string", False, "Old identifier"),
      "new": _sch("string", False, "New identifier"),
      "path": _sch("string", False, "File for extract"),
      "function": _sch("string", False, "Enclosing function"),
      "start_line": _sch("integer", False, "Start line"),
      "end_line": _sch("integer", False, "End line"),
      "new_name": _sch("string", False, "New function name"),
      "dry_run": _sch("boolean", False, "Preview only (default true)")},
     "computer", 120),
    ("api_scaffold", "Scaffold REST endpoint handlers from a route list",
     {"name": _sch("string", False, "API name"),
      "endpoints": _sch("array", False, 'e.g. ["GET /items","POST /items"]'),
      "dest": _sch("string", False, "Destination dir")},
     "computer", 60),
    ("sdk_scaffold", "Scaffold a urllib client SDK from endpoints",
     {"name": _sch("string", False, "API name"),
      "base_url": _sch("string", False, "Base URL"),
      "endpoints": _sch("array", False, "Endpoint list"),
      "dest": _sch("string", False, "Destination dir")},
     "computer", 60),
    ("db_schema_generate", "Generate CREATE TABLE DDL from field specs",
     {"table": _sch("string", False, "Table name"),
      "fields": _sch("array", False, "[{name, type}]"),
      "output": _sch("string", False, "Write .sql path")},
     "computer", 30),
    ("docs_generate", "Generate markdown docs from docstrings via ast",
     {"path": _sch("string", False, "File or dir"),
      "output": _sch("string", False, "Write .md path")},
     "computer", 120),
    ("code_review", "Static review: syntax, bare excepts, TODOs, complexity",
     {"path": _sch("string", False, "File or dir")},
     "computer", 180),
    ("arch_analyze", "Module dependency graph for a repo",
     {"root": _sch("string", False, "Repo root")},
     "computer", 120),
]
