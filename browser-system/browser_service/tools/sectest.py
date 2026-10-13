"""Tool: sectest — authorized security testing probes.

STRICTLY authorized-targets/program-scope only: every handler runs the scope
guard FIRST and refuses out-of-scope targets with a clear error. Nothing is
sent to an unauthorized target, ever.

Coverage (detection-only, no exploitation):
- xss_probe: reflected-XSS canary probe (benign marker, checks reflection +
  context: HTML/attribute/JS).
- sqli_probe: error-based + blind boolean-based + time-based detection using
  benign payloads and response differential analysis.
- ssrf_check: in-band SSRF surface analysis (parameter discovery for URL-like
  inputs). Out-of-band callbacks are unavailable in the free tier — reported.
- idor_check: insecure-direct-object-reference pattern analysis; requires the
  caller to hold an authorized session (never steals/uses other sessions).
- csrf_check: form analysis for missing/weak anti-CSRF tokens.
- nmap_scan / zap_scan: thin wrappers — run ONLY if the binary is installed;
  otherwise return an honest "not installed" result (never fake output).

Free-first: everything here works with stdlib + httpx + bs4. No paid APIs.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from typing import Any, Dict, List
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import httpx
from bs4 import BeautifulSoup

from browser_manager import BrowserError
from registry import ToolContext, ToolRegistry
from ._common import client_kwargs as _common_client_kwargs
from scope_guard import check_target_scope, scope_denied_result

UA = "ahsanAgentBrowser/1.0 (authorized-security-testing)"


def _client(ctx: ToolContext, timeout: float = 25) -> httpx.AsyncClient:
    s = ctx.settings
    proxy = s.https_proxy or s.http_proxy
    proxies = {"http://": proxy, "https://": proxy} if proxy else None
    return httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                             **_common_client_kwargs(proxies), trust_env=False, headers={"User-Agent": UA})


def _require_scope(tool: str, target: str) -> tuple[bool, Dict[str, Any], str]:
    """Returns (ok, denial_result, scope_reason)."""
    ok, reason = check_target_scope(target)
    if not ok:
        return False, scope_denied_result(tool, target, reason), ""
    return True, {}, reason


async def _emit(ctx, task_id, event, **kw):
    await ctx.emit(task_id or "sectest", {"event": event, **kw})


def _with_param(url: str, param: str, value: str) -> str:
    p = urlparse(url)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    q[param] = value
    return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(q), p.fragment))


_CANARY = "s3cpr0b3canary"


# ---------------------------------------------------------------------------
# XSS probe (reflected, benign canary)
# ---------------------------------------------------------------------------

async def _xss_probe(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    url = args["url"]
    ok, denial, scope_reason = _require_scope("xss_probe", url)
    if not ok:
        return denial
    task_id = args.get("task_id")
    p = urlparse(url)
    params = [k for k, _ in parse_qsl(p.query, keep_blank_values=True)]
    if not params and args.get("param"):
        params = [args["param"]]
    if not params:
        return {"ok": True, "tool": "xss_probe", "url": url, "scope": scope_reason,
                "findings": [], "note": "No query parameters to test on this URL."}
    await _emit(ctx, task_id, "xss_start", url=url, params=params)
    findings: List[Dict[str, Any]] = []
    marker = f"{_CANARY}<b>{_CANARY}</b>"
    try:
        async with _client(ctx) as client:
            for param in params[:10]:
                test_url = _with_param(url, param, marker)
                try:
                    r = await client.get(test_url)
                    body = r.text
                    if _CANARY not in body:
                        continue
                    # Context analysis: where did the canary land?
                    contexts = []
                    if f"<b>{_CANARY}</b>" in body:
                        contexts.append("html-context (unencoded tag reflection)")
                    for m in re.finditer(re.escape(_CANARY), body):
                        start = max(0, m.start() - 120)
                        snippet = body[start:m.start() + len(_CANARY) + 40]
                        if "<script" in snippet.lower():
                            contexts.append("script-context")
                            break
                    if re.search(r'=\s*["\'][^"\']*' + re.escape(_CANARY), body):
                        contexts.append("attribute-context")
                    findings.append({"param": param, "reflected": True,
                                     "contexts": sorted(set(contexts)) or ["unknown-context"],
                                     "severity_hint": "potential reflected XSS — verify manually"})
                except Exception as e:
                    findings.append({"param": param, "error": str(e)})
    except Exception as e:
        raise BrowserError(f"xss probe failed: {e}", code="probe_failed") from e
    await _emit(ctx, task_id, "xss_done", url=url, findings=len(findings))
    return {"ok": True, "tool": "xss_probe", "url": url, "scope": scope_reason,
            "findings": findings,
            "note": "Benign canary only; no payload execution. Verify manually before reporting."}


# ---------------------------------------------------------------------------
# SQLi probe: error-based + blind boolean-based + time-based
# ---------------------------------------------------------------------------

_SQLI_ERROR_PATTERNS = [
    r"SQL syntax.*MySQL", r"Warning.*mysql_.*", r"MySQLSyntaxErrorException",
    r"ORA-[0-9]{5}", r"PostgreSQL.*ERROR", r"SQLite.*error",
    r"Microsoft.*ODBC.*SQL Server", r"Unclosed quotation mark",
    r"quoted string not properly terminated",
]

_BLIND_TRUE = "' AND '1'='1"
_BLIND_FALSE = "' AND '1'='2"
_TIME_PAYLOAD_TPL = "' AND (SELECT * FROM (SELECT(SLEEP({d})))x)='"
_TIME_PAYLOAD_MSSQL = "'; WAITFOR DELAY '0:0:{d}'--"


async def _sqli_probe(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    url = args["url"]
    ok, denial, scope_reason = _require_scope("sqli_probe", url)
    if not ok:
        return denial
    task_id = args.get("task_id")
    p = urlparse(url)
    params = [k for k, _ in parse_qsl(p.query, keep_blank_values=True)]
    if not params and args.get("param"):
        params = [args["param"]]
    if not params:
        return {"ok": True, "tool": "sqli_probe", "url": url, "scope": scope_reason,
                "findings": [], "note": "No query parameters to test on this URL."}
    include_time = bool(args.get("include_time_based", True))
    time_delay = min(int(args.get("time_delay_sec", 3)), 5)
    await _emit(ctx, task_id, "sqli_start", url=url, params=params,
                time_based=include_time)
    findings: List[Dict[str, Any]] = []
    try:
        async with _client(ctx, timeout=30) as client:
            # Baseline per param (also calibrates "normal" response time).
            for param in params[:8]:
                entry: Dict[str, Any] = {"param": param, "signals": []}
                try:
                    t0 = time.monotonic()
                    base_r = await client.get(url)
                    base_time = time.monotonic() - t0
                    base_body = base_r.text
                    base_len = len(base_body)

                    # 1) Error-based: single quote.
                    r1 = await client.get(_with_param(url, param, "'"))
                    for pat in _SQLI_ERROR_PATTERNS:
                        if re.search(pat, r1.text, re.IGNORECASE):
                            entry["signals"].append(
                                {"type": "error-based",
                                 "detail": f"DB error pattern matched: {pat[:40]}"})
                            break

                    # 2) Blind boolean-based: TRUE vs FALSE response differential.
                    r_true = await client.get(_with_param(url, param, _BLIND_TRUE))
                    r_false = await client.get(_with_param(url, param, _BLIND_FALSE))
                    len_diff = abs(len(r_true.text) - len(r_false.text))
                    base_diff = abs(len(r_true.text) - base_len)
                    # Heuristic: TRUE and FALSE differ from each other more than
                    # TRUE differs from baseline => boolean inference possible.
                    if len_diff > max(30, base_diff * 2) and len_diff > 0:
                        entry["signals"].append(
                            {"type": "blind-boolean",
                             "detail": f"TRUE/FALSE responses differ by {len_diff} bytes "
                                      f"(baseline delta {base_diff}) — boolean inference possible"})

                    # 3) Blind time-based: SLEEP payload vs baseline timing.
                    if include_time:
                        t1 = time.monotonic()
                        await client.get(_with_param(
                            url, param, _TIME_PAYLOAD_TPL.format(d=time_delay)))
                        elapsed = time.monotonic() - t1
                        # Allow 40% tolerance under the injected delay.
                        if elapsed >= time_delay * 0.6 and elapsed > base_time + time_delay * 0.5:
                            entry["signals"].append(
                                {"type": "blind-time",
                                 "detail": f"response took {elapsed:.1f}s vs baseline "
                                          f"{base_time:.1f}s with {time_delay}s SLEEP payload — "
                                          "time-based inference possible"})
                except Exception as e:
                    entry["signals"].append({"type": "error", "detail": str(e)[:200]})
                if entry["signals"]:
                    findings.append(entry)
    except Exception as e:
        raise BrowserError(f"sqli probe failed: {e}", code="probe_failed") from e
    await _emit(ctx, task_id, "sqli_done", url=url, params_with_signals=len(findings))
    return {"ok": True, "tool": "sqli_probe", "url": url, "scope": scope_reason,
            "coverage": ["error-based", "blind-boolean-based"] +
                        (["blind-time-based"] if include_time else []),
            "findings": findings,
            "note": "Detection heuristics only — benign payloads, no data extraction. "
                    "Verify manually before reporting."}


# ---------------------------------------------------------------------------
# SSRF check (in-band surface analysis)
# ---------------------------------------------------------------------------

_URL_PARAM_HINTS = ("url", "uri", "link", "redirect", "next", "callback",
                    "webhook", "fetch", "file", "path", "image", "avatar")


async def _ssrf_check(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    url = args["url"]
    ok, denial, scope_reason = _require_scope("ssrf_check", url)
    if not ok:
        return denial
    task_id = args.get("task_id")
    await _emit(ctx, task_id, "ssrf_start", url=url)
    p = urlparse(url)
    params = [k for k, _ in parse_qsl(p.query, keep_blank_values=True)]
    candidates = [x for x in params if any(h in x.lower() for h in _URL_PARAM_HINTS)]
    # Also fetch the page and look for URL-like inputs in forms.
    try:
        async with _client(ctx) as client:
            r = await client.get(url)
            soup = BeautifulSoup(r.text, "html.parser")
            for form in soup.find_all("form"):
                for inp in form.find_all(["input", "textarea"]):
                    name = (inp.get("name") or "").lower()
                    if name and any(h in name for h in _URL_PARAM_HINTS) and name not in candidates:
                        candidates.append(inp.get("name"))
    except Exception as e:
        return {"ok": True, "tool": "ssrf_check", "url": url, "scope": scope_reason,
                "candidates": [], "note": f"page fetch failed: {e}"}
    await _emit(ctx, task_id, "ssrf_done", url=url, candidates=len(candidates))
    return {"ok": True, "tool": "ssrf_check", "url": url, "scope": scope_reason,
            "url_like_parameters": candidates,
            "note": "In-band surface analysis only. Out-of-band callbacks are "
                    "unavailable in the free tier (no collaborator server). "
                    "Test candidates manually with your own OOB infrastructure."}


# ---------------------------------------------------------------------------
# IDOR check (pattern analysis; authorized session required)
# ---------------------------------------------------------------------------

_ID_PARAM_HINTS = ("id", "user_id", "userid", "account", "order", "invoice",
                   "doc", "file", "record", "profile")


async def _idor_check(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    url = args["url"]
    ok, denial, scope_reason = _require_scope("idor_check", url)
    if not ok:
        return denial
    if not args.get("authorized_session_note"):
        return {"ok": False, "tool": "idor_check", "url": url, "scope": scope_reason,
                "error": "missing authorized_session_note",
                "reason": "IDOR testing needs an authorized test session (your own "
                          "bug-bounty test account). Pass authorized_session_note "
                          "describing the session you control. Never test with "
                          "other users' sessions."}
    task_id = args.get("task_id")
    await _emit(ctx, task_id, "idor_start", url=url)
    p = urlparse(url)
    params = dict(parse_qsl(p.query, keep_blank_values=True))
    id_params = [k for k in params if any(h in k.lower() for h in _ID_PARAM_HINTS)]
    numeric = {k: v for k, v in params.items() if v.isdigit()}
    await _emit(ctx, task_id, "idor_done", url=url)
    return {"ok": True, "tool": "idor_check", "url": url, "scope": scope_reason,
            "id_like_parameters": id_params,
            "numeric_parameters": numeric,
            "guidance": "Try sequential/guessable values ONLY within your authorized "
                        "test accounts. Compare responses across two accounts you own. "
                        "This tool performs pattern analysis only — it does not "
                        "access other users' objects.",
            "note": "Manual verification required before reporting."}


# ---------------------------------------------------------------------------
# CSRF check (form token analysis)
# ---------------------------------------------------------------------------

async def _csrf_check(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    url = args["url"]
    ok, denial, scope_reason = _require_scope("csrf_check", url)
    if not ok:
        return denial
    task_id = args.get("task_id")
    await _emit(ctx, task_id, "csrf_start", url=url)
    forms: List[Dict[str, Any]] = []
    try:
        async with _client(ctx) as client:
            r = await client.get(url)
            soup = BeautifulSoup(r.text, "html.parser")
            for form in soup.find_all("form"):
                action = form.get("action", "")
                method = (form.get("method") or "get").upper()
                inputs = [i.get("name") for i in form.find_all("input") if i.get("name")]
                token_names = [n for n in inputs
                               if any(t in n.lower() for t in ("csrf", "token", "nonce", "_token"))]
                forms.append({"action": action, "method": method,
                              "has_token": bool(token_names),
                              "token_fields": token_names,
                              "risk": "low" if token_names or method == "GET"
                                      else "potential missing CSRF token on state-changing form"})
    except Exception as e:
        raise BrowserError(f"csrf check failed: {e}", code="probe_failed") from e
    await _emit(ctx, task_id, "csrf_done", url=url, forms=len(forms))
    return {"ok": True, "tool": "csrf_check", "url": url, "scope": scope_reason,
            "forms": forms,
            "note": "Static form analysis only; SameSite cookie attributes and "
                    "referer checks need manual review."}


# ---------------------------------------------------------------------------
# Nmap / ZAP wrappers (only when installed — never fake output)
# ---------------------------------------------------------------------------

def _run_tool_binary(name: str, argv: List[str], timeout: int = 120) -> Dict[str, Any]:
    if not shutil.which(name):
        return {"ok": False, "installed": False,
                "note": f"{name} is not installed on this host. Install it to enable this tool."}
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return {"ok": proc.returncode == 0, "installed": True,
                "returncode": proc.returncode,
                "stdout": proc.stdout[-8000:], "stderr": proc.stderr[-2000:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "installed": True, "note": f"{name} timed out after {timeout}s."}
    except Exception as e:
        return {"ok": False, "installed": True, "note": f"{name} failed: {e}"}


async def _nmap_scan(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    target = args["target"]
    ok, denial, scope_reason = _require_scope("nmap_scan", target)
    if not ok:
        return denial
    host = urlparse(target if "://" in target else "https://" + target).hostname or target
    profile = args.get("profile", "fast")  # fast | service | full
    argv = {"fast": ["nmap", "-F", host],
            "service": ["nmap", "-sV", "-F", host],
            "full": ["nmap", "-sV", "-p-", "--max-retries", "1", host]}.get(profile, ["nmap", "-F", host])
    task_id = args.get("task_id")
    await _emit(ctx, task_id, "nmap_start", host=host, profile=profile)
    res = await ctx.run_in_executor(_run_tool_binary, "nmap", argv, 180) \
        if hasattr(ctx, "run_in_executor") else _run_tool_binary("nmap", argv, 180)
    await _emit(ctx, task_id, "nmap_done", host=host)
    return {"ok": res.get("ok", False), "tool": "nmap_scan", "host": host,
            "scope": scope_reason, **res}


async def _zap_scan(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    target = args["target"]
    ok, denial, scope_reason = _require_scope("zap_scan", target)
    if not ok:
        return denial
    scan_type = args.get("scan_type", "baseline")  # baseline | quick
    argv = ["zap-baseline.py", "-t", target, "-r", "zap_report.html"] \
        if scan_type == "baseline" else ["zap-cli", "quick-scan", target]
    task_id = args.get("task_id")
    await _emit(ctx, task_id, "zap_start", target=target, scan_type=scan_type)
    res = _run_tool_binary("zap-baseline.py" if scan_type == "baseline" else "zap-cli",
                           argv, 600)
    await _emit(ctx, task_id, "zap_done", target=target)
    return {"ok": res.get("ok", False), "tool": "zap_scan", "target": target,
            "scope": scope_reason, **res}


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

def _schema(props: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"type": "object",
            "properties": {"task_id": {"type": "string",
                                       "description": "Optional: attach to a task's history."},
                           **props},
            "required": required}


_URL_PROP = {"url": {"type": "string", "description": "Target URL (must be in authorized scope)."}}


def register(registry: ToolRegistry) -> None:
    specs = [
        ("xss_probe",
         "AUTHORIZED TARGETS ONLY: reflected-XSS canary probe (benign marker, context analysis).",
         _schema({**_URL_PROP, "param": {"type": "string"}}, ["url"]), _xss_probe),
        ("sqli_probe",
         "AUTHORIZED TARGETS ONLY: SQLi detection — error-based + blind boolean-based + blind time-based (benign payloads).",
         _schema({**_URL_PROP, "param": {"type": "string"},
                  "include_time_based": {"type": "boolean", "default": True},
                  "time_delay_sec": {"type": "integer", "default": 3, "maximum": 5}}, ["url"]),
         _sqli_probe),
        ("ssrf_check",
         "AUTHORIZED TARGETS ONLY: in-band SSRF surface analysis (URL-like parameter discovery).",
         _schema(_URL_PROP, ["url"]), _ssrf_check),
        ("idor_check",
         "AUTHORIZED TARGETS ONLY: IDOR pattern analysis; requires authorized_session_note (your own test session).",
         _schema({**_URL_PROP,
                  "authorized_session_note": {"type": "string",
                    "description": "Describe the authorized test session you control."}}, ["url"]),
         _idor_check),
        ("csrf_check",
         "AUTHORIZED TARGETS ONLY: form analysis for missing/weak anti-CSRF tokens.",
         _schema(_URL_PROP, ["url"]), _csrf_check),
        ("nmap_scan",
         "AUTHORIZED TARGETS ONLY: Nmap wrapper (runs only if nmap installed; honest 'not installed' otherwise).",
         _schema({"target": {"type": "string"},
                  "profile": {"type": "string", "enum": ["fast", "service", "full"], "default": "fast"}},
                 ["target"]), _nmap_scan),
        ("zap_scan",
         "AUTHORIZED TARGETS ONLY: OWASP ZAP wrapper (runs only if ZAP installed; honest 'not installed' otherwise).",
         _schema({"target": {"type": "string"},
                  "scan_type": {"type": "string", "enum": ["baseline", "quick"], "default": "baseline"}},
                 ["target"]), _zap_scan),
    ]
    for name, desc, schema, handler in specs:
        registry.register(name=name, description=desc, schema=schema, handler=handler,
                           tags=["sectest", "scope-checked", "authorized-only", "no-browser-needed"])
