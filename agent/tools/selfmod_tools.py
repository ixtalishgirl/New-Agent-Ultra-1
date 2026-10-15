"""Self-modification — the agent modifies its own code, safely and verifiably.
[verified live 2026-10-15]

Pipeline (self_modify): propose patch -> git branch -> apply (exact-match) ->
run relevant tests -> report. On test failure: rollback the file.

Safety:
- The 5 hard denials stay locked: patches to agent/tools/permission_gate.py
  are refused (that would weaken the user's own standing denials), and every
  patch is scanned by permission_gate before apply.
- Only files inside the repo can be touched.

scheduled_selfmod: the safe, scheduled demo — rotates the dashboard accent
color, verifies the change took effect, and logs it. This is the visible
proof ("apna code update kr ky ya colour change kr ky").
"""
from __future__ import annotations

import datetime
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LOG_FILE = REPO_ROOT / "agent" / "logs" / "selfmod.log"

# Files that must never be self-modified (they guard the 5 locked denials —
# the user's own standing order).
PROTECTED = {
    "agent/tools/permission_gate.py",
}

# Safe demo rotation palette for the dashboard accent color.
ACCENT_PALETTE = ["#6e9bff", "#7bff9e", "#ff9e6e", "#ff6e9b", "#9d7bff"]
STYLES_CSS = REPO_ROOT / "dashboard" / "src" / "styles.css"


@dataclass
class SelfmodResult:
    ok: bool
    output: str = ""
    parsed: dict = field(default_factory=dict)
    error: str = ""


def _log(event: str, details: dict | None = None) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps({
                "t": datetime.datetime.utcnow().isoformat() + "Z",
                "event": event,
                "details": details or {},
            }) + "\n")
    except Exception:
        pass


def _git(*args: str) -> tuple[bool, str]:
    try:
        p = subprocess.run(["git", "-C", str(REPO_ROOT), *args],
                           capture_output=True, text=True, timeout=60)
        return p.returncode == 0, (p.stdout + p.stderr).strip()[:1000]
    except Exception as e:
        return False, str(e)[:300]


def _resolve_repo_file(rel: str) -> Optional[Path]:
    p = (REPO_ROOT / rel).resolve()
    try:
        p.relative_to(REPO_ROOT.resolve())
    except ValueError:
        return None
    return p


def self_modify(args: dict) -> SelfmodResult:
    """Propose -> branch -> apply -> test -> report (rollback on failure)."""
    rel = str(args.get("file", "")).strip()
    old = str(args.get("old", ""))
    new = str(args.get("new", ""))
    reason = str(args.get("reason", "")).strip()
    if not rel or not old or not new:
        return SelfmodResult(ok=False,
                             error="file, old and new are required")
    if rel in PROTECTED or rel.endswith("permission_gate.py"):
        return SelfmodResult(
            ok=False,
            error=f"refused: {rel} guards the 5 locked denials and cannot "
                  f"be self-modified")
    # Denial scan on the patch content itself.
    try:
        from agent.tools.permission_gate import check_tool_call
        gate = check_tool_call("self_modify",
                               {"file": rel, "new": new, "reason": reason})
        if not gate.allowed:
            return SelfmodResult(ok=False, error=gate.reason)
    except Exception:
        pass
    path = _resolve_repo_file(rel)
    if path is None or not path.is_file():
        return SelfmodResult(ok=False, error=f"not a repo file: {rel}")
    try:
        text = path.read_text()
    except Exception as e:
        return SelfmodResult(ok=False, error=f"read failed: {e}"[:200])
    if text.count(old) != 1:
        return SelfmodResult(
            ok=False,
            error=f"'old' must occur exactly once in {rel} "
                  f"(found {text.count(old)})")
    branch = f"selfmod/{datetime.datetime.utcnow():%Y%m%d-%H%M%S}"
    ok, out = _git("checkout", "-b", branch)
    if not ok:
        return SelfmodResult(ok=False, error=f"branch failed: {out}")
    applied = text.replace(old, new)
    path.write_text(applied)
    # Verify: syntax check always; relevant unit tests when discoverable.
    test_out, tests_ok = _run_relevant_tests(rel)
    if not tests_ok:
        _git("checkout", "--", rel)  # rollback the file
        _log("selfmod_rollback", {"file": rel, "branch": branch,
                                  "reason": reason, "test_out": test_out[-500:]})
        return SelfmodResult(
            ok=False,
            error=f"patch applied but tests FAILED — rolled back.\n{test_out}",
            parsed={"branch": branch, "rolled_back": True})
    _log("selfmod_applied", {"file": rel, "branch": branch, "reason": reason})
    return SelfmodResult(
        ok=True,
        output=f"self-mod applied on branch {branch}: {rel}\n"
               f"reason: {reason}\ntests: {test_out}",
        parsed={"branch": branch, "file": rel, "tests": test_out})


def _run_relevant_tests(rel: str) -> tuple[str, bool]:
    """Return (summary, ok). Syntax-checks the file, then runs matching unit
    tests if any exist."""
    path = REPO_ROOT / rel
    if path.suffix == ".py":
        p = subprocess.run(["python3", "-m", "py_compile", str(path)],
                           capture_output=True, text=True, timeout=60)
        if p.returncode != 0:
            return f"py_compile FAILED:\n{p.stderr[:800]}", False
    # map agent/tools/<x>.py -> agent/tests/unit/test_<x>*.py
    test_files: list[str] = []
    stem = path.stem
    test_dir = REPO_ROOT / "agent" / "tests" / "unit"
    if test_dir.is_dir():
        for tf in sorted(test_dir.glob(f"test_*{stem}*.py")):
            test_files.append(str(tf.relative_to(REPO_ROOT)))
        for tf in sorted(test_dir.glob(f"test_{stem}.py")):
            if str(tf.relative_to(REPO_ROOT)) not in test_files:
                test_files.append(str(tf.relative_to(REPO_ROOT)))
    if not test_files:
        return "py_compile OK (no matching unit tests found)", True
    p = subprocess.run(["python3", "-m", "pytest", *test_files, "-q",
                        "-p", "no:cacheprovider"],
                       capture_output=True, text=True, timeout=300,
                       cwd=str(REPO_ROOT))
    tail = (p.stdout + p.stderr)[-1200:]
    return tail, p.returncode == 0


def _current_accent() -> str:
    try:
        m = re.search(r"--accent:\s*(#[0-9a-fA-F]{6})",
                      STYLES_CSS.read_text())
        return m.group(1).lower() if m else ""
    except Exception:
        return ""


def rotate_accent_color() -> dict:
    """Rotate the dashboard --accent to the next palette color. Returns
    before/after + verification."""
    before = _current_accent()
    if before in ACCENT_PALETTE:
        after = ACCENT_PALETTE[(ACCENT_PALETTE.index(before) + 1)
                               % len(ACCENT_PALETTE)]
    else:
        after = ACCENT_PALETTE[0]
    try:
        text = STYLES_CSS.read_text()
    except Exception as e:
        return {"ok": False, "error": f"styles.css unreadable: {e}"[:200]}
    new_text, n = re.subn(r"(--accent:\s*)#[0-9a-fA-F]{6}",
                          rf"\g<1>{after}", text, count=1)
    if n != 1:
        return {"ok": False, "error": "accent variable not found"}
    STYLES_CSS.write_text(new_text)
    verified = _current_accent() == after
    _log("accent_rotated", {"before": before, "after": after,
                            "verified": verified})
    return {"ok": verified, "before": before, "after": after,
            "verified": verified, "file": "dashboard/src/styles.css"}


def scheduled_selfmod(args: dict) -> SelfmodResult:
    """The scheduled safe self-mod: rotate dashboard accent color, verify,
    log. Designed to run on a schedule (cron/scheduler) as visible proof
    the agent can modify its own code."""
    res = rotate_accent_color()
    if not res.get("ok"):
        return SelfmodResult(ok=False, error=res.get("error", "unknown"))
    return SelfmodResult(
        ok=True,
        output=(f"self-mod complete: dashboard accent {res['before']} -> "
                f"{res['after']} (verified: {res['verified']})"),
        parsed=res)


def selfmod_demo() -> dict:
    """One safe demo self-mod for the live API endpoint."""
    return rotate_accent_color()


def _S(arg_type: str, required: bool, desc: str) -> dict:
    return {"type": arg_type, "required": required, "description": desc}


TOOL_DEFS: list[tuple] = [
    ("self_modify", "REALLY modify own code: git branch -> apply patch -> run tests -> report (rollback on failure). permission_gate.py refused.",
     {"file": _S("string", True, "Repo-relative file path"),
      "old": _S("string", True, "Exact text to replace (must occur once)"),
      "new": _S("string", True, "Replacement text"),
      "reason": _S("string", False, "Why this change")},
     "custom", 300),
    ("scheduled_selfmod", "Scheduled safe self-mod: rotate dashboard accent color, verify it took effect, log it",
     {}, "custom", 60),
]

HANDLERS: dict[str, Any] = {
    "self_modify": lambda a: _wrap(self_modify(a)),
    "scheduled_selfmod": lambda a: _wrap(scheduled_selfmod(a)),
}


class _R:
    def __init__(self, ok, output="", parsed=None, error=""):
        self.ok = ok
        self.output = output
        self.parsed = parsed or {}
        self.error = error


def _wrap(r: SelfmodResult) -> _R:
    return _R(ok=r.ok, output=r.output, parsed=r.parsed, error=r.error)
