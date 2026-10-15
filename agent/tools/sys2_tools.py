"""Advanced system capabilities.

Real implementations: heuristic task planning/decomposition into tool-call
steps, a file-backed agent-to-agent message bus, a persistent FIFO work
queue, subprocess resource monitoring (psutil when present, /proc fallback),
rate-limit-aware execution wrapper, versioned config snapshots, reproducible
build hashing, and release-note generation from git history.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_TIMEOUT = 120
STATE_DIR = Path.home() / ".haley_agent"
BUS_DIR = STATE_DIR / "bus"
QUEUE_FILE = STATE_DIR / "work_queue.jsonl"
CONFIG_DIR = STATE_DIR / "config_versions"


@dataclass
class Sys2Result:
    ok: bool
    output: str = ""
    parsed: dict = field(default_factory=dict)
    error: str = ""


def _fail(msg: str) -> Sys2Result:
    return Sys2Result(ok=False, error=msg, output=msg)


def _sch(t: str, req: bool, desc: str) -> dict:
    return {"type": t, "required": req, "description": desc}


# ---------------------------------------------------------------------------
# task_plan / task_decompose — heuristic planners producing tool-call steps
# ---------------------------------------------------------------------------

_VERB_TOOL = [
    (re.compile(r"\b(search|find|look up)\b", re.I), "web_search", {"query": "<topic>"}),
    (re.compile(r"\b(read|open|show)\b.*\bfile\b", re.I), "file_read", {"path": "<path>"}),
    (re.compile(r"\b(write|create)\b.*\bfile\b", re.I), "file_write", {"path": "<path>", "content": "<...>"}),
    (re.compile(r"\b(test|pytest)\b", re.I), "test_run", {"target": "agent/tests"}),
    (re.compile(r"\b(commit)\b", re.I), "git_commit", {"message": "<msg>"}),
    (re.compile(r"\b(screenshot|capture)\b", re.I), "browser_screenshot", {}),
    (re.compile(r"\b(navigate|open|visit)\b", re.I), "browser_navigate", {"url": "<url>"}),
    (re.compile(r"\b(backup)\b", re.I), "backup_create", {"dest": "<path>"}),
    (re.compile(r"\b(python|run code)\b", re.I), "python", {"code": "<code>"}),
    (re.compile(r"\b(shell|terminal|command)\b", re.I), "shell", {"command": "<cmd>"}),
]


def _task_plan(args: dict) -> Sys2Result:
    goal = str(args.get("goal", ""))
    if not goal:
        return _fail("goal required")
    steps = [{"step": 1, "action": "understand",
              "detail": f"Restate the goal and list unknowns: {goal[:120]}"}]
    i = 2
    for sent in re.split(r"[.;]\s*", goal):
        sent = sent.strip()
        if not sent:
            continue
        matched = False
        for rx, tool, tmpl in _VERB_TOOL:
            if rx.search(sent):
                steps.append({"step": i, "action": "tool_call", "tool": tool,
                              "args": tmpl, "detail": sent[:120]})
                i += 1
                matched = True
                break
        if not matched:
            steps.append({"step": i, "action": "decide",
                          "detail": sent[:120]})
            i += 1
    steps.append({"step": i, "action": "verify",
                  "detail": "Verify outcomes; log to audit trail"})
    return Sys2Result(ok=True, output=f"plan: {len(steps)} steps for goal",
                     parsed={"goal": goal, "steps": steps,
                             "note": "heuristic plan — review before executing"})


def _task_decompose(args: dict) -> Sys2Result:
    goal = str(args.get("goal", ""))
    max_steps = min(int(args.get("max_steps", 8)), 30)
    if not goal:
        return _fail("goal required")
    plan = _task_plan({"goal": goal}).parsed["steps"]
    chunks, cur = [], []
    for s in plan:
        cur.append(s)
        if s["action"] == "tool_call" or len(cur) >= 3:
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    subtasks = [{"id": n + 1, "steps": ch,
                 "summary": "; ".join(str(x.get("detail", x.get("tool", "")))[:80]
                                      for x in ch)}
                for n, ch in enumerate(chunks[:max_steps])]
    return Sys2Result(ok=True, output=f"decomposed into {len(subtasks)} subtasks",
                     parsed={"subtasks": subtasks})


# ---------------------------------------------------------------------------
# agent_message — file-backed agent-to-agent bus
# ---------------------------------------------------------------------------

def _agent_message(args: dict) -> Sys2Result:
    action = str(args.get("action", "send")).lower()
    BUS_DIR.mkdir(parents=True, exist_ok=True)
    if action == "send":
        to = re.sub(r"[^a-z0-9_-]", "", str(args.get("to", "all")))
        body = str(args.get("body", ""))
        if not body:
            return _fail("body required")
        msg = {"from": "agent", "to": to, "ts": time.time(), "body": body[:4000]}
        fp = BUS_DIR / f"{to}_{int(time.time() * 1000)}.json"
        fp.write_text(json.dumps(msg))
        return Sys2Result(ok=True, output=f"message queued for '{to}'",
                         parsed={"file": str(fp)})
    if action == "recv":
        who = re.sub(r"[^a-z0-9_-]", "", str(args.get("to", "all")))
        msgs = []
        for fp in sorted(BUS_DIR.glob(f"{who}_*.json")):
            try:
                msgs.append(json.loads(fp.read_text()))
                if not args.get("keep"):
                    fp.unlink()
            except Exception:
                pass
        return Sys2Result(ok=True, output=f"{len(msgs)} messages",
                         parsed={"messages": msgs})
    if action == "list":
        files = sorted(p.name for p in BUS_DIR.glob("*.json"))
        return Sys2Result(ok=True, output=f"{len(files)} queued",
                         parsed={"queued": files[:50]})
    return _fail("action must be send|recv|list")


# ---------------------------------------------------------------------------
# work_queue — persistent FIFO
# ---------------------------------------------------------------------------

def _queue_load() -> list[dict]:
    if not QUEUE_FILE.exists():
        return []
    out = []
    for ln in QUEUE_FILE.read_text().splitlines():
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            pass
    return out


def _queue_save(items: list[dict]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    QUEUE_FILE.write_text("\n".join(json.dumps(i) for i in items))


def _work_queue(args: dict) -> Sys2Result:
    action = str(args.get("action", "list")).lower()
    items = _queue_load()
    if action == "push":
        task = {"tool": str(args.get("tool", "")), "args": args.get("args") or {},
                "enqueued": time.time()}
        if not task["tool"]:
            return _fail("tool required")
        items.append(task)
        _queue_save(items)
        return Sys2Result(ok=True, output=f"queued (depth {len(items)})",
                         parsed={"depth": len(items)})
    if action == "pop":
        if not items:
            return Sys2Result(ok=True, output="queue empty", parsed={"empty": True})
        task = items.pop(0)
        _queue_save(items)
        return Sys2Result(ok=True, output=f"popped {task['tool']}", parsed={"task": task})
    if action == "list":
        return Sys2Result(ok=True, output=f"{len(items)} queued",
                         parsed={"depth": len(items),
                                 "items": items[:50]})
    if action == "clear":
        _queue_save([])
        return Sys2Result(ok=True, output="queue cleared", parsed={})
    return _fail("action must be push|pop|list|clear")


# ---------------------------------------------------------------------------
# task_monitor — track a subprocess by pid
# ---------------------------------------------------------------------------

def _task_monitor(args: dict) -> Sys2Result:
    pid = int(args.get("pid", 0))
    if not pid:
        return _fail("pid required")
    alive = False
    try:
        os.kill(pid, 0)
        alive = True
    except (OSError, PermissionError):
        alive = False
    info: dict = {"pid": pid, "alive": alive}
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        try:
            parts = stat.read_text().split()
            info["utime_ticks"] = int(parts[13])
            info["rss_pages"] = int(parts[23])
        except Exception:
            pass
    return Sys2Result(ok=True,
                     output=f"pid {pid}: {'alive' if alive else 'not running'}",
                     parsed=info)


# ---------------------------------------------------------------------------
# resource_monitor
# ---------------------------------------------------------------------------

def _resource_monitor(args: dict) -> Sys2Result:
    out: dict = {}
    try:
        import psutil  # type: ignore
        out = {"cpu_pct": psutil.cpu_percent(interval=0.5),
               "ram": {"pct": psutil.virtual_memory().percent,
                       "used_mb": round(psutil.virtual_memory().used / 1e6)},
               "disk": {"pct": psutil.disk_usage("/").percent},
               "source": "psutil"}
    except ImportError:
        try:
            with open("/proc/loadavg") as fh:
                out["loadavg"] = fh.read().strip()
            mem = {}
            with open("/proc/meminfo") as fh:
                for ln in fh:
                    m = re.match(r"(\w+):\s+(\d+)", ln)
                    if m:
                        mem[m.group(1)] = int(m.group(2))
            total, avail = mem.get("MemTotal", 1), mem.get("MemAvailable", 0)
            out["ram"] = {"pct": round(100 * (total - avail) / total, 1)}
            st = os.statvfs("/")
            out["disk"] = {"pct": round(100 * (1 - st.f_bavail / st.f_blocks), 1)}
            out["source"] = "/proc"
        except Exception as e:
            return _fail(f"resource read failed: {e}")
    return Sys2Result(ok=True, output=json.dumps(out), parsed=out)


# ---------------------------------------------------------------------------
# rate_limit_handle — wrapper honoring rate_limit_status
# ---------------------------------------------------------------------------

def _rate_limit_handle(args: dict) -> Sys2Result:
    # thin wrapper: checks a local token-bucket file before "executing"
    bucket = STATE_DIR / "rate_bucket.json"
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    per_min = int(args.get("per_minute", 30))
    now = time.time()
    data = {"tokens": per_min, "ts": now}
    if bucket.exists():
        try:
            data = json.loads(bucket.read_text())
        except json.JSONDecodeError:
            pass
    elapsed = now - data.get("ts", now)
    data["tokens"] = min(per_min, data.get("tokens", per_min) + elapsed * per_min / 60)
    data["ts"] = now
    if data["tokens"] < 1:
        wait = (1 - data["tokens"]) * 60 / per_min
        return Sys2Result(ok=False, output="",
                         error=f"rate limited — retry in {wait:.1f}s",
                         parsed={"retry_in_s": round(wait, 1)})
    data["tokens"] -= 1
    bucket.write_text(json.dumps(data))
    return Sys2Result(ok=True, output="slot granted",
                     parsed={"tokens_left": round(data["tokens"], 1),
                             "note": "advisory limiter; wrap external calls with this"})


# ---------------------------------------------------------------------------
# config_version — versioned snapshots of a config file/dir
# ---------------------------------------------------------------------------

def _config_version(args: dict) -> Sys2Result:
    action = str(args.get("action", "snapshot")).lower()
    target = str(args.get("target", ""))
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if action == "snapshot":
        if not target:
            return _fail("target required")
        p = Path(target)
        if not p.exists():
            return _fail(f"not found: {target}")
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = CONFIG_DIR / f"{p.name}.{ts}"
        if p.is_dir():
            shutil.copytree(p, dest)
        else:
            shutil.copy2(p, dest)
        return Sys2Result(ok=True, output=f"snapshot -> {dest}",
                         parsed={"snapshot": str(dest)})
    if action == "list":
        snaps = sorted(x.name for x in CONFIG_DIR.iterdir())
        return Sys2Result(ok=True, output=f"{len(snaps)} snapshots",
                         parsed={"snapshots": snaps[:50]})
    if action == "restore":
        name = str(args.get("name", ""))
        dest = str(args.get("dest", ""))
        src = CONFIG_DIR / name
        if not src.exists() or not dest:
            return _fail("valid name and dest required")
        dp = Path(dest)
        if src.is_dir():
            shutil.rmtree(dp, ignore_errors=True)
            shutil.copytree(src, dp)
        else:
            shutil.copy2(src, dp)
        return Sys2Result(ok=True, output=f"restored {name} -> {dest}",
                         parsed={})
    return _fail("action must be snapshot|list|restore")


# ---------------------------------------------------------------------------
# repro_build — hash inputs for reproducibility evidence
# ---------------------------------------------------------------------------

def _repro_build(args: dict) -> Sys2Result:
    root = Path(str(args.get("root", "."))).expanduser()
    h = hashlib.sha256()
    files = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or ".git" in p.parts or "__pycache__" in p.parts:
            continue
        rel = str(p.relative_to(root))
        h.update(rel.encode())
        fh = hashlib.sha256()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                fh.update(chunk)
        h.update(fh.hexdigest().encode())
        files.append(rel)
        if len(files) >= 5000:
            break
    digest = h.hexdigest()
    return Sys2Result(ok=True, output=f"{len(files)} files -> build hash {digest[:16]}",
                     parsed={"files": len(files), "sha256": digest,
                             "note": "same inputs => same hash; record per release"})


# ---------------------------------------------------------------------------
# release_notes — from git log
# ---------------------------------------------------------------------------

def _release_notes(args: dict) -> Sys2Result:
    root = str(args.get("root", "."))
    since = str(args.get("since", ""))
    cmd = ["git", "log", "--pretty=format:%h %s", "-50"]
    if since:
        cmd.insert(2, f"{since}..HEAD")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                           cwd=root)
    except Exception as e:
        return _fail(f"git log failed: {e}")
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    feats = [l for l in lines if re.search(r"\badd\b", l, re.I)]
    fixes = [l for l in lines if re.search(r"\bfix\b", l, re.I)]
    other = [l for l in lines if l not in feats and l not in fixes]
    md = "# Release notes\n\n"
    if feats:
        md += "## Added\n" + "".join(f"- {l}\n" for l in feats)
    if fixes:
        md += "\n## Fixed\n" + "".join(f"- {l}\n" for l in fixes)
    if other:
        md += "\n## Other\n" + "".join(f"- {l}\n" for l in other)
    out = str(args.get("output", ""))
    if out:
        Path(out).write_text(md, encoding="utf-8")
    return Sys2Result(ok=True, output=f"{len(lines)} commits summarized",
                     parsed={"commits": len(lines), "notes": md[:4000],
                             "output": out})


HANDLERS: dict = {
    "task_plan": _task_plan,
    "task_decompose": _task_decompose,
    "agent_message": _agent_message,
    "work_queue": _work_queue,
    "task_monitor": _task_monitor,
    "resource_monitor": _resource_monitor,
    "rate_limit_handle": _rate_limit_handle,
    "config_version": _config_version,
    "repro_build": _repro_build,
    "release_notes": _release_notes,
}

TOOL_DEFS: list[tuple] = [
    ("task_plan", "Heuristic plan: goal -> ordered steps (some mapped to tools)",
     {"goal": _sch("string", True, "Goal description")}, "computer", 30),
    ("task_decompose", "Split a goal into subtasks of tool-call steps",
     {"goal": _sch("string", True, "Goal"),
      "max_steps": _sch("integer", False, "Default 8")}, "computer", 30),
    ("agent_message", "send|recv|list on the file-backed agent bus",
     {"action": _sch("string", False, "send|recv|list"),
      "to": _sch("string", False, "Recipient"),
      "body": _sch("string", False, "Message body"),
      "keep": _sch("boolean", False, "Keep after recv")}, "computer", 20),
    ("work_queue", "Persistent FIFO: push|pop|list|clear tool-call tasks",
     {"action": _sch("string", False, "push|pop|list|clear"),
      "tool": _sch("string", False, "Tool name"),
      "args": _sch("object", False, "Args")}, "computer", 20),
    ("task_monitor", "Check whether a pid is alive (+rss/utime)",
     {"pid": _sch("integer", True, "Process id")}, "computer", 20),
    ("resource_monitor", "CPU/RAM/disk via psutil or /proc",
     {}, "computer", 30),
    ("rate_limit_handle", "Advisory token-bucket slot grant",
     {"per_minute": _sch("integer", False, "Default 30")}, "computer", 10),
    ("config_version", "snapshot|list|restore versioned configs",
     {"action": _sch("string", False, "snapshot|list|restore"),
      "target": _sch("string", False, "File/dir to snapshot"),
      "name": _sch("string", False, "Snapshot name"),
      "dest": _sch("string", False, "Restore destination")}, "computer", 120),
    ("repro_build", "SHA-256 over a tree for reproducibility evidence",
     {"root": _sch("string", False, "Root")}, "computer", 300),
    ("release_notes", "Generate release notes from git log",
     {"root": _sch("string", False, "Repo root"),
      "since": _sch("string", False, "Since ref"),
      "output": _sch("string", False, "Write .md path")}, "computer", 60),
]
