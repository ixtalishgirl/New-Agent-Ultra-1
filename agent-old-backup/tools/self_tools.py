"""Self-improvement & tool-creation tools.

Real implementations: persistent failure->fix memory, registry tool
discovery, tool specification/test generation, capability gap analysis
against the live registry, improvement hypotheses, git branches for patch
proposals, tagged releases, and knowledge-base updates.
"""
from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TIMEOUT = 120
STATE_DIR = Path.home() / ".haley_agent"
FAIL_MEM = STATE_DIR / "failure_memory.jsonl"
KB_DIR = Path.home() / ".haley_kb"


@dataclass
class SelfResult:
    ok: bool
    output: str = ""
    parsed: dict = field(default_factory=dict)
    error: str = ""


def _fail(msg: str) -> SelfResult:
    return SelfResult(ok=False, error=msg, output=msg)


def _sch(t: str, req: bool, desc: str) -> dict:
    return {"type": t, "required": req, "description": desc}


def _failure_memory(args: dict) -> SelfResult:
    action = str(args.get("action", "add")).lower()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if action == "add":
        failure = str(args.get("failure", ""))
        fix = str(args.get("fix", ""))
        if not failure:
            return _fail("failure required")
        rec = {"ts": time.time(), "failure": failure[:1000], "fix": fix[:1000],
               "tags": args.get("tags") or []}
        with FAIL_MEM.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
        return SelfResult(ok=True, output="failure recorded",
                         parsed={"recorded": True})
    if action == "search":
        q = str(args.get("query", "")).lower()
        hits = []
        if FAIL_MEM.exists():
            for ln in FAIL_MEM.read_text().splitlines():
                try:
                    rec = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if not q or q in rec.get("failure", "").lower() \
                        or q in rec.get("fix", "").lower():
                    hits.append(rec)
        return SelfResult(ok=True, output=f"{len(hits)} matching failures",
                         parsed={"hits": hits[-20:]})
    return _fail("action must be add|search")


def _tool_discover(args: dict) -> SelfResult:
    query = str(args.get("query", "")).lower()
    try:
        from .registry import build_full_registry
    except Exception as e:
        return _fail(f"registry unavailable: {e}")
    reg = build_full_registry()
    out = []
    for t in reg.discover():
        blob = f"{t['name']} {t.get('description', '')}".lower()
        if not query or query in blob:
            out.append({"name": t["name"],
                        "description": t.get("description", "")[:120],
                        "enabled": t.get("enabled", True)})
    return SelfResult(ok=True, output=f"{len(out)} tools match '{query}'",
                     parsed={"tools": out[:100]})


def _tool_spec_gen(args: dict) -> SelfResult:
    name = re.sub(r"[^a-z0-9_]", "", str(args.get("name", "")))
    purpose = str(args.get("purpose", ""))
    if not name or not purpose:
        return _fail("name and purpose required")
    params = args.get("params") or [{"name": "input", "type": "string",
                                     "required": True, "desc": "Input"}]
    spec = {"name": name, "description": purpose,
            "input_schema": {p["name"]: {"type": p.get("type", "string"),
                                        "required": bool(p.get("required")),
                                        "description": p.get("desc", "")}
                             for p in params},
            "handler_skeleton": textwrap.dedent(f"""\
                def _{name}(args: dict):
                    value = str(args.get("input", ""))
                    # TODO: implement
                    return {{"ok": True, "output": value}}
                """)}
    out = str(args.get("output", ""))
    if out:
        Path(out).write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return SelfResult(ok=True, output=f"spec for '{name}' ready",
                     parsed={"spec": spec, "output": out})


def _tool_test_gen(args: dict) -> SelfResult:
    name = re.sub(r"[^a-z0-9_]", "", str(args.get("name", "")))
    if not name:
        return _fail("name required")
    code = textwrap.dedent(f'''\
        """Generated smoke test for tool '{name}'."""
        from agent.tools.registry import build_full_registry, collect_handlers

        def test_{name}_smoke():
            reg = build_full_registry()
            assert reg.get("{name}") is not None, "tool not registered"
            handlers = collect_handlers()
            assert "{name}" in handlers, "handler missing"
            # TODO: call handlers["{name}"]({{...safe args...}}) and assert .ok
        ''')
    out = str(args.get("output", f"agent/tests/unit/test_gen_{name}.py"))
    Path(out).write_text(code, encoding="utf-8")
    return SelfResult(ok=True, output=f"test scaffold -> {out}",
                     parsed={"output": out})


def _gap_analyze(args: dict) -> SelfResult:
    requested = args.get("requested") or []
    if not requested:
        return _fail("requested required (list of capability names)")
    try:
        from .registry import build_full_registry
    except Exception as e:
        return _fail(f"registry unavailable: {e}")
    names = {t["name"] for t in build_full_registry().discover()}
    have, missing = [], []
    for r in requested:
        key = re.sub(r"[^a-z0-9_]", "", str(r).lower().replace(" ", "_").replace("-", "_"))
        hit = key if key in names else next(
            (n for n in names if key in n or n in key), None)
        (have if hit else missing).append({"requested": str(r), "matched": hit})
    return SelfResult(ok=True, output=f"{len(have)} covered, {len(missing)} gaps",
                     parsed={"covered": have, "gaps": missing})


def _improve_hypothesis(args: dict) -> SelfResult:
    area = str(args.get("area", ""))
    if not area:
        return _fail("area required")
    hyps = [
        f"Cache repeated {area} results (see cache_perf_note) and measure with optimize_verify.",
        f"Add regression tests for {area} (see test_generate), then track with perf_regression.",
        f"Record {area} failures in failure_memory to avoid repeats.",
        f"Profile {area} with profile_cpu before changing anything.",
    ]
    return SelfResult(ok=True, output="\n".join(f"- {h}" for h in hyps),
                     parsed={"hypotheses": hyps,
                             "note": "heuristics — validate each with benchmarks"})


def _branch_create(args: dict) -> SelfResult:
    name = re.sub(r"[^a-z0-9/_-]", "", str(args.get("name", "")))
    root = str(args.get("root", "."))
    if not name:
        return _fail("name required")
    try:
        r = subprocess.run(["git", "checkout", "-b", name], capture_output=True,
                           text=True, timeout=30, cwd=root)
    except Exception as e:
        return _fail(f"git failed: {e}")
    ok = r.returncode == 0
    return SelfResult(ok=ok, output=(r.stdout + r.stderr)[-800:],
                     parsed={"branch": name})


def _release_deploy(args: dict) -> SelfResult:
    version = str(args.get("version", ""))
    root = str(args.get("root", "."))
    confirm = str(args.get("confirm", ""))
    if not version:
        return _fail("version required (e.g. 1.2.0)")
    if confirm != "RELEASE":
        return SelfResult(ok=False, output="",
                         error="pass confirm='RELEASE' to tag the release")
    try:
        r1 = subprocess.run(["git", "tag", "-a", f"v{version}", "-m",
                             f"release v{version}"], capture_output=True,
                            text=True, timeout=30, cwd=root)
        if r1.returncode != 0:
            return SelfResult(ok=False, output=(r1.stdout + r1.stderr)[-800:],
                             error="tag failed")
    except Exception as e:
        return _fail(f"git tag failed: {e}")
    return SelfResult(ok=True, output=f"tagged v{version} (push tags separately)",
                     parsed={"tag": f"v{version}"})


def _kb_update(args: dict) -> SelfResult:
    topic = re.sub(r"[^a-z0-9_-]", "", str(args.get("topic", "general")))
    content = str(args.get("content", ""))
    if not content:
        return _fail("content required")
    KB_DIR.mkdir(parents=True, exist_ok=True)
    fp = KB_DIR / f"{topic}.md"
    with fp.open("a", encoding="utf-8") as fh:
        fh.write(f"\n## {time.strftime('%Y-%m-%d %H:%M')}\n\n{content[:4000]}\n")
    return SelfResult(ok=True, output=f"kb updated: {fp}", parsed={"file": str(fp)})


HANDLERS: dict = {
    "failure_memory": _failure_memory,
    "tool_discover": _tool_discover,
    "tool_spec_gen": _tool_spec_gen,
    "tool_test_gen": _tool_test_gen,
    "gap_analyze": _gap_analyze,
    "improve_hypothesis": _improve_hypothesis,
    "branch_create": _branch_create,
    "release_deploy": _release_deploy,
    "kb_update": _kb_update,
}

TOOL_DEFS: list[tuple] = [
    ("failure_memory", "add|search persistent failure->fix records",
     {"action": _sch("string", False, "add|search"),
      "failure": _sch("string", False, "What failed"),
      "fix": _sch("string", False, "What fixed it"),
      "tags": _sch("array", False, "Tags"),
      "query": _sch("string", False, "Search text")}, "computer", 20),
    ("tool_discover", "Search the live tool registry",
     {"query": _sch("string", False, "Search text")}, "computer", 20),
    ("tool_spec_gen", "Generate a tool spec (schema + handler skeleton)",
     {"name": _sch("string", True, "Tool name"),
      "purpose": _sch("string", True, "Purpose"),
      "params": _sch("array", False, "[{name,type,required,desc}]"),
      "output": _sch("string", False, "Write JSON path")}, "computer", 30),
    ("tool_test_gen", "Generate a pytest smoke scaffold for a tool",
     {"name": _sch("string", True, "Tool name"),
      "output": _sch("string", False, "Output path")}, "computer", 20),
    ("gap_analyze", "Compare requested capabilities vs the registry",
     {"requested": _sch("array", True, "Capability names")}, "computer", 30),
    ("improve_hypothesis", "Heuristic improvement ideas for an area",
     {"area": _sch("string", True, "Area")}, "computer", 20),
    ("branch_create", "git checkout -b for a patch proposal",
     {"name": _sch("string", True, "Branch name"),
      "root": _sch("string", False, "Repo root")}, "computer", 60),
    ("release_deploy", "Tag a release (confirm='RELEASE')",
     {"version": _sch("string", True, "e.g. 1.2.0"),
      "root": _sch("string", False, "Repo root"),
      "confirm": _sch("string", True, "Must be RELEASE")}, "computer", 60),
    ("kb_update", "Append to the local knowledge base",
     {"topic": _sch("string", False, "Topic"),
      "content": _sch("string", True, "Markdown content")}, "computer", 20),
]
