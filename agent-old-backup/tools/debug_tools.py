"""Bug fixing & debugging tools.

Real implementations: static bug scanning, traceback parsing, patch proposal
generation for common error patterns (applied only on explicit approval),
regression checking via the test suite, tracemalloc-based leak detection,
faulthandler deadlock dumps, and dependency conflict analysis.
"""
from __future__ import annotations

import ast
import faulthandler
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

DEFAULT_TIMEOUT = 120
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass
class DebugResult:
    ok: bool
    output: str = ""
    parsed: dict = field(default_factory=dict)
    error: str = ""


def _fail(msg: str) -> DebugResult:
    return DebugResult(ok=False, error=msg, output=msg)


def _sch(t: str, req: bool, desc: str) -> dict:
    return {"type": t, "required": req, "description": desc}


# ---------------------------------------------------------------------------
# bug_scan: static checks across a repo
# ---------------------------------------------------------------------------

_RISK_PATTERNS = [
    (re.compile(r"\beval\s*\("), "eval() usage"),
    (re.compile(r"\bexec\s*\("), "exec() usage"),
    (re.compile(r"subprocess\.(call|run|Popen)\s*\([^)]*shell\s*=\s*True"), "shell=True"),
    (re.compile(r"except\s*:\s*$", re.MULTILINE), "bare except"),
    (re.compile(r"password\s*=\s*['\"][^'\"]+['\"]", re.I), "hardcoded password"),
    (re.compile(r"api[_-]?key\s*=\s*['\"][^'\"]+['\"]", re.I), "hardcoded api key"),
    (re.compile(r"TODO|FIXME|XXX|HACK"), "todo marker"),
]


def _bug_scan(args: dict) -> DebugResult:
    root = Path(str(args.get("root", "."))).expanduser()
    files = [p for p in root.rglob("*.py")
             if "__pycache__" not in p.parts and ".git" not in p.parts][:400]
    findings = []
    for p in files:
        try:
            src = p.read_text(encoding="utf-8")
        except Exception:
            continue
        try:
            compile(src, str(p), "exec")
        except SyntaxError as e:
            findings.append({"file": str(p), "kind": "syntax",
                            "msg": f"{e.msg} at line {e.lineno}"})
            continue
        for rx, label in _RISK_PATTERNS:
            for m in rx.finditer(src):
                ln = src.count("\n", 0, m.start()) + 1
                findings.append({"file": str(p), "line": ln, "kind": "pattern",
                                "msg": label})
        # unused imports via ast (simple)
        try:
            tree = ast.parse(src)
        except Exception:
            continue
        imported = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    imported[(a.asname or a.name).split(".")[0]] = node.lineno
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    if a.name != "*":
                        imported[a.asname or a.name] = node.lineno
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        used |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        for name, ln in imported.items():
            if name not in used:
                findings.append({"file": str(p), "line": ln, "kind": "unused-import",
                                "msg": f"unused import: {name}"})
    # pyflakes if available
    pyflakes = shutil.which("pyflakes")
    extra = ""
    if pyflakes:
        try:
            r = subprocess.run([pyflakes] + [str(p) for p in files[:100]],
                               capture_output=True, text=True, timeout=120)
            extra = r.stdout[:4000]
        except Exception:
            pass
    return DebugResult(ok=True,
                      output=f"scanned {len(files)} files, {len(findings)} findings"
                             + (f"\npyflakes:\n{extra}" if extra else ""),
                      parsed={"files": len(files), "findings": findings[:300]})


# ---------------------------------------------------------------------------
# type_check: mypy if present, else annotation coverage
# ---------------------------------------------------------------------------

def _type_check(args: dict) -> DebugResult:
    path = str(args.get("path", "."))
    mypy = shutil.which("mypy")
    if mypy:
        try:
            r = subprocess.run([mypy, path], capture_output=True, text=True,
                               timeout=180)
            return DebugResult(ok=r.returncode == 0, output=r.stdout[:6000],
                              parsed={"tool": "mypy", "rc": r.returncode})
        except Exception as e:
            return _fail(f"mypy failed: {e}")
    # fallback: annotation coverage via ast
    p = Path(path)
    files = [p] if p.is_file() else [f for f in p.rglob("*.py")
                                     if "__pycache__" not in f.parts
                                     and ".git" not in f.parts][:200]
    total, annotated = 0, 0
    missing = []
    for f in files:
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                total += 1
                has_ann = bool(node.returns) or any(
                    a.annotation for a in node.args.args)
                if has_ann:
                    annotated += 1
                else:
                    missing.append(f"{f.name}:{node.lineno} {node.name}")
    pct = round(100 * annotated / total, 1) if total else 0
    return DebugResult(
        ok=True,
        output=(f"mypy not installed — annotation coverage: {annotated}/{total} "
                f"({pct}%). `pip install mypy` for real type checking."),
        parsed={"coverage_pct": pct, "missing": missing[:100]})


# ---------------------------------------------------------------------------
# stacktrace_analyze
# ---------------------------------------------------------------------------

_TB_HINTS = [
    (re.compile(r"NameError: name '(\w+)' is not defined"), "undefined name — typo or missing import"),
    (re.compile(r"ImportError|ModuleNotFoundError"), "missing dependency or bad import path"),
    (re.compile(r"KeyError: (.+)"), "dict key missing — validate input keys"),
    (re.compile(r"IndexError"), "sequence index out of range — check lengths"),
    (re.compile(r"TypeError: .*argument"), "wrong argument count/type in a call"),
    (re.compile(r"AttributeError: '(\w+)' object has no attribute '(\w+)'"),
     "attribute missing — wrong type or typo"),
    (re.compile(r"ZeroDivisionError"), "division by zero — guard denominator"),
    (re.compile(r"FileNotFoundError"), "path does not exist — check cwd/paths"),
    (re.compile(r"PermissionError"), "OS permission denied"),
    (re.compile(r"RecursionError"), "infinite recursion — check base case"),
    (re.compile(r"TimeoutError|concurrent.futures.*Timeout"), "operation timed out"),
]


def _stacktrace_analyze(args: dict) -> DebugResult:
    tb = str(args.get("traceback", ""))
    path = str(args.get("path", ""))
    if path and not tb:
        tb = Path(path).read_text(encoding="utf-8")
    if not tb.strip():
        return _fail("no traceback given (traceback or path)")
    frames = re.findall(r'File "([^"]+)", line (\d+), in (\S+)', tb)
    err_line = tb.strip().splitlines()[-1] if tb.strip() else ""
    hints = [h for rx, h in _TB_HINTS if rx.search(tb)]
    return DebugResult(ok=True, output=err_line or "(no error line found)",
                      parsed={"error_line": err_line, "frames": frames[-8:],
                              "likely_causes": hints or ["no known pattern matched"]})


# ---------------------------------------------------------------------------
# log_analyze + root_cause
# ---------------------------------------------------------------------------

def _log_analyze(args: dict) -> DebugResult:
    path = str(args.get("path", ""))
    tail = int(args.get("tail", 500))
    if not path:
        return _fail("path required")
    p = Path(path)
    if not p.exists():
        return _fail(f"not found: {path}")
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-tail:]
    buckets: dict[str, int] = {}
    err_lines = []
    for ln in lines:
        m = re.search(r"(ERROR|Error|error|Traceback|Exception|CRITICAL|FATAL)", ln)
        if m:
            key = ln.strip()[:100]
            buckets[key] = buckets.get(key, 0) + 1
            if len(err_lines) < 50:
                err_lines.append(ln.strip()[:200])
    top = sorted(buckets.items(), key=lambda kv: -kv[1])[:20]
    return DebugResult(ok=True,
                      output=f"{len(lines)} lines scanned, {len(err_lines)} error-ish",
                      parsed={"top_errors": top, "samples": err_lines})


def _root_cause(args: dict) -> DebugResult:
    tb = str(args.get("traceback", ""))
    log_path = str(args.get("log_path", ""))
    if log_path and not tb:
        r = _log_analyze({"path": log_path, "tail": 200})
        samples = r.parsed.get("samples", [])
        tb = "\n".join(samples[-5:])
    if not tb.strip():
        return _fail("need traceback or log_path")
    st = _stacktrace_analyze({"traceback": tb})
    frames = st.parsed.get("frames", [])
    causes = st.parsed.get("likely_causes", [])
    # heuristic: deepest user frame (not stdlib/site-packages)
    suspect = None
    for f, ln, fn in reversed(frames):
        if "site-packages" not in f and "<" not in f:
            suspect = {"file": f, "line": ln, "function": fn}
            break
    return DebugResult(ok=True,
                      output=f"suspect: {suspect} — {'; '.join(causes)}",
                      parsed={"suspect_frame": suspect, "causes": causes,
                              "frames": frames})


# ---------------------------------------------------------------------------
# patch_generate (template fixes for common errors; applied only with approval)
# ---------------------------------------------------------------------------

_PATCH_TEMPLATES = [
    ("NameError", re.compile(r"NameError: name '(\w+)' is not defined"),
     lambda m: f"# FIX: '{m.group(1)}' is undefined — define it or import it, e.g.:\n"
               f"{m.group(1)} = None  # TODO: give a real value"),
    ("KeyError-safe-get", re.compile(r"KeyError: (.+)"),
     lambda m: f"# FIX: use .get() with a default instead of direct indexing:\n"
               f"# value = mapping.get({m.group(1)}, default)"),
    ("None-guard", re.compile(r"AttributeError: 'NoneType'"),
     lambda m: "# FIX: guard against None before attribute access:\n"
               "# if obj is not None:\n#     obj.attr"),
    ("Index-guard", re.compile(r"IndexError"),
     lambda m: "# FIX: check length before indexing:\n# if i < len(seq):\n#     seq[i]"),
    ("ZeroDivision", re.compile(r"ZeroDivisionError"),
     lambda m: "# FIX: guard the denominator:\n# if denom != 0:\n#     x / denom"),
]


def _patch_generate(args: dict) -> DebugResult:
    tb = str(args.get("traceback", ""))
    apply = bool(args.get("apply", False))
    target = str(args.get("target", ""))
    if not tb.strip():
        return _fail("traceback required")
    proposals = []
    for name, rx, tpl in _PATCH_TEMPLATES:
        m = rx.search(tb)
        if m:
            proposals.append({"pattern": name, "patch": tpl(m)})
    if not proposals:
        return DebugResult(ok=True, output="no template matched this traceback",
                          parsed={"proposals": []})
    note = ("Template-based proposals only — review before applying. "
            "Pass apply=true + target=<file> to append the patch as comments.")
    applied = False
    if apply and target:
        p = Path(target)
        if p.exists():
            with p.open("a", encoding="utf-8") as fh:
                fh.write("\n# --- patch proposal ---\n")
                for pr in proposals:
                    fh.write(pr["patch"] + "\n")
            applied = True
    return DebugResult(ok=True, output="\n\n".join(pr["patch"] for pr in proposals),
                      parsed={"proposals": proposals, "applied": applied, "note": note})


def _multi_file_fix(args: dict) -> DebugResult:
    # apply the same patch_generate proposal across files containing a pattern
    root = Path(str(args.get("root", "."))).expanduser()
    pattern = str(args.get("pattern", ""))
    patch_text = str(args.get("patch", ""))
    dry = bool(args.get("dry_run", True))
    if not pattern or not patch_text:
        return _fail("pattern and patch required")
    rx = re.compile(pattern)
    touched = []
    for p in root.rglob("*.py"):
        if "__pycache__" in p.parts or ".git" in p.parts:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        if rx.search(text):
            touched.append(str(p))
            if not dry:
                p.write_text(text + "\n# --- auto patch ---\n" + patch_text + "\n",
                             encoding="utf-8")
    return DebugResult(ok=True,
                      output=f"{len(touched)} files match" + (" (dry_run)" if dry else " (patched)"),
                      parsed={"dry_run": dry, "files": touched[:100]})


# ---------------------------------------------------------------------------
# regression_check / fix_verify / test_failure_diagnose
# ---------------------------------------------------------------------------

def _run_pytest(target: str, timeout: int) -> dict:
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", target, "-q",
                            "-p", "no:cacheprovider"],
                           capture_output=True, text=True, timeout=timeout,
                           cwd=str(REPO_ROOT))
    except subprocess.TimeoutExpired:
        return {"rc": -1, "summary": "timeout", "output": ""}
    tail = (r.stdout + r.stderr)[-4000:]
    m = re.search(r"(\d+) passed|(\d+) failed|(\d+) error", tail)
    return {"rc": r.returncode, "summary": tail.strip().splitlines()[-3:],
            "output": tail}


def _regression_check(args: dict) -> DebugResult:
    target = str(args.get("target", "agent/tests"))
    timeout = int(args.get("timeout", 600))
    before = _run_pytest(target, timeout)
    return DebugResult(ok=before["rc"] == 0,
                      output="regression baseline:\n" + "\n".join(before["summary"]),
                      parsed={"baseline": before,
                              "note": "run again after your change and diff summaries"})


def _fix_verify(args: dict) -> DebugResult:
    target = str(args.get("target", "agent/tests"))
    timeout = int(args.get("timeout", 600))
    res = _run_pytest(target, timeout)
    return DebugResult(ok=res["rc"] == 0,
                      output="fix verification:\n" + "\n".join(res["summary"]),
                      parsed={"result": res})


def _test_failure_diagnose(args: dict) -> DebugResult:
    target = str(args.get("target", "agent/tests"))
    timeout = int(args.get("timeout", 600))
    res = _run_pytest(target, timeout)
    if res["rc"] == 0:
        return DebugResult(ok=True, output="no failures", parsed={})
    fails = re.findall(r"FAILED (\S+)", res["output"])
    errs = re.findall(r"ERROR (\S+)", res["output"])
    analysis = _stacktrace_analyze({"traceback": res["output"]})
    return DebugResult(ok=True,
                      output=f"{len(fails)} failed, {len(errs)} errors",
                      parsed={"failed": fails[:50], "errors": errs[:50],
                              "traceback_hints": analysis.parsed})


# ---------------------------------------------------------------------------
# memleak_detect / deadlock_detect / race_note / dep_conflicts / runtime_check
# ---------------------------------------------------------------------------

def _memleak_detect(args: dict) -> DebugResult:
    code = str(args.get("code", ""))
    rounds = int(args.get("rounds", 5))
    if not code:
        return _fail("code required (python snippet to run repeatedly)")
    import tracemalloc
    tracemalloc.start()
    try:
        for _ in range(rounds):
            exec(compile(code, "<probe>", "exec"), {"__name__": "__probe__"})
        snap1 = tracemalloc.take_snapshot()
        for _ in range(rounds):
            exec(compile(code, "<probe>", "exec"), {"__name__": "__probe__"})
        snap2 = tracemalloc.take_snapshot()
    except Exception as e:
        return _fail(f"probe raised: {e}")
    finally:
        tracemalloc.stop()
    diff = snap2.compare_to(snap1, "filename")
    growth = sum(s.size_diff for s in diff if s.size_diff > 0)
    top = [{"file": str(s.traceback[0].filename) if s.traceback else "?",
            "line": s.traceback[0].lineno if s.traceback else 0,
            "growth_bytes": s.size_diff} for s in diff[:10] if s.size_diff > 0]
    verdict = "likely leak" if growth > 1024 * 100 else "no significant growth"
    return DebugResult(ok=True, output=f"{verdict}: +{growth} bytes over {rounds} rounds",
                      parsed={"growth_bytes": growth, "verdict": verdict, "top": top})


def _deadlock_detect(args: dict) -> DebugResult:
    secs = int(args.get("sample_seconds", 5))
    # faulthandler needs a real file (fileno) — StringIO won't do.
    with tempfile.NamedTemporaryFile("w+", suffix=".threads", delete=True) as fh:
        faulthandler.dump_traceback(file=fh)
        fh.flush()
        fh.seek(0)
        dump = fh.read()
    time.sleep(min(secs, 10))
    threads = re.findall(r'[Tt]hread (0x[0-9a-fA-F]+)', dump)
    waiting = len(re.findall(r'locked|waiting|acquire', dump, re.I))
    return DebugResult(
        ok=True,
        output=f"{len(threads)} threads sampled; lock/wait mentions: {waiting}",
        parsed={"threads": len(threads), "lock_mentions": waiting,
                "dump_head": dump[:3000],
                "note": "point-in-time sample of THIS process; for target "
                        "processes, run with faulthandler enabled and send SIGABRT"})


def _race_note(args: dict) -> DebugResult:
    return DebugResult(
        ok=True,
        output=("Race-condition analysis is heuristic here: run the suspect code "
                "under `threading` with many iterations and compare outputs; "
                "use `python -X dev` and faulthandler for warnings. "
                "No deterministic detector exists for arbitrary code."),
        parsed={"heuristic": True})


def _dep_conflicts(args: dict) -> DebugResult:
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "check"],
                           capture_output=True, text=True, timeout=60)
    except Exception as e:
        return _fail(f"pip check failed: {e}")
    out = r.stdout.strip()
    return DebugResult(ok=True,
                      output=out or "no dependency conflicts reported by pip check",
                      parsed={"conflicts": out.splitlines()[:50]})


def _runtime_check(args: dict) -> DebugResult:
    code = str(args.get("code", ""))
    timeout = int(args.get("timeout", 30))
    if not code:
        return _fail("code required")
    try:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return DebugResult(ok=False, output="runtime check timed out",
                          parsed={"timeout": True})
    ok = r.returncode == 0
    analysis = None
    if not ok and r.stderr:
        analysis = _stacktrace_analyze({"traceback": r.stderr}).parsed
    return DebugResult(ok=ok, output=r.stdout[-2000:] or r.stderr[-2000:],
                      parsed={"rc": r.returncode, "analysis": analysis})


HANDLERS: dict = {
    "bug_scan": _bug_scan,
    "type_check": _type_check,
    "stacktrace_analyze": _stacktrace_analyze,
    "log_analyze": _log_analyze,
    "root_cause": _root_cause,
    "patch_generate": _patch_generate,
    "multi_file_fix": _multi_file_fix,
    "regression_check": _regression_check,
    "fix_verify": _fix_verify,
    "test_failure_diagnose": _test_failure_diagnose,
    "memleak_detect": _memleak_detect,
    "deadlock_detect": _deadlock_detect,
    "race_note": _race_note,
    "dep_conflicts": _dep_conflicts,
    "runtime_check": _runtime_check,
}

TOOL_DEFS: list[tuple] = [
    ("bug_scan", "Static bug scan: syntax, risky patterns, unused imports (+pyflakes)",
     {"root": _sch("string", False, "Repo root")}, "computer", 180),
    ("type_check", "mypy if installed, else annotation-coverage report",
     {"path": _sch("string", False, "File or dir")}, "computer", 240),
    ("stacktrace_analyze", "Parse a traceback: frames + likely causes",
     {"traceback": _sch("string", False, "Traceback text"),
      "path": _sch("string", False, "Or file containing it")}, "computer", 30),
    ("log_analyze", "Bucket error lines in a log file",
     {"path": _sch("string", True, "Log file"),
      "tail": _sch("integer", False, "Lines to scan (default 500)")}, "computer", 60),
    ("root_cause", "Heuristic root-cause from traceback or log",
     {"traceback": _sch("string", False, "Traceback text"),
      "log_path": _sch("string", False, "Or log file")}, "computer", 60),
    ("patch_generate", "Template fix proposals for common errors (apply needs target)",
     {"traceback": _sch("string", True, "Traceback text"),
      "apply": _sch("boolean", False, "Append as comments"),
      "target": _sch("string", False, "File to append to")}, "computer", 30),
    ("multi_file_fix", "Append a patch comment to all files matching a pattern",
     {"root": _sch("string", False, "Root"),
      "pattern": _sch("string", True, "Regex to find"),
      "patch": _sch("string", True, "Patch text"),
      "dry_run": _sch("boolean", False, "Default true")}, "computer", 120),
    ("regression_check", "Record pytest baseline for later diffing",
     {"target": _sch("string", False, "Test path"),
      "timeout": _sch("integer", False, "Seconds")}, "computer", 900),
    ("fix_verify", "Run pytest to verify a fix",
     {"target": _sch("string", False, "Test path"),
      "timeout": _sch("integer", False, "Seconds")}, "computer", 900),
    ("test_failure_diagnose", "Run pytest, list failures + traceback hints",
     {"target": _sch("string", False, "Test path"),
      "timeout": _sch("integer", False, "Seconds")}, "computer", 900),
    ("memleak_detect", "tracemalloc growth check over repeated exec of code",
     {"code": _sch("string", True, "Python snippet"),
      "rounds": _sch("integer", False, "Rounds (default 5)")}, "computer", 120),
    ("deadlock_detect", "Sample this process threads via faulthandler",
     {"sample_seconds": _sch("integer", False, "Seconds (default 5)")}, "computer", 60),
    ("race_note", "Honest guidance for race-condition hunting",
     {}, "computer", 10),
    ("dep_conflicts", "pip check for dependency conflicts",
     {}, "computer", 90),
    ("runtime_check", "Run a python snippet, analyze any traceback",
     {"code": _sch("string", True, "Python code"),
      "timeout": _sch("integer", False, "Seconds")}, "computer", 120),
]
