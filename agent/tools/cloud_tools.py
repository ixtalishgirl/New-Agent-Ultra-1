"""Cloud & Infrastructure tools.

Real implementations where the host allows: local CI workflow validation
(and `act` execution when installed), local deploy previews (build + serve),
HTTP uptime checks, Terraform/YAML validation, Kubernetes manifest schema
checks, sqlite migration dry-runs, backup verification, and webhook alerts.
Cloud-provider actions without credentials return honest notes, never fake
success.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_TIMEOUT = 300
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass
class CloudResult:
    ok: bool
    output: str = ""
    parsed: dict = field(default_factory=dict)
    error: str = ""


def _fail(msg: str) -> CloudResult:
    return CloudResult(ok=False, error=msg, output=msg)


def _sch(t: str, req: bool, desc: str) -> dict:
    return {"type": t, "required": req, "description": desc}


def _yaml_load(text: str):
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text)
    except ImportError:
        return None


def _cicd_run(args: dict) -> CloudResult:
    root = Path(str(args.get("root", "."))).expanduser()
    wf_dir = root / ".github" / "workflows"
    files = sorted(wf_dir.glob("*.yml")) + sorted(wf_dir.glob("*.yaml"))
    if not files:
        alt = root / ".gitlab-ci.yml"
        if alt.exists():
            files = [alt]
    if not files:
        return _fail("no CI workflow found (.github/workflows or .gitlab-ci.yml)")
    reports = []
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        doc = _yaml_load(text)
        if doc is None:
            reports.append({"file": f.name, "valid_yaml": "unknown (pyyaml missing)",
                           "jobs": []})
            continue
        jobs = list((doc.get("jobs") or {}).keys()) if isinstance(doc, dict) else []
        reports.append({"file": f.name, "valid_yaml": True, "jobs": jobs})
    act = shutil.which("act")
    ran = None
    if act and args.get("execute"):
        try:
            r = subprocess.run([act, "-n"], capture_output=True, text=True,
                               timeout=120, cwd=str(root))
            ran = (r.stdout + r.stderr)[-2000:]
        except Exception as e:
            ran = f"act failed: {e}"
    return CloudResult(ok=True,
                      output="\n".join(f"{r['file']}: jobs={r['jobs']}" for r in reports)
                      + (f"\nact dry-run:\n{ran}" if ran else
                         "\n(act not installed or execute=false — validation only)"),
                      parsed={"workflows": reports, "act_output": ran})


def _deploy_preview(args: dict) -> CloudResult:
    root = Path(str(args.get("root", "."))).expanduser()
    port = int(args.get("port", 8931))
    seconds = min(int(args.get("seconds", 20)), 120)
    build = str(args.get("build", ""))
    serve_dir = root
    if build:
        try:
            r = subprocess.run(build, shell=True, capture_output=True, text=True,
                               timeout=300, cwd=str(root))
            if r.returncode != 0:
                return CloudResult(ok=False, output="",
                                  error=f"build failed:\n{(r.stdout + r.stderr)[-2000:]}")
        except subprocess.TimeoutExpired:
            return _fail("build timed out")
        for cand in ("dist", "build", "public", "_site"):
            if (root / cand).is_dir():
                serve_dir = root / cand
                break
    # find a free port if busy
    srv = None
    for p in range(port, port + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p),
                                     SimpleHTTPRequestHandler)
            port = p
            break
        except OSError:
            continue
    if srv is None:
        return _fail("no free port found")
    t = threading.Thread(target=lambda: srv.serve_forever(), daemon=True)
    cwd = os.getcwd()
    os.chdir(str(serve_dir))
    try:
        t.start()
        time.sleep(1)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/",
                                        timeout=10) as r:
                body = r.read(4096)
                status = r.status
        except Exception as e:
            status, body = -1, str(e).encode()
        time.sleep(max(seconds - 1, 0))
    finally:
        os.chdir(cwd)
        srv.shutdown()
    ok = status == 200
    return CloudResult(ok=ok,
                      output=f"served {serve_dir} on :{port} for {seconds}s — GET / -> {status}",
                      parsed={"port": port, "status": status,
                              "body_head": body[:200].decode("utf-8", "replace")})


def _uptime_check(args: dict) -> CloudResult:
    urls = args.get("urls") or []
    if not urls:
        return _fail("urls required")
    results = []
    for u in urls:
        t0 = time.time()
        try:
            with urllib.request.urlopen(str(u), timeout=15) as r:
                results.append({"url": u, "status": r.status,
                               "ms": round((time.time() - t0) * 1000, 1),
                               "up": 200 <= r.status < 400})
        except Exception as e:
            results.append({"url": u, "status": -1,
                           "ms": round((time.time() - t0) * 1000, 1),
                           "up": False, "error": f"{type(e).__name__}: {e}"[:120]})
    up = sum(1 for r in results if r["up"])
    return CloudResult(ok=up == len(results),
                      output=f"{up}/{len(results)} up",
                      parsed={"checks": results})


def _iac_validate(args: dict) -> CloudResult:
    path = str(args.get("path", "."))
    p = Path(path)
    tf_files = ([p] if p.is_file() else list(p.rglob("*.tf")))[:50]
    if not tf_files:
        return _fail("no .tf files found")
    tf = shutil.which("terraform")
    results = []
    if tf:
        for f in tf_files:
            try:
                r = subprocess.run([tf, "fmt", "-check", str(f)],
                                   capture_output=True, text=True, timeout=30)
                results.append({"file": str(f), "fmt_ok": r.returncode == 0})
            except Exception as e:
                results.append({"file": str(f), "error": str(e)[:100]})
        try:
            r = subprocess.run([tf, "validate"], capture_output=True, text=True,
                               timeout=60, cwd=str(tf_files[0].parent))
            valid = r.returncode == 0
            detail = (r.stdout + r.stderr)[-1500:]
        except Exception as e:
            valid, detail = False, str(e)
        return CloudResult(ok=valid, output=detail,
                          parsed={"files": results, "validate": valid})
    # fallback: brace balance + block sanity
    issues = []
    for f in tf_files:
        text = f.read_text(encoding="utf-8", errors="replace")
        if text.count("{") != text.count("}"):
            issues.append(f"{f}: unbalanced braces")
        if re.search(r'password\s*=\s*"[^"]+"', text):
            issues.append(f"{f}: possible hardcoded secret")
    return CloudResult(ok=not issues,
                      output=("terraform not installed — heuristic check only. "
                              + ("; ".join(issues) if issues else "no issues found")),
                      parsed={"issues": issues})


def _k8s_validate(args: dict) -> CloudResult:
    path = str(args.get("path", "."))
    p = Path(path)
    files = ([p] if p.is_file() else
             [f for f in p.rglob("*.yaml") if "k8s" in str(f) or "manifest" in str(f)]
             or list(p.rglob("*.yaml"))[:30])
    if not files:
        return _fail("no yaml manifests found")
    required = {"apiVersion", "kind", "metadata"}
    issues = []
    checked = 0
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        docs = _yaml_load(text)
        if docs is None:
            issues.append(f"{f}: pyyaml missing, skipped deep check")
            continue
        for doc in ([docs] if isinstance(docs, dict) else (docs or [])):
            if not isinstance(doc, dict):
                continue
            checked += 1
            missing = required - set(doc.keys())
            if missing:
                issues.append(f"{f}: {doc.get('kind', '?')} missing {sorted(missing)}")
            if doc.get("kind") == "Deployment":
                cont = (doc.get("spec", {}).get("template", {})
                        .get("spec", {}).get("containers", []))
                if not cont:
                    issues.append(f"{f}: Deployment has no containers")
    return CloudResult(ok=not issues,
                      output=f"checked {checked} manifests, {len(issues)} issues",
                      parsed={"issues": issues[:50],
                              "note": "schema-level check; use kubeval/kubeconform for full validation"})


def _db_migrate_test(args: dict) -> CloudResult:
    migrations = args.get("migrations") or []
    if not migrations:
        return _fail("migrations required (list of SQL strings)")
    db = sqlite3.connect(":memory:")
    applied = []
    try:
        for i, sql in enumerate(migrations):
            try:
                db.executescript(str(sql))
                applied.append(i)
            except Exception as e:
                return CloudResult(ok=False, output="",
                                  error=f"migration {i} failed: {e}",
                                  parsed={"applied": applied, "failed": i})
        tables = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
    finally:
        db.close()
    return CloudResult(ok=True, output=f"{len(applied)} migrations dry-run ok; tables: {tables}",
                      parsed={"applied": applied, "tables": tables,
                              "note": "sqlite dry-run; run against a staging clone for prod parity"})


def _backup_verify(args: dict) -> CloudResult:
    path = str(args.get("path", ""))
    if not path:
        return _fail("path required")
    p = Path(path)
    if not p.exists():
        return _fail(f"not found: {path}")
    if tarfile.is_tarfile(str(p)):
        try:
            with tarfile.open(str(p)) as tf:
                members = tf.getnames()
            return CloudResult(ok=True,
                              output=f"tar valid: {len(members)} members",
                              parsed={"members": len(members),
                                      "sample": members[:10]})
        except Exception as e:
            return _fail(f"tar corrupt: {e}")
    if p.suffix == ".zip":
        import zipfile
        try:
            with zipfile.ZipFile(str(p)) as zf:
                bad = zf.testzip()
            return CloudResult(ok=bad is None,
                              output="zip ok" if bad is None else f"corrupt member: {bad}",
                              parsed={"bad": bad})
        except Exception as e:
            return _fail(f"zip unreadable: {e}")
    return _fail("unsupported backup format (tar/zip)")


def _alert_emit(args: dict) -> CloudResult:
    webhook = str(args.get("webhook", ""))
    message = str(args.get("message", ""))
    severity = str(args.get("severity", "info"))
    if not webhook or not message:
        return _fail("webhook and message required")
    body = json.dumps({"text": f"[{severity}] {message}"}).encode()
    try:
        req = urllib.request.Request(webhook, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return CloudResult(ok=200 <= r.status < 300,
                              output=f"alert sent -> HTTP {r.status}",
                              parsed={"status": r.status})
    except Exception as e:
        return _fail(f"webhook failed: {e}")


def _cloud_note(args: dict) -> CloudResult:
    return CloudResult(ok=True,
                      output=("Cloud provider actions (SDK/resource/cost/log) need "
                              "provider credentials, which are not connected here. "
                              "Connect via the provider's connector, then use its CLI "
                              "(aws/gcloud/az) through the shell tool."),
                      parsed={"guidance": True})


def _staging_note(args: dict) -> CloudResult:
    return CloudResult(ok=True,
                      output=("Staging deployment = deploy_preview against a staging "
                              "clone + uptime_check afterwards. No provider is "
                              "connected, so remote staging is out of scope here."),
                      parsed={"guidance": True})


def _container_note(args: dict) -> CloudResult:
    path = str(args.get("path", "."))
    df = Path(path) / "Dockerfile"
    if df.exists():
        stages = len(re.findall(r"^FROM", df.read_text(), re.M))
        return CloudResult(ok=True,
                          output=f"Dockerfile found ({stages} stages). "
                                 "Container builds are intentionally out of scope "
                                 "(no Docker layer in this agent by design).",
                          parsed={"dockerfile": True, "stages": stages})
    return CloudResult(ok=True,
                      output="No Dockerfile found. Container builds are out of scope "
                             "by design (no Docker layer in this agent).",
                      parsed={"dockerfile": False})


HANDLERS: dict = {
    "cicd_run": _cicd_run,
    "deploy_preview": _deploy_preview,
    "uptime_check": _uptime_check,
    "iac_validate": _iac_validate,
    "k8s_validate": _k8s_validate,
    "db_migrate_test": _db_migrate_test,
    "backup_verify": _backup_verify,
    "alert_emit": _alert_emit,
    "cloud_note": _cloud_note,
    "staging_note": _staging_note,
    "container_note": _container_note,
}

TOOL_DEFS: list[tuple] = [
    ("cicd_run", "Validate CI workflows; run with `act` if installed",
     {"root": _sch("string", False, "Repo root"),
      "execute": _sch("boolean", False, "Run via act (default false)")}, "computer", 300),
    ("deploy_preview", "Optional build, then serve locally and smoke-test",
     {"root": _sch("string", False, "Project root"),
      "build": _sch("string", False, "Build command"),
      "port": _sch("integer", False, "Default 8931"),
      "seconds": _sch("integer", False, "Serve seconds (default 20)")}, "computer", 600),
    ("uptime_check", "HTTP up/down checks for URLs",
     {"urls": _sch("array", True, "URLs")}, "computer", 120),
    ("iac_validate", "terraform validate/fmt or heuristic check",
     {"path": _sch("string", False, "File or dir")}, "computer", 120),
    ("k8s_validate", "Kubernetes manifest schema sanity checks",
     {"path": _sch("string", False, "File or dir")}, "computer", 60),
    ("db_migrate_test", "Dry-run SQL migrations on throwaway sqlite",
     {"migrations": _sch("array", True, "SQL strings")}, "computer", 120),
    ("backup_verify", "Validate tar/zip backup integrity",
     {"path": _sch("string", True, "Backup file")}, "computer", 300),
    ("alert_emit", "POST an alert to a webhook URL",
     {"webhook": _sch("string", True, "Webhook URL"),
      "message": _sch("string", True, "Message"),
      "severity": _sch("string", False, "info|warning|critical")}, "computer", 30),
    ("cloud_note", "Guidance: cloud provider actions need connected credentials",
     {}, "computer", 10),
    ("staging_note", "Guidance: staging via deploy_preview + uptime_check",
     {}, "computer", 10),
    ("container_note", "Dockerfile presence check; builds out of scope by design",
     {"path": _sch("string", False, "Dir")}, "computer", 20),
]
