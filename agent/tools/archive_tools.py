"""Archive tools — zip, tar, gzip handling.

Tools:
- archive_create: Create zip/tar archives
- archive_extract: Extract archives
- archive_list: List archive contents
- archive_info: Get archive metadata
"""
from __future__ import annotations

import gzip
import os
import shutil
import tarfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional


def archive_create(paths: List[str], output: str,
                   format: str = "zip") -> Dict[str, Any]:
    """Create an archive from files/directories."""
    out = Path(output)
    fmt = format.lower()

    try:
        if fmt == "zip":
            if not output.endswith(".zip"):
                out = Path(str(out) + ".zip")
            with zipfile.ZipFile(out, "w",
                                 zipfile.ZIP_DEFLATED) as zf:
                for p in paths:
                    pp = Path(p)
                    if not pp.exists():
                        return {"ok": False,
                                "error": f"Not found: {p}"}
                    if pp.is_dir():
                        for f in pp.rglob("*"):
                            if f.is_file():
                                zf.write(f, f.relative_to(pp.parent))
                    else:
                        zf.write(pp, pp.name)
        elif fmt in ("tar", "tar.gz", "tgz"):
            mode = "w:gz" if fmt in ("tar.gz", "tgz") else "w"
            if fmt == "tar" and not output.endswith(".tar"):
                out = Path(str(out) + ".tar")
            elif fmt in ("tar.gz", "tgz") and not output.endswith(
                    (".tar.gz", ".tgz")):
                out = Path(str(out) + ".tar.gz")
            with tarfile.open(out, mode) as tf:
                for p in paths:
                    pp = Path(p)
                    if not pp.exists():
                        return {"ok": False,
                                "error": f"Not found: {p}"}
                    tf.add(pp, arcname=pp.name)
        else:
            return {"ok": False,
                    "error": f"Unsupported format: {format}"}

        return {"ok": True, "output": str(out),
                "size_bytes": out.stat().st_size}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def archive_extract(archive: str, dest: Optional[str] = None,
                    ) -> Dict[str, Any]:
    """Extract an archive."""
    p = Path(archive)
    if not p.exists():
        return {"ok": False, "error": f"Not found: {archive}"}

    dest_path = Path(dest) if dest else p.parent / p.stem
    dest_path.mkdir(parents=True, exist_ok=True)

    try:
        suffix = p.suffix.lower()
        name_lower = p.name.lower()

        if suffix == ".zip":
            with zipfile.ZipFile(p, "r") as zf:
                # Security: check for path traversal
                for member in zf.namelist():
                    member_path = (dest_path / member).resolve()
                    if not str(member_path).startswith(
                            str(dest_path.resolve())):
                        return {"ok": False,
                                "error": f"Path traversal blocked: "
                                         f"{member}"}
                zf.extractall(dest_path)
                count = len(zf.namelist())
        elif name_lower.endswith((".tar.gz", ".tgz")) or \
                suffix in (".tar", ".gz"):
            with tarfile.open(p, "r:*") as tf:
                # Security: check for path traversal
                for member in tf.getmembers():
                    member_path = (dest_path / member.name).resolve()
                    if not str(member_path).startswith(
                            str(dest_path.resolve())):
                        return {"ok": False,
                                "error": f"Path traversal blocked"}
                tf.extractall(dest_path)
                count = len(tf.getmembers())
        else:
            return {"ok": False,
                    "error": f"Unsupported archive: {suffix}"}

        return {"ok": True, "dest": str(dest_path),
                "files_extracted": count}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def archive_list(archive: str) -> Dict[str, Any]:
    """List contents of an archive."""
    p = Path(archive)
    if not p.exists():
        return {"ok": False, "error": f"Not found: {archive}"}

    try:
        suffix = p.suffix.lower()
        name_lower = p.name.lower()
        files: List[Dict[str, Any]] = []

        if suffix == ".zip":
            with zipfile.ZipFile(p, "r") as zf:
                for info in zf.infolist():
                    files.append({
                        "name": info.filename,
                        "size": info.file_size,
                        "compressed": info.compress_size,
                        "is_dir": info.is_dir(),
                    })
        elif name_lower.endswith((".tar.gz", ".tgz")) or \
                suffix in (".tar", ".gz"):
            with tarfile.open(p, "r:*") as tf:
                for m in tf.getmembers():
                    files.append({
                        "name": m.name,
                        "size": m.size,
                        "is_dir": m.isdir(),
                        "mtime": m.mtime,
                    })
        else:
            return {"ok": False,
                    "error": f"Unsupported archive: {suffix}"}

        total_size = sum(f.get("size", 0) for f in files)
        return {"ok": True, "archive": str(p),
                "file_count": len(files),
                "total_size": total_size,
                "files": files[:100]}  # Limit to 100
    except Exception as e:
        return {"ok": False, "error": str(e)}


def archive_info(archive: str) -> Dict[str, Any]:
    """Get archive metadata."""
    p = Path(archive)
    if not p.exists():
        return {"ok": False, "error": f"Not found: {archive}"}

    result: Dict[str, Any] = {
        "ok": True,
        "path": str(p),
        "size_bytes": p.stat().st_size,
        "format": "unknown",
    }

    name_lower = p.name.lower()
    if name_lower.endswith(".zip"):
        result["format"] = "zip"
    elif name_lower.endswith((".tar.gz", ".tgz")):
        result["format"] = "tar.gz"
    elif name_lower.endswith(".tar"):
        result["format"] = "tar"
    elif name_lower.endswith(".gz"):
        result["format"] = "gzip"

    # Get file count
    lst = archive_list(archive)
    if lst["ok"]:
        result["file_count"] = lst["file_count"]
        result["total_uncompressed"] = lst["total_size"]

    return result


def gzip_compress(path: str,
                  output: Optional[str] = None) -> Dict[str, Any]:
    """Compress a file with gzip."""
    p = Path(path)
    if not p.exists():
        return {"ok": False, "error": f"Not found: {path}"}
    if not p.is_file():
        return {"ok": False, "error": "Not a file"}

    out = Path(output) if output else Path(str(p) + ".gz")
    try:
        with open(p, "rb") as f_in:
            with gzip.open(out, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        return {"ok": True, "output": str(out),
                "original_size": p.stat().st_size,
                "compressed_size": out.stat().st_size}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def gzip_decompress(path: str,
                    output: Optional[str] = None) -> Dict[str, Any]:
    """Decompress a gzip file."""
    p = Path(path)
    if not p.exists():
        return {"ok": False, "error": f"Not found: {path}"}

    if output:
        out = Path(output)
    elif p.suffix == ".gz":
        out = p.with_suffix("")
    else:
        out = Path(str(p) + ".decompressed")

    try:
        with gzip.open(p, "rb") as f_in:
            with open(out, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        return {"ok": True, "output": str(out),
                "size_bytes": out.stat().st_size}
    except Exception as e:
        return {"ok": False, "error": str(e)}
