"""Coding & Tool Creation extensions.

Git fetch/push/clone (uses the git CLI and the repo's existing remote
config — credentials are never accepted, printed, or stored here),
lint/format via installed tools, dependency/env inspection, user-crontab
management, webhook handler scaffolding, JSON-aware HTTP, zipapp builds,
and a harness to execute a named tool module's handler with sample args.

Boundary: ``git_push``/``git_clone`` operate over whatever auth the host
already has configured (e.g. stored SSH keys); this module never handles
usernames, passwords, or tokens, and never echoes URLs with embedded
credentials into output.
"""
from __future__ import annotations

import ast
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT = 60
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WEBHOOK_DIR = REPO_ROOT / "agent" / "api" / "webhooks"


# ---------------------------------------------------------------------------
# Result type (local; decoupled from other tool modules by design)
# ---------------------------------------------------------------------------

@dataclass
class CodeResult:
    """Structured result of a coding/tool-creation operation."""
    ok: bool
    output: str = ""
    parsed: dict = field(default_factory=dict)
    error: str = ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _result_from_exc(exc: Exception) -> CodeResult:
    return CodeResult(ok=False, error=f"{type(exc).__name__}: {exc}")


def _git(args: list[str], cwd: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                          text=True, timeout=timeout)


def _scrub_url(url: str) -> str:
    """Strip any userinfo (user:pass@) from a URL before it reaches output."""
    try:
        p = urllib.parse.urlsplit(url)
        if p.username or p.password:
            netloc = p.hostname or ""
            if p.port:
                netloc += f":{p.port}"
            return urllib.parse.urlunsplit(
                (p.scheme, netloc, p.path, p.query, p.fragment))
    except Exception:
        pass
    return url


def _validate_cron(expr: str) -> str | None:
    """Return error message if expr is not a 5-field cron, else None."""
    fields = expr.strip().split()
    if len(fields) != 5:
        return f"cron needs 5 fields, got {len(fields)}: '{expr}'"
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    for f, (lo, hi) in zip(fields, ranges):
        for part in f.split(","):
            part = part.strip()
            if not part:
                return f"empty cron field in '{expr}'"
            if part == "*":
                continue
            step = part.split("/")
            if len(step) == 2 and step[1].isdigit() and (step[0] == "*" or "-" in step[0]):
                base = step[0]
            else:
                base, _ = part, None
                if "/" in part:
                    return f"bad step syntax '{part}' in '{expr}'"
            for rng in base.split("-"):
                rng = rng.strip()
                if rng == "*":
                    continue
                if not rng.isdigit() or not (lo <= int(rng) <= hi):
                    return f"value '{rng}' out of range {lo}-{hi} in '{expr}'"
    return None


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _git_clone(args: dict) -> CodeResult:
    try:
        url = args["url"]
        if "@" in urllib.parse.urlsplit(url).netloc:
            return CodeResult(
                ok=False,
                error="Refused: URLs with embedded credentials are not accepted. "
                      "Use the host's configured SSH/credential helper instead.")
        dest = Path(args["dest"])
        r = subprocess.run(["git", "clone", url, str(dest)], capture_output=True,
                           text=True, timeout=300)
        out = (r.stdout + r.stderr)[-2000:]
        return CodeResult(
            ok=r.returncode == 0,
            output=f"git clone {_scrub_url(url)} -> {dest}: rc={r.returncode}\n{out}",
            parsed={"url": _scrub_url(url), "dest": str(dest), "rc": r.returncode},
            error="" if r.returncode == 0 else out[-500:])
    except Exception as e:
        return _result_from_exc(e)


def _git_pull(args: dict) -> CodeResult:
    try:
        path = args.get("path") or "."
        r = _git(["pull"], cwd=path)
        out = (r.stdout + r.stderr)[-2000:]
        return CodeResult(
            ok=r.returncode == 0,
            output=f"git pull ({path}): rc={r.returncode}\n{out}",
            parsed={"path": path, "rc": r.returncode, "detail": out[-1000:]},
            error="" if r.returncode == 0 else out[-500:])
    except Exception as e:
        return _result_from_exc(e)


def _git_push(args: dict) -> CodeResult:
    try:
        path = args.get("path") or "."
        remote = args.get("remote") or "origin"
        branch = args.get("branch")
        cmd = ["push", remote] + ([branch] if branch else [])
        r = _git(cmd, cwd=path)
        out = (r.stdout + r.stderr)[-2000:]
        return CodeResult(
            ok=r.returncode == 0,
            output=f"git {' '.join(cmd)} ({path}): rc={r.returncode}\n{out}",
            parsed={"path": path, "remote": remote, "branch": branch,
                    "rc": r.returncode, "detail": out[-1000:]},
            error="" if r.returncode == 0 else out[-500:])
    except Exception as e:
        return _result_from_exc(e)


def _code_lint(args: dict) -> CodeResult:
    try:
        path = Path(args["path"])
        issues: list[str] = []
        src = path.read_text(encoding="utf-8")
        try:
            ast.parse(src, filename=str(path))
        except SyntaxError as se:
            issues.append(f"SyntaxError line {se.lineno}: {se.msg}")
            return CodeResult(ok=False, output="\n".join(issues),
                              parsed={"path": str(path), "issues": issues},
                              error=issues[0])
        checks = ["py_compile: OK", "ast.parse: OK"]
        if shutil.which("pyflakes"):
            r = subprocess.run([sys.executable, "-m", "pyflakes", str(path)],
                               capture_output=True, text=True, timeout=60)
            pyf = (r.stdout + r.stderr).strip()
            checks.append("pyflakes: " + ("clean" if not pyf else "issues"))
            if pyf:
                issues.extend(pyf.splitlines())
        else:
            checks.append("pyflakes: not installed (skipped)")
        ok = not issues
        return CodeResult(
            ok=ok,
            output="\n".join(checks + ([""] + issues if issues else [])),
            parsed={"path": str(path), "issues": issues})
    except Exception as e:
        return _result_from_exc(e)


def _code_format(args: dict) -> CodeResult:
    try:
        if shutil.which("black") is None:
            return CodeResult(
                ok=False,
                error="black is not installed. Hint: use app_install {package: black, manager: pip}.")
        path = Path(args["path"])
        r = subprocess.run([sys.executable, "-m", "black", str(path)],
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return CodeResult(
            ok=r.returncode == 0, output=f"black {path}:\n{out}",
            parsed={"path": str(path), "rc": r.returncode},
            error="" if r.returncode == 0 else out[-500:])
    except Exception as e:
        return _result_from_exc(e)


def _dep_check(args: dict) -> CodeResult:
    try:
        package = args["package"]
        info: dict[str, Any] = {"package": package, "importable": False,
                                "version": None, "location": None}
        try:
            mod = importlib.import_module(package)
            info["importable"] = True
            info["version"] = getattr(mod, "__version__", None) or getattr(mod, "version", None)
            info["location"] = getattr(mod, "__file__", None)
        except ImportError:
            pass
        r = subprocess.run([sys.executable, "-m", "pip", "show", package],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if line.startswith("Version:"):
                    info["version"] = info["version"] or line.split(":", 1)[1].strip()
                if line.startswith("Location:"):
                    info["location"] = info["location"] or line.split(":", 1)[1].strip()
        status = "installed" if (info["importable"] or r.returncode == 0) else "missing"
        return CodeResult(
            ok=status == "installed",
            output=f"{package}: {status} (version={info['version']}, location={info['location']})",
            parsed={**info, "status": status})
    except Exception as e:
        return _result_from_exc(e)


def _env_get(args: dict) -> CodeResult:
    try:
        name = args["name"]
        val = os.environ.get(name)
        masked = val is not None and any(
            s in name.lower() for s in ("key", "token", "secret", "password", "auth"))
        shown = "*** (masked)" if masked and val else val
        return CodeResult(
            ok=val is not None,
            output=f"{name}={'<set>' if val is not None else '<unset>'}"
                   + (f" = {shown}" if val is not None else ""),
            parsed={"name": name, "set": val is not None,
                    "value": shown if val is not None else None})
    except Exception as e:
        return _result_from_exc(e)


def _env_set(args: dict) -> CodeResult:
    try:
        name = args["name"]
        value = str(args["value"])
        os.environ[name] = value
        return CodeResult(
            ok=True,
            output=f"Set {name} in this process environment. "
                   "Process-scoped only: does not persist to new shells, "
                   "the OS environment, or other processes.",
            parsed={"name": name, "persisted": False, "scope": "process"})
    except Exception as e:
        return _result_from_exc(e)


def _cron_add(args: dict) -> CodeResult:
    try:
        if shutil.which("crontab") is None:
            return CodeResult(ok=False, error="crontab not available on this host.")
        schedule = args["schedule"]
        command = args["command"]
        err = _validate_cron(schedule)
        if err:
            return CodeResult(ok=False, error=f"Invalid schedule: {err}")
        existing = subprocess.run(["crontab", "-l"], capture_output=True,
                                  text=True, timeout=15)
        lines = existing.stdout.splitlines() if existing.returncode == 0 else []
        entry = f"{schedule} {command}"
        if entry in lines:
            return CodeResult(ok=True, output=f"Cron entry already present:\n{entry}",
                              parsed={"schedule": schedule, "command": command,
                                      "added": False})
        lines.append(entry)
        r = subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n",
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return CodeResult(ok=False, error=f"crontab install failed: {r.stderr.strip()}")
        return CodeResult(
            ok=True, output=f"Added cron entry:\n{entry}",
            parsed={"schedule": schedule, "command": command, "added": True})
    except Exception as e:
        return _result_from_exc(e)


_WEBHOOK_TEMPLATE = '''"""Webhook handler: {name}

Saved by webhook_register. To wire it into the stdlib API server
(agent/api/server.py), add a branch in Handler.do_POST, e.g.:

    elif self.path.startswith("/webhooks/{route}"):
        from agent.api.webhooks.{modname} import handle
        self._json(200, handle(self._body()))

Then restart the server process. Until wired, this file is inert.
"""
from __future__ import annotations


def handle(body: dict) -> dict:
    """Handle an incoming webhook POST body. Return a JSON-serializable dict."""
{body}
'''


def _webhook_register(args: dict) -> CodeResult:
    try:
        name = args["path"]
        code = args["code"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            return CodeResult(ok=False, error=f"Unsafe webhook name: {name!r} "
                                              "(use [A-Za-z0-9_.-] only, no directories).")
        WEBHOOK_DIR.mkdir(parents=True, exist_ok=True)
        init = WEBHOOK_DIR / "__init__.py"
        if not init.exists():
            init.write_text('"""Webhook handler modules (wired manually into server.py)."""\n')
        modname = Path(name).stem
        target = WEBHOOK_DIR / f"{modname}.py"
        body_lines = code.strip().splitlines() or ["    return {\"ok\": True}"]
        indented = "\n".join("    " + ln if ln.strip() else "" for ln in body_lines)
        target.write_text(_WEBHOOK_TEMPLATE.format(
            name=name, route=modname, modname=modname, body=indented))
        instructions = (
            f"Saved {target}. agent/api/server.py has no webhook hook point, "
            "so wire it manually: in Handler.do_POST add\n"
            f'    elif self.path.startswith("/webhooks/{modname}"):'
            f'\n        from agent.api.webhooks.{modname} import handle'
            "\n        self._json(200, handle(self._body()))\n"
            "then restart the server process. Until then the handler is inert."
        )
        return CodeResult(
            ok=True, output=instructions,
            parsed={"handler_path": str(target), "route": f"/webhooks/{modname}",
                    "wired": False})
    except Exception as e:
        return _result_from_exc(e)


def _api_request(args: dict) -> CodeResult:
    try:
        method = str(args.get("method", "GET")).upper()
        url = args["url"]
        headers = dict(args.get("headers") or {})
        body = args.get("body")
        data = None
        if body is not None:
            if isinstance(body, (dict, list)):
                data = json.dumps(body).encode()
                headers.setdefault("Content-Type", "application/json")
            else:
                data = str(body).encode()
        timeout = int(args.get("timeout", 30))
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            status = resp.status
        parsed_body: Any = None
        text = raw.decode("utf-8", errors="replace")
        if "json" in ctype:
            try:
                parsed_body = json.loads(text)
            except json.JSONDecodeError:
                pass
        return CodeResult(
            ok=200 <= status < 300,
            output=f"{method} {url} -> {status} ({len(raw)} bytes)",
            parsed={"method": method, "url": url, "status": status,
                    "content_type": ctype, "body": parsed_body,
                    "text_preview": text[:2000]})
    except Exception as e:
        return _result_from_exc(e)


def _package_build(args: dict) -> CodeResult:
    try:
        src_dir = Path(args["src_dir"])
        output = Path(args["output"])
        if not (src_dir / "__main__.py").exists():
            return CodeResult(
                ok=False,
                error=f"{src_dir} has no __main__.py; zipapp needs an entry point.")
        output.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            [sys.executable, "-m", "zipapp", str(src_dir), "-o", str(output), "-p",
             "/usr/bin/env python3"],
            capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return CodeResult(ok=False, error=f"zipapp failed: {(r.stdout + r.stderr).strip()[-500:]}")
        size = output.stat().st_size
        return CodeResult(
            ok=True, output=f"Built {output} ({size} bytes) from {src_dir}.",
            parsed={"src_dir": str(src_dir), "output": str(output), "size_bytes": size})
    except Exception as e:
        return _result_from_exc(e)


_TOOL_MODULES = [
    "file_tools", "system_tools", "git_tools", "network_tools", "data_tools",
    "media_tools", "archive_tools", "text_tools",
    "vision_tools", "input_tools", "browser_tools",
    "filemgmt_tools", "app_tools", "code_tools", "memory_tools", "safety_tools",
]


def _tool_test(args: dict) -> CodeResult:
    try:
        name = args["name"]
        sample_args = args.get("args") or {}
        handlers = None
        used_module = None
        for modname in _TOOL_MODULES:
            try:
                mod = importlib.import_module(f"agent.tools.{modname}")
            except ImportError:
                continue
            h = getattr(mod, "HANDLERS", None)
            if isinstance(h, dict) and name in h:
                handlers, used_module = h, modname
                break
        if handlers is not None:
            fn = handlers[name]
            result = fn(dict(sample_args))
            return CodeResult(
                ok=bool(getattr(result, "ok", False)),
                output=f"tool_test {name} via agent.tools.{used_module}:\n"
                       f"{getattr(result, 'output', '')}",
                parsed={"tool": name, "module": used_module,
                        "ok": bool(getattr(result, "ok", False))},
                error=str(getattr(result, "error", "") or ""))
        # Fallback: legacy class-based tools (system_tools, file_tools, ...)
        # have no HANDLERS dict — run them through the central dispatcher.
        try:
            from agent.tools.registry import build_full_registry
            from agent.tools.dispatcher import Dispatcher
            disp = Dispatcher(build_full_registry(), workspace=os.getcwd())
            res = disp.dispatch(name, dict(sample_args))
            return CodeResult(
                ok=res.ok,
                output=f"tool_test {name} via Dispatcher:\n{res.output}",
                parsed={"tool": name, "module": "dispatcher", "ok": res.ok},
                error="" if res.ok else res.output[:300])
        except Exception as e:
            return CodeResult(ok=False,
                              error=f"No tool named '{name}' in known tool modules. ({e})")
    except Exception as e:
        return _result_from_exc(e)


HANDLERS: dict = {
    "git_clone": _git_clone,
    "git_pull": _git_pull,
    "git_push": _git_push,
    "code_lint": _code_lint,
    "code_format": _code_format,
    "dep_check": _dep_check,
    "env_get": _env_get,
    "env_set": _env_set,
    "cron_add": _cron_add,
    "webhook_register": _webhook_register,
    "api_request": _api_request,
    "package_build": _package_build,
    "tool_test": _tool_test,
}


def _sch(t: str, req: bool, desc: str) -> dict:
    return {"type": t, "required": req, "description": desc}


TOOL_DEFS: list[tuple] = [
    ("git_clone", "Clone a repo (no credential URLs accepted)",
     {"url": _sch("string", True, "Repo URL (https/ssh, no userinfo)"),
      "dest": _sch("string", True, "Destination directory")},
     "computer", 300),
    ("git_pull", "git pull in a repo dir",
     {"path": _sch("string", False, "Repo dir (default cwd)")},
     "computer", 120),
    ("git_push", "git push to remote/branch (uses host-configured auth)",
     {"path": _sch("string", False, "Repo dir (default cwd)"),
      "remote": _sch("string", False, "Remote (default origin)"),
      "branch": _sch("string", False, "Branch (default current)")},
     "computer", 120),
    ("code_lint", "py_compile + ast.parse; pyflakes if installed",
     {"path": _sch("string", True, "Python file")},
     "computer", 60),
    ("code_format", "Format with black (fails with hint if missing)",
     {"path": _sch("string", True, "Python file")},
     "computer", 120),
    ("dep_check", "Check a package: importable, version, location",
     {"package": _sch("string", True, "Package/module name")},
     "computer", 30),
    ("env_get", "Read an env var (secrets masked in output)",
     {"name": _sch("string", True, "Variable name")},
     "computer", 20),
    ("env_set", "Set an env var for THIS process only (documented)",
     {"name": _sch("string", True, "Variable name"),
      "value": _sch("string", True, "Value")},
     "computer", 20),
    ("cron_add", "Append a validated 5-field entry to the user crontab",
     {"schedule": _sch("string", True, "5-field cron expression"),
      "command": _sch("string", True, "Command to run")},
     "computer", 30),
    ("webhook_register", "Scaffold a webhook handler file (manual wiring needed)",
     {"path": _sch("string", True, "Handler name [A-Za-z0-9_.-]"),
      "code": _sch("string", True, "Body code for handle(body)")},
     "computer", 30),
    ("api_request", "HTTP request via urllib, JSON-aware",
     {"method": _sch("string", False, "GET|POST|PUT|DELETE... (default GET)"),
      "url": _sch("string", True, "URL"),
      "headers": _sch("object", False, "Headers dict"),
      "body": _sch("object", False, "JSON-serializable body or text"),
      "timeout": _sch("integer", False, "Seconds (default 30)")},
     "computer", 60),
    ("package_build", "Build a zipapp executable from a dir with __main__.py",
     {"src_dir": _sch("string", True, "Source directory"),
      "output": _sch("string", True, "Output .pyz path")},
     "computer", 120),
    ("tool_test", "Run a named tool's handler with sample args",
     {"name": _sch("string", True, "Tool name"),
      "args": _sch("object", False, "Sample args dict")},
     "computer", 60),
]
