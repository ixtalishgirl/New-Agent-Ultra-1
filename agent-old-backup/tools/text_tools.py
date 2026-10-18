"""Text processing tools — search, replace, statistics, encoding.

Tools:
- text_search: Search text in files (grep-like)
- text_replace: Find and replace in files
- text_stats: Word/line/character statistics
- text_encode: Convert text encoding
- text_diff: Show differences between texts
"""
from __future__ import annotations

import difflib
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional


def text_search(pattern: str, path: str,
                regex: bool = False,
                case_sensitive: bool = True,
                max_results: int = 100) -> Dict[str, Any]:
    """Search for pattern in file(s)."""
    p = Path(path)
    results: List[Dict[str, Any]] = []

    # Collect files
    files = []
    if p.is_dir():
        files = [f for f in p.rglob("*")
                 if f.is_file() and _is_text_file(f)]
    elif p.is_file():
        files = [p]
    else:
        return {"ok": False, "error": f"Not found: {path}"}

    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        compiled = re.compile(pattern if regex else re.escape(pattern),
                              flags)
    except re.error as e:
        return {"ok": False, "error": f"Invalid regex: {e}"}

    for f in files:
        try:
            with open(f, "r", encoding="utf-8",
                      errors="ignore") as fh:
                for lineno, line in enumerate(fh, 1):
                    m = compiled.search(line)
                    if m:
                        results.append({
                            "file": str(f),
                            "line": lineno,
                            "text": line.strip()[:200],
                            "match": m.group(0)[:100],
                        })
                        if len(results) >= max_results:
                            break
        except Exception:
            continue
        if len(results) >= max_results:
            break

    return {"ok": True, "pattern": pattern,
            "matches": len(results),
            "results": results}


def _is_text_file(path: Path) -> bool:
    """Heuristic: is this a text file?"""
    # Skip binary extensions
    binary_exts = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip",
                   ".tar", ".gz", ".mp3", ".mp4", ".exe", ".so",
                   ".pyc", ".o", ".a", ".bin"}
    if path.suffix.lower() in binary_exts:
        return False
    # Check first bytes
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
            if b"\x00" in chunk:
                return False
            return True
    except Exception:
        return False


def text_replace(path: str, old: str, new: str,
                 regex: bool = False,
                 count: int = 0) -> Dict[str, Any]:
    """Find and replace in a file. count=0 means replace all."""
    p = Path(path)
    if not p.is_file():
        return {"ok": False, "error": f"Not a file: {path}"}

    try:
        content = p.read_text(encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": f"Cannot read: {e}"}

    if regex:
        try:
            new_content, n = re.subn(old, new, content,
                                     count=count if count > 0 else 0)
        except re.error as e:
            return {"ok": False, "error": f"Invalid regex: {e}"}
    else:
        if count > 0:
            new_content = content.replace(old, new, count)
            n = min(count, content.count(old))
        else:
            n = content.count(old)
            new_content = content.replace(old, new)

    if n == 0:
        return {"ok": True, "replacements": 0,
                "message": "No matches found"}

    try:
        p.write_text(new_content, encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": f"Cannot write: {e}"}

    return {"ok": True, "replacements": n, "file": str(p)}


def text_stats(path: str) -> Dict[str, Any]:
    """Get text statistics: lines, words, characters."""
    p = Path(path)
    if not p.is_file():
        return {"ok": False, "error": f"Not a file: {path}"}

    try:
        content = p.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return {"ok": False, "error": f"Cannot read: {e}"}

    lines = content.splitlines()
    words = content.split()
    chars = len(content)
    chars_no_space = len(content.replace(" ", "").replace("\n", "")
                            .replace("\t", ""))

    # Word frequency (top 10)
    word_counts = Counter(w.lower().strip(".,!?;:\"'()[]{}")
                          for w in words)
    # Remove empty strings, get top 10
    top_words = dict(
        (w, c) for w, c in word_counts.most_common(10) if w)

    # Line length stats
    line_lengths = [len(l) for l in lines]
    avg_line = (sum(line_lengths) / len(line_lengths)
                if line_lengths else 0)

    return {
        "ok": True,
        "file": str(p),
        "lines": len(lines),
        "words": len(words),
        "characters": chars,
        "characters_no_spaces": chars_no_space,
        "avg_line_length": round(avg_line, 1),
        "max_line_length": max(line_lengths) if line_lengths else 0,
        "empty_lines": sum(1 for l in lines if not l.strip()),
        "top_words": top_words,
    }


def text_encode(path: str, from_enc: str, to_enc: str,
                output: Optional[str] = None) -> Dict[str, Any]:
    """Convert file encoding."""
    p = Path(path)
    if not p.is_file():
        return {"ok": False, "error": f"Not a file: {path}"}

    try:
        content = p.read_text(encoding=from_enc)
    except LookupError:
        return {"ok": False, "error": f"Unknown encoding: {from_enc}"}
    except Exception as e:
        return {"ok": False, "error": f"Cannot read: {e}"}

    out = Path(output) if output else p
    try:
        out.write_text(content, encoding=to_enc)
    except LookupError:
        return {"ok": False, "error": f"Unknown encoding: {to_enc}"}
    except Exception as e:
        return {"ok": False, "error": f"Cannot write: {e}"}

    return {"ok": True, "file": str(out),
            "from": from_enc, "to": to_enc}


def text_diff(text1: str, text2: str,
              context: int = 3) -> Dict[str, Any]:
    """Show unified diff between two texts."""
    lines1 = text1.splitlines(keepends=True)
    lines2 = text2.splitlines(keepends=True)
    diff = list(difflib.unified_diff(
        lines1, lines2, n=context,
        fromfile="text1", tofile="text2"))
    diff_text = "".join(diff)
    return {
        "ok": True,
        "diff": diff_text,
        "diff_lines": len(diff),
        "has_changes": bool(diff),
    }


def text_diff_files(path1: str, path2: str,
                    context: int = 3) -> Dict[str, Any]:
    """Show diff between two files."""
    p1, p2 = Path(path1), Path(path2)
    if not p1.is_file():
        return {"ok": False, "error": f"Not a file: {path1}"}
    if not p2.is_file():
        return {"ok": False, "error": f"Not a file: {path2}"}
    try:
        t1 = p1.read_text(encoding="utf-8", errors="ignore")
        t2 = p2.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return {"ok": False, "error": str(e)}
    result = text_diff(t1, t2, context)
    result["file1"] = str(p1)
    result["file2"] = str(p2)
    return result


def text_sort(path: str, reverse: bool = False,
              unique: bool = False,
              output: Optional[str] = None) -> Dict[str, Any]:
    """Sort lines in a file."""
    p = Path(path)
    if not p.is_file():
        return {"ok": False, "error": f"Not a file: {path}"}
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        return {"ok": False, "error": str(e)}

    lines.sort(reverse=reverse)
    if unique:
        seen = set()
        deduped = []
        for l in lines:
            if l not in seen:
                seen.add(l)
                deduped.append(l)
        lines = deduped

    out = Path(output) if output else p
    try:
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": str(e)}

    return {"ok": True, "file": str(out), "lines": len(lines)}
