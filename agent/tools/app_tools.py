"""Application Control — subprocess-based process/app management.

Linux-first with graceful degradation: app/window listing reads
``/proc`` directly with ``ps`` fallbacks; window-manager actions
(``app_focus``, ``app_windows``) use ``wmctrl`` when installed and fail
cleanly on headless hosts. Destructive verbs (``app_close``,
``app_uninstall``, ``process_kill``) require ``confirm=True``.

Boundary: this module only starts/stops/manages processes and packages.
It does not touch boot/recovery, container runtimes, or AV/EDR services —
those are hard denials and are never implemented here.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

DEFAULT_TIMEOUT = 60


# ---------------------------------------------------------------------------
# Result type (local; decoupled from other tool modules by design)
# ---------------------------------------------------------------------------

@dataclass
class AppResult:
    """Structured result of an application-control operation."""
    ok: bool
    output: str = ""
    parsed: dict = field(default_factory=dict)
    error: str = ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _result_from_exc(exc: Exception) -> AppResult:
    return AppResult(ok=False, error=f"{type(exc).__name__}: {exc}")


def _run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _ps_snapshot() -> list[dict]:
    """Process snapshot: pid, name, cpu%, mem RSS bytes. /proc first, ps fallback."""
    procs: list[dict] = []
    if Path("/proc").is_dir():
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                with open(entry / "stat") as f:
                    parts = f.read().rsplit(")", 1)
                name = parts[0].split("(", 1)[1]
                fields = parts[1].split()
                utime, stime = int(fields[11]), int(fields[12])
                rss_pages = int(fields[21])
                clk_tck = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
                cpu_sec = (utime + stime) / clk_tck
                mem = rss_pages * os.sysconf(os.sysconf_names["SC_PAGE_SIZE"])
                with open(entry / "cmdline", "rb") as f:
                    cmdline = f.read().replace(b"\x00", b" ").decode(errors="replace").strip()
            except (OSError, IndexError, ValueError):
                continue
            procs.append({"pid": pid, "name": name, "cpu_sec": round(cpu_sec, 2),
                          "mem_bytes": mem, "cmdline": cmdline[:200]})
        return procs
    # Fallback: ps
    try:
        r = _run(["ps", "-eo", "pid,comm,etimes,rss,args"])
        for line in r.stdout.splitlines()[1:]:
            cols = line.split(None, 4)
            if len(cols) < 5:
                continue
            procs.append({"pid": int(cols[0]), "name": cols[1],
                          "cpu_sec": float(cols[2]), "mem_bytes": int(cols[3]) * 1024,
                          "cmdline": cols[4][:200]})
    except Exception:
        pass
    return procs


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _app_launch(args: dict) -> AppResult:
    try:
        cmd = args["cmd"]
        extra = args.get("args", []) or []
        full = [cmd] + list(extra)
        proc = subprocess.Popen(full, start_new_session=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL)
        return AppResult(
            ok=True, output=f"Launched {' '.join(full)} (pid {proc.pid}).",
            parsed={"pid": proc.pid, "cmd": full})
    except Exception as e:
        return _result_from_exc(e)


def _app_close(args: dict) -> AppResult:
    try:
        if not args.get("confirm", False):
            return AppResult(ok=False, error="Refused: app_close requires confirm=True.")
        name = args["name"]
        if shutil.which("pkill") is None:
            return AppResult(ok=False, error="pkill not available on this host.")
        r = _run(["pkill", "-f", name])
        # pkill exit 0 = matched, 1 = no match; both are non-errors here.
        matched = r.returncode == 0
        return AppResult(
            ok=True,
            output=(f"Signalled processes matching '{name}'." if matched
                    else f"No running processes matched '{name}'."),
            parsed={"name": name, "matched": matched})
    except Exception as e:
        return _result_from_exc(e)


def _app_list(args: dict) -> AppResult:
    try:
        procs = _ps_snapshot()
        procs.sort(key=lambda p: p["mem_bytes"], reverse=True)
        top = procs[: int(args.get("limit", 50) or 50)]
        lines = [f"{p['pid']:>7}  {p['name'][:30]:<30}  "
                 f"cpu {p['cpu_sec']:.1f}s  mem {p['mem_bytes'] // 1024}K"
                 for p in top]
        return AppResult(
            ok=True,
            output=f"{len(procs)} processes (top by memory):\n" + "\n".join(lines),
            parsed={"count": len(procs), "processes": top})
    except Exception as e:
        return _result_from_exc(e)


def _app_focus(args: dict) -> AppResult:
    try:
        if shutil.which("wmctrl") is None:
            return AppResult(
                ok=False,
                error="no window manager control in headless env (wmctrl not installed).")
        name = args["name"]
        r = _run(["wmctrl", "-a", name])
        if r.returncode != 0:
            return AppResult(ok=False, error=f"wmctrl could not focus window '{name}'.")
        return AppResult(ok=True, output=f"Focused window matching '{name}'.",
                         parsed={"name": name})
    except Exception as e:
        return _result_from_exc(e)


def _app_windows(args: dict) -> AppResult:
    try:
        if shutil.which("wmctrl") is None:
            return AppResult(
                ok=False,
                error="no window manager control in headless env (wmctrl not installed).")
        r = _run(["wmctrl", "-l"])
        wins = []
        for line in r.stdout.splitlines():
            cols = line.split(None, 3)
            if len(cols) >= 4:
                wins.append({"id": cols[0], "desktop": cols[1],
                             "pid": cols[2], "title": cols[3]})
        return AppResult(
            ok=True, output=f"{len(wins)} windows.",
            parsed={"count": len(wins), "windows": wins})
    except Exception as e:
        return _result_from_exc(e)


def _app_install(args: dict) -> AppResult:
    try:
        package = args["package"]
        manager = str(args.get("manager", "pip")).lower()
        if manager == "apt":
            r = _run(["sudo", "-n", "apt-get", "install", "-y", package], timeout=300)
            if r.returncode != 0 and "sudo: a password is required" in (r.stderr or ""):
                r = _run(["sudo", "apt-get", "install", "-y", package], timeout=300)
            out = (r.stdout + r.stderr)[-2000:]
            return AppResult(ok=r.returncode == 0,
                             output=f"apt install {package}: rc={r.returncode}\n{out}",
                             parsed={"package": package, "manager": "apt",
                                     "rc": r.returncode},
                             error="" if r.returncode == 0 else out[-500:])
        elif manager == "pip":
            r = _run([sys.executable, "-m", "pip", "install", package], timeout=300)
            out = (r.stdout + r.stderr)[-2000:]
            return AppResult(ok=r.returncode == 0,
                             output=f"pip install {package}: rc={r.returncode}\n{out}",
                             parsed={"package": package, "manager": "pip",
                                     "rc": r.returncode},
                             error="" if r.returncode == 0 else out[-500:])
        return AppResult(ok=False, error=f"Unknown manager: {manager} (use apt|pip)")
    except Exception as e:
        return _result_from_exc(e)


def _app_uninstall(args: dict) -> AppResult:
    try:
        if not args.get("confirm", False):
            return AppResult(ok=False, error="Refused: app_uninstall requires confirm=True.")
        package = args["package"]
        manager = str(args.get("manager", "pip")).lower()
        if manager == "apt":
            r = _run(["sudo", "-n", "apt-get", "remove", "-y", package], timeout=300)
            out = (r.stdout + r.stderr)[-2000:]
        elif manager == "pip":
            r = _run([sys.executable, "-m", "pip", "uninstall", "-y", package], timeout=300)
            out = (r.stdout + r.stderr)[-2000:]
        else:
            return AppResult(ok=False, error=f"Unknown manager: {manager} (use apt|pip)")
        return AppResult(ok=r.returncode == 0,
                         output=f"{manager} uninstall {package}: rc={r.returncode}\n{out}",
                         parsed={"package": package, "manager": manager,
                                 "rc": r.returncode},
                         error="" if r.returncode == 0 else out[-500:])
    except Exception as e:
        return _result_from_exc(e)


def _app_update(args: dict) -> AppResult:
    try:
        package = args["package"]
        manager = str(args.get("manager", "pip")).lower()
        if manager == "apt":
            r = _run(["sudo", "-n", "apt-get", "update"], timeout=300)
            if r.returncode == 0:
                r = _run(["sudo", "-n", "apt-get", "install", "-y", "--only-upgrade", package],
                         timeout=300)
            out = (r.stdout + r.stderr)[-2000:]
        elif manager == "pip":
            r = _run([sys.executable, "-m", "pip", "install", "-U", package], timeout=300)
            out = (r.stdout + r.stderr)[-2000:]
        else:
            return AppResult(ok=False, error=f"Unknown manager: {manager} (use apt|pip)")
        return AppResult(ok=r.returncode == 0,
                         output=f"{manager} update {package}: rc={r.returncode}\n{out}",
                         parsed={"package": package, "manager": manager,
                                 "rc": r.returncode},
                         error="" if r.returncode == 0 else out[-500:])
    except Exception as e:
        return _result_from_exc(e)


def _process_kill(args: dict) -> AppResult:
    try:
        if not args.get("confirm", False):
            return AppResult(ok=False, error="Refused: process_kill requires confirm=True.")
        pid = int(args["pid"])
        sig = signal.SIGKILL if args.get("force", True) else signal.SIGTERM
        os.kill(pid, sig)
        return AppResult(ok=True, output=f"Sent {sig.name} to pid {pid}.",
                         parsed={"pid": pid, "signal": sig.name})
    except ProcessLookupError:
        return AppResult(ok=False, error=f"No such process: {args.get('pid')}")
    except PermissionError:
        return AppResult(ok=False, error=f"Permission denied killing pid {args.get('pid')}")
    except Exception as e:
        return _result_from_exc(e)


def _process_nice(args: dict) -> AppResult:
    try:
        pid = int(args["pid"])
        nice = int(args["nice"])
        try:
            os.setpriority(os.PRIO_PROCESS, pid, nice)
        except AttributeError:
            r = _run(["renice", str(nice), "-p", str(pid)])
            if r.returncode != 0:
                return AppResult(ok=False, error=f"renice failed: {r.stderr.strip()}")
        return AppResult(ok=True, output=f"Set nice {nice} on pid {pid}.",
                         parsed={"pid": pid, "nice": nice})
    except ProcessLookupError:
        return AppResult(ok=False, error=f"No such process: {args.get('pid')}")
    except Exception as e:
        return _result_from_exc(e)


def _service_control(args: dict) -> AppResult:
    try:
        service = args["service"]
        action = str(args.get("action", "status")).lower()
        if action not in ("start", "stop", "restart", "status"):
            return AppResult(ok=False, error=f"Unknown action: {action}")
        if shutil.which("systemctl") is None:
            return AppResult(ok=False, error="systemctl not available (no systemd on this host).")
        if action in ("start", "stop", "restart"):
            r = _run(["sudo", "-n", "systemctl", action, service], timeout=60)
        else:
            r = _run(["systemctl", "is-active", service], timeout=30)
        out = (r.stdout + r.stderr).strip()[-1500:]
        return AppResult(
            ok=(r.returncode == 0) if action == "status" else r.returncode == 0,
            output=f"systemctl {action} {service}: {out}",
            parsed={"service": service, "action": action,
                    "rc": r.returncode, "detail": out})
    except Exception as e:
        return _result_from_exc(e)


HANDLERS: dict = {
    "app_launch": _app_launch,
    "app_close": _app_close,
    "app_list": _app_list,
    "app_focus": _app_focus,
    "app_windows": _app_windows,
    "app_install": _app_install,
    "app_uninstall": _app_uninstall,
    "app_update": _app_update,
    "process_kill": _process_kill,
    "process_nice": _process_nice,
    "service_control": _service_control,
}


def _sch(t: str, req: bool, desc: str) -> dict:
    return {"type": t, "required": req, "description": desc}


TOOL_DEFS: list[tuple] = [
    ("app_launch", "Launch a program detached, return its pid",
     {"cmd": _sch("string", True, "Executable path or command"),
      "args": _sch("array", False, "Argument list")},
     "computer", 30),
    ("app_close", "Terminate processes by name via pkill (requires confirm)",
     {"name": _sch("string", True, "Process name/pattern"),
      "confirm": _sch("boolean", False, "Must be true")},
     "computer", 30),
    ("app_list", "Snapshot of running processes (pid, name, cpu, mem)",
     {"limit": _sch("integer", False, "Max rows returned (default 50)")},
     "computer", 30),
    ("app_focus", "Focus a window by name (wmctrl; headless -> not supported)",
     {"name": _sch("string", True, "Window title substring")},
     "computer", 20),
    ("app_windows", "List open windows (wmctrl; headless -> not supported)",
     {},
     "computer", 20),
    ("app_install", "Install a package via apt (sudo) or pip",
     {"package": _sch("string", True, "Package name"),
      "manager": _sch("string", False, "apt|pip (default pip)")},
     "computer", 300),
    ("app_uninstall", "Uninstall a package (requires confirm)",
     {"package": _sch("string", True, "Package name"),
      "manager": _sch("string", False, "apt|pip (default pip)"),
      "confirm": _sch("boolean", False, "Must be true")},
     "computer", 300),
    ("app_update", "Upgrade a package via apt or pip",
     {"package": _sch("string", True, "Package name"),
      "manager": _sch("string", False, "apt|pip (default pip)")},
     "computer", 300),
    ("process_kill", "Kill a process by pid (requires confirm)",
     {"pid": _sch("integer", True, "Process id"),
      "force": _sch("boolean", False, "SIGKILL vs SIGTERM (default true)"),
      "confirm": _sch("boolean", False, "Must be true")},
     "computer", 20),
    ("process_nice", "Set process niceness (-20..19)",
     {"pid": _sch("integer", True, "Process id"),
      "nice": _sch("integer", True, "Nice value")},
     "computer", 20),
    ("service_control", "systemctl start|stop|restart|status (best-effort)",
     {"service": _sch("string", True, "Service name"),
      "action": _sch("string", False, "start|stop|restart|status (default status)")},
     "computer", 60),
]
