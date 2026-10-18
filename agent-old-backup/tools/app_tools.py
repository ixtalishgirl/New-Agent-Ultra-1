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
        if args.get("pid") is None or args.get("nice") is None:
            return AppResult(ok=False,
                             error="pid and nice are required")
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




def _window_resize_move(args: dict) -> AppResult:
    """Resize/move an OS window (wmctrl, else xdotool); honest note when headless."""
    try:
        name = args.get("name")
        width = args.get("width")
        height = args.get("height")
        x = args.get("x")
        y = args.get("y")
        if not name:
            return AppResult(ok=False, error="name (window title substring) is required")
        if shutil.which("wmctrl"):
            r = _run(["wmctrl", "-l"])
            wid = None
            for line in r.stdout.splitlines():
                if name.lower() in line.lower():
                    wid = line.split()[0]
                    break
            if not wid:
                return AppResult(ok=False, error=f"no window matching '{name}' (wmctrl -l)")
            parts = [str(x if x is not None else -1), str(y if y is not None else -1),
                     str(width if width else -1), str(height if height else -1)]
            rr = _run(["wmctrl", "-i", "-r", wid, "-e", "0," + ",".join(parts)])
            if rr.returncode != 0:
                return AppResult(ok=False, error=f"wmctrl move/resize failed: {rr.stderr[:300]}")
            return AppResult(ok=True, output=f"window {wid} resized/moved",
                             parsed={"window_id": wid, "x": x, "y": y,
                                     "width": width, "height": height,
                                     "backend": "wmctrl"})
        if shutil.which("xdotool") and os.environ.get("DISPLAY"):
            r = _run(["xdotool", "search", "--name", name])
            ids = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
            if not ids:
                return AppResult(ok=False, error=f"no window matching '{name}' (xdotool)")
            wid = ids[0]
            if width and height:
                _run(["xdotool", "windowsize", wid, str(width), str(height)])
            if x is not None and y is not None:
                _run(["xdotool", "windowmove", wid, str(x), str(y)])
            return AppResult(ok=True, output=f"window {wid} resized/moved",
                             parsed={"window_id": wid, "backend": "xdotool"})
        return AppResult(ok=False, error=
            "no window-manager control available (wmctrl/xdotool missing or no DISPLAY); "
            "headless hosts cannot move OS windows")
    except Exception as e:
        return _result_from_exc(e)


def _menu_navigate(args: dict) -> AppResult:
    """Keyboard-driven menu navigation via input_tools (Alt accelerators + arrow keys)."""
    try:
        from agent.tools import input_tools  # lazy: avoid import cycles
        seq = list(args.get("sequence") or [])
        if not seq:
            alt = args.get("alt_menu")
            path = args.get("path", [])
            if alt:
                seq.append({"hotkey": ["alt", str(alt)]})
            for k in path:
                k = str(k)
                if k in ("up", "down", "left", "right", "enter", "escape", "tab"):
                    seq.append({"key": k})
                else:
                    seq.append({"type": k})
        if not seq:
            return AppResult(ok=False, error="sequence (or alt_menu + path) is required")
        delay = float(args.get("step_delay", 0.4))
        steps = []
        for item in seq:
            if "hotkey" in item:
                r = input_tools.key_hotkey({"keys": item["hotkey"]})
            elif "key" in item:
                r = input_tools.key_press({"key": item["key"]})
            elif "type" in item:
                r = input_tools.key_type({"text": item["type"]})
            else:
                return AppResult(ok=False, error=f"bad sequence item: {item}")
            steps.append({"item": item, "ok": r.ok,
                          "output": (r.output or r.error or "")[:200]})
            time.sleep(delay)
            if not r.ok:
                return AppResult(ok=False,
                    error=f"menu step failed at {item}: {r.error}",
                    parsed={"steps": steps})
        return AppResult(ok=True,
            output=f"menu navigation complete ({len(steps)} steps)",
            parsed={"steps": steps})
    except Exception as e:
        return _result_from_exc(e)


def _dialog_handle(args: dict) -> AppResult:
    """Best-effort dialog interaction on the focused window: confirm|cancel|fill."""
    try:
        from agent.tools import input_tools  # lazy: avoid import cycles
        action = str(args.get("action", "confirm")).lower()
        if action == "confirm":
            r = input_tools.key_press({"key": "enter"})
        elif action == "cancel":
            r = input_tools.key_press({"key": "escape"})
        elif action == "fill":
            for _ in range(max(0, int(args.get("tabs", 0)))):
                input_tools.key_press({"key": "tab"})
                time.sleep(0.2)
            r = input_tools.key_type({"text": str(args.get("text", ""))})
            if r.ok and args.get("submit", True):
                r = input_tools.key_press({"key": "enter"})
        else:
            return AppResult(ok=False, error="action must be confirm|cancel|fill")
        if not r.ok:
            return AppResult(ok=False,
                error=f"dialog action failed (no keyboard backend?): {r.error}",
                parsed={"action": action})
        return AppResult(ok=True,
            output=f"dialog action '{action}' sent (best-effort: acts on the focused window)",
            parsed={"action": action,
                    "note": "best-effort; verify visually with screen_shot"})
    except Exception as e:
        return _result_from_exc(e)


def _editor_control(args: dict) -> AppResult:
    """Open a file at a line: VS Code CLI if present, else $EDITOR detached, else xdg-open."""
    try:
        path = args.get("path")
        if not path:
            return AppResult(ok=False, error="path is required")
        line = max(1, int(args.get("line", 1)))
        if shutil.which("code"):
            _run(["code", "--goto", f"{path}:{line}"])
            backend = "vscode"
        elif os.environ.get("DISPLAY") and shutil.which("xdg-open"):
            _run(["xdg-open", path])
            backend = "xdg-open"
        else:
            editor = os.environ.get("EDITOR", "nano")
            if not shutil.which(editor):
                return AppResult(ok=False,
                                 error=f"editor '{editor}' not installed "
                                       f"(and no DISPLAY/code CLI); set $EDITOR")
            subprocess.Popen([editor, f"+{line}", str(path)],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             start_new_session=True)
            backend = editor
        return AppResult(ok=True, output=f"opened {path}:{line} via {backend}",
                         parsed={"path": path, "line": line, "backend": backend,
                                 "note": "best-effort on headless hosts"})
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
    "window_resize_move": _window_resize_move,
    "menu_navigate": _menu_navigate,
    "dialog_handle": _dialog_handle,
    "editor_control": _editor_control,

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
    ("window_resize_move", "Resize/move an OS window (wmctrl/xdotool); honest headless note",
     {"name": _sch("string", True, "Window title substring"),
      "width": _sch("integer", False, "Width px"), "height": _sch("integer", False, "Height px"),
      "x": _sch("integer", False, "X px"), "y": _sch("integer", False, "Y px")},
     "computer", 30),
    ("menu_navigate", "Keyboard-driven menu navigation (Alt accelerators + arrow keys)",
     {"sequence": _sch("array", False, "Steps: {hotkey:[..]} | {key:name} | {type:text}"),
      "alt_menu": _sch("string", False, "Alt+letter to open a menu"),
      "path": _sch("array", False, "Keys after the menu opens"),
      "step_delay": _sch("number", False, "Delay between steps (default 0.4)")},
     "computer", 120),
    ("dialog_handle", "Best-effort dialog interaction: confirm|cancel|fill on focused window",
     {"action": _sch("string", False, "confirm|cancel|fill (default confirm)"),
      "text": _sch("string", False, "Text for fill"),
      "tabs": _sch("integer", False, "Tab presses before typing (default 0)"),
      "submit": _sch("boolean", False, "Press Enter after fill (default true)")},
     "computer", 60),
    ("editor_control", "Open a file at a line (VS Code CLI, $EDITOR, or xdg-open)",
     {"path": _sch("string", True, "File path"),
      "line": _sch("integer", False, "Line number (default 1)")},
     "computer", 30),

]
