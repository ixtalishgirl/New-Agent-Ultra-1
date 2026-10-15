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
import threading
import time
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
from typing import Any, Callable, Optional

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




# ---------------------------------------------------------------------------
# Executor injection (bound by Dispatcher so nested tool calls are gated)
# ---------------------------------------------------------------------------
_EXECUTOR: Optional[Callable[[str, dict], Any]] = None


def set_executor(fn: Callable[[str, dict], Any]) -> None:
    """Bind the dispatcher executor: fn(tool_name, args) -> result with .ok/.output/.parsed/.error."""
    global _EXECUTOR
    _EXECUTOR = fn


def _exec(tool: str, args: dict) -> Any:
    if _EXECUTOR is None:
        return None
    return _EXECUTOR(tool, args)


_CODE_TEMPLATES = {
    "script": (
        '#!/usr/bin/env python3\n"""{name} — generated scaffold."""\n'
        "\n\n"
        "def main() -> int:\n"
        '    print("hello from {name}")\n'
        "    return 0\n"
        "\n\n"
        'if __name__ == "__main__":\n'
        "    raise SystemExit(main())\n"
    ),
    "class": (
        '"""{name} — generated scaffold."""\n'
        "\n\n"
        "class {ClassName}:\n"
        '    """TODO: document."""\n'
        "\n"
        "    def __init__(self) -> None:\n"
        "        pass\n"
        "\n"
        "    def run(self):\n"
        '        """TODO: implement."""\n'
        "        raise NotImplementedError\n"
    ),
    "test": (
        '"""Tests for {name}."""\n'
        "import unittest\n"
        "\n\n"
        "class Test{name_cap}(unittest.TestCase):\n"
        "    def test_smoke(self):\n"
        "        self.assertTrue(True)\n"
        "\n\n"
        'if __name__ == "__main__":\n'
        '    unittest.main()\n'
    ),
    "cli": (
        '#!/usr/bin/env python3\n"""{name} — generated CLI scaffold."""\n'
        "import argparse\n"
        "\n\n"
        "def build_parser() -> argparse.ArgumentParser:\n"
        '    p = argparse.ArgumentParser(description="{name}")\n'
        '    p.add_argument("--name", default="world")\n'
        "    return p\n"
        "\n\n"
        "def main() -> int:\n"
        "    args = build_parser().parse_args()\n"
        '    print(f"hello {{args.name}}")\n'
        "    return 0\n"
        "\n\n"
        'if __name__ == "__main__":\n'
        "    raise SystemExit(main())\n"
    ),
}


def _code_generate(args: dict) -> CodeResult:
    """Render a code scaffold template (script/class/test/cli), syntax-check it, write to file."""
    try:
        kind = str(args.get("kind", "script")).lower()
        name = str(args.get("name", "generated")).strip() or "generated"
        output = args.get("output")
        if kind not in _CODE_TEMPLATES:
            return CodeResult(ok=False,
                error=f"unknown kind '{kind}'; choose from {sorted(_CODE_TEMPLATES)}")
        safe = "".join(c if (c.isalnum() or c in "_-") else "_" for c in name)
        class_name = "".join(p[:1].upper() + p[1:] for p in safe.split("_") if p) or "Generated"
        code = _CODE_TEMPLATES[kind].format(name=safe, ClassName=class_name,
                                           name_cap=class_name)
        extra = args.get("extra_code")
        if extra:
            code += "\n\n" + str(extra)
        try:
            ast.parse(code)
        except SyntaxError as se:
            return CodeResult(ok=False, error=f"template produced invalid Python: {se}")
        if not output:
            output = f"{safe}.py"
        out = Path(output)
        if out.exists() and not args.get("overwrite"):
            return CodeResult(ok=False,
                error=f"{out} exists; pass overwrite=true to replace")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(code, encoding="utf-8")
        return CodeResult(ok=True,
            output=f"generated {kind} scaffold -> {out} ({len(code)} chars, syntax OK)",
            parsed={"path": str(out), "kind": kind, "chars": len(code)})
    except Exception as e:
        return CodeResult(ok=False, error=str(e)[:600])


def _error_log_read(args: dict) -> CodeResult:
    """Tail agent/logs/* with level filter and keyword search."""
    try:
        log_dir = REPO_ROOT / "agent" / "logs"
        name = str(args.get("log", "")).strip()
        if name:
            files = [log_dir / name] if (log_dir / name).exists() else []
            if not files:
                return CodeResult(ok=False, error=f"log not found: {name}")
        else:
            files = sorted(log_dir.glob("*.log")) + sorted(log_dir.glob("*.jsonl"))
        tail = max(1, min(int(args.get("tail", 100)), 5000))
        level = str(args.get("level", "")).upper()
        keyword = str(args.get("keyword", ""))
        hits = []
        for f in files:
            try:
                lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                up = line.upper()
                if level and level not in up and not any(
                        k in up for k in (level,)):
                    continue
                if keyword and keyword not in line:
                    continue
                hits.append(f"[{f.name}] {line}")
        hits = hits[-tail:]
        return CodeResult(ok=True,
            output="\n".join(hits) if hits else "(no matching log lines)",
            parsed={"matches": len(hits), "files": [f.name for f in files],
                    "level": level, "keyword": keyword})
    except Exception as e:
        return CodeResult(ok=False, error=str(e)[:600])


_VERSIONS_FILE = REPO_ROOT / "agent" / "logs" / "tool_versions.json"


def _tool_version(args: dict) -> CodeResult:
    """Get/set/list sidecar versions for registry tools."""
    try:
        action = str(args.get("action", "list")).lower()
        try:
            data = json.loads(_VERSIONS_FILE.read_text()) if _VERSIONS_FILE.exists() else {}
        except Exception:
            data = {}
        if action == "get":
            name = args.get("name", "")
            return CodeResult(ok=True, output=f"{name} = {data.get(name, '1.0.0 (default)')}",
                              parsed={"name": name, "version": data.get(name, "1.0.0")})
        if action == "set":
            name = args.get("name", "")
            version = str(args.get("version", ""))
            if not name or not version:
                return CodeResult(ok=False, error="set needs name and version")
            import re as _re
            if not _re.match(r"^\d+\.\d+\.\d+([.\-+]\w+)*$", version):
                return CodeResult(ok=False, error=f"bad version '{version}' (want X.Y.Z)")
            data[name] = version
            _VERSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
            _VERSIONS_FILE.write_text(json.dumps(data, indent=2))
            return CodeResult(ok=True, output=f"{name} version set to {version}",
                              parsed={"name": name, "version": version})
        if action == "list":
            return CodeResult(ok=True,
                output=json.dumps(data, indent=2) if data else "(no overrides; all tools at registry default 1.0.0)",
                parsed={"versions": data, "count": len(data)})
        return CodeResult(ok=False, error="action must be get|set|list")
    except Exception as e:
        return CodeResult(ok=False, error=str(e)[:600])


def _benchmark_run(args: dict) -> CodeResult:
    """Run a registered tool N times via the dispatcher executor; report timing stats."""
    try:
        tool = args.get("tool")
        if not tool:
            return CodeResult(ok=False, error="tool is required")
        if _EXECUTOR is None:
            return CodeResult(ok=False, error=
                "no executor bound (Dispatcher binds set_executor at runtime); "
                "cannot benchmark outside dispatch")
        targs = args.get("args") or {}
        times = max(1, min(int(args.get("times", 5)), 50))
        durations = []
        ok_count = 0
        last_err = ""
        for _ in range(times):
            t0 = time.perf_counter()
            try:
                r = _EXECUTOR(tool, dict(targs))
                ok = bool(getattr(r, "ok", False))
                if ok:
                    ok_count += 1
                else:
                    last_err = str(getattr(r, "error", getattr(r, "output", "")))[:200]
            except Exception as e:
                last_err = str(e)[:200]
            durations.append(time.perf_counter() - t0)
        durations.sort()
        stats = {"runs": times, "ok": ok_count, "failed": times - ok_count,
                 "min_s": round(durations[0], 4), "max_s": round(durations[-1], 4),
                 "mean_s": round(sum(durations) / len(durations), 4),
                 "p50_s": round(durations[len(durations) // 2], 4),
                 "last_error": last_err}
        return CodeResult(ok=ok_count > 0,
            output=(f"{tool} x{times}: ok={ok_count} min={stats['min_s']}s "
                    f"max={stats['max_s']}s mean={stats['mean_s']}s"),
            parsed={"tool": tool, **stats})
    except Exception as e:
        return CodeResult(ok=False, error=str(e)[:600])




def _build_run(args: dict) -> CodeResult:
    """Detect build system (make/npm/gradle/cargo) and run the build."""
    root = Path(str(args.get("root", "."))).expanduser()
    target = str(args.get("target", ""))
    timeout = int(args.get("timeout", 600))
    cmd = None
    label = ""
    if (root / "Makefile").exists() or (root / "makefile").exists():
        cmd, label = ["make"] + ([target] if target else []), "make"
    elif (root / "package.json").exists():
        script = target or "build"
        cmd, label = ["npm", "run", script], "npm"
    elif (root / "build.gradle").exists() or (root / "build.gradle.kts").exists():
        cmd, label = ["gradle"] + ([target] if target else ["build"]), "gradle"
    elif (root / "Cargo.toml").exists():
        cmd, label = ["cargo", "build"], "cargo"
    elif (root / "pyproject.toml").exists() or (root / "setup.py").exists():
        cmd, label = [sys.executable, "-m", "build"], "python-build"
    if not cmd:
        return CodeResult(ok=False, error="no recognized build system in " + str(root))
    if not shutil.which(cmd[0]):
        return CodeResult(ok=False, error=f"build tool '{cmd[0]}' not installed")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=str(root))
    except subprocess.TimeoutExpired:
        return CodeResult(ok=False, error="build timed out")
    ok = r.returncode == 0
    out = (r.stdout + r.stderr)[-5000:]
    logp = root / "build.log"
    try:
        logp.write_text(out, encoding="utf-8")
    except OSError:
        pass
    return CodeResult(ok=ok, output=f"[{label}] rc={r.returncode}\n" + out,
                      parsed={"rc": r.returncode, "system": label, "log": str(logp)})


def _compiler_run(args: dict) -> CodeResult:
    """Compile a source file with the detected compiler (gcc/javac/rustc)."""
    path = str(args.get("path", ""))
    output = str(args.get("output", ""))
    extra = args.get("flags") or []
    if not path:
        return CodeResult(ok=False, error="path required")
    p = Path(path)
    if not p.exists():
        return CodeResult(ok=False, error=f"not found: {path}")
    comp = {"c": "gcc", "cpp": "g++", "cc": "gcc", "java": "javac",
            "rs": "rustc"}.get(p.suffix.lstrip("."))
    if not comp:
        return CodeResult(ok=False, error=f"no compiler mapping for {p.suffix}")
    if not shutil.which(comp):
        return CodeResult(ok=False, error=f"compiler '{comp}' not installed")
    cmd = [comp, str(p)] + list(extra)
    if output:
        cmd += ["-o", output]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return CodeResult(ok=False, error="compile timed out")
    return CodeResult(ok=r.returncode == 0,
                      output=(r.stdout + r.stderr)[-3000:] or "compiled ok",
                      parsed={"rc": r.returncode, "compiler": comp})


def _debugger_run(args: dict) -> CodeResult:
    """Scripted pdb run: breakpoints + commands, non-interactive."""
    path = str(args.get("path", ""))
    breakpoints = args.get("breakpoints") or []
    commands = args.get("commands") or ["c"]
    timeout = int(args.get("timeout", 120))
    if not path:
        return CodeResult(ok=False, error="path required")
    script = "".join(f"b {b}\n" for b in breakpoints)
    script += "".join(c + "\n" for c in commands) + "q\n"
    try:
        r = subprocess.run([sys.executable, "-m", "pdb", path],
                           input=script, capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return CodeResult(ok=False, error="debugger run timed out")
    return CodeResult(ok=True, output=(r.stdout + r.stderr)[-5000:],
                      parsed={"note": "scripted pdb; for live debugging use an IDE"})


def _env_detect(args: dict) -> CodeResult:
    """Detect OS, python, and key dev tools on PATH."""
    info = {"os": os.name, "platform": sys.platform,
            "python": sys.version.split()[0]}
    try:
        info["lsb"] = subprocess.run(["lsb_release", "-ds"], capture_output=True,
                                     text=True, timeout=10).stdout.strip()
    except Exception:
        pass
    for tool in ("git", "gcc", "g++", "javac", "rustc", "node", "npm", "go",
                 "docker", "kubectl", "terraform", "make", "cmake", "pytest"):
        info[tool] = shutil.which(tool) or ""
    info["cpu_count"] = os.cpu_count()
    return CodeResult(ok=True, output=json.dumps(info, indent=2)[:2000],
                      parsed=info)


def _checkpoint_create(args: dict) -> CodeResult:
    """Tar snapshot of a workspace dir (excludes .git/__pycache__)."""
    import tarfile
    root = Path(str(args.get("root", "."))).expanduser()
    dest = Path(str(args.get("dest", f"./checkpoint_{int(__import__('time').time())}.tar.gz"))).expanduser()
    def _filter(ti):
        if ".git" in ti.name.split("/") or "__pycache__" in ti.name:
            return None
        return ti
    try:
        with tarfile.open(dest, "w:gz") as tf:
            tf.add(root, arcname=root.name, filter=_filter)
    except Exception as e:
        return CodeResult(ok=False, error=f"checkpoint failed: {e}")
    return CodeResult(ok=True, output=f"checkpoint -> {dest}",
                      parsed={"dest": str(dest)})


def _checkpoint_restore(args: dict) -> CodeResult:
    """Restore a tar.gz checkpoint over a directory (with confirm)."""
    import tarfile
    path = str(args.get("path", ""))
    dest = Path(str(args.get("dest", "."))).expanduser()
    if not path or not Path(path).exists():
        return CodeResult(ok=False, error="valid path required")
    if str(args.get("confirm", "")) != "RESTORE":
        return CodeResult(ok=False,
                          error="pass confirm='RESTORE' (overwrites files in dest)")
    try:
        with tarfile.open(path, "r:gz") as tf:
            tf.extractall(dest)
    except Exception as e:
        return CodeResult(ok=False, error=f"restore failed: {e}")
    return CodeResult(ok=True, output=f"restored {path} -> {dest}",
                      parsed={"dest": str(dest)})


def _build_log_collect(args: dict) -> CodeResult:
    """Collect build logs: build.log files + last build_run output."""
    root = Path(str(args.get("root", "."))).expanduser()
    tail = int(args.get("tail", 200))
    chunks = []
    for logf in list(root.rglob("build.log"))[:5]:
        try:
            lines = logf.read_text(encoding="utf-8", errors="replace").splitlines()
            chunks.append(f"== {logf} ==\n" + "\n".join(lines[-tail:]))
        except Exception:
            pass
    return CodeResult(ok=True,
                      output="\n\n".join(chunks)[:6000] or "no build.log files found",
                      parsed={"logs": len(chunks)})


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
    "code_generate": _code_generate,
    "error_log_read": _error_log_read,
    "tool_version": _tool_version,
    "benchmark_run": _benchmark_run,
    "build_run": _build_run,
    "compiler_run": _compiler_run,
    "debugger_run": _debugger_run,
    "env_detect": _env_detect,
    "checkpoint_create": _checkpoint_create,
    "checkpoint_restore": _checkpoint_restore,
    "build_log_collect": _build_log_collect,

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
    ("code_generate", "Render a code scaffold template (script/class/test/cli), syntax-check, write file",
     {"kind": _sch("string", False, "script|class|test|cli (default script)"),
      "name": _sch("string", False, "Base name (default generated)"),
      "output": _sch("string", False, "Output path (default <name>.py)"),
      "extra_code": _sch("string", False, "Extra Python appended to the scaffold"),
      "overwrite": _sch("boolean", False, "Replace existing file")},
     "computer", 60),
    ("error_log_read", "Tail agent/logs/* with level filter and keyword search",
     {"log": _sch("string", False, "Log filename (default: all)"),
      "tail": _sch("integer", False, "Max lines (default 100)"),
      "level": _sch("string", False, "ERROR|WARNING|INFO filter"),
      "keyword": _sch("string", False, "Substring filter")},
     "computer", 30),
    ("tool_version", "Get/set/list sidecar versions for registry tools",
     {"action": _sch("string", False, "get|set|list (default list)"),
      "name": _sch("string", False, "Tool name"),
      "version": _sch("string", False, "X.Y.Z for set")},
     "computer", 30),
    ("benchmark_run", "Run a registered tool N times; report min/max/mean timings",
     {"tool": _sch("string", True, "Tool name"),
      "args": _sch("object", False, "Args per run"),
      "times": _sch("integer", False, "Runs 1-50 (default 5)")},
     "computer", 600),

    ("build_run", "Detect build system and run the build (log to build.log)",
     {"root": _sch("string", False, "Project root"),
      "target": _sch("string", False, "Target/script"),
      "timeout": _sch("integer", False, "Seconds")},
     "computer", 900),
    ("compiler_run", "Compile a file with gcc/g++/javac/rustc",
     {"path": _sch("string", True, "Source file"),
      "output": _sch("string", False, "Output binary"),
      "flags": _sch("array", False, "Extra flags")},
     "computer", 300),
    ("debugger_run", "Scripted non-interactive pdb run",
     {"path": _sch("string", True, "Python script"),
      "breakpoints": _sch("array", False, "Line numbers"),
      "commands": _sch("array", False, "pdb commands"),
      "timeout": _sch("integer", False, "Seconds")},
     "computer", 300),
    ("env_detect", "Detect OS, python, and dev tools on PATH",
     {},
     "computer", 30),
    ("checkpoint_create", "Tar.gz snapshot of a workspace dir",
     {"root": _sch("string", False, "Dir"),
      "dest": _sch("string", False, "Output .tar.gz")},
     "computer", 600),
    ("checkpoint_restore", "Restore a checkpoint (confirm='RESTORE')",
     {"path": _sch("string", True, "Checkpoint .tar.gz"),
      "dest": _sch("string", False, "Destination dir"),
      "confirm": _sch("string", True, "Must be RESTORE")},
     "computer", 600),
    ("build_log_collect", "Collect build.log files under a root",
     {"root": _sch("string", False, "Root"),
      "tail": _sch("integer", False, "Lines per log")},
     "computer", 60),

]