"""Direct Python runner — no sandbox, no resource limits (per ahsan's order).

Executes Python code directly via subprocess with timeout and output capture.
Only the 5 hard denials are enforced at the execution layer.
"""
from __future__ import annotations
import subprocess
import tempfile
import os
from dataclasses import dataclass


@dataclass
class ExecResult:
    ok: bool
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int = 0


def run_python(code: str, timeout: int = 60,
               workspace: str | None = None) -> ExecResult:
    """Run Python code directly (no sandbox)."""
    import time
    ws = workspace or tempfile.gettempdir()
    os.makedirs(ws, exist_ok=True)
    start = time.time()
    try:
        proc = subprocess.run(
            ["python3", "-c", code],
            capture_output=True, text=True, timeout=timeout, cwd=ws,
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
