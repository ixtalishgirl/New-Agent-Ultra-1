"""System tool module — real host introspection and maintenance.

Reads ``/proc`` directly on Linux (with ``ps``/``platform`` fallbacks),
parses ``pip list``, manages environment variables and temp-file cleanup.
Every method returns a :class:`SysResult` (ok, output, parsed, error).
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


@dataclass
class SysResult:
    """Structured result of a system operation."""
    ok: bool
    output: str = ""
    parsed: dict = field(default_factory=dict)
    error: str = ""


def _read_proc(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def _human(n: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.1f}{u}" if u != "B" else f"{int(f)}B"
        f /= 1024
    return f"{f:.1f}PB"


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


class SystemTools:
    """Host introspection and maintenance utilities."""

    # -- identity ---------------------------------------------------------------

    def sysinfo(self) -> SysResult:
        """Platform, Python, CPU, memory summary — one snapshot."""
        mem = self.memory_info().parsed if self.memory_info().ok else {}
        cpu = self.cpu_info().parsed if self.cpu_info().ok else {}
        parsed = {
            "hostname": platform.node(),
            "os": platform.system(),
            "os_release": platform.release(),
            "os_version": platform.version(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "python_version": platform.python_version(),
            "python_impl": platform.python_implementation(),
            "python_executable": sys.executable,
            "cpu_count_logical": os.cpu_count(),
            "cpu_model": cpu.get("model_name", ""),
            "cpu_cores_physical": cpu.get("cores_physical"),
            "mem_total": mem.get("total"),
            "mem_available": mem.get("available"),
            "pid": os.getpid(),
            "cwd": os.getcwd(),
            "user": os.environ.get("USER", os.environ.get("USERNAME", "")),
        }
        lines = [
            f"host={parsed['hostname']} os={parsed['os']} {parsed['os_release']} ({parsed['machine']})",
            f"python={parsed['python_version']} ({parsed['python_impl']}) @ {parsed['python_executable']}",
            f"cpu={parsed['cpu_model'] or 'n/a'} x{parsed['cpu_count_logical']}",
            f"mem_total={_human(parsed['mem_total']) if parsed['mem_total'] else 'n/a'} "
            f"available={_human(parsed['mem_available']) if parsed['mem_available'] else 'n/a'}",
        ]
        return SysResult(ok=True, output="\n".join(lines), parsed=parsed)

    def cpu_info(self) -> SysResult:
        """/proc/cpuinfo parsing: model, physical cores, flags count."""
        text = _read_proc("/proc/cpuinfo")
        if not text:
            # Fallback: platform module only
            return SysResult(ok=True,
                             output=f"cpu_count={os.cpu_count()} (no /proc/cpuinfo)",
                             parsed={"logical": os.cpu_count(),
                                     "model_name": platform.processor(),
                                     "source": "fallback"})
        models: list[str] = []
        physical_ids: set[str] = set()
        cores: set[str] = set()
        flags = ""
        mhz: list[float] = []
        for block in text.strip().split("\n\n"):
            info: dict[str, str] = {}
            for line in block.splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    info[k.strip()] = v.strip()
            if info.get("model name"):
                models.append(info["model name"])
            if "physical id" in info and "core id" in info:
                physical_ids.add(info["physical id"])
                cores.add(info["physical id"] + ":" + info["core id"])
            if info.get("flags"):
                flags = info["flags"]
            if info.get("cpu MHz"):
                try:
                    mhz.append(float(info["cpu MHz"]))
                except ValueError:
                    pass
        parsed = {
            "logical": len(models) or os.cpu_count(),
            "model_name": models[0] if models else "",
            "cores_physical": len(cores) or None,
            "sockets": len(physical_ids) or None,
            "flag_count": len(flags.split()),
            "mhz_avg": round(sum(mhz) / len(mhz), 1) if mhz else None,
            "source": "/proc/cpuinfo",
        }
        out = (f"model={parsed['model_name']} logical={parsed['logical']} "
               f"physical_cores={parsed['cores_physical']} "
               f"mhz_avg={parsed['mhz_avg']}")
        return SysResult(ok=True, output=out, parsed=parsed)

    def memory_info(self) -> SysResult:
        """/proc/meminfo parsing: total/free/available/swap."""
        text = _read_proc("/proc/meminfo")
        if not text:
            return SysResult(ok=False, output="No /proc/meminfo",
                             parsed={}, error="no meminfo")
        info: dict[str, int] = {}
        for line in text.splitlines():
            m = re.match(r"(\w+):\s+(\d+)\s*kB", line)
            if m:
                info[m.group(1)] = int(m.group(2)) * 1024
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        parsed = {
            "total": total,
            "free": info.get("MemFree", 0),
            "available": avail,
            "used": total - avail,
            "used_pct": round((total - avail) / total * 100, 1) if total else 0.0,
            "buffers": info.get("Buffers", 0),
            "cached": info.get("Cached", 0),
            "swap_total": info.get("SwapTotal", 0),
            "swap_free": info.get("SwapFree", 0),
        }
        out = (f"mem total={_human(total)} used={_human(parsed['used'])} "
               f"({parsed['used_pct']}%) available={_human(avail)} "
               f"swap={_human(parsed['swap_total'])}")
        return SysResult(ok=True, output=out, parsed=parsed)

    def disk_usage(self, path: str = "/") -> SysResult:
        """Filesystem usage for the mount containing ``path``."""
        try:
            total, used, free = shutil.disk_usage(path)
        except OSError as e:
            return SysResult(ok=False, output=str(e), parsed={}, error=str(e))
        pct = round(used / total * 100, 1) if total else 0.0
        # Find mount point
        mount = path
        try:
            proc = subprocess.run(["df", "-P", path], capture_output=True,
                                  text=True, timeout=10)
            if proc.returncode == 0:
                parts = proc.stdout.strip().splitlines()
                if len(parts) >= 2:
                    cols = parts[1].split()
                    mount = cols[5] if len(cols) >= 6 else path
        except (OSError, subprocess.TimeoutExpired):
            pass
        parsed = {"path": path, "mount": mount, "total": total,
                  "used": used, "free": free, "used_pct": pct}
        out = (f"{mount}: total={_human(total)} used={_human(used)} ({pct}%) "
               f"free={_human(free)}")
        return SysResult(ok=True, output=out, parsed=parsed)

    def all_mounts(self) -> SysResult:
        """All mounted filesystems with usage (parses /proc/mounts + statvfs)."""
        mounts = []
        for line in _read_proc("/proc/mounts").splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            device, mnt, fstype = parts[0], parts[1], parts[2]
            # Skip pseudo filesystems, but keep tmpfs mounts we care about.
            if fstype in ("proc", "sysfs", "cgroup", "cgroup2", "devpts",
                          "mqueue", "tracefs", "debugfs", "securityfs") \
                    or (fstype == "tmpfs"
                        and mnt not in ("/tmp", "/dev/shm")):
                continue
            try:
                vfs = os.statvfs(mnt)
                total = vfs.f_frsize * vfs.f_blocks
                free = vfs.f_frsize * vfs.f_bavail
                used = total - free
                mounts.append({
                    "device": device, "mount": mnt, "fstype": fstype,
                    "total": total, "used": used, "free": free,
                    "used_pct": round(used / total * 100, 1) if total else 0.0,
                })
            except OSError:
                continue
        mounts.sort(key=lambda m: m["mount"])
        lines = ["mounts:"]
        for m in mounts:
            lines.append(f"  {m['mount']:<20} {m['fstype']:<10} "
                         f"{_human(m['used']):>8}/{_human(m['total']):<8} "
                         f"{m['used_pct']}%")
        return SysResult(ok=True, output="\n".join(lines),
                         parsed={"mounts": mounts, "count": len(mounts)})

    # -- load / uptime ------------------------------------------------------------

    def uptime(self) -> SysResult:
        """/proc/uptime: how long the system has been running."""
        text = _read_proc("/proc/uptime").strip()
        if not text:
            return SysResult(ok=False, output="No /proc/uptime",
                             parsed={}, error="no uptime")
        parts = text.split()
        up_sec = float(parts[0])
        idle_sec = float(parts[1]) if len(parts) > 1 else 0.0
        boot = datetime.now(timezone.utc).timestamp() - up_sec
        parsed = {"uptime_seconds": up_sec,
                  "uptime_human": _fmt_duration(up_sec),
                  "idle_seconds": idle_sec,
                  "boot_time": datetime.fromtimestamp(
                      boot, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}
        return SysResult(ok=True,
                         output=f"up {parsed['uptime_human']} (since {parsed['boot_time']})",
                         parsed=parsed)

    def load_avg(self) -> SysResult:
        """1/5/15-minute load averages plus runnable process counts."""
        text = _read_proc("/proc/loadavg").strip()
        if not text:
            try:
                avgs = os.getloadavg()
                text = f"{avgs[0]:.2f} {avgs[1]:.2f} {avgs[2]:.2f}"
            except OSError:
                return SysResult(ok=False, output="Load average unavailable",
                                 parsed={}, error="unavailable")
        parts = text.split()
        parsed = {"load_1": float(parts[0]), "load_5": float(parts[1]),
                  "load_15": float(parts[2]),
                  "runnable": parts[3] if len(parts) > 3 else "",
                  "last_pid": parts[4] if len(parts) > 4 else ""}
        ncpu = os.cpu_count() or 1
        verdict = ("HIGH" if parsed["load_1"] > ncpu
                   else "moderate" if parsed["load_1"] > ncpu * 0.7 else "low")
        out = (f"load {parsed['load_1']:.2f} {parsed['load_5']:.2f} "
               f"{parsed['load_15']:.2f} (cpus={ncpu}, load={verdict})")
        parsed["verdict"] = verdict
        return SysResult(ok=True, output=out, parsed=parsed)

    # -- processes ------------------------------------------------------------------

    def process_list(self, sort_by: str = "cpu", limit: int = 25,
                     name_filter: str = "") -> SysResult:
        """Process list from /proc (fallback: ``ps``). Sorted by cpu or mem."""
        procs = self._procs_from_proc()
        if not procs:
            procs = self._procs_from_ps()
        if name_filter:
            pat = name_filter.lower()
            procs = [p for p in procs if pat in p["name"].lower()
                     or pat in p["cmdline"].lower()]
        key = {"cpu": lambda p: p["cpu_pct"],
               "mem": lambda p: p["mem_kb"],
               "pid": lambda p: p["pid"]}.get(sort_by, lambda p: p["cpu_pct"])
        procs.sort(key=key, reverse=True)
        shown = procs[:limit]
        lines = [f"{'PID':>7} {'CPU%':>6} {'MEM':>9} {'USER':<12} NAME"]
        for p in shown:
            lines.append(f"{p['pid']:>7} {p['cpu_pct']:>6.1f} "
                         f"{_human(p['mem_kb'] * 1024):>9} "
                         f"{p['user']:<12} {p['name'][:60]}")
        return SysResult(ok=True, output="\n".join(lines),
                         parsed={"processes": shown, "shown": len(shown),
                                 "total": len(procs), "sort_by": sort_by})

    def _procs_from_proc(self) -> list[dict]:
        procs: list[dict] = []
        try:
            hz = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        except (KeyError, AttributeError, ValueError, OSError):
            hz = 100
        try:
            with open("/proc/uptime") as f:
                uptime = float(f.read().split()[0])
        except OSError:
            uptime = 0.0
        try:
            pids = [p for p in os.listdir("/proc") if p.isdigit()]
        except OSError:
            return []
        for pid in pids:
            try:
                with open(f"/proc/{pid}/stat") as f:
                    parts = f.read().rsplit(")", 1)
                comm = parts[0].split("(", 1)[1] if len(parts) > 1 else pid
                rest = parts[1].split() if len(parts) > 1 else []
                # fields after comm: state(0) ppid(1) ... utime(11) stime(12)
                # cutime(13) cstime(14) ... starttime(19) vsize(20) rss(21)
                utime = float(rest[11]) if len(rest) > 11 else 0.0
                stime = float(rest[12]) if len(rest) > 12 else 0.0
                starttime = float(rest[19]) if len(rest) > 19 else 0.0
                rss_pages = int(rest[21]) if len(rest) > 21 else 0
                elapsed = max(uptime - starttime / hz, 0.01)
                cpu_pct = (utime + stime) / hz / elapsed * 100
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmdline = f.read().replace(b"\x00", b" ").decode(
                        "utf-8", "replace").strip()
                try:
                    st = os.stat(f"/proc/{pid}")
                    import pwd
                    user = pwd.getpwuid(st.st_uid).pw_name
                except (OSError, KeyError, ImportError):
                    user = "?"
                procs.append({
                    "pid": int(pid), "name": comm or cmdline.split()[0] if cmdline else comm,
                    "cmdline": cmdline or comm,
                    "cpu_pct": round(min(cpu_pct, 9999.0), 1),
                    "mem_kb": rss_pages * 4,
                    "user": user,
                })
            except (OSError, IndexError, ValueError):
                continue
        return procs

    def _procs_from_ps(self) -> list[dict]:
        try:
            proc = subprocess.run(
                ["ps", "-eo", "pid,pcpu,rss,user,comm,args"],
                capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return []
        procs = []
        for line in proc.stdout.splitlines()[1:]:
            parts = line.split(None, 5)
            if len(parts) < 5:
                continue
            try:
                procs.append({
                    "pid": int(parts[0]),
                    "cpu_pct": float(parts[1]),
                    "mem_kb": int(parts[2]),
                    "user": parts[3],
                    "name": parts[4],
                    "cmdline": parts[5] if len(parts) > 5 else parts[4],
                })
            except (ValueError, IndexError):
                continue
        return procs

    def process_kill(self, pid: int, signal_name: str = "TERM") -> SysResult:
        """Send a signal to a process. SAFETY: refuses pid 1 and self."""
        import signal as _signal
        if pid <= 1:
            return SysResult(ok=False, output="Refusing to signal pid <= 1",
                             parsed={}, error="protected pid")
        if pid == os.getpid():
            return SysResult(ok=False, output="Refusing to signal self",
                             parsed={}, error="self pid")
        try:
            sig = getattr(_signal, f"SIG{signal_name.upper()}")
        except AttributeError:
            return SysResult(ok=False, output=f"Unknown signal: {signal_name}",
                             parsed={}, error="bad signal")
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return SysResult(ok=False, output=f"No such process: {pid}",
                             parsed={}, error="no such process")
        except PermissionError as e:
            return SysResult(ok=False, output=f"Permission denied: {e}",
                             parsed={}, error="permission denied")
        return SysResult(ok=True, output=f"Sent SIG{signal_name.upper()} to pid {pid}",
                         parsed={"pid": pid, "signal": signal_name.upper()})

    # -- environment ------------------------------------------------------------------

    def env_get(self, name: str = "", prefix: str = "") -> SysResult:
        """Get one env var (``name``), all vars with ``prefix``, or everything."""
        if name:
            value = os.environ.get(name)
            if value is None:
                return SysResult(ok=False, output=f"Env var not set: {name}",
                                 parsed={"name": name, "set": False},
                                 error="not set")
            return SysResult(ok=True, output=f"{name}={value}",
                             parsed={"name": name, "value": value, "set": True})
        items = {k: v for k, v in os.environ.items()
                 if not prefix or k.startswith(prefix)}
        # Never echo likely secrets in output text; mark them redacted.
        secret_hint = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|AUTH|PRIVATE)",
                                 re.IGNORECASE)
        lines = []
        parsed_vars: dict[str, str] = {}
        for k in sorted(items):
            v = items[k]
            if secret_hint.search(k):
                lines.append(f"{k}=<redacted>")
                parsed_vars[k] = "<redacted>"
            else:
                shown = v if len(v) <= 200 else v[:200] + "..."
                lines.append(f"{k}={shown}")
                parsed_vars[k] = v
        return SysResult(ok=True,
                         output=f"{len(items)} var(s):\n" + "\n".join(lines),
                         parsed={"vars": parsed_vars, "count": len(items),
                                 "prefix": prefix})

    def env_set(self, name: str, value: str) -> SysResult:
        """Set an env var for this process (and children it spawns)."""
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
            return SysResult(ok=False, output=f"Invalid env var name: {name!r}",
                             parsed={}, error="bad name")
        existed = name in os.environ
        os.environ[name] = value
        return SysResult(ok=True,
                         output=f"Set {name} ({'updated' if existed else 'new'})",
                         parsed={"name": name, "existed": existed,
                                 "value_length": len(value)})

    def env_unset(self, name: str) -> SysResult:
        """Remove an env var from this process."""
        existed = name in os.environ
        os.environ.pop(name, None)
        return SysResult(ok=True,
                         output=f"Unset {name} ({'was set' if existed else 'was not set'})",
                         parsed={"name": name, "existed": existed})

    # -- lookup / packages ---------------------------------------------------------------

    def which_command(self, name: str) -> SysResult:
        """Locate executables on PATH (like ``which -a``)."""
        if not re.match(r"^[A-Za-z0-9_.\-]+$", name):
            return SysResult(ok=False, output=f"Bad command name: {name!r}",
                             parsed={}, error="bad name")
        paths = []
        seen: set[str] = set()
        for directory in os.environ.get("PATH", "").split(os.pathsep):
            candidate = os.path.join(directory, name)
            if candidate not in seen and os.path.isfile(candidate) \
                    and os.access(candidate, os.X_OK):
                seen.add(candidate)
                paths.append(candidate)
        if not paths:
            return SysResult(ok=False, output=f"Command not found on PATH: {name}",
                             parsed={"name": name, "paths": []},
                             error="not found")
        return SysResult(ok=True,
                         output=f"{name}: " + ", ".join(paths),
                         parsed={"name": name, "paths": paths,
                                 "primary": paths[0]})

    def python_packages(self, name_filter: str = "",
                        timeout: int = 60) -> SysResult:
        """Parse ``pip list --format=json``: installed packages + versions."""
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pip", "list", "--format=json"],
                capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            return SysResult(ok=False, output=f"pip list failed: {e}",
                             parsed={}, error=str(e))
        if proc.returncode != 0:
            return SysResult(ok=False, output=proc.stderr.strip() or "pip failed",
                             parsed={}, error="pip failed")
        try:
            pkgs = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            return SysResult(ok=False, output=f"Bad pip output: {e}",
                             parsed={}, error=str(e))
        if name_filter:
            pat = name_filter.lower()
            pkgs = [p for p in pkgs if pat in p["name"].lower()]
        pkgs.sort(key=lambda p: p["name"].lower())
        lines = [f"{len(pkgs)} package(s) installed"]
        for p in pkgs[:100]:
            lines.append(f"  {p['name']}=={p['version']}")
        if len(pkgs) > 100:
            lines.append(f"  ... and {len(pkgs) - 100} more")
        return SysResult(ok=True, output="\n".join(lines),
                         parsed={"packages": pkgs, "count": len(pkgs)})

    def pip_show(self, package: str, timeout: int = 30) -> SysResult:
        """Details of one installed package (``pip show``)."""
        if not re.match(r"^[A-Za-z0-9_.\-]+$", package):
            return SysResult(ok=False, output=f"Bad package name: {package!r}",
                             parsed={}, error="bad name")
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pip", "show", package],
                capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            return SysResult(ok=False, output=str(e), parsed={}, error=str(e))
        if proc.returncode != 0:
            return SysResult(ok=False, output=f"Package not installed: {package}",
                             parsed={"package": package, "installed": False},
                             error="not installed")
        info: dict[str, str] = {}
        for line in proc.stdout.splitlines():
            if ": " in line:
                k, _, v = line.partition(": ")
                info[k.strip()] = v.strip()
        return SysResult(ok=True, output=proc.stdout.strip(),
                         parsed={"package": package, "installed": True,
                                 "info": info})

    # -- temp cleanup ----------------------------------------------------------------------

    def temp_files_cleanup(self, older_than_hours: float = 24.0,
                           dry_run: bool = True,
                           extra_dirs: list[str] | None = None) -> SysResult:
        """Delete files in temp dirs older than N hours.

        SAFETY: dry_run=True by default (reports only). Only regular files
        are removed; directories and sockets are left alone.
        """
        now = time.time()
        cutoff = older_than_hours * 3600
        dirs = [tempfile.gettempdir(), "/tmp", "/var/tmp"]
        for extra in extra_dirs or []:
            p = str(Path(extra).expanduser())
            if p not in dirs:
                dirs.append(p)
        candidates: list[dict[str, Any]] = []
        for d in dirs:
            base = Path(d)
            if not base.is_dir():
                continue
            try:
                for entry in base.iterdir():
                    try:
                        if entry.is_symlink() or not entry.is_file():
                            continue
                        age = now - entry.stat().st_mtime
                        if age >= cutoff:
                            candidates.append({
                                "path": str(entry),
                                "size": entry.stat().st_size,
                                "age_hours": round(age / 3600, 1),
                            })
                    except OSError:
                        continue
            except OSError:
                continue
        freed = sum(c["size"] for c in candidates)
        if dry_run:
            lines = [f"DRY RUN — {len(candidates)} file(s) would be removed "
                     f"({_human(freed)}), older than {older_than_hours}h:"]
            for c in candidates[:50]:
                lines.append(f"  {_human(c['size']):>8}  {c['age_hours']:>7}h  {c['path']}")
            if len(candidates) > 50:
                lines.append(f"  ... and {len(candidates) - 50} more")
            return SysResult(ok=True, output="\n".join(lines),
                             parsed={"dry_run": True,
                                     "candidates": candidates,
                                     "count": len(candidates),
                                     "bytes": freed})
        removed, errors = 0, 0
        for c in candidates:
            try:
                os.unlink(c["path"])
                removed += 1
            except OSError:
                errors += 1
        return SysResult(ok=True,
                         output=f"Removed {removed} file(s) ({_human(freed)}), "
                                f"{errors} error(s)",
                         parsed={"dry_run": False, "removed": removed,
                                 "errors": errors, "bytes_freed": freed})

    def disk_temp_summary(self) -> SysResult:
        """Quick sizes of common temp/cache locations."""
        locations = [tempfile.gettempdir(), "/tmp", "/var/tmp",
                     str(Path.home() / ".cache")]
        rows = []
        for loc in dict.fromkeys(locations):
            p = Path(loc)
            if not p.is_dir():
                rows.append({"path": loc, "exists": False, "size": 0})
                continue
            total = 0
            count = 0
            try:
                for dirpath, _d, files in os.walk(p):
                    for fn in files:
                        fp = os.path.join(dirpath, fn)
                        try:
                            total += os.path.getsize(fp)
                            count += 1
                        except OSError:
                            continue
            except OSError:
                pass
            rows.append({"path": loc, "exists": True, "size": total,
                         "files": count})
        lines = ["temp locations:"]
        for r in rows:
            if r["exists"]:
                lines.append(f"  {_human(r['size']):>9}  {r['files']:>7} files  {r['path']}")
            else:
                lines.append(f"  {'-':>9}  {'-':>7}        {r['path']} (missing)")
        return SysResult(ok=True, output="\n".join(lines),
                         parsed={"locations": rows})

    # -- users / misc ------------------------------------------------------------------------

    def logged_in_users(self) -> SysResult:
        """Users currently logged in (parses ``who`` output)."""
        try:
            proc = subprocess.run(["who"], capture_output=True, text=True,
                                  timeout=10)
        except (OSError, subprocess.TimeoutExpired) as e:
            return SysResult(ok=False, output=str(e), parsed={}, error=str(e))
        users = []
        for line in proc.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                users.append({"user": parts[0], "terminal": parts[1],
                              "detail": " ".join(parts[2:])})
        return SysResult(ok=True,
                         output="\n".join(
                             f"{u['user']} on {u['terminal']} {u['detail']}"
                             for u in users) or "(no one logged in)",
                         parsed={"users": users, "count": len(users)})

    def open_ports_listening(self) -> SysResult:
        """Listening TCP/UDP sockets via /proc/net/tcp* (Linux)."""
        results: list[dict[str, Any]] = []
        for path, proto in (("/proc/net/tcp", "tcp"), ("/proc/net/tcp6", "tcp6"),
                            ("/proc/net/udp", "udp"), ("/proc/net/udp6", "udp6")):
            text = _read_proc(path)
            for line in text.splitlines()[1:]:
                parts = line.split()
                if len(parts) < 4:
                    continue
                local, state = parts[1], parts[3]
                if state != "0A":  # LISTEN
                    continue
                try:
                    ip_hex, port_hex = local.rsplit(":", 1)
                    port = int(port_hex, 16)
                    ip = self._hex_ip(ip_hex)
                except ValueError:
                    continue
                results.append({"proto": proto, "ip": ip, "port": port})
        results.sort(key=lambda r: (r["proto"], r["port"]))
        lines = [f"{len(results)} listening socket(s):"]
        for r in results:
            lines.append(f"  {r['proto']:<5} {r['ip']}:{r['port']}")
        return SysResult(ok=True, output="\n".join(lines),
                         parsed={"listening": results,
                                 "count": len(results)})

    @staticmethod
    def _hex_ip(hex_ip: str) -> str:
        """Decode /proc/net hex IP (little-endian IPv4 or IPv6)."""
        hex_ip = hex_ip.strip()
        if len(hex_ip) == 8:  # IPv4 little-endian
            b = bytes.fromhex(hex_ip)
            return ".".join(str(x) for x in reversed(b))
        if len(hex_ip) == 32:  # IPv6
            b = bytes.fromhex(hex_ip)
            groups = []
            for i in range(0, 16, 4):
                word = int.from_bytes(b[i:i + 4], "little")
                groups.append(f"{word:04x}")
            return ":".join(groups)
        return hex_ip


__all__ = ["SystemTools", "SysResult"]
