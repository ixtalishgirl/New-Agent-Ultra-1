"""Advanced debugging & performance tools.

Real implementations: cProfile CPU profiling with hotspot reports, tracemalloc
memory profiling/allocation tracking, thread dumps, sqlite query plans, network
latency measurement, AST-based complexity heuristics, benchmark regression
detection, and optimization suggestion/verification. Honest notes where a
backend (GPU, renderer) is absent.
"""
from __future__ import annotations

import ast
import cProfile
import faulthandler
import io
import json
import pstats
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import tracemalloc
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TIMEOUT = 300
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass
class PerfResult:
    ok: bool
    output: str = ""
    parsed: dict = field(default_factory=dict)
    error: str = ""


def _fail(msg: str) -> PerfResult:
    return PerfResult(ok=False, error=msg, output=msg)


def _sch(t: str, req: bool, desc: str) -> dict:
    return {"type": t, "required": req, "description": desc}


def _profile_cpu(args: dict) -> PerfResult:
    code = str(args.get("code", ""))
    topn = int(args.get("top", 15))
    if not code:
        return _fail("code required")
    pr = cProfile.Profile()
    ns: dict = {}
    try:
        pr.enable()
        exec(compile(code, "<profile>", "exec"), ns)
        pr.disable()
    except Exception as e:
        pr.disable()
        return _fail(f"profiled code raised: {e}")
    s = io.StringIO()
    ps = pstats.Stats(pr, stream=s).sort_stats("cumulative")
    ps.print_stats(topn)
    report = s.getvalue()
    # hotspot extraction
    hotspots = []
    for line in report.splitlines():
        m = re.match(r"\s*[\d.]+\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+(.*)",
                     line)
        if m:
            hotspots.append({"cumtime": float(m.group(3)), "location": m.group(5).strip()})
            if len(hotspots) >= topn:
                break
    return PerfResult(ok=True, output=report[:5000],
                     parsed={"hotspots": hotspots})


def _profile_memory(args: dict) -> PerfResult:
    code = str(args.get("code", ""))
    topn = int(args.get("top", 15))
    if not code:
        return _fail("code required")
    tracemalloc.start()
    ns: dict = {}
    try:
        exec(compile(code, "<memprofile>", "exec"), ns)
        snap = tracemalloc.take_snapshot()
    except Exception as e:
        return _fail(f"profiled code raised: {e}")
    finally:
        tracemalloc.stop()
    rows = []
    for stat in snap.statistics("filename")[:topn]:
        frame = stat.traceback[0] if stat.traceback else None
        rows.append({"file": str(frame.filename) if frame else "?",
                    "line": frame.lineno if frame else 0,
                    "size_kb": round(stat.size / 1024, 1),
                    "count": stat.count})
    total_kb = round(sum(s.size for s in snap.statistics("filename")) / 1024, 1)
    return PerfResult(ok=True,
                     output=f"total {total_kb} KB across {len(rows)} top sites",
                     parsed={"total_kb": total_kb, "top": rows})


def _alloc_track(args: dict) -> PerfResult:
    code = str(args.get("code", ""))
    if not code:
        return _fail("code required")
    tracemalloc.start(10)
    ns: dict = {}
    try:
        exec(compile(code, "<alloctrack>", "exec"), ns)
        snap = tracemalloc.take_snapshot()
    except Exception as e:
        return _fail(f"code raised: {e}")
    finally:
        tracemalloc.stop()
    rows = []
    for stat in snap.statistics("traceback")[:10]:
        frames = stat.traceback  # Frame objects (not .format() strings)
        tb = [f"{fr.filename}:{fr.lineno}" for fr in frames[:3]]
        rows.append({"size_kb": round(stat.size / 1024, 1), "traceback": tb})
    return PerfResult(ok=True, output=f"{len(rows)} allocation sites",
                     parsed={"sites": rows})


def _thread_analyze(args: dict) -> PerfResult:
    import tempfile as _tf
    # faulthandler needs a real file (fileno) — StringIO won't do.
    with _tf.NamedTemporaryFile("w+", suffix=".threads", delete=True) as fh:
        faulthandler.dump_traceback(file=fh)
        fh.flush()
        fh.seek(0)
        dump = fh.read()
    threads = re.findall(r"[Tt]hread (0x[0-9a-fA-F]+)(?: \(([^)]+)\))?", dump)
    frames = re.findall(r'File "([^"]+)", line (\d+), in (\S+)', dump)
    return PerfResult(ok=True,
                     output=f"{len(threads)} threads in this process",
                     parsed={"threads": [{"id": t[0], "name": t[1]} for t in threads],
                             "frames": [{"file": f, "line": int(l), "func": fn}
                                        for f, l, fn in frames[:40]]})


def _db_query_profile(args: dict) -> PerfResult:
    db_path = str(args.get("db", ":memory:"))
    queries = args.get("queries") or ["SELECT 1"]
    setup = str(args.get("setup", ""))
    db = sqlite3.connect(db_path)
    out = []
    try:
        if setup:
            db.executescript(setup)
        for q in queries:
            plan = db.execute(f"EXPLAIN QUERY PLAN {q}").fetchall()
            t0 = time.time()
            db.execute(str(q)).fetchall()
            ms = (time.time() - t0) * 1000
            out.append({"query": str(q)[:120],
                       "plan": [tuple(r) for r in plan],
                       "ms": round(ms, 2)})
    except Exception as e:
        return _fail(f"query failed: {e}")
    finally:
        db.close()
    slow = [o for o in out if o["ms"] > 50]
    return PerfResult(ok=True,
                     output=f"{len(out)} queries profiled, {len(slow)} slow (>50ms)",
                     parsed={"queries": out,
                             "note": "add indexes on columns used in WHERE/JOIN of slow queries"})


def _net_latency(args: dict) -> PerfResult:
    url = str(args.get("url", ""))
    n = min(int(args.get("samples", 5)), 20)
    if not url:
        return _fail("url required")
    samples = []
    for _ in range(n):
        t0 = time.time()
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                r.read(1024)
            samples.append((time.time() - t0) * 1000)
        except Exception as e:
            samples.append(-1)
    good = [s for s in samples if s >= 0]
    stats = {"samples": n, "ok": len(good), "failed": n - len(good),
             "min_ms": round(min(good), 1) if good else None,
             "mean_ms": round(sum(good) / len(good), 1) if good else None,
             "max_ms": round(max(good), 1) if good else None}
    return PerfResult(ok=bool(good), output=json.dumps(stats), parsed=stats)


def _complexity_estimate(args: dict) -> PerfResult:
    path = str(args.get("path", ""))
    if not path:
        return _fail("path required")
    p = Path(path)
    if not p.exists():
        return _fail(f"not found: {path}")
    try:
        tree = ast.parse(p.read_text(encoding="utf-8"))
    except SyntaxError as e:
        return _fail(f"syntax error: {e}")
    results = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            depth = 0
            for sub in ast.walk(node):
                if isinstance(sub, (ast.For, ast.AsyncFor, ast.While,
                                   ast.comprehension)):
                    # nesting depth
                    d = 1
                    parent = sub
                    results_depth = 0
                    depth = max(depth, d)
            # better: compute max loop nesting via recursion
            def nest(n, d):
                m = d
                for ch in ast.iter_child_nodes(n):
                    inc = 1 if isinstance(ch, (ast.For, ast.AsyncFor, ast.While)) else 0
                    m = max(m, nest(ch, d + inc))
                return m
            depth = nest(node, 0)
            rec = any(isinstance(n, ast.Call) and getattr(n.func, "id", "") == node.name
                      for n in ast.walk(node))
            est = "O(1)"
            if rec:
                est = "O(2^n)? (recursive — verify)"
            elif depth >= 3:
                est = f"O(n^{depth})?"
            elif depth == 2:
                est = "O(n^2)?"
            elif depth == 1:
                est = "O(n)?"
            results.append({"function": node.name, "line": node.lineno,
                           "loop_depth": depth, "recursive": rec,
                           "estimate": est})
    return PerfResult(ok=True, output=f"analyzed {len(results)} functions",
                     parsed={"functions": results,
                             "note": "heuristic from loop nesting; verify with profiling"})


def _perf_regression(args: dict) -> PerfResult:
    code = str(args.get("code", ""))
    baseline_ms = float(args.get("baseline_ms", 0))
    runs = min(int(args.get("runs", 10)), 100)
    tolerance = float(args.get("tolerance_pct", 20))
    if not code:
        return _fail("code required")
    if baseline_ms <= 0:
        return _fail("baseline_ms required (>0)")
    ns: dict = {}
    try:
        compiled = compile(code, "<perfregr>", "exec")
    except SyntaxError as e:
        return _fail(f"syntax error: {e}")
    times = []
    for _ in range(runs):
        t0 = time.time()
        try:
            exec(compiled, ns)
        except Exception as e:
            return _fail(f"code raised: {e}")
        times.append((time.time() - t0) * 1000)
    times.sort()
    median = times[len(times) // 2]
    delta = (median - baseline_ms) / baseline_ms * 100
    regressed = delta > tolerance
    return PerfResult(ok=not regressed,
                     output=(f"median {median:.2f}ms vs baseline {baseline_ms:.2f}ms "
                             f"({delta:+.1f}%) — "
                             + ("REGRESSION" if regressed else "within tolerance")),
                     parsed={"median_ms": round(median, 2), "baseline_ms": baseline_ms,
                             "delta_pct": round(delta, 1), "regressed": regressed})


def _optimize_suggest(args: dict) -> PerfResult:
    path = str(args.get("path", ""))
    if not path:
        return _fail("path required")
    p = Path(path)
    if not p.exists():
        return _fail(f"not found: {path}")
    try:
        tree = ast.parse(p.read_text(encoding="utf-8"))
        src = p.read_text(encoding="utf-8")
    except SyntaxError as e:
        return _fail(f"syntax error: {e}")
    tips = []
    if re.search(r"for .* in .*:\s*\n\s*.*\.append\(", src):
        tips.append("list-building loop -> consider list comprehension")
    if ".items()" not in src and re.search(r"for \w+ in \w+:\s*\n\s*\w+\[", src):
        tips.append("dict key loop with indexing -> iterate .items()")
    if re.search(r"\+\=.*\[\]", src) or re.search(r"\"\".join", src) is None and "+=" in src:
        pass
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "range":
            if any(isinstance(k, ast.Call) and getattr(k.func, "id", "") == "len"
                   for k in ast.walk(node)):
                tips.append(f"line {node.lineno}: range(len(x)) -> enumerate(x)")
        if isinstance(node, ast.JoinedStr):
            pass
    # string concat in loop
    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.While)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.AugAssign) and isinstance(sub.op, ast.Add):
                    tips.append(f"line {getattr(sub, 'lineno', '?')}: "
                                "string/list += in loop -> use join() or extend()")
                    break
    tips = sorted(set(tips))
    return PerfResult(ok=True,
                     output="\n".join(tips) or "no obvious heuristic wins found",
                     parsed={"suggestions": tips,
                             "note": "heuristics only — verify with profile_cpu"})


def _optimize_verify(args: dict) -> PerfResult:
    before = str(args.get("before", ""))
    after = str(args.get("after", ""))
    runs = min(int(args.get("runs", 10)), 50)
    if not before or not after:
        return _fail("before and after code required")
    def bench(code):
        ns: dict = {}
        c = compile(code, "<opt>", "exec")
        ts = []
        for _ in range(runs):
            t0 = time.time()
            exec(c, ns)
            ts.append((time.time() - t0) * 1000)
        ts.sort()
        return ts[len(ts) // 2]
    try:
        mb, ma = bench(before), bench(after)
    except Exception as e:
        return _fail(f"benchmark raised: {e}")
    improv = (mb - ma) / mb * 100 if mb else 0
    return PerfResult(ok=True,
                     output=f"before {mb:.2f}ms -> after {ma:.2f}ms ({improv:+.1f}%)",
                     parsed={"before_ms": round(mb, 2), "after_ms": round(ma, 2),
                             "improvement_pct": round(improv, 1)})


def _gpu_profile_note(args: dict) -> PerfResult:
    has_nvidia = shutil.which("nvidia-smi") is not None
    if has_nvidia:
        try:
            r = subprocess.run(["nvidia-smi",
                                "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                                "--format=csv,noheader"],
                               capture_output=True, text=True, timeout=15)
            return PerfResult(ok=True, output=r.stdout.strip(),
                             parsed={"gpus": r.stdout.strip().splitlines()})
        except Exception as e:
            return _fail(f"nvidia-smi failed: {e}")
    return PerfResult(ok=False, output="",
                     error="no GPU detected (nvidia-smi absent). GPU profiling "
                           "needs CUDA hardware + nvidia-smi.")


def _render_perf_note(args: dict) -> PerfResult:
    return PerfResult(ok=True,
                     output=("Rendering performance analysis needs a real renderer "
                             "(browser/GPU/engine). Use browser_console + "
                             "screen_record timings, or game_build with an engine, "
                             "then profile with profile_cpu."),
                     parsed={"guidance": True})


def _cache_perf_note(args: dict) -> PerfResult:
    code = str(args.get("code", ""))
    if not code:
        return PerfResult(ok=True,
                         output=("Cache analysis: wrap suspects with functools.lru_cache "
                                 "and compare via optimize_verify; measure hit rates "
                                 "via cache_info()."),
                         parsed={"guidance": True})
    # run code that should print/use lru_cache and report cache_info if present
    ns: dict = {}
    try:
        exec(compile(code, "<cache>", "exec"), ns)
    except Exception as e:
        return _fail(f"code raised: {e}")
    infos = {}
    for k, v in ns.items():
        ci = getattr(v, "cache_info", None)
        if callable(ci):
            try:
                infos[k] = str(ci())
            except Exception:
                pass
    return PerfResult(ok=True, output=json.dumps(infos, indent=2) or "no lru_cache found",
                     parsed={"cache_info": infos})


def _concurrency_note(args: dict) -> PerfResult:
    return PerfResult(ok=True,
                     output=("Concurrency analysis: check thread_analyze for live threads; "
                             "suspect shared mutable state -> add locks or use "
                             "queue.Queue; verify with stress_test + deadlock_detect."),
                     parsed={"guidance": True})


HANDLERS: dict = {
    "profile_cpu": _profile_cpu,
    "profile_memory": _profile_memory,
    "alloc_track": _alloc_track,
    "thread_analyze": _thread_analyze,
    "db_query_profile": _db_query_profile,
    "net_latency": _net_latency,
    "complexity_estimate": _complexity_estimate,
    "perf_regression": _perf_regression,
    "optimize_suggest": _optimize_suggest,
    "optimize_verify": _optimize_verify,
    "gpu_profile_note": _gpu_profile_note,
    "render_perf_note": _render_perf_note,
    "cache_perf_note": _cache_perf_note,
    "concurrency_note": _concurrency_note,
}

TOOL_DEFS: list[tuple] = [
    ("profile_cpu", "cProfile a snippet; hotspot report",
     {"code": _sch("string", True, "Python code"),
      "top": _sch("integer", False, "Top N rows")}, "computer", 300),
    ("profile_memory", "tracemalloc memory profile of a snippet",
     {"code": _sch("string", True, "Python code"),
      "top": _sch("integer", False, "Top N sites")}, "computer", 300),
    ("alloc_track", "Allocation traceback sites for a snippet",
     {"code": _sch("string", True, "Python code")}, "computer", 300),
    ("thread_analyze", "Dump live threads of this process",
     {}, "computer", 30),
    ("db_query_profile", "EXPLAIN QUERY PLAN + timing for sqlite queries",
     {"db": _sch("string", False, "DB path (default :memory:)"),
      "setup": _sch("string", False, "Setup SQL"),
      "queries": _sch("array", False, "Queries")}, "computer", 120),
    ("net_latency", "Measure HTTP latency samples",
     {"url": _sch("string", True, "URL"),
      "samples": _sch("integer", False, "Default 5, max 20")}, "computer", 120),
    ("complexity_estimate", "Heuristic Big-O from loop nesting via ast",
     {"path": _sch("string", True, "Python file")}, "computer", 60),
    ("perf_regression", "Compare snippet median vs baseline_ms",
     {"code": _sch("string", True, "Python code"),
      "baseline_ms": _sch("number", True, "Baseline ms"),
      "runs": _sch("integer", False, "Default 10"),
      "tolerance_pct": _sch("integer", False, "Default 20")}, "computer", 300),
    ("optimize_suggest", "Heuristic optimization suggestions for a file",
     {"path": _sch("string", True, "Python file")}, "computer", 60),
    ("optimize_verify", "Before/after benchmark of two snippets",
     {"before": _sch("string", True, "Before code"),
      "after": _sch("string", True, "After code"),
      "runs": _sch("integer", False, "Default 10")}, "computer", 300),
    ("gpu_profile_note", "nvidia-smi stats if GPU present, else honest note",
     {}, "computer", 30),
    ("render_perf_note", "Guidance for renderer performance analysis",
     {}, "computer", 10),
    ("cache_perf_note", "Run snippet; report lru_cache stats if present",
     {"code": _sch("string", False, "Python code")}, "computer", 60),
    ("concurrency_note", "Guidance for concurrency analysis",
     {}, "computer", 10),
]
