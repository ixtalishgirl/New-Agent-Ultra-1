"""File tool module — safe, real filesystem operations.

Design principles:
    * No silent data loss: writes are atomic (temp file + rename),
      deletes go to a trash directory (``~/.trash``) instead of ``rm``.
    * Reads detect encoding (tries chardet when installed, else a BOM/utf-8
      fallback chain) and refuse to decode obvious binaries.
    * Every method returns a :class:`FileResult` (ok, output, parsed, error).
"""
from __future__ import annotations

import fnmatch
import hashlib
import mimetypes
import os
import re
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional


TRASH_DIR = Path.home() / ".trash"
DEFAULT_TIMEOUT = 30

# Bytes that strongly suggest binary content when found in a sample.
_BINARY_HINT = b"\x00"


@dataclass
class FileResult:
    """Structured result of a file operation."""
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


def detect_encoding(path: str | os.PathLike, sample_bytes: int = 65536) -> dict[str, Any]:
    """Detect file encoding.

    Tries ``chardet`` when installed; otherwise walks a fallback chain:
    utf-8-sig -> utf-8 -> latin-1 (which always succeeds). Also reports
    whether the sample looks binary (NUL byte present).
    """
    p = Path(path)
    raw = p.read_bytes()[:sample_bytes]
    result: dict[str, Any] = {
        "encoding": "utf-8",
        "confidence": 1.0,
        "is_binary": _BINARY_HINT in raw,
        "method": "fallback",
        "bom": False,
    }
    if raw.startswith(b"\xef\xbb\xbf"):
        result.update(encoding="utf-8-sig", bom=True, method="bom")
        return result
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        result.update(encoding="utf-16", bom=True, method="bom")
        return result
    try:
        import chardet  # type: ignore
        det = chardet.detect(raw)
        enc = (det.get("encoding") or "utf-8")
        conf = float(det.get("confidence") or 0.0)
        result.update(encoding=enc, confidence=conf, method="chardet")
        return result
    except ImportError:
        pass
    # Fallback chain
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            raw.decode(enc)
            result.update(encoding=enc,
                          confidence=0.9 if enc == "utf-8" else 0.5,
                          method="fallback")
            return result
        except (UnicodeDecodeError, LookupError):
            continue
    result.update(encoding="latin-1", confidence=0.1, method="fallback")
    return result


def detect_mime(path: str | os.PathLike) -> str:
    """Best-effort MIME type: mimetypes guess first, then content sniffing."""
    p = Path(path)
    guessed, _ = mimetypes.guess_type(str(p))
    if guessed:
        return guessed
    try:
        head = p.read_bytes()[:8]
    except OSError:
        return "application/octet-stream"
    if head.startswith(b"\x7fELF"):
        return "application/x-elf"
    if head.startswith(b"MZ"):
        return "application/x-dosexec"
    if head.startswith(b"%PDF"):
        return "application/pdf"
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"GIF8"):
        return "image/gif"
    if head.startswith(b"PK\x03\x04"):
        return "application/zip"
    if _BINARY_HINT in head:
        return "application/octet-stream"
    return "text/plain"


def _resolve(path: str | os.PathLike) -> Path:
    return Path(path).expanduser().resolve()


def _format_mtime(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class FileTools:
    """Advanced, safe file operations."""

    def __init__(self, trash_dir: str | os.PathLike | None = None):
        self.trash_dir = Path(trash_dir).expanduser() if trash_dir else TRASH_DIR

    # -- reading ------------------------------------------------------------

    def advanced_read(self, path: str | os.PathLike, start_line: int = 1,
                      end_line: int = 0, encoding: str = "",
                      max_bytes: int = 200000,
                      max_lines: int = 10000) -> FileResult:
        """Read a text file with optional line range.

        Args:
            start_line: 1-based first line (default 1).
            end_line:   1-based last line inclusive (0 = to end).
            encoding:   force an encoding; otherwise auto-detect.
            max_bytes / max_lines: safety caps.
        """
        p = _resolve(path)
        if not p.exists():
            return FileResult(ok=False, output=f"File not found: {p}",
                              parsed={}, error="File not found")
        if p.is_dir():
            return FileResult(ok=False, output=f"Path is a directory: {p}",
                              parsed={}, error="Is a directory")
        try:
            size = p.stat().st_size
        except OSError as e:
            return FileResult(ok=False, output=str(e), parsed={}, error=str(e))
        det = detect_encoding(p)
        if det["is_binary"]:
            return FileResult(ok=False,
                              output=f"Refusing to decode binary file: {p} "
                                     f"(mime={detect_mime(p)})",
                              parsed={"path": str(p), "is_binary": True,
                                      "mime": detect_mime(p), "size": size},
                              error="Binary file")
        enc = encoding or det["encoding"]
        try:
            text = p.read_text(encoding=enc, errors="replace")
        except (OSError, LookupError) as e:
            return FileResult(ok=False, output=str(e), parsed={}, error=str(e))
        lines = text.splitlines()
        total_lines = len(lines)
        s = max(1, start_line) - 1
        e = total_lines if not end_line or end_line <= 0 else min(end_line, total_lines)
        if s >= total_lines:
            return FileResult(ok=False, output=f"start_line {start_line} beyond EOF ({total_lines} lines)",
                              parsed={"path": str(p), "total_lines": total_lines},
                              error="start_line beyond EOF")
        window = lines[s:e][:max_lines]
        body = "\n".join(window)
        if len(body.encode("utf-8", "ignore")) > max_bytes:
            body = body.encode("utf-8", "ignore")[:max_bytes].decode("utf-8", "ignore")
            truncated = True
        else:
            truncated = len(lines[s:e]) > max_lines
        parsed = {
            "path": str(p), "encoding": enc, "detected_encoding": det["encoding"],
            "total_lines": total_lines, "start_line": s + 1, "end_line": s + len(window),
            "size": size, "truncated": truncated, "is_binary": False,
        }
        out = f"--- {p} lines {s + 1}-{s + len(window)} of {total_lines} ({enc}) ---\n{body}"
        return FileResult(ok=True, output=out, parsed=parsed)

    def read_head(self, path: str | os.PathLike, lines: int = 50) -> FileResult:
        """Read the first N lines of a file."""
        return self.advanced_read(path, start_line=1, end_line=lines)

    def read_tail(self, path: str | os.PathLike, lines: int = 50) -> FileResult:
        """Read the last N lines of a file."""
        p = _resolve(path)
        if not p.exists():
            return FileResult(ok=False, output=f"File not found: {p}",
                              parsed={}, error="File not found")
        det = detect_encoding(p)
        if det["is_binary"]:
            return FileResult(ok=False, output=f"Binary file: {p}",
                              parsed={"is_binary": True}, error="Binary file")
        try:
            text = p.read_text(encoding=det["encoding"], errors="replace")
        except OSError as e:
            return FileResult(ok=False, output=str(e), parsed={}, error=str(e))
        all_lines = text.splitlines()
        tail = all_lines[-lines:]
        start = max(1, len(all_lines) - lines + 1)
        out = f"--- {p} last {len(tail)} of {len(all_lines)} lines ---\n" + "\n".join(tail)
        return FileResult(ok=True, output=out,
                          parsed={"path": str(p), "total_lines": len(all_lines),
                                  "shown_from": start})

    # -- writing ------------------------------------------------------------

    def write_atomic(self, path: str | os.PathLike, content: str,
                     encoding: str = "utf-8", make_backup: bool = False,
                     mode: int = 0o644) -> FileResult:
        """Write a file atomically: write to temp file in the same directory,
        fsync, then os.replace. Optionally keep a ``.bak`` of the old file."""
        p = _resolve(path)
        parent = p.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return FileResult(ok=False, output=str(e), parsed={}, error=str(e))
        existed = p.exists()
        backup_path = ""
        if existed and make_backup:
            backup_path = str(p) + ".bak"
            try:
                shutil.copy2(p, backup_path)
            except OSError as e:
                return FileResult(ok=False, output=f"Backup failed: {e}",
                                  parsed={}, error=str(e))
        fd, tmp_name = tempfile.mkstemp(dir=str(parent), prefix=".atomic-")
        try:
            with os.fdopen(fd, "w", encoding=encoding) as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_name, mode)
            os.replace(tmp_name, p)
        except OSError as e:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            return FileResult(ok=False, output=str(e), parsed={}, error=str(e))
        parsed = {"path": str(p), "bytes": len(content.encode(encoding, "replace")),
                  "existed": existed, "backup": backup_path, "atomic": True}
        return FileResult(ok=True,
                          output=f"Wrote {parsed['bytes']} bytes atomically to {p}",
                          parsed=parsed)

    def append(self, path: str | os.PathLike, content: str,
               encoding: str = "utf-8", ensure_newline: bool = True) -> FileResult:
        """Append text to a file (creates it if missing)."""
        p = _resolve(path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            needs_nl = False
            if ensure_newline and p.exists() and p.stat().st_size > 0:
                with open(p, "rb") as f:
                    f.seek(-1, os.SEEK_END)
                    needs_nl = f.read(1) != b"\n"
            with open(p, "a", encoding=encoding) as f:
                if needs_nl:
                    f.write("\n")
                f.write(content)
        except OSError as e:
            return FileResult(ok=False, output=str(e), parsed={}, error=str(e))
        return FileResult(ok=True, output=f"Appended {len(content)} chars to {p}",
                          parsed={"path": str(p), "appended_chars": len(content)})

    def prepend(self, path: str | os.PathLike, content: str,
                encoding: str = "utf-8") -> FileResult:
        """Insert text at the beginning of a file (atomic)."""
        p = _resolve(path)
        existing = ""
        if p.exists():
            r = self.advanced_read(p, max_bytes=10_000_000)
            if not r.ok:
                return r
            existing = r.output.split("\n", 1)[1] if "\n" in r.output else ""
        return self.write_atomic(p, content + existing, encoding=encoding)

    # -- copy / move / delete -----------------------------------------------

    def copy(self, src: str | os.PathLike, dst: str | os.PathLike,
             overwrite: bool = False, preserve: bool = True) -> FileResult:
        """Copy a file or directory tree."""
        s, d = _resolve(src), _resolve(dst)
        if not s.exists():
            return FileResult(ok=False, output=f"Source not found: {s}",
                              parsed={}, error="Source not found")
        if d.exists() and not overwrite:
            return FileResult(ok=False, output=f"Destination exists: {d} (overwrite=False)",
                              parsed={}, error="Destination exists")
        try:
            if s.is_dir():
                if d.exists():
                    shutil.rmtree(d)
                shutil.copytree(s, d, copy_function=shutil.copy2 if preserve else shutil.copy)
                kind = "directory"
            else:
                d.parent.mkdir(parents=True, exist_ok=True)
                if preserve:
                    shutil.copy2(s, d)
                else:
                    shutil.copy(s, d)
                kind = "file"
        except OSError as e:
            return FileResult(ok=False, output=str(e), parsed={}, error=str(e))
        return FileResult(ok=True, output=f"Copied {kind} {s} -> {d}",
                          parsed={"src": str(s), "dst": str(d), "kind": kind})

    def move(self, src: str | os.PathLike, dst: str | os.PathLike,
             overwrite: bool = False) -> FileResult:
        """Move/rename a file or directory."""
        s, d = _resolve(src), _resolve(dst)
        if not s.exists():
            return FileResult(ok=False, output=f"Source not found: {s}",
                              parsed={}, error="Source not found")
        if d.exists() and not overwrite:
            return FileResult(ok=False, output=f"Destination exists: {d}",
                              parsed={}, error="Destination exists")
        try:
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(s), str(d))
        except OSError as e:
            return FileResult(ok=False, output=str(e), parsed={}, error=str(e))
        return FileResult(ok=True, output=f"Moved {s} -> {d}",
                          parsed={"src": str(s), "dst": str(d)})

    def delete_safe(self, path: str | os.PathLike) -> FileResult:
        """Move a file/directory to the trash instead of deleting it."""
        p = _resolve(path)
        if not p.exists() and not p.is_symlink():
            return FileResult(ok=False, output=f"Not found: {p}",
                              parsed={}, error="Not found")
        dest = _trash_path(p)
        try:
            shutil.move(str(p), str(dest))
        except OSError as e:
            return FileResult(ok=False, output=str(e), parsed={}, error=str(e))
        return FileResult(ok=True, output=f"Moved to trash: {dest}",
                          parsed={"original": str(p), "trash_path": str(dest),
                                  "restorable": True})

    def trash_list(self) -> FileResult:
        """List items currently in the trash."""
        trash = _ensure_trash()
        items = []
        for entry in sorted(trash.iterdir(), key=lambda e: e.stat().st_mtime, reverse=True):
            try:
                st = entry.stat()
                items.append({"name": entry.name, "size": st.st_size,
                              "trashed_at": _format_mtime(st.st_mtime),
                              "is_dir": entry.is_dir()})
            except OSError:
                continue
        return FileResult(ok=True,
                          output="\n".join(f"{i['trashed_at']} {i['size']:>10} {i['name']}" for i in items) or "(trash empty)",
                          parsed={"items": items, "count": len(items),
                                  "trash_dir": str(trash)})

    def trash_restore(self, trash_name: str, dest: str | os.PathLike) -> FileResult:
        """Restore an item from the trash to ``dest``."""
        src = _ensure_trash() / trash_name
        if not src.exists():
            return FileResult(ok=False, output=f"Not in trash: {trash_name}",
                              parsed={}, error="Not in trash")
        d = _resolve(dest)
        if d.exists():
            return FileResult(ok=False, output=f"Destination exists: {d}",
                              parsed={}, error="Destination exists")
        try:
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(d))
        except OSError as e:
            return FileResult(ok=False, output=str(e), parsed={}, error=str(e))
        return FileResult(ok=True, output=f"Restored {trash_name} -> {d}",
                          parsed={"trash_name": trash_name, "dest": str(d)})

    def trash_empty(self) -> FileResult:
        """Permanently empty the trash. DESTRUCTIVE — caller must confirm."""
        trash = _ensure_trash()
        removed = 0
        for entry in trash.iterdir():
            try:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
                removed += 1
            except OSError:
                continue
        return FileResult(ok=True, output=f"Emptied trash ({removed} item(s) removed)",
                          parsed={"removed": removed})

    def mkdir_p(self, path: str | os.PathLike, mode: int = 0o755) -> FileResult:
        """Create a directory and all parents (like mkdir -p)."""
        p = _resolve(path)
        try:
            p.mkdir(parents=True, exist_ok=True)
            os.chmod(p, mode)
        except OSError as e:
            return FileResult(ok=False, output=str(e), parsed={}, error=str(e))
        return FileResult(ok=True, output=f"Directory ready: {p}",
                          parsed={"path": str(p)})

    # -- listing & searching -------------------------------------------------

    def list_tree(self, path: str | os.PathLike = ".", max_depth: int = 5,
                  include_hidden: bool = False,
                  extensions: list[str] | None = None,
                  min_size: int = 0, max_size: int = 0,
                  sort_by: str = "name") -> FileResult:
        """Recursive tree listing with filters.

        Args:
            max_depth:  how deep to descend (0 = the dir itself).
            extensions: e.g. [".py", ".md"] — only matching files.
            min_size/max_size: byte filters (max_size=0 means no limit).
            sort_by: name | size | mtime.
        """
        root = _resolve(path)
        if not root.exists():
            return FileResult(ok=False, output=f"Not found: {root}",
                              parsed={}, error="Not found")
        if not root.is_dir():
            return FileResult(ok=False, output=f"Not a directory: {root}",
                              parsed={}, error="Not a directory")
        exts = {e.lower() if e.startswith(".") else "." + e.lower()
                for e in (extensions or [])}
        entries: list[dict[str, Any]] = []
        base_depth = len(root.parts)

        def walk(current: Path):
            try:
                children = sorted(current.iterdir(), key=lambda e: e.name.lower())
            except OSError:
                return
            for child in children:
                if not include_hidden and child.name.startswith("."):
                    continue
                depth = len(child.parts) - base_depth
                if depth > max_depth:
                    continue
                try:
                    st = child.stat()
                except OSError:
                    continue
                is_dir = child.is_dir()
                if not is_dir:
                    if exts and child.suffix.lower() not in exts:
                        pass
                    elif st.st_size < min_size:
                        pass
                    elif max_size and st.st_size > max_size:
                        pass
                    else:
                        entries.append({
                            "path": str(child.relative_to(root)),
                            "name": child.name,
                            "size": st.st_size,
                            "mtime": _format_mtime(st.st_mtime),
                            "mtime_ts": st.st_mtime,
                            "is_dir": False,
                            "depth": depth,
                            "mime": detect_mime(child),
                        })
                else:
                    entries.append({
                        "path": str(child.relative_to(root)),
                        "name": child.name,
                        "size": st.st_size,
                        "mtime": _format_mtime(st.st_mtime),
                        "mtime_ts": st.st_mtime,
                        "is_dir": True,
                        "depth": depth,
                        "mime": "inode/directory",
                    })
                    if depth < max_depth:
                        walk(child)

        walk(root)
        key = {"name": lambda e: e["name"].lower(),
               "size": lambda e: e["size"],
               "mtime": lambda e: e["mtime_ts"]}.get(sort_by, lambda e: e["name"].lower())
        entries.sort(key=key, reverse=(sort_by in ("size", "mtime")))
        dirs = sum(1 for e in entries if e["is_dir"])
        files = len(entries) - dirs
        total_size = sum(e["size"] for e in entries if not e["is_dir"])
        lines = [f"{root} — {files} file(s), {dirs} dir(s), {total_size} bytes"]
        for e in entries[:500]:
            indent = "  " * min(e["depth"], 8)
            marker = "📁" if e["is_dir"] else "📄"
            lines.append(f"{indent}{marker} {e['name']} ({e['size']} B, {e['mtime']})")
        if len(entries) > 500:
            lines.append(f"... and {len(entries) - 500} more (capped at 500 shown)")
        return FileResult(ok=True, output="\n".join(lines),
                          parsed={"root": str(root), "entries": entries,
                                  "file_count": files, "dir_count": dirs,
                                  "total_size": total_size})

    def find_by_pattern(self, root: str | os.PathLike = ".",
                        name_glob: str = "*",
                        content_regex: str = "",
                        max_results: int = 200,
                        include_hidden: bool = False,
                        extensions: list[str] | None = None) -> FileResult:
        """Find files by name glob, optionally filtering by content regex."""
        base = _resolve(root)
        if not base.exists():
            return FileResult(ok=False, output=f"Not found: {base}",
                              parsed={}, error="Not found")
        cregex = re.compile(content_regex) if content_regex else None
        exts = {e.lower() if e.startswith(".") else "." + e.lower()
                for e in (extensions or [])}
        matches: list[dict[str, Any]] = []
        truncated = False
        for dirpath, dirnames, filenames in os.walk(base):
            if not include_hidden:
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                filenames = [f for f in filenames if not f.startswith(".")]
            for fname in filenames:
                if len(matches) >= max_results:
                    truncated = True
                    break
                if not fnmatch.fnmatch(fname, name_glob):
                    continue
                full = Path(dirpath) / fname
                if exts and full.suffix.lower() not in exts:
                    continue
                rel = str(full.relative_to(base))
                hit_lines: list[int] = []
                if cregex:
                    det = detect_encoding(full)
                    if det["is_binary"]:
                        continue
                    try:
                        text = full.read_text(encoding=det["encoding"], errors="ignore")
                    except OSError:
                        continue
                    for i, line in enumerate(text.splitlines(), 1):
                        if cregex.search(line):
                            hit_lines.append(i)
                            if len(hit_lines) >= 10:
                                break
                    if not hit_lines:
                        continue
                try:
                    size = full.stat().st_size
                except OSError:
                    size = 0
                matches.append({"path": rel, "size": size,
                                "content_hits": hit_lines})
            if truncated:
                break
        lines = [f"{len(matches)} match(es) for glob={name_glob!r}"
                 + (f" content=/{content_regex}/" if content_regex else "")]
        for m in matches:
            extra = f" (content hits: {m['content_hits']})" if m["content_hits"] else ""
            lines.append(f"  {m['path']} ({m['size']} B){extra}")
        if truncated:
            lines.append(f"... capped at {max_results} results")
        return FileResult(ok=True, output="\n".join(lines),
                          parsed={"matches": matches, "count": len(matches),
                                  "truncated": truncated, "glob": name_glob,
                                  "content_regex": content_regex})

    def grep(self, pattern: str, root: str | os.PathLike = ".",
             extensions: list[str] | None = None,
             max_results: int = 200, context: int = 0,
             case_insensitive: bool = False) -> FileResult:
        """Search file contents for a regex pattern with optional context."""
        flags = re.IGNORECASE if case_insensitive else 0
        try:
            cregex = re.compile(pattern, flags)
        except re.error as e:
            return FileResult(ok=False, output=f"Invalid regex: {e}",
                              parsed={}, error=str(e))
        base = _resolve(root)
        exts = {e.lower() if e.startswith(".") else "." + e.lower()
                for e in (extensions or [])}
        hits: list[dict[str, Any]] = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fname in filenames:
                if fname.startswith("."):
                    continue
                full = Path(dirpath) / fname
                if exts and full.suffix.lower() not in exts:
                    continue
                if len(hits) >= max_results:
                    break
                det = detect_encoding(full)
                if det["is_binary"]:
                    continue
                try:
                    text = full.read_text(encoding=det["encoding"], errors="ignore")
                except OSError:
                    continue
                file_lines = text.splitlines()
                for i, line in enumerate(file_lines):
                    if cregex.search(line):
                        ctx_before = file_lines[max(0, i - context):i]
                        ctx_after = file_lines[i + 1:i + 1 + context]
                        hits.append({
                            "file": str(full.relative_to(base)),
                            "line": i + 1,
                            "text": line[:500],
                            "context_before": ctx_before,
                            "context_after": ctx_after,
                        })
                        if len(hits) >= max_results:
                            break
            if len(hits) >= max_results:
                break
        lines = [f"{len(hits)} hit(s) for /{pattern}/"]
        for h in hits:
            lines.append(f"  {h['file']}:{h['line']}: {h['text']}")
        return FileResult(ok=True, output="\n".join(lines),
                          parsed={"hits": hits, "count": len(hits),
                                  "pattern": pattern})

    # -- metadata ------------------------------------------------------------

    def file_info(self, path: str | os.PathLike) -> FileResult:
        """Full stat details: size, times, permissions, owner, mime, type."""
        p = _resolve(path)
        if not p.exists() and not p.is_symlink():
            return FileResult(ok=False, output=f"Not found: {p}",
                              parsed={}, error="Not found")
        try:
            st = p.lstat()
        except OSError as e:
            return FileResult(ok=False, output=str(e), parsed={}, error=str(e))
        kind = "symlink" if p.is_symlink() else ("directory" if p.is_dir() else "file")
        parsed = {
            "path": str(p),
            "name": p.name,
            "kind": kind,
            "size": st.st_size,
            "size_human": self._human_size(st.st_size),
            "mode": oct(stat.S_IMODE(st.st_mode)),
            "permissions": stat.filemode(st.st_mode),
            "uid": st.st_uid,
            "gid": st.st_gid,
            "atime": _format_mtime(st.st_atime),
            "mtime": _format_mtime(st.st_mtime),
            "ctime": _format_mtime(st.st_ctime),
            "mtime_ts": st.st_mtime,
            "mime": detect_mime(p),
            "inode": st.st_ino,
            "nlink": st.st_nlink,
        }
        if p.is_symlink():
            try:
                parsed["symlink_target"] = str(p.readlink())
            except OSError:
                parsed["symlink_target"] = "(unreadable)"
        if kind == "file":
            det = detect_encoding(p)
            parsed["is_binary"] = det["is_binary"]
            parsed["encoding"] = det["encoding"]
        out = (f"{parsed['permissions']} {parsed['size_human']:>9} {parsed['mtime']} "
               f"{parsed['kind']:>9} {parsed['mime']}\n{p}")
        return FileResult(ok=True, output=out, parsed=parsed)

    @staticmethod
    def _human_size(n: int) -> str:
        units = ["B", "KB", "MB", "GB", "TB", "PB"]
        f = float(n)
        for u in units:
            if f < 1024 or u == units[-1]:
                return f"{f:.1f}{u}" if u != "B" else f"{int(f)}B"
            f /= 1024
        return f"{f:.1f}EB"

    def checksum(self, path: str | os.PathLike,
                 algorithm: str = "sha256") -> FileResult:
        """Hash a file (sha256, sha1, md5, blake2b). Streams in chunks."""
        p = _resolve(path)
        if not p.is_file():
            return FileResult(ok=False, output=f"Not a file: {p}",
                              parsed={}, error="Not a file")
        algo = algorithm.lower().replace("-", "")
        try:
            h = hashlib.new(algo)
        except ValueError:
            return FileResult(ok=False,
                              output=f"Unsupported algorithm: {algorithm}",
                              parsed={}, error="Unsupported algorithm")
        try:
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
        except OSError as e:
            return FileResult(ok=False, output=str(e), parsed={}, error=str(e))
        digest = h.hexdigest()
        return FileResult(ok=True, output=f"{algo}({p.name}) = {digest}",
                          parsed={"path": str(p), "algorithm": algo,
                                  "digest": digest})

    def verify_checksum(self, path: str | os.PathLike, expected: str,
                        algorithm: str = "sha256") -> FileResult:
        """Verify a file against an expected hex digest."""
        r = self.checksum(path, algorithm)
        if not r.ok:
            return r
        match = r.parsed["digest"].lower() == expected.strip().lower()
        return FileResult(ok=match,
                          output=("MATCH" if match else "MISMATCH")
                                 + f" {algorithm} for {path}",
                          parsed={"match": match,
                                  "expected": expected.strip().lower(),
                                  "actual": r.parsed["digest"]})

    # -- watching ------------------------------------------------------------

    def watch_file(self, path: str | os.PathLike, timeout_sec: float = 10.0,
                   poll_interval: float = 0.5) -> FileResult:
        """Poll a file for changes (mtime/size) up to ``timeout_sec``.

        Returns immediately when a change is observed; otherwise reports
        timeout. This is a bounded poll, not a daemon.
        """
        p = _resolve(path)
        if not p.exists():
            # Watch for creation instead.
            deadline = time.time() + timeout_sec
            while time.time() < deadline:
                if p.exists():
                    return FileResult(ok=True, output=f"File created: {p}",
                                      parsed={"path": str(p), "event": "created"})
                time.sleep(poll_interval)
            return FileResult(ok=True, output=f"No change within {timeout_sec}s (still absent)",
                              parsed={"path": str(p), "event": "timeout",
                                      "changed": False})
        try:
            before = (p.stat().st_mtime, p.stat().st_size)
        except OSError as e:
            return FileResult(ok=False, output=str(e), parsed={}, error=str(e))
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            time.sleep(poll_interval)
            if not p.exists():
                return FileResult(ok=True, output=f"File deleted: {p}",
                                  parsed={"path": str(p), "event": "deleted",
                                          "changed": True})
            try:
                after = (p.stat().st_mtime, p.stat().st_size)
            except OSError:
                continue
            if after != before:
                what = []
                if after[0] != before[0]:
                    what.append("mtime")
                if after[1] != before[1]:
                    what.append("size")
                return FileResult(ok=True,
                                  output=f"File changed ({', '.join(what)}): {p}",
                                  parsed={"path": str(p), "event": "modified",
                                          "changed": True,
                                          "before": {"mtime": before[0], "size": before[1]},
                                          "after": {"mtime": after[0], "size": after[1]}})
        return FileResult(ok=True, output=f"No change within {timeout_sec}s",
                          parsed={"path": str(p), "event": "timeout",
                                  "changed": False})

    # -- bulk helpers --------------------------------------------------------

    def touch(self, path: str | os.PathLike) -> FileResult:
        """Create an empty file or update its mtime (like touch)."""
        p = _resolve(path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch(exist_ok=True)
        except OSError as e:
            return FileResult(ok=False, output=str(e), parsed={}, error=str(e))
        return FileResult(ok=True, output=f"Touched {p}",
                          parsed={"path": str(p)})

    def chmod(self, path: str | os.PathLike, mode: int | str) -> FileResult:
        """Change file permissions (int octal or string like '755')."""
        p = _resolve(path)
        if isinstance(mode, str):
            try:
                mode_int = int(mode, 8)
            except ValueError:
                return FileResult(ok=False, output=f"Bad mode string: {mode}",
                                  parsed={}, error="Bad mode")
        else:
            mode_int = mode
        try:
            os.chmod(p, mode_int)
        except OSError as e:
            return FileResult(ok=False, output=str(e), parsed={}, error=str(e))
        return FileResult(ok=True, output=f"chmod {oct(mode_int)} {p}",
                          parsed={"path": str(p), "mode": oct(mode_int)})

    def disk_usage_of(self, path: str | os.PathLike = ".",
                      max_depth: int = 2) -> FileResult:
        """Directory sizes (like du), one level summary per subdir."""
        root = _resolve(path)
        if not root.exists():
            return FileResult(ok=False, output=f"Not found: {root}",
                              parsed={}, error="Not found")

        def dir_size(d: Path, depth: int) -> tuple[int, list[dict]]:
            total = 0
            children: list[dict] = []
            try:
                entries = list(d.iterdir())
            except OSError:
                return 0, []
            for e in entries:
                try:
                    if e.is_symlink():
                        continue
                    if e.is_dir() and depth < max_depth:
                        s, _ = dir_size(e, depth + 1)
                        total += s
                        children.append({"path": str(e.relative_to(root)),
                                         "size": s, "is_dir": True})
                    elif e.is_file():
                        sz = e.stat().st_size
                        total += sz
                    elif e.is_dir():
                        s = sum(f.stat().st_size for f in e.rglob("*")
                                if f.is_file() and not f.is_symlink())
                        total += s
                        children.append({"path": str(e.relative_to(root)),
                                         "size": s, "is_dir": True})
                except OSError:
                    continue
            return total, children

        total, children = dir_size(root, 0)
        children.sort(key=lambda c: c["size"], reverse=True)
        lines = [f"{root}: {self._human_size(total)} total"]
        for c in children[:30]:
            lines.append(f"  {self._human_size(c['size']):>9}  {c['path']}/")
        return FileResult(ok=True, output="\n".join(lines),
                          parsed={"root": str(root), "total_bytes": total,
                                  "children": children})

    def temp_file(self, suffix: str = "", prefix: str = "agent-",
                  content: str = "") -> FileResult:
        """Create a temp file (optionally pre-filled). Caller owns cleanup."""
        try:
            fd, name = tempfile.mkstemp(suffix=suffix, prefix=prefix)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as e:
            return FileResult(ok=False, output=str(e), parsed={}, error=str(e))
        return FileResult(ok=True, output=f"Temp file: {name}",
                          parsed={"path": name, "bytes": len(content)})

    def temp_dir(self, prefix: str = "agent-") -> FileResult:
        """Create a temp directory. Caller owns cleanup."""
        try:
            name = tempfile.mkdtemp(prefix=prefix)
        except OSError as e:
            return FileResult(ok=False, output=str(e), parsed={}, error=str(e))
        return FileResult(ok=True, output=f"Temp dir: {name}",
                          parsed={"path": name})


__all__ = ["FileTools", "FileResult", "TRASH_DIR", "detect_encoding",
           "detect_mime"]
