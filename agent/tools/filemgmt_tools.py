"""File Management extensions — real os/shutil/pathlib operations.

Covers the day-to-day file management verbs missing from file_tools.py:
append, copy, move, rename, delete-to-trash, mkdir, flat directory listing,
stat metadata, chmod, file watching (mtime/hash polling), advisory file
locks (fcntl), and restoring files from the trash.

Safety:
- Deletes go to ``~/.trash`` (timestamped, unique names), never ``rm``.
  Restore via ``trash_restore``.
- ``file_delete`` requires ``confirm=True`` (a dispatcher-level
  confirmation toggle also exists; this tool enforces its own arg).
- No sandbox/isolation layer by user architecture decision: direct
  Python filesystem calls.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    import fcntl  # POSIX only
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None  # type: ignore[assignment]

TRASH_DIR = Path.home() / ".trash"
DEFAULT_TIMEOUT = 60


# ---------------------------------------------------------------------------
# Result type (local; decoupled from other tool modules by design)
# ---------------------------------------------------------------------------

@dataclass
class FileMgmtResult:
    """Structured result of a file-management operation."""
    ok: bool
    output: str = ""
    parsed: dict = field(default_factory=dict)
    error: str = ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ensure_trash() -> Path:
    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    return TRASH_DIR


def _trash_path(original: Path) -> Path:
    """Unique trash destination preserving a hint of the original name."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", original.name) or "unnamed"
    candidate = _ensure_trash() / f"{stamp}_{safe_name}"
    n = 1
    while candidate.exists():
        candidate = _ensure_trash() / f"{stamp}_{n}_{safe_name}"
        n += 1
    return candidate


def _file_fingerprint(path: Path) -> tuple[float, int, str]:
    """(mtime_ns, size, sha256-hexdigest) snapshot of a file."""
    st = path.stat()
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return (st.st_mtime_ns, st.st_size, h.hexdigest())


def _result_from_exc(exc: Exception) -> FileMgmtResult:
    return FileMgmtResult(ok=False, error=f"{type(exc).__name__}: {exc}")


_LOCKED_FDS: dict[str, Any] = {}  # path -> open file object holding the lock


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _file_append(args: dict) -> FileMgmtResult:
    try:
        _deny = _check_access(args.get("path", ""))
        if _deny:
            return FileMgmtResult(ok=False, error=_deny)
        path = Path(args["path"])
        content = str(args.get("content", ""))
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(content)
        size = path.stat().st_size
        return FileMgmtResult(
            ok=True, output=f"Appended {len(content)} chars to {path} ({size} bytes total).",
            parsed={"path": str(path), "appended_chars": len(content), "size_bytes": size})
    except Exception as e:
        return _result_from_exc(e)


def _file_copy(args: dict) -> FileMgmtResult:
    try:
        _deny = _check_access(args.get("src", ""))
        if _deny:
            return FileMgmtResult(ok=False, error=_deny)
        src = Path(args["src"]); dest = Path(args["dest"])
        deny = _check_access(args.get("dest", ""))
        if deny:
            return FileMgmtResult(ok=False, error=deny)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        st = dest.stat()
        return FileMgmtResult(
            ok=True, output=f"Copied {src} -> {dest} ({st.st_size} bytes).",
            parsed={"src": str(src), "dest": str(dest), "size_bytes": st.st_size})
    except Exception as e:
        return _result_from_exc(e)


def _file_move(args: dict) -> FileMgmtResult:
    try:
        _deny = _check_access(args.get("src", ""))
        if _deny:
            return FileMgmtResult(ok=False, error=_deny)
        src = Path(args["src"]); dest = Path(args["dest"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        return FileMgmtResult(
            ok=True, output=f"Moved {src} -> {dest}.",
            parsed={"src": str(src), "dest": str(dest)})
    except Exception as e:
        return _result_from_exc(e)


def _file_delete(args: dict) -> FileMgmtResult:
    try:
        _deny = _check_access(args.get("path", ""))
        if _deny:
            return FileMgmtResult(ok=False, error=_deny)
        if not args.get("confirm", False):
            return FileMgmtResult(
                ok=False,
                error="Refused: file_delete requires confirm=True.")
        path = Path(args["path"])
        if not path.exists() and not path.is_symlink():
            return FileMgmtResult(ok=False, error=f"Path does not exist: {path}")
        dest = _trash_path(path)
        shutil.move(str(path), str(dest))
        return FileMgmtResult(
            ok=True,
            output=f"Moved {path} to trash: {dest.name}",
            parsed={"original": str(path), "trash_name": dest.name,
                    "trash_path": str(dest)})
    except Exception as e:
        return _result_from_exc(e)


def _file_rename(args: dict) -> FileMgmtResult:
    try:
        _deny = _check_access(args.get("src", ""))
        if _deny:
            return FileMgmtResult(ok=False, error=_deny)
        src = Path(args["src"]); dest = Path(args["dest"])
        os.rename(src, dest)
        return FileMgmtResult(
            ok=True, output=f"Renamed {src} -> {dest}.",
            parsed={"src": str(src), "dest": str(dest)})
    except Exception as e:
        return _result_from_exc(e)


def _dir_create(args: dict) -> FileMgmtResult:
    try:
        _deny = _check_access(args.get("path", ""))
        if _deny:
            return FileMgmtResult(ok=False, error=_deny)
        path = Path(args["path"])
        path.mkdir(parents=True, exist_ok=True)
        return FileMgmtResult(
            ok=True, output=f"Directory ready: {path}",
            parsed={"path": str(path)})
    except Exception as e:
        return _result_from_exc(e)


def _dir_list(args: dict) -> FileMgmtResult:
    try:
        path = Path(args["path"])
        entries = []
        for child in sorted(path.iterdir(), key=lambda p: p.name):
            try:
                st = child.stat()
                entries.append({
                    "name": child.name,
                    "is_dir": child.is_dir(),
                    "size_bytes": st.st_size,
                    "mtime": st.st_mtime,
                })
            except OSError:
                entries.append({"name": child.name, "is_dir": False,
                                "size_bytes": -1, "mtime": 0.0})
        lines = [f"{'[DIR] ' if e['is_dir'] else '      '}{e['name']}"
                 f"{'' if e['is_dir'] else f'  ({e['size_bytes']}B)'}" for e in entries]
        return FileMgmtResult(
            ok=True, output=f"{path} ({len(entries)} entries):\n" + "\n".join(lines),
            parsed={"path": str(path), "count": len(entries), "entries": entries})
    except Exception as e:
        return _result_from_exc(e)


def _file_metadata(args: dict) -> FileMgmtResult:
    try:
        path = Path(args["path"])
        st = path.stat()
        import pwd, grp
        try:
            owner = pwd.getpwuid(st.st_uid).pw_name
        except (KeyError, ImportError):
            owner = str(st.st_uid)
        try:
            group = grp.getgrgid(st.st_gid).gr_name
        except (KeyError, ImportError):
            group = str(st.st_gid)
        meta = {
            "path": str(path),
            "size_bytes": st.st_size,
            "mtime": st.st_mtime,
            "mtime_iso": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            "mode_octal": oct(stat.S_IMODE(st.st_mode)),
            "mode_str": stat.filemode(st.st_mode),
            "owner": owner,
            "group": group,
            "uid": st.st_uid,
            "gid": st.st_gid,
            "is_dir": path.is_dir(),
            "is_symlink": path.is_symlink(),
        }
        return FileMgmtResult(
            ok=True,
            output=(f"{path}: {meta['size_bytes']} bytes, mode {meta['mode_octal']}, "
                    f"owner {meta['owner']}:{meta['group']}, mtime {meta['mtime_iso']}"),
            parsed=meta)
    except Exception as e:
        return _result_from_exc(e)


def _file_chmod(args: dict) -> FileMgmtResult:
    try:
        path = Path(args["path"])
        mode_arg = args["mode"]
        mode = int(str(mode_arg), 8) if isinstance(mode_arg, str) else int(mode_arg)
        os.chmod(path, mode)
        new_mode = oct(stat.S_IMODE(path.stat().st_mode))
        return FileMgmtResult(
            ok=True, output=f"chmod {new_mode} {path}",
            parsed={"path": str(path), "mode_octal": new_mode})
    except Exception as e:
        return _result_from_exc(e)


def _file_watch(args: dict) -> FileMgmtResult:
    try:
        path = Path(args["path"])
        timeout = float(args.get("timeout_sec", 10))
        poll_interval = 0.5
        if not path.exists():
            return FileMgmtResult(ok=False, error=f"Path does not exist: {path}")
        events: list[dict] = []
        prev = _file_fingerprint(path)
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            time.sleep(poll_interval)
            if not path.exists():
                events.append({"event": "deleted",
                               "ts": datetime.now(timezone.utc).isoformat()})
                break
            cur = _file_fingerprint(path)
            if cur[2] != prev[2]:  # content hash changed
                events.append({"event": "modified",
                               "ts": datetime.now(timezone.utc).isoformat(),
                               "old_size": prev[1], "new_size": cur[1],
                               "old_hash": prev[2][:16], "new_hash": cur[2][:16]})
            prev = cur
        status = f"change(s) detected" if events else "no changes"
        return FileMgmtResult(
            ok=True,
            output=f"Watched {path} for {timeout}s: {status} ({len(events)} events).",
            parsed={"path": str(path), "timeout_sec": timeout,
                    "events": events})
    except Exception as e:
        return _result_from_exc(e)


def _file_lock(args: dict) -> FileMgmtResult:
    try:
        if fcntl is None:
            return FileMgmtResult(
                ok=False, error="fcntl unavailable on this platform; file locks not supported.")
        path = str(Path(args["path"]))
        action = str(args.get("action", "lock")).lower()
        if action == "lock":
            if path in _LOCKED_FDS:
                return FileMgmtResult(ok=False, error=f"Already locked by this process: {path}")
            fh = open(path, "a+b")
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                fh.close()
                return FileMgmtResult(ok=False, error=f"Lock contention on {path}: {e}")
            _LOCKED_FDS[path] = fh
            return FileMgmtResult(
                ok=True, output=f"Exclusive lock acquired on {path}.",
                parsed={"path": path, "action": "lock"})
        elif action == "unlock":
            fh = _LOCKED_FDS.pop(path, None)
            if fh is None:
                return FileMgmtResult(ok=False, error=f"No lock held by this process on: {path}")
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            finally:
                fh.close()
            return FileMgmtResult(
                ok=True, output=f"Lock released on {path}.",
                parsed={"path": path, "action": "unlock"})
        else:
            return FileMgmtResult(ok=False, error=f"Unknown action: {action} (use lock|unlock)")
    except Exception as e:
        return _result_from_exc(e)


def _trash_restore(args: dict) -> FileMgmtResult:
    try:
        trash_name = args["trash_name"]
        trash_dir = _ensure_trash()
        src = trash_dir / trash_name
        if not src.exists():
            # Fallback: match by original filename suffix
            # (trash entries are "<timestamp>_<original name>").
            candidates = [p for p in trash_dir.iterdir()
                          if p.name == trash_name or
                          p.name.endswith("_" + trash_name)]
            if not candidates:
                return FileMgmtResult(ok=False,
                                      error=f"Not in trash: {trash_name}")
            # Most recent first.
            candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            src = candidates[0]
        dest = Path(args["dest"]) if args.get("dest") else Path.cwd() / src.name.split("_", 1)[-1]
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        return FileMgmtResult(
            ok=True, output=f"Restored {src.name} -> {dest}.",
            parsed={"trash_name": src.name, "dest": str(dest)})
    except Exception as e:
        return _result_from_exc(e)




_RULES_FILE = Path(__file__).resolve().parent.parent / "logs" / "file_access_rules.json"


def _check_access(path: str) -> Optional[str]:
    """Enforce the file_access_rules config (managed by the file_access_rules tool).

    Absent/unparseable rules file -> allow all (user's standing: no restrictions
    beyond the 5 denials). Returns a denial reason or None when allowed.
    """
    try:
        if not _RULES_FILE.exists():
            return None
        rules = json.loads(_RULES_FILE.read_text())
    except Exception:
        return None
    try:
        rp = str(Path(path).resolve())
    except Exception:
        rp = str(path)
    for prefix in rules.get("deny_prefixes", []) or []:
        try:
            if rp.startswith(str(Path(prefix).resolve())):
                return f"denied by file_access_rules (deny_prefixes): {path}"
        except Exception:
            continue
    if (rules.get("mode") or "allow_all") == "allowlist":
        for prefix in rules.get("allow_prefixes", []) or []:
            try:
                if rp.startswith(str(Path(prefix).resolve())):
                    return None
            except Exception:
                continue
        return f"denied by file_access_rules (allowlist mode): {path}"
    return None


def _file_preview(args: dict) -> FileMgmtResult:
    """Preview the head of a file: first N text lines, or a hex head for binaries."""
    try:
        path = args.get("path")
        if not path:
            return FileMgmtResult(ok=False, error="path is required")
        p = Path(path)
        if not p.exists():
            return FileMgmtResult(ok=False, error=f"not found: {path}")
        if p.is_dir():
            return FileMgmtResult(ok=False, error="path is a directory; use dir_list")
        max_lines = max(1, min(int(args.get("max_lines", 50)), 2000))
        max_bytes = max(1024, min(int(args.get("max_bytes", 65536)), 1048576))
        with open(p, "rb") as f:
            head = f.read(min(max_bytes, 131072))
        size = p.stat().st_size
        if b"\x00" in head:
            hexhead = " ".join(f"{b:02x}" for b in head[:64])
            return FileMgmtResult(ok=True,
                output=f"binary file, {size} bytes; hex head: {hexhead}",
                parsed={"path": str(p), "binary": True, "size": size,
                        "hex_head": hexhead})
        text = head.decode("utf-8", errors="replace")
        all_lines = text.splitlines()
        lines = all_lines[:max_lines]
        truncated = len(all_lines) > max_lines or size > len(head)
        out = "\n".join(lines)
        if truncated:
            out += f"\n... [truncated, {size} bytes total]"
        return FileMgmtResult(ok=True, output=out,
            parsed={"path": str(p), "binary": False, "size": size,
                    "lines_shown": len(lines), "truncated": truncated})
    except Exception as e:
        return _result_from_exc(e)


HANDLERS: dict = {
    "file_append": _file_append,
    "file_copy": _file_copy,
    "file_move": _file_move,
    "file_delete": _file_delete,
    "file_rename": _file_rename,
    "dir_create": _dir_create,
    "dir_list": _dir_list,
    "file_metadata": _file_metadata,
    "file_chmod": _file_chmod,
    "file_watch": _file_watch,
    "file_lock": _file_lock,
    "trash_restore": _trash_restore,
    "file_preview": _file_preview,

}


def _sch(t: str, req: bool, desc: str) -> dict:
    return {"type": t, "required": req, "description": desc}


TOOL_DEFS: list[tuple] = [
    ("file_append", "Append text content to a file (creates parent dirs)",
     {"path": _sch("string", True, "Target file path"),
      "content": _sch("string", True, "Text to append")},
     "computer", 30),
    ("file_copy", "Copy a file preserving metadata",
     {"src": _sch("string", True, "Source path"),
      "dest": _sch("string", True, "Destination path")},
     "computer", 60),
    ("file_move", "Move/rename a file or directory",
     {"src": _sch("string", True, "Source path"),
      "dest": _sch("string", True, "Destination path")},
     "computer", 60),
    ("file_delete", "Move a file/dir to trash (requires confirm=True)",
     {"path": _sch("string", True, "Path to delete"),
      "confirm": _sch("boolean", False, "Must be true; refuses when false")},
     "computer", 30),
    ("file_rename", "Rename within same directory",
     {"src": _sch("string", True, "Current path"),
      "dest": _sch("string", True, "New path")},
     "computer", 30),
    ("dir_create", "Create a directory (parents as needed)",
     {"path": _sch("string", True, "Directory path")},
     "computer", 30),
    ("dir_list", "Flat listing of a directory with sizes",
     {"path": _sch("string", True, "Directory path")},
     "computer", 30),
    ("file_metadata", "stat metadata: size, mtime, mode, owner/group",
     {"path": _sch("string", True, "File path")},
     "computer", 20),
    ("file_chmod", "Change file mode (octal string like '644' or int)",
     {"path": _sch("string", True, "File path"),
      "mode": _sch("string", True, "Octal mode, e.g. '644'")},
     "computer", 20),
    ("file_watch", "Poll mtime/hash up to timeout_sec, report change events",
     {"path": _sch("string", True, "File to watch"),
      "timeout_sec": _sch("number", False, "Max seconds to watch (default 10)")},
     "computer", 120),
    ("file_lock", "Advisory exclusive file lock via fcntl (lock|unlock)",
     {"path": _sch("string", True, "File path"),
      "action": _sch("string", False, "lock|unlock (default lock)")},
     "computer", 20),
    ("trash_restore", "Restore a file from ~/.trash (name or original filename; dest optional)",
     {"trash_name": _sch("string", True, "Name inside ~/.trash or original filename"),
      "dest": _sch("string", False, "Restore destination path (default: cwd/original name)")},
     "computer", 30),
    ("file_preview", "Preview file head: first N text lines, or hex head for binaries",
     {"path": _sch("string", True, "File path"),
      "max_lines": _sch("integer", False, "Max text lines (default 50)"),
      "max_bytes": _sch("integer", False, "Max bytes to read (default 65536)")},
     "computer", 30),

]
