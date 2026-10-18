"""Process manager — tracks and controls running subprocesses.

Per ChatGPT spec: agent/execution/process_manager.py
Start, poll, kill, and list managed processes. No sandbox.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ProcessStatus(Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"
    TIMEOUT = "timeout"


@dataclass
class ManagedProcess:
    """A tracked subprocess."""
    id: str
    command: str
    pid: Optional[int] = None
    status: ProcessStatus = ProcessStatus.RUNNING
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    timeout: int = 300
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        end = self.ended_at or time.time()
        return end - self.started_at

    @property
    def is_alive(self) -> bool:
        return self.status == ProcessStatus.RUNNING

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "command": self.command[:200],
            "pid": self.pid, "status": self.status.value,
            "started_at": self.started_at, "ended_at": self.ended_at,
            "exit_code": self.exit_code,
            "duration_seconds": round(self.duration_seconds, 1),
            "stdout_tail": self.stdout[-2000:],
            "stderr_tail": self.stderr[-2000:],
            "timeout": self.timeout,
        }


class ProcessManager:
    """Manages subprocess lifecycle."""

    def __init__(self):
        self._procs: Dict[str, ManagedProcess] = {}
        self._handles: Dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()
        self._watcher = threading.Thread(target=self._watch_loop,
                                         daemon=True)
        self._watcher.start()

    def start(self, command: str, timeout: int = 300,
              cwd: Optional[str] = None,
              env: Optional[Dict[str, str]] = None,
              shell: bool = True) -> ManagedProcess:
        """Start a process in the background."""
        proc_id = f"proc_{uuid.uuid4().hex[:8]}"
        mp = ManagedProcess(id=proc_id, command=command, timeout=timeout)
        try:
            handle = subprocess.Popen(
                command if shell else command.split(),
                shell=shell, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
                cwd=cwd, env={**os.environ, **(env or {})},
                start_new_session=True)
            mp.pid = handle.pid
        except Exception as e:
            mp.status = ProcessStatus.FAILED
            mp.stderr = str(e)
            mp.ended_at = time.time()
        with self._lock:
            self._procs[proc_id] = mp
            if mp.pid:
                self._handles[proc_id] = handle
        return mp

    def poll(self, proc_id: str) -> Optional[ManagedProcess]:
        """Check status; collect output if finished."""
        with self._lock:
            mp = self._procs.get(proc_id)
            handle = self._handles.get(proc_id)
        if not mp or not handle:
            return mp
        ret = handle.poll()
        if ret is not None and mp.status == ProcessStatus.RUNNING:
            try:
                out, err = handle.communicate(timeout=5)
            except Exception:
                out, err = "", ""
            mp.stdout = (out or "")[-20000:]
            mp.stderr = (err or "")[-20000:]
            mp.exit_code = ret
            mp.ended_at = time.time()
            mp.status = (ProcessStatus.COMPLETED if ret == 0
                         else ProcessStatus.FAILED)
        return mp

    def wait(self, proc_id: str,
             timeout: Optional[float] = None) -> Optional[ManagedProcess]:
        """Block until process completes or timeout."""
        deadline = time.time() + (timeout or 300)
        while time.time() < deadline:
            mp = self.poll(proc_id)
            if not mp or not mp.is_alive:
                return mp
            time.sleep(0.2)
        # Timeout: kill it
        self.kill(proc_id)
        mp = self.poll(proc_id)
        if mp and mp.status == ProcessStatus.RUNNING:
            mp.status = ProcessStatus.TIMEOUT
            mp.ended_at = time.time()
        return mp

    def kill(self, proc_id: str) -> bool:
        """Terminate a running process (and its group)."""
        with self._lock:
            mp = self._procs.get(proc_id)
            handle = self._handles.get(proc_id)
        if not mp or not handle or not mp.is_alive:
            return False
        try:
            os.killpg(os.getpgid(handle.pid), signal.SIGTERM)
            time.sleep(1)
            if handle.poll() is None:
                os.killpg(os.getpgid(handle.pid), signal.SIGKILL)
        except Exception:
            try:
                handle.kill()
            except Exception:
                pass
        mp.status = ProcessStatus.KILLED
        mp.ended_at = time.time()
        self.poll(proc_id)  # collect final output
        return True

    def get(self, proc_id: str) -> Optional[ManagedProcess]:
        with self._lock:
            return self._procs.get(proc_id)

    def list(self, only_running: bool = False) -> List[ManagedProcess]:
        with self._lock:
            procs = list(self._procs.values())
        if only_running:
            procs = [p for p in procs if p.is_alive]
        procs.sort(key=lambda p: -p.started_at)
        return procs

    def cleanup(self, older_than_seconds: float = 3600) -> int:
        """Remove finished process records older than threshold."""
        cutoff = time.time() - older_than_seconds
        removed = 0
        with self._lock:
            for pid in list(self._procs.keys()):
                mp = self._procs[pid]
                if not mp.is_alive and (mp.ended_at or 0) < cutoff:
                    del self._procs[pid]
                    self._handles.pop(pid, None)
                    removed += 1
        return removed

    def kill_all(self) -> int:
        """Kill all running processes."""
        count = 0
        for mp in self.list(only_running=True):
            if self.kill(mp.id):
                count += 1
        return count

    def _watch_loop(self) -> None:
        """Background: enforce timeouts."""
        while True:
            time.sleep(5)
            try:
                for mp in self.list(only_running=True):
                    if mp.duration_seconds > mp.timeout:
                        self.kill(mp.id)
                        mp.status = ProcessStatus.TIMEOUT
            except Exception:
                pass
