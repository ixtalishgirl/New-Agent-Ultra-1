"""Testing & QA tools.

Real implementations: pytest runner, pytest scaffold generation from functions
via ast, coverage.py when installed, concurrent HTTP load testing with stats,
random-input fuzzing of a target function, sqlite-backed DB tests, and honest
capability notes where a real backend is absent.
"""
from __future__ import annotations

import ast
import concurrent.futures
import json
import os
import random
import re
import shutil
import sqlite3
import string
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT = 300
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass
class TestResult:
    ok: bool
    output: str = ""
    parsed: dict = field(default_factory=dict)
    error: str = ""


def _fail(msg: str) -> TestResult:
    return TestResult(ok=False, error=msg, output=msg)


def _sch(t: str, req: bool, desc: str) -> dict:
    return {"type": t, "required": req, "description": desc}


def _run_pytest(target: str, timeout: int, extra: list[str] | None = None):
    cmd = [sys.executable, "-m", "pytest", target, "-q", "-p", "no:cacheprovider"]
    if extra:
        cmd.extend(extra)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, cwd=str(REPO_ROOT))
    except subprocess.TimeoutExpired:
        return None
    return r


def _test_run(args: dict) -> TestResult:
    target = str(args.get("target", "agent/tests"))
    timeout = int(args.get("timeout", 600))
    markers = str(args.get("markers", ""))
    extra = ["-m", markers] if markers else []
    r = _run_pytest(target, timeout, extra)
    if r is None:
        return TestResult(ok=False, output="pytest timed out", parsed={"timeout": True})
    tail = (r.stdout + r.stderr)[-5000:]
    passed = re.findall(r"(\d+) passed", tail)
    failed = re.findall(r"(\d+) failed", tail)
    return TestResult(ok=r.returncode == 0,
                    output="\n".join(tail.strip().splitlines()[-5:]),
                    parsed={"rc": r.returncode, "passed": passed, "failed": failed})


def _test_generate(args: dict) -> TestResult:
    path = str(args.get("path", ""))
    out = str(args.get("output", ""))
    if not path:
        return _fail("path required")
    p = Path(path)
    if not p.exists():
        return _fail(f"not found: {path}")
    try:
        tree = ast.parse(p.read_text(encoding="utf-8"))
    except SyntaxError as e:
        return _fail(f"syntax error: {e}")
    mod = p.stem
    tests = [f'"""Generated tests for {p.name}."""', f"import {mod}", ""]
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and not node.name.startswith("_"):
            count += 1
            argnames = [a.arg for a in node.args.args][:3]
            callargs = ", ".join("None" for _ in argnames)
            tests.append(textwrap.dedent(f'''\
                def test_{node.name}_smoke():
                    # TODO: replace with real assertions
                    try:
                        {mod}.{node.name}({callargs})
                    except TypeError:
                        pass  # signature differs — fill in real args
                    except Exception:
                        pass
                '''))
    code = "\n".join(tests)
    dest = out or str(p.parent / f"test_{mod}_gen.py")
    Path(dest).write_text(code, encoding="utf-8")
    return TestResult(ok=True, output=f"generated {count} smoke tests -> {dest}",
                    parsed={"tests": count, "output": dest,
                            "note": "smoke-level; fill real assertions"})


def _coverage_run(args: dict) -> TestResult:
    target = str(args.get("target", "agent/tests"))
    timeout = int(args.get("timeout", 600))
    if not shutil.which("coverage"):
        return TestResult(ok=False, output="",
                         error="coverage.py not installed (`pip install coverage`). "
                               "Install it, then rerun.")
    try:
        subprocess.run([sys.executable, "-m", "coverage", "run", "-m", "pytest",
                        target, "-q", "-p", "no:cacheprovider"],
                       capture_output=True, text=True, timeout=timeout,
                       cwd=str(REPO_ROOT))
        r = subprocess.run([sys.executable, "-m", "coverage", "report", "-m"],
                           capture_output=True, text=True, timeout=60,
                           cwd=str(REPO_ROOT))
    except subprocess.TimeoutExpired:
        return _fail("coverage run timed out")
    return TestResult(ok=True, output=r.stdout[-4000:], parsed={"report": r.stdout[-4000:]})


def _http_hit(url: str, timeout: int) -> tuple[bool, float]:
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            r.read(65536)
        return True, (time.time() - t0) * 1000
    except Exception:
        return False, (time.time() - t0) * 1000


def _load_test(args: dict) -> TestResult:
    url = str(args.get("url", ""))
    n = min(int(args.get("requests", 50)), 500)
    conc = min(int(args.get("concurrency", 5)), 50)
    timeout = int(args.get("timeout", 120))
    if not url:
        return _fail("url required")
    lat: list[float] = []
    okc = 0
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=conc) as ex:
        futs = [ex.submit(_http_hit, url, 15) for _ in range(n)]
        for f in concurrent.futures.as_completed(futs, timeout=timeout):
            ok, ms = f.result()
            okc += ok
            lat.append(ms)
    dur = time.time() - t0
    lat.sort()
    def pct(p: float) -> float:
        return lat[int(p * (len(lat) - 1))] if lat else 0.0
    stats = {"requests": n, "ok": okc, "failed": n - okc,
             "duration_s": round(dur, 2), "rps": round(n / dur, 1) if dur else 0,
             "p50_ms": round(pct(.5), 1), "p95_ms": round(pct(.95), 1),
             "p99_ms": round(pct(.99), 1)}
    return TestResult(ok=okc > 0, output=json.dumps(stats, indent=2), parsed=stats)


def _stress_test(args: dict) -> TestResult:
    args = dict(args)
    args["requests"] = min(int(args.get("requests", 200)), 2000)
    args["concurrency"] = min(int(args.get("concurrency", 20)), 100)
    r = _load_test(args)
    r.parsed["mode"] = "stress"
    return r


def _fuzz_run(args: dict) -> TestResult:
    code = str(args.get("code", ""))
    func = str(args.get("function", "fuzz_target"))
    iters = min(int(args.get("iterations", 200)), 5000)
    seed = int(args.get("seed", 0))
    if not code:
        return _fail("code required (must define function named by 'function')")
    rnd = random.Random(seed)
    crashes = []
    ns: dict = {}
    try:
        exec(compile(code, "<fuzz>", "exec"), ns)
    except Exception as e:
        return _fail(f"setup failed: {e}")
    target = ns.get(func)
    if not callable(target):
        return _fail(f"function '{func}' not defined by code")
    alphabet = string.ascii_letters + string.digits + " \n\t\x00!@#"
    for i in range(iters):
        kind = rnd.choice(["str", "int", "list", "none"])
        if kind == "str":
            arg = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 64)))
        elif kind == "int":
            arg = rnd.choice([0, 1, -1, 2**31 - 1, -(2**31), 10**18])
        elif kind == "list":
            arg = [rnd.randint(-99, 99) for _ in range(rnd.randint(0, 10))]
        else:
            arg = None
        try:
            target(arg)
        except Exception as e:
            crashes.append({"iter": i, "arg_repr": repr(arg)[:120],
                           "error": f"{type(e).__name__}: {e}"[:200]})
            if len(crashes) >= 20:
                break
    return TestResult(ok=True,
                    output=f"{iters} iterations, {len(crashes)} crashes",
                    parsed={"iterations": iters, "crashes": crashes})


def _api_test(args: dict) -> TestResult:
    base = str(args.get("base_url", "")).rstrip("/")
    cases = args.get("cases") or [{"method": "GET", "path": "/health",
                                  "expect_status": 200}]
    if not base:
        return _fail("base_url required")
    results = []
    for c in cases:
        method = str(c.get("method", "GET"))
        path = str(c.get("path", "/"))
        expect = int(c.get("expect_status", 200))
        body = c.get("body")
        try:
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(base + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                status, payload = r.status, r.read(65536).decode("utf-8", "replace")
        except Exception as e:
            status, payload = -1, f"{type(e).__name__}: {e}"
        results.append({"case": f"{method} {path}", "status": status,
                       "expected": expect, "pass": status == expect,
                       "body_head": payload[:300]})
    passed = sum(1 for r in results if r["pass"])
    return TestResult(ok=passed == len(results),
                    output=f"{passed}/{len(results)} cases passed",
                    parsed={"results": results})


def _db_test(args: dict) -> TestResult:
    sql = args.get("sql") or ["SELECT 1"]
    db = sqlite3.connect(":memory:")
    results = []
    try:
        for stmt in sql:
            try:
                cur = db.execute(str(stmt))
                rows = cur.fetchall() if cur.description else []
                results.append({"sql": str(stmt)[:120], "ok": True,
                               "rows": len(rows)})
            except Exception as e:
                results.append({"sql": str(stmt)[:120], "ok": False,
                               "error": str(e)[:200]})
    finally:
        db.close()
    passed = sum(1 for r in results if r["ok"])
    return TestResult(ok=passed == len(results),
                    output=f"{passed}/{len(results)} statements ok",
                    parsed={"results": results})


def _e2e_note(args: dict) -> TestResult:
    return TestResult(
        ok=True,
        output=("End-to-end testing needs a running system under test. "
                "Use browser_* tools + api_test + load_test to script an E2E "
                "flow against your staging URL; use deploy_preview to serve it locally."),
        parsed={"guidance": True})


def _ui_test(args: dict) -> TestResult:
    url = str(args.get("url", ""))
    checks = args.get("checks") or ["title_present"]
    if not url:
        return _fail("url required (page to smoke-test)")
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            html = r.read(200000).decode("utf-8", "replace")
    except Exception as e:
        return TestResult(ok=False, output="", error=f"fetch failed: {e}")
    results = []
    for c in checks:
        c = str(c)
        if c == "title_present":
            ok = bool(re.search(r"<title>[^<]+</title>", html, re.I))
        elif c.startswith("text:"):
            ok = c[5:] in html
        elif c.startswith("selector:"):
            ok = c[9:] in html  # substring heuristic
        else:
            ok = False
        results.append({"check": c, "pass": bool(ok)})
    passed = sum(1 for r in results if r["pass"])
    return TestResult(ok=passed == len(results),
                    output=f"{passed}/{len(results)} UI checks passed",
                    parsed={"results": results})


def _property_test(args: dict) -> TestResult:
    try:
        import hypothesis  # noqa
        from hypothesis import given, strategies as st, settings
    except ImportError:
        return TestResult(ok=False, output="",
                         error="hypothesis not installed (`pip install hypothesis`).")
    code = str(args.get("code", ""))
    prop = str(args.get("property", ""))
    if not code or not prop:
        return _fail("code and property required")
    ns: dict = {}
    try:
        exec(compile(code, "<prop>", "exec"), ns)
        test_src = (f"from hypothesis import given, strategies as st, settings\n"
                    f"{code}\n"
                    f"@settings(max_examples={int(args.get('examples', 50))})\n"
                    f"@given(st.integers(), st.integers())\n"
                    f"def _t(a, b):\n    assert ({prop}), 'property failed'\n"
                    f"_t()\n")
        exec(compile(test_src, "<proptest>", "exec"), {})
    except AssertionError as e:
        return TestResult(ok=False, output=f"property falsified: {e}",
                         parsed={"falsified": True})
    except Exception as e:
        return _fail(f"property test error: {e}")
    return TestResult(ok=True, output="property held for generated examples",
                    parsed={"falsified": False})


def _a11y_check(args: dict) -> TestResult:
    url = str(args.get("url", ""))
    html = str(args.get("html", ""))
    if url and not html:
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                html = r.read(200000).decode("utf-8", "replace")
        except Exception as e:
            return _fail(f"fetch failed: {e}")
    if not html:
        return _fail("url or html required")
    issues = []
    for m in re.finditer(r"<img(?![^>]*alt=)[^>]*>", html, re.I):
        issues.append("img without alt attribute")
    if not re.search(r"<title>[^<]+</title>", html, re.I):
        issues.append("missing <title>")
    if not re.search(r"<html[^>]*lang=", html, re.I):
        issues.append("missing lang attribute on <html>")
    for m in re.finditer(r"<input(?![^>]*aria-label)(?![^>]*id=)[^>]*>", html, re.I):
        issues.append("input without label/aria-label")
        break
    return TestResult(ok=not issues,
                    output="no basic a11y issues found" if not issues
                    else f"{len(issues)} issues: " + "; ".join(issues[:20]),
                    parsed={"issues": issues[:50],
                            "note": "heuristic checks only; use axe-core for full audits"})


def _fail_repro(args: dict) -> TestResult:
    code = str(args.get("code", ""))
    if not code:
        return _fail("code required (failing snippet to reproduce)")
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(code)
        tmp = fh.name
    try:
        r = subprocess.run([sys.executable, tmp], capture_output=True,
                           text=True, timeout=int(args.get("timeout", 30)))
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    reproduced = r.returncode != 0
    return TestResult(ok=True,
                    output=("reproduced" if reproduced else "did NOT reproduce")
                           + f" (rc={r.returncode})\n{r.stderr[-2000:]}",
                    parsed={"reproduced": reproduced, "rc": r.returncode,
                            "stderr": r.stderr[-2000:]})


HANDLERS: dict = {
    "test_run": _test_run,
    "test_generate": _test_generate,
    "coverage_run": _coverage_run,
    "load_test": _load_test,
    "stress_test": _stress_test,
    "fuzz_run": _fuzz_run,
    "api_test": _api_test,
    "db_test": _db_test,
    "e2e_note": _e2e_note,
    "ui_test": _ui_test,
    "property_test": _property_test,
    "a11y_check": _a11y_check,
    "fail_repro": _fail_repro,
}

TOOL_DEFS: list[tuple] = [
    ("test_run", "Run pytest on a target (markers supported)",
     {"target": _sch("string", False, "Test path"),
      "markers": _sch("string", False, "pytest -m expression"),
      "timeout": _sch("integer", False, "Seconds")}, "computer", 900),
    ("test_generate", "Generate pytest smoke scaffolds from a module's functions",
     {"path": _sch("string", True, "Python module"),
      "output": _sch("string", False, "Output test file")}, "computer", 60),
    ("coverage_run", "coverage.py run + report (fails with hint if missing)",
     {"target": _sch("string", False, "Test path"),
      "timeout": _sch("integer", False, "Seconds")}, "computer", 900),
    ("load_test", "Concurrent HTTP load test with latency stats",
     {"url": _sch("string", True, "Target URL"),
      "requests": _sch("integer", False, "Total requests (default 50, max 500)"),
      "concurrency": _sch("integer", False, "Workers (default 5, max 50)"),
      "timeout": _sch("integer", False, "Seconds")}, "computer", 300),
    ("stress_test", "Heavier load test (up to 2000 req)",
     {"url": _sch("string", True, "Target URL"),
      "requests": _sch("integer", False, "Default 200"),
      "concurrency": _sch("integer", False, "Default 20"),
      "timeout": _sch("integer", False, "Seconds")}, "computer", 600),
    ("fuzz_run", "Random-input fuzzing of a python function",
     {"code": _sch("string", True, "Code defining the function"),
      "function": _sch("string", False, "Function name (default fuzz_target)"),
      "iterations": _sch("integer", False, "Default 200, max 5000"),
      "seed": _sch("integer", False, "Random seed")}, "computer", 300),
    ("api_test", "Run HTTP API test cases against a base URL",
     {"base_url": _sch("string", True, "Base URL"),
      "cases": _sch("array", False, "[{method,path,expect_status,body}]")}, "computer", 120),
    ("db_test", "Run SQL statements against throwaway sqlite",
     {"sql": _sch("array", False, "Statements")}, "computer", 60),
    ("e2e_note", "Guidance for scripting end-to-end tests",
     {}, "computer", 10),
    ("ui_test", "Fetch a page, run basic UI checks",
     {"url": _sch("string", True, "Page URL"),
      "checks": _sch("array", False, "[title_present|text:X|selector:X]")}, "computer", 60),
    ("property_test", "hypothesis property test (fails with hint if missing)",
     {"code": _sch("string", True, "Code defining helpers"),
      "property": _sch("string", True, "Boolean expression over a,b"),
      "examples": _sch("integer", False, "Default 50")}, "computer", 300),
    ("a11y_check", "Heuristic accessibility checks on HTML",
     {"url": _sch("string", False, "Page URL"),
      "html": _sch("string", False, "Or raw HTML")}, "computer", 60),
    ("fail_repro", "Reproduce a failure from a code snippet",
     {"code": _sch("string", True, "Failing snippet"),
      "timeout": _sch("integer", False, "Seconds")}, "computer", 120),
]
