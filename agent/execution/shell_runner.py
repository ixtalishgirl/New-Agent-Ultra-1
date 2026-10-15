"""Direct shell runner — no sandbox, no resource limits (per ahsan's order).

Executes bash commands directly. Only the 5 hard denials are enforced.
The rm -rf confirmation toggle remains per ahsan's explicit 2026-10-15 order.
"""
from __future__ import annotations
import subprocess
import time
from .python_runner import ExecResult

# Destructive commands require explicit confirmation flag
DESTRUCTIVE = ("rm -rf", "mkfs", "dd if=", ":(){", "shutdown", "reboot")


def run_shell(command: str, timeout: int = 60,
              workspace: str | None = None,
              allow_destructive: bool = False) -> ExecResult:
    """Run a bash command. Destructive ops blocked unless explicitly allowed."""
    lowered = command.lower()
    if not allow_destructive and any(d in lowered for d in DESTRUCTIVE):
        return ExecResult(ok=False, stdout="",
                          stderr="BLOCKED: destructive command needs approval",
                          exit_code=126)
    start = time.time()
    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=workspace or None,
        )
        dur = int((time.time() - start) * 1000)
        return ExecResult(ok=proc.returncode == 0,
                          stdout=proc.stdout[-8000:],
                          stderr=proc.stderr[-8000:],
                          exit_code=proc.returncode, duration_ms=dur)
    except subprocess.TimeoutExpired:
        return ExecResult(ok=False, stdout="", stderr="TIMEOUT", exit_code=124)
    except Exception as e:
        return ExecResult(ok=False, stdout="", stderr=str(e), exit_code=1)
