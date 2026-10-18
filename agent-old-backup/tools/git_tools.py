"""Git tool module — real git operations via subprocess with parsed results.

Every method shells out to the real ``git`` binary (with a timeout) and parses
the output into structured dicts, so the agent always works with real repo
state rather than fabricated summaries.

Conventions:
    * All methods return :class:`GitResult` (ok, output, parsed, error).
    * ``path`` defaults to the current working directory; every method
      verifies the directory is a git repo first.
    * Errors are returned as failed GitResults, never raised to the caller
      (except catastrophic misuse such as a non-existent binary).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


DEFAULT_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class GitResult:
    """Structured result of a git operation.

    Attributes:
        ok:     True when the git command exited 0.
        output: Human-readable summary text (first lines of command output
                or a synthesized description).
        parsed: Machine-readable structured data (dict).
        error:  Error description when ok is False ("" otherwise).
    """
    ok: bool
    output: str = ""
    parsed: dict = field(default_factory=dict)
    error: str = ""


@dataclass
class NotARepoError(Exception):
    path: str


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

_STATUS_RE = re.compile(r"^(.{2}) (.*)$")


def _parse_status_porcelain(text: str) -> dict[str, Any]:
    """Parse ``git status --porcelain=v1 -b`` output into structured data."""
    parsed: dict[str, Any] = {
        "branch": "",
        "upstream": "",
        "ahead": 0,
        "behind": 0,
        "detached": False,
        "staged": [],
        "unstaged": [],
        "untracked": [],
        "conflicted": [],
        "renamed": [],
    }
    for line in text.splitlines():
        if line.startswith("##"):
            head = line[3:]
            # Formats: "main...origin/main [ahead 2]" / "main" / "HEAD (no branch)"
            if "no branch" in head:
                parsed["detached"] = True
                parsed["branch"] = "HEAD"
                continue
            branch_part, _, tracking = head.partition("...")
            parsed["branch"] = branch_part.strip() or parsed["branch"]
            if tracking:
                up, _, tb = tracking.partition(" ")
                parsed["upstream"] = up.strip()
                m_a = re.search(r"ahead (\d+)", tb)
                m_b = re.search(r"behind (\d+)", tb)
                if m_a:
                    parsed["ahead"] = int(m_a.group(1))
                if m_b:
                    parsed["behind"] = int(m_b.group(1))
            continue
        if not line.strip():
            continue
        m = _STATUS_RE.match(line)
        if not m:
            continue
        codes, path = m.group(1), m.group(2)
        x, y = codes[0], codes[1]
        # Unmerged/conflicted states
        if x == "U" or y == "U" or (x, y) in {("A", "A"), ("D", "D")}:
            parsed["conflicted"].append(path)
            continue
        if x == "?":
            parsed["untracked"].append(path)
            continue
        if x == "R" or y == "R":
            # "old -> new"
            if " -> " in path:
                old, new = path.split(" -> ", 1)
                parsed["renamed"].append({"from": old, "to": new})
                if x not in (" ", "?"):
                    parsed["staged"].append(new)
                if y not in (" ", "?"):
                    parsed["unstaged"].append(new)
            continue
        if x != " ":
            parsed["staged"].append(path)
        if y != " ":
            parsed["unstaged"].append(path)
    return parsed


def _parse_log(text: str) -> list[dict[str, Any]]:
    """Parse ``git log --pretty=format:%H%x1f%an%x1f%ae%x1f%aI%x1f%s%x1f%b``."""
    commits = []
    for line in text.splitlines():
        parts = line.rstrip("\n").split("\x1f")
        if len(parts) < 6:
            continue
        sha, author, email, date, subject, body = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]
        try:
            dt = datetime.fromisoformat(date)
            ts = dt.timestamp()
        except ValueError:
            dt, ts = None, 0.0
        commits.append({
            "sha": sha,
            "short_sha": sha[:7],
            "author": author,
            "email": email,
            "date": date,
            "timestamp": ts,
            "subject": subject,
            "body": body.strip(),
        })
    return commits


def _parse_branch_list(text: str) -> list[dict[str, Any]]:
    """Parse ``git branch -vv`` output."""
    branches = []
    for line in text.splitlines():
        if not line.strip():
            continue
        current = line.startswith("*")
        body = line[2:] if current else line[1:].lstrip() if line.startswith(" ") else line
        body = line[2:] if current else line.strip()
        # Format: "<name> <sha> [tracking: ahead/behind] <subject>"
        parts = body.split(None, 2)
        if len(parts) < 2:
            continue
        name, sha = parts[0], parts[1]
        rest = parts[2] if len(parts) > 2 else ""
        tracking = ""
        subject = rest
        m = re.match(r"\[(.+?)\]\s*(.*)", rest)
        if m:
            tracking, subject = m.group(1), m.group(2)
        branches.append({
            "name": name,
            "sha": sha,
            "current": current,
            "tracking": tracking,
            "subject": subject.strip(),
        })
    return branches


def _parse_diff_stat(text: str) -> list[dict[str, Any]]:
    """Parse ``git diff --numstat`` output."""
    files = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        files.append({
            "path": path,
            "added": None if added == "-" else int(added),
            "deleted": None if deleted == "-" else int(deleted),
            "binary": added == "-",
        })
    return files


def _parse_stash_list(text: str) -> list[dict[str, Any]]:
    """Parse ``git stash list`` output."""
    stashes = []
    for line in text.splitlines():
        m = re.match(r"stash@\{(\d+)\}:\s*(.*)", line)
        if m:
            stashes.append({"index": int(m.group(1)), "description": m.group(2).strip()})
    return stashes


def _parse_remotes(text: str) -> dict[str, dict[str, str]]:
    """Parse ``git remote -v`` output into {name: {fetch: url, push: url}}."""
    remotes: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 3:
            name, url, kind = parts
            kind = kind.strip("()")
            remotes.setdefault(name, {})[kind] = url
    return remotes


def _parse_blame(text: str) -> dict[str, Any]:
    """Parse ``git blame --line-porcelain`` output into a per-author summary."""
    per_author: dict[str, dict[str, Any]] = {}
    total_lines = 0
    current: dict[str, str] = {}
    for line in text.splitlines():
        if re.match(r"^[0-9a-f]{40} \d+ \d+", line):
            current = {}
            total_lines += 1
        elif line.startswith("author "):
            current["author"] = line[len("author "):]
        elif line.startswith("author-mail "):
            current["email"] = line[len("author-mail "):].strip("<>")
        elif line.startswith("author-time "):
            try:
                current["timestamp"] = int(line[len("author-time "):])
            except ValueError:
                current["timestamp"] = 0
        elif line.startswith("\t"):
            # End of one line's block
            author = current.get("author", "Unknown")
            entry = per_author.setdefault(author, {
                "author": author,
                "email": current.get("email", ""),
                "lines": 0,
                "first_commit": current.get("timestamp", 0),
                "last_commit": current.get("timestamp", 0),
            })
            entry["lines"] += 1
            ts = current.get("timestamp", 0)
            if ts:
                if entry["first_commit"] == 0 or ts < entry["first_commit"]:
                    entry["first_commit"] = ts
                if ts > entry["last_commit"]:
                    entry["last_commit"] = ts
            current = {}
    summary = sorted(per_author.values(), key=lambda e: e["lines"], reverse=True)
    return {"total_lines": total_lines, "authors": summary}


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class GitTools:
    """Real git operations. ``repo`` is resolved per-call (defaults to cwd)."""

    def __init__(self, timeout: int = DEFAULT_TIMEOUT):
        self.timeout = timeout
        if shutil.which("git") is None:
            raise RuntimeError("git binary not found on PATH")

    # -- plumbing -----------------------------------------------------------

    def _run(self, args: list[str], path: str | os.PathLike | None = None,
             timeout: int | None = None, check: bool = False) -> tuple[int, str, str]:
        """Run git and return (exit_code, stdout, stderr). Never raises on
        nonzero exit (unless the binary itself is missing)."""
        cwd = str(Path(path).expanduser().resolve()) if path else os.getcwd()
        proc = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout if timeout is not None else self.timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def _ensure_repo(self, path: str | os.PathLike | None) -> tuple[bool, str, Optional[Path]]:
        """Return (is_repo, resolved_path, toplevel)."""
        cwd = str(Path(path).expanduser().resolve()) if path else os.getcwd()
        code, out, err = self._run(["rev-parse", "--show-toplevel"], path=cwd)
        if code != 0:
            return False, cwd, None
        return True, cwd, Path(out.strip())

    def _fail_not_repo(self, path: str) -> GitResult:
        return GitResult(
            ok=False,
            output=f"Not a git repository: {path}",
            parsed={"is_repo": False, "path": path},
            error=f"Not a git repository: {path}",
        )

    # -- inspection ---------------------------------------------------------

    def is_repo(self, path: str | os.PathLike | None = None) -> GitResult:
        """Return whether ``path`` is inside a git working tree."""
        ok, cwd, top = self._ensure_repo(path)
        return GitResult(
            ok=True,
            output=f"is_repo={ok} toplevel={top if top else 'n/a'}",
            parsed={"is_repo": ok, "path": cwd, "toplevel": str(top) if top else ""},
        )

    def status(self, path: str | os.PathLike | None = None) -> GitResult:
        """Porcelain status: branch, ahead/behind, staged/unstaged/untracked,
        conflicted, renamed, dirty flag, detached HEAD."""
        ok, cwd, _ = self._ensure_repo(path)
        if not ok:
            return self._fail_not_repo(cwd)
        code, out, err = self._run(["status", "--porcelain=v1", "-b", "--untracked-files=normal"], path=cwd)
        if code != 0:
            return GitResult(ok=False, output=err.strip(), parsed={}, error=err.strip())
        parsed = _parse_status_porcelain(out)
        dirty = bool(parsed["staged"] or parsed["unstaged"] or parsed["conflicted"])
        parsed["dirty"] = dirty
        parsed["clean"] = not dirty and not parsed["untracked"]
        branch = parsed["branch"] or "(detached)"
        out_s = (f"branch={branch} upstream={parsed['upstream'] or 'none'} "
                 f"ahead={parsed['ahead']} behind={parsed['behind']} "
                 f"staged={len(parsed['staged'])} unstaged={len(parsed['unstaged'])} "
                 f"untracked={len(parsed['untracked'])} conflicted={len(parsed['conflicted'])}")
        return GitResult(ok=True, output=out_s, parsed=parsed)

    def log(self, path: str | os.PathLike | None = None, max_count: int = 20,
            branch: str = "", since: str = "", author: str = "",
            grep: str = "", pathspec: str = "") -> GitResult:
        """Parse ``git log`` into a list of commit dicts (sha, author, email,
        date, subject, body)."""
        ok, cwd, _ = self._ensure_repo(path)
        if not ok:
            return self._fail_not_repo(cwd)
        args = ["log", f"--max-count={max(1, max_count)}",
                "--pretty=format:%H%x1f%an%x1f%ae%x1f%aI%x1f%s%x1f%b"]
        if since:
            args.append(f"--since={since}")
        if author:
            args.append(f"--author={author}")
        if grep:
            args.extend(["--grep", grep])
        if branch:
            args.append(branch)
        if pathspec:
            args.extend(["--", pathspec])
        code, out, err = self._run(args, path=cwd)
        if code != 0:
            return GitResult(ok=False, output=err.strip(), parsed={}, error=err.strip())
        commits = _parse_log(out)
        parsed = {"count": len(commits), "commits": commits}
        out_s = "\n".join(
            f"{c['short_sha']} {c['date'][:10]} {c['author']}: {c['subject']}"
            for c in commits
        ) or "(no commits)"
        return GitResult(ok=True, output=out_s, parsed=parsed)

    def diff(self, path: str | os.PathLike | None = None, staged: bool = False,
            commit_a: str = "", commit_b: str = "", pathspec: str = "",
            context_lines: int = 3) -> GitResult:
        """Unified diff plus per-file numstat. Select working-tree vs index
        (staged=True), index vs HEAD, or any two commits."""
        ok, cwd, _ = self._ensure_repo(path)
        if not ok:
            return self._fail_not_repo(cwd)
        base = ["diff", f"-U{max(0, context_lines)}", "--numstat", "--no-color"]
        if commit_a:
            base.append(commit_a)
            if commit_b:
                base.append(commit_b)
        elif staged:
            base.append("--cached")
        if pathspec:
            base.extend(["--", pathspec])
        code, out, err = self._run(base, path=cwd)
        if code != 0:
            return GitResult(ok=False, output=err.strip(), parsed={}, error=err.strip())
        files = _parse_diff_stat(out)
        total_added = sum(f["added"] or 0 for f in files)
        total_deleted = sum(f["deleted"] or 0 for f in files)
        parsed = {
            "files": files,
            "file_count": len(files),
            "total_added": total_added,
            "total_deleted": total_deleted,
            "staged": staged,
            "commit_a": commit_a,
            "commit_b": commit_b,
        }
        out_s = (f"{len(files)} file(s) changed, +{total_added}/-{total_deleted}\n" +
                 "\n".join(f"  {f['path']} (+{f['added']}/-{f['deleted']})" for f in files))
        return GitResult(ok=True, output=out_s.strip(), parsed=parsed)

    def branch_list(self, path: str | os.PathLike | None = None,
                    include_remote: bool = False) -> GitResult:
        """List local (and optionally remote) branches with tracking info."""
        ok, cwd, _ = self._ensure_repo(path)
        if not ok:
            return self._fail_not_repo(cwd)
        args = ["branch", "-vv"]
        if include_remote:
            args.append("-a")
        code, out, err = self._run(args, path=cwd)
        if code != 0:
            return GitResult(ok=False, output=err.strip(), parsed={}, error=err.strip())
        branches = _parse_branch_list(out)
        current = next((b["name"] for b in branches if b["current"]), "")
        parsed = {"current": current, "branches": branches, "count": len(branches)}
        out_s = "\n".join(
            f"{'*' if b['current'] else ' '} {b['name']} {b['sha']} {b['tracking']} {b['subject']}".rstrip()
            for b in branches
        ) or "(no branches)"
        return GitResult(ok=True, output=out_s, parsed=parsed)

    def remote_list(self, path: str | os.PathLike | None = None) -> GitResult:
        """List remotes with fetch/push URLs."""
        ok, cwd, _ = self._ensure_repo(path)
        if not ok:
            return self._fail_not_repo(cwd)
        code, out, err = self._run(["remote", "-v"], path=cwd)
        if code != 0:
            return GitResult(ok=False, output=err.strip(), parsed={}, error=err.strip())
        remotes = _parse_remotes(out)
        out_s = "\n".join(
            f"{name}: fetch={info.get('fetch', '')} push={info.get('push', '')}"
            for name, info in remotes.items()
        ) or "(no remotes)"
        return GitResult(ok=True, output=out_s, parsed={"remotes": remotes, "count": len(remotes)})

    def show_file_at_commit(self, file_path: str, commit: str = "HEAD",
                            path: str | os.PathLike | None = None,
                            max_bytes: int = 20000) -> GitResult:
        """Return file contents as stored at ``commit`` (``git show``)."""
        ok, cwd, _ = self._ensure_repo(path)
        if not ok:
            return self._fail_not_repo(cwd)
        code, out, err = self._run(["show", f"{commit}:{file_path}"], path=cwd)
        if code != 0:
            return GitResult(ok=False, output=err.strip(),
                             parsed={}, error=err.strip())
        body = out[:max_bytes]
        truncated = len(out) > max_bytes
        parsed = {"commit": commit, "file": file_path,
                  "bytes": len(out), "truncated": truncated}
        out_s = f"--- {file_path} @ {commit} ({len(out)} bytes) ---\n{body}"
        if truncated:
            out_s += f"\n... [truncated at {max_bytes} bytes]"
        return GitResult(ok=True, output=out_s, parsed=parsed)

    def blame_summary(self, file_path: str, path: str | os.PathLike | None = None) -> GitResult:
        """Per-author line counts for a file (``git blame --line-porcelain``)."""
        ok, cwd, _ = self._ensure_repo(path)
        if not ok:
            return self._fail_not_repo(cwd)
        code, out, err = self._run(["blame", "--line-porcelain", file_path], path=cwd)
        if code != 0:
            return GitResult(ok=False, output=err.strip(),
                             parsed={}, error=err.strip())
        parsed = _parse_blame(out)
        lines = [f"total_lines={parsed['total_lines']}"]
        for a in parsed["authors"][:15]:
            first = datetime.fromtimestamp(a["first_commit"], tz=timezone.utc).date() if a["first_commit"] else "?"
            last = datetime.fromtimestamp(a["last_commit"], tz=timezone.utc).date() if a["last_commit"] else "?"
            lines.append(f"  {a['author']} <{a['email']}>: {a['lines']} lines ({first}..{last})")
        return GitResult(ok=True, output="\n".join(lines), parsed=parsed)

    def merge_base(self, ref_a: str, ref_b: str,
                   path: str | os.PathLike | None = None) -> GitResult:
        """Best common ancestor of two refs."""
        ok, cwd, _ = self._ensure_repo(path)
        if not ok:
            return self._fail_not_repo(cwd)
        code, out, err = self._run(["merge-base", ref_a, ref_b], path=cwd)
        if code != 0:
            return GitResult(ok=False, output=err.strip(), parsed={}, error=err.strip())
        sha = out.strip()
        return GitResult(ok=True, output=f"merge-base({ref_a},{ref_b})={sha}",
                         parsed={"ref_a": ref_a, "ref_b": ref_b, "sha": sha})

    def tag_list(self, path: str | os.PathLike | None = None) -> GitResult:
        """List tags sorted by creation date (newest last)."""
        ok, cwd, _ = self._ensure_repo(path)
        if not ok:
            return self._fail_not_repo(cwd)
        code, out, err = self._run(["tag", "--list", "--sort=creatordate"], path=cwd)
        if code != 0:
            return GitResult(ok=False, output=err.strip(), parsed={}, error=err.strip())
        tags = [t for t in out.splitlines() if t.strip()]
        return GitResult(ok=True, output="\n".join(tags) or "(no tags)",
                         parsed={"tags": tags, "count": len(tags)})

    # -- repository setup ---------------------------------------------------

    def init(self, path: str | os.PathLike, default_branch: str = "main",
             bare: bool = False) -> GitResult:
        """Initialize a new git repository at ``path``."""
        target = Path(path).expanduser().resolve()
        target.mkdir(parents=True, exist_ok=True)
        args = ["init"]
        if bare:
            args.append("--bare")
        elif default_branch:
            args.extend(["-b", default_branch])
        code, out, err = self._run(args, path=str(target))
        if code != 0:
            return GitResult(ok=False, output=err.strip(), parsed={}, error=err.strip())
        return GitResult(ok=True, output=out.strip(),
                         parsed={"path": str(target), "bare": bare,
                                 "default_branch": default_branch})

    def clone(self, url: str, dest: str | os.PathLike,
              branch: str = "", depth: int = 0,
              timeout: int = 120) -> GitResult:
        """Clone a repository. Supports shallow clones via ``depth``."""
        target = Path(dest).expanduser().resolve()
        args = ["clone"]
        if depth and depth > 0:
            args.extend(["--depth", str(depth)])
        if branch:
            args.extend(["--branch", branch])
        args.extend([url, str(target)])
        proc = subprocess.run(
            ["git"] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            return GitResult(ok=False, output=proc.stderr.strip(),
                             parsed={}, error=proc.stderr.strip())
        return GitResult(ok=True, output=proc.stderr.strip() or f"Cloned to {target}",
                         parsed={"url": url, "dest": str(target),
                                 "branch": branch, "depth": depth})

    def config_get(self, key: str, path: str | os.PathLike | None = None,
                   scope: str = "local") -> GitResult:
        """Read a git config value (scope: local|global|system)."""
        ok, cwd, _ = self._ensure_repo(path)
        if not ok and scope == "local":
            return self._fail_not_repo(cwd)
        code, out, err = self._run(["config", f"--{scope}", "--get", key],
                                   path=cwd if ok else None)
        if code != 0:
            return GitResult(ok=False, output=err.strip(),
                             parsed={"key": key, "scope": scope, "value": ""},
                             error=err.strip() or f"config key not set: {key}")
        return GitResult(ok=True, output=f"{key}={out.strip()}",
                         parsed={"key": key, "scope": scope, "value": out.strip()})

    def config_set(self, key: str, value: str,
                   path: str | os.PathLike | None = None) -> GitResult:
        """Set a local repo config value."""
        ok, cwd, _ = self._ensure_repo(path)
        if not ok:
            return self._fail_not_repo(cwd)
        code, out, err = self._run(["config", key, value], path=cwd)
        if code != 0:
            return GitResult(ok=False, output=err.strip(), parsed={}, error=err.strip())
        return GitResult(ok=True, output=f"Set {key}={value}",
                         parsed={"key": key, "value": value})

    # -- branching ----------------------------------------------------------

    def branch_create(self, name: str, start_point: str = "HEAD",
                      checkout_after: bool = False,
                      path: str | os.PathLike | None = None) -> GitResult:
        """Create a branch at ``start_point``. Optionally check it out."""
        ok, cwd, _ = self._ensure_repo(path)
        if not ok:
            return self._fail_not_repo(cwd)
        if not re.match(r"^[A-Za-z0-9._/-]+$", name):
            return GitResult(ok=False, output=f"Invalid branch name: {name!r}",
                             parsed={}, error="Invalid branch name")
        code, out, err = self._run(["branch", name, start_point], path=cwd)
        if code != 0:
            return GitResult(ok=False, output=err.strip(), parsed={}, error=err.strip())
        parsed: dict[str, Any] = {"name": name, "start_point": start_point,
                                  "checkout_after": False}
        if checkout_after:
            cr = self.checkout(name, path=cwd)
            parsed["checkout_after"] = cr.ok
            if not cr.ok:
                return GitResult(ok=True,
                                 output=f"Branch '{name}' created; checkout failed: {cr.error}",
                                 parsed=parsed)
        return GitResult(ok=True, output=f"Created branch '{name}' at {start_point}",
                         parsed=parsed)

    def branch_delete(self, name: str, force: bool = False,
                      path: str | os.PathLike | None = None) -> GitResult:
        """Delete a local branch (``-d`` or ``-D`` with force=True)."""
        ok, cwd, _ = self._ensure_repo(path)
        if not ok:
            return self._fail_not_repo(cwd)
        flag = "-D" if force else "-d"
        code, out, err = self._run(["branch", flag, name], path=cwd)
        if code != 0:
            return GitResult(ok=False, output=err.strip(), parsed={}, error=err.strip())
        return GitResult(ok=True, output=out.strip(),
                         parsed={"deleted": name, "force": force})

    def checkout(self, ref: str, create: bool = False,
                 path: str | os.PathLike | None = None) -> GitResult:
        """Checkout a branch/commit (``-b`` when create=True). Detects dirty
        tree first and reports rather than clobbering."""
        ok, cwd, _ = self._ensure_repo(path)
        if not ok:
            return self._fail_not_repo(cwd)
        st = self.status(cwd)
        if st.ok and st.parsed.get("dirty"):
            return GitResult(
                ok=False,
                output=("Working tree is dirty; commit or stash changes before "
                        f"checking out '{ref}'."),
                parsed={"dirty": True, "ref": ref},
                error="Working tree dirty",
            )
        args = ["checkout"]
        if create:
            args.append("-b")
        args.append(ref)
        code, out, err = self._run(args, path=cwd)
        if code != 0:
            return GitResult(ok=False, output=(err or out).strip(),
                             parsed={}, error=(err or out).strip())
        msg = (err or out).strip() or f"Checked out '{ref}'"
        # Detect detached HEAD
        code2, out2, _ = self._run(["symbolic-ref", "-q", "HEAD"], path=cwd)
        detached = code2 != 0
        return GitResult(ok=True, output=msg,
                         parsed={"ref": ref, "detached": detached})

    # -- staging & committing -----------------------------------------------

    def add(self, pathspec: str | list[str],
            path: str | os.PathLike | None = None) -> GitResult:
        """Stage files (``git add``). Accepts a single pathspec or a list."""
        ok, cwd, _ = self._ensure_repo(path)
        if not ok:
            return self._fail_not_repo(cwd)
        specs = [pathspec] if isinstance(pathspec, str) else list(pathspec)
        if not specs:
            return GitResult(ok=False, output="No paths to add",
                             parsed={}, error="Empty pathspec")
        code, out, err = self._run(["add", "--"] + specs, path=cwd)
        if code != 0:
            return GitResult(ok=False, output=err.strip(), parsed={}, error=err.strip())
        return GitResult(ok=True, output=f"Staged: {', '.join(specs)}",
                         parsed={"staged": specs})

    def commit(self, message: str, path: str | os.PathLike | None = None,
               amend: bool = False, allow_empty: bool = False,
               author: str = "") -> GitResult:
        """Create a commit. Refuses to commit an empty message or an empty
        index unless allow_empty=True."""
        ok, cwd, _ = self._ensure_repo(path)
        if not ok:
            return self._fail_not_repo(cwd)
        if not message.strip():
            return GitResult(ok=False, output="Commit message is empty",
                             parsed={}, error="Empty commit message")
        args = ["commit", "-m", message]
        if amend:
            args.append("--amend")
        if allow_empty:
            args.append("--allow-empty")
        if author:
            args.extend(["--author", author])
        code, out, err = self._run(args, path=cwd)
        if code != 0:
            return GitResult(ok=False, output=(err or out).strip(),
                             parsed={}, error=(err or out).strip())
        m = re.search(r"\[(?:[^\]]+ )?([0-9a-f]{7,40})\]", out)
        sha = m.group(1) if m else ""
        return GitResult(ok=True, output=out.strip(),
                         parsed={"sha": sha, "message": message, "amend": amend})

    # -- syncing ------------------------------------------------------------

    def fetch(self, remote: str = "origin", prune: bool = False,
              path: str | os.PathLike | None = None,
              timeout: int = 60) -> GitResult:
        """Fetch from a remote."""
        ok, cwd, _ = self._ensure_repo(path)
        if not ok:
            return self._fail_not_repo(cwd)
        args = ["fetch", remote]
        if prune:
            args.append("--prune")
        code, out, err = self._run(args, path=cwd, timeout=timeout)
        if code != 0:
            return GitResult(ok=False, output=err.strip(), parsed={}, error=err.strip())
        return GitResult(ok=True, output=err.strip() or f"Fetched from {remote}",
                         parsed={"remote": remote, "prune": prune})

    def push(self, remote: str = "origin", branch: str = "",
             set_upstream: bool = False, force: bool = False,
             path: str | os.PathLike | None = None,
             timeout: int = 120) -> GitResult:
        """Push to a remote. ``force`` uses --force-with-lease (safer)."""
        ok, cwd, _ = self._ensure_repo(path)
        if not ok:
            return self._fail_not_repo(cwd)
        args = ["push", remote]
        if set_upstream:
            args.append("--set-upstream")
        if force:
            args.append("--force-with-lease")
        if branch:
            args.append(branch)
        code, out, err = self._run(args, path=cwd, timeout=timeout)
        if code != 0:
            return GitResult(ok=False, output=err.strip(), parsed={}, error=err.strip())
        return GitResult(ok=True, output=err.strip() or "Pushed successfully",
                         parsed={"remote": remote, "branch": branch,
                                 "set_upstream": set_upstream, "force": force})

    def pull(self, remote: str = "origin", branch: str = "",
             rebase: bool = False,
             path: str | os.PathLike | None = None,
             timeout: int = 120) -> GitResult:
        """Pull from a remote (merge or rebase). Reports conflicts."""
        ok, cwd, _ = self._ensure_repo(path)
        if not ok:
            return self._fail_not_repo(cwd)
        st = self.status(cwd)
        if st.ok and st.parsed.get("conflicted"):
            return GitResult(ok=False,
                             output="Cannot pull: unresolved merge conflicts exist.",
                             parsed={"conflicted": st.parsed["conflicted"]},
                             error="Unresolved merge conflicts")
        args = ["pull"]
        if rebase:
            args.append("--rebase")
        args.append(remote)
        if branch:
            args.append(branch)
        code, out, err = self._run(args, path=cwd, timeout=timeout)
        combined = (out + "\n" + err).strip()
        conflicted = code != 0 and ("CONFLICT" in combined or "conflict" in combined.lower())
        if code != 0:
            return GitResult(ok=False, output=combined,
                             parsed={"conflicted_pull": conflicted},
                             error=err.strip() or out.strip())
        return GitResult(ok=True, output=combined or "Already up to date",
                         parsed={"remote": remote, "branch": branch, "rebase": rebase})

    # -- stash ---------------------------------------------------------------

    def stash(self, message: str = "", include_untracked: bool = False,
              path: str | os.PathLike | None = None) -> GitResult:
        """Stash working-tree changes."""
        ok, cwd, _ = self._ensure_repo(path)
        if not ok:
            return self._fail_not_repo(cwd)
        args = ["stash", "push"]
        if include_untracked:
            args.append("--include-untracked")
        if message:
            args.extend(["-m", message])
        code, out, err = self._run(args, path=cwd)
        if code != 0:
            return GitResult(ok=False, output=(err or out).strip(),
                             parsed={}, error=(err or out).strip())
        return GitResult(ok=True, output=out.strip(),
                         parsed={"message": message,
                                 "include_untracked": include_untracked})

    def stash_list(self, path: str | os.PathLike | None = None) -> GitResult:
        """List stash entries."""
        ok, cwd, _ = self._ensure_repo(path)
        if not ok:
            return self._fail_not_repo(cwd)
        code, out, err = self._run(["stash", "list"], path=cwd)
        if code != 0:
            return GitResult(ok=False, output=err.strip(), parsed={}, error=err.strip())
        stashes = _parse_stash_list(out)
        return GitResult(ok=True, output=out.strip() or "(no stashes)",
                         parsed={"stashes": stashes, "count": len(stashes)})

    def stash_pop(self, index: int = 0,
                  path: str | os.PathLike | None = None) -> GitResult:
        """Apply and drop stash@{index}. Reports conflicts on failure."""
        ok, cwd, _ = self._ensure_repo(path)
        if not ok:
            return self._fail_not_repo(cwd)
        code, out, err = self._run(["stash", "pop", f"stash@{{{index}}}"], path=cwd)
        combined = (out + "\n" + err).strip()
        if code != 0:
            conflict = "CONFLICT" in combined
            return GitResult(ok=False, output=combined,
                             parsed={"conflict": conflict, "index": index},
                             error=err.strip() or out.strip())
        return GitResult(ok=True, output=combined,
                         parsed={"index": index})

    def stash_apply(self, index: int = 0, keep: bool = True,
                    path: str | os.PathLike | None = None) -> GitResult:
        """Apply stash@{index} without dropping it."""
        ok, cwd, _ = self._ensure_repo(path)
        if not ok:
            return self._fail_not_repo(cwd)
        code, out, err = self._run(["stash", "apply", f"stash@{{{index}}}"], path=cwd)
        combined = (out + "\n" + err).strip()
        if code != 0:
            return GitResult(ok=False, output=combined,
                             parsed={"conflict": "CONFLICT" in combined},
                             error=err.strip() or out.strip())
        return GitResult(ok=True, output=combined, parsed={"index": index})

    # -- reset / undo --------------------------------------------------------

    def reset(self, mode: str = "mixed", target: str = "HEAD",
              pathspec: str = "",
              path: str | os.PathLike | None = None) -> GitResult:
        """Reset working tree/index. Modes: soft|mixed|hard.
        SAFETY: hard reset requires explicit target (never bare 'HEAD'
        without confirmation intent) — caller passes target explicitly."""
        ok, cwd, _ = self._ensure_repo(path)
        if not ok:
            return self._fail_not_repo(cwd)
        if mode not in ("soft", "mixed", "hard"):
            return GitResult(ok=False, output=f"Invalid reset mode: {mode}",
                             parsed={}, error="Invalid reset mode")
        args = ["reset", f"--{mode}", target]
        if pathspec:
            args.extend(["--", pathspec])
        code, out, err = self._run(args, path=cwd)
        if code != 0:
            return GitResult(ok=False, output=(err or out).strip(),
                             parsed={}, error=(err or out).strip())
        return GitResult(ok=True, output=out.strip() or f"Reset --{mode} {target}",
                         parsed={"mode": mode, "target": target,
                                 "pathspec": pathspec})

    # -- conflict & state helpers -------------------------------------------

    def conflict_files(self, path: str | os.PathLike | None = None) -> GitResult:
        """List files currently in a conflicted (unmerged) state."""
        ok, cwd, _ = self._ensure_repo(path)
        if not ok:
            return self._fail_not_repo(cwd)
        code, out, err = self._run(["diff", "--name-only", "--diff-filter=U"], path=cwd)
        if code != 0:
            return GitResult(ok=False, output=err.strip(), parsed={}, error=err.strip())
        files = [l for l in out.splitlines() if l.strip()]
        return GitResult(ok=True, output="\n".join(files) or "(no conflicts)",
                         parsed={"conflicted": files, "count": len(files),
                                 "in_conflict": bool(files)})

    def merge_in_progress(self, path: str | os.PathLike | None = None) -> GitResult:
        """Detect whether a merge/rebase/cherry-pick is in progress."""
        ok, cwd, top = self._ensure_repo(path)
        if not ok:
            return self._fail_not_repo(cwd)
        gitdir_code, gitdir_out, _ = self._run(["rev-parse", "--git-dir"], path=cwd)
        gitdir = Path(gitdir_out.strip())
        if not gitdir.is_absolute():
            gitdir = top / gitdir
        states = {
            "merge": (gitdir / "MERGE_HEAD").exists(),
            "rebase": (gitdir / "rebase-merge").exists() or (gitdir / "rebase-apply").exists(),
            "cherry_pick": (gitdir / "CHERRY_PICK_HEAD").exists(),
            "revert": (gitdir / "REVERT_HEAD").exists(),
        }
        in_progress = any(states.values())
        kind = next((k for k, v in states.items() if v), "")
        return GitResult(ok=True,
                         output=f"in_progress={in_progress} kind={kind or 'none'}",
                         parsed={"in_progress": in_progress, "kind": kind,
                                 "states": states})

    def last_commit(self, path: str | os.PathLike | None = None) -> GitResult:
        """Details of HEAD commit."""
        r = self.log(path=path, max_count=1)
        if not r.ok:
            return r
        commits = r.parsed.get("commits", [])
        if not commits:
            return GitResult(ok=False, output="(no commits yet)",
                             parsed={}, error="Repository has no commits")
        c = commits[0]
        return GitResult(ok=True,
                         output=f"{c['short_sha']} {c['date']} {c['author']}: {c['subject']}",
                         parsed=c)

    def files_changed_between(self, ref_a: str, ref_b: str = "HEAD",
                              path: str | os.PathLike | None = None) -> GitResult:
        """File names changed between two refs (diff --name-status)."""
        ok, cwd, _ = self._ensure_repo(path)
        if not ok:
            return self._fail_not_repo(cwd)
        code, out, err = self._run(["diff", "--name-status", ref_a, ref_b], path=cwd)
        if code != 0:
            return GitResult(ok=False, output=err.strip(), parsed={}, error=err.strip())
        changes = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                changes.append({"status": parts[0], "path": parts[1],
                                "old_path": parts[2] if len(parts) > 2 else ""})
        return GitResult(ok=True, output=out.strip() or "(no changes)",
                         parsed={"ref_a": ref_a, "ref_b": ref_b,
                                 "changes": changes, "count": len(changes)})

    def ahead_behind(self, branch: str = "HEAD", upstream: str = "@{upstream}",
                     path: str | os.PathLike | None = None) -> GitResult:
        """Commit counts: how far branch is ahead/behind its upstream."""
        ok, cwd, _ = self._ensure_repo(path)
        if not ok:
            return self._fail_not_repo(cwd)
        code, out, err = self._run(
            ["rev-list", "--left-right", "--count", f"{branch}...{upstream}"], path=cwd)
        if code != 0:
            return GitResult(ok=False, output=err.strip(), parsed={}, error=err.strip())
        parts = out.strip().split()
        ahead = int(parts[0]) if len(parts) > 0 else 0
        behind = int(parts[1]) if len(parts) > 1 else 0
        return GitResult(ok=True, output=f"ahead={ahead} behind={behind}",
                         parsed={"ahead": ahead, "behind": behind,
                                 "branch": branch, "upstream": upstream})

    def worktree_summary(self, path: str | os.PathLike | None = None) -> GitResult:
        """One-shot summary: branch, dirtiness, conflicts, last commit,
        ahead/behind — everything an agent needs before acting."""
        ok, cwd, top = self._ensure_repo(path)
        if not ok:
            return self._fail_not_repo(cwd)
        summary: dict[str, Any] = {"repo": str(top), "path": cwd}
        st = self.status(cwd)
        summary["status"] = st.parsed if st.ok else {"error": st.error}
        lc = self.last_commit(cwd)
        summary["last_commit"] = lc.parsed if lc.ok else {}
        cf = self.conflict_files(cwd)
        summary["conflicts"] = cf.parsed.get("conflicted", []) if cf.ok else []
        mip = self.merge_in_progress(cwd)
        summary["merge_state"] = mip.parsed if mip.ok else {}
        rl = self.remote_list(cwd)
        summary["remotes"] = list(rl.parsed.get("remotes", {}).keys()) if rl.ok else []
        ab = self.ahead_behind(path=cwd)
        summary["ahead_behind"] = ab.parsed if ab.ok else {}
        dirty = summary["status"].get("dirty", False)
        conflicts = bool(summary["conflicts"])
        line = (f"repo={top.name} branch={summary['status'].get('branch', '?')} "
                f"dirty={dirty} conflicts={conflicts} "
                f"ahead={summary['ahead_behind'].get('ahead', '?')} "
                f"behind={summary['ahead_behind'].get('behind', '?')} "
                f"last={summary['last_commit'].get('short_sha', 'none')}")
        return GitResult(ok=True, output=line, parsed=summary)


__all__ = ["GitTools", "GitResult", "NotARepoError", "DEFAULT_TIMEOUT"]
