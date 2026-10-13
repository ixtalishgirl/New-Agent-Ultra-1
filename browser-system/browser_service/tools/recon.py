"""Tool: recon — passive + authorized active reconnaissance.

Passive (unrestricted per standing grant): DNS records, WHOIS/RDAP, TLS cert
inspection, subdomain enumeration via cert transparency (crt.sh, free),
wayback/archive URL discovery (web.archive.org CDX, free), tech fingerprinting
(headers + meta tags + common JS libraries).

Active (STRICTLY authorized-targets/program-scope only — scope guard,
fail-closed): TCP port/service discovery, endpoint + parameter discovery.

Free-first: no API keys anywhere. Optional `dnspython` / `python-whois`
are used when installed, otherwise stdlib + free HTTP APIs.
"""
from __future__ import annotations

import re
import shutil
import socket
import ssl
import time
from typing import Any, Dict, List
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from browser_manager import BrowserError
from registry import ToolContext, ToolRegistry
from ._common import client_kwargs as _common_client_kwargs
from scope_guard import check_target_scope, scope_denied_result

UA = "ahsanAgentBrowser/1.0 (recon; passive)"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _host(target: str) -> str:
    t = target.strip()
    if "://" not in t:
        t = "https://" + t
    h = urlparse(t).hostname or ""
    if not h:
        raise BrowserError(f"invalid target: {target}", code="invalid_argument")
    return h.lower()


def _client(ctx: ToolContext, timeout: float = 20) -> httpx.AsyncClient:
    s = ctx.settings
    proxy = s.https_proxy or s.http_proxy
    proxies = {"http://": proxy, "https://": proxy} if proxy else None
    return httpx.AsyncClient(timeout=timeout, follow_redirects=True, trust_env=False,
                             **_common_client_kwargs(proxies), headers={"User-Agent": UA})


async def _emit(ctx, task_id, event, **kw):
    await ctx.emit(task_id or "recon", {"event": event, **kw})


# ---------------------------------------------------------------------------
# passive tools (unrestricted)
# ---------------------------------------------------------------------------

async def _dns_records(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    host = _host(args["target"])
    task_id = args.get("task_id")
    await _emit(ctx, task_id, "dns_start", host=host)
    out: Dict[str, Any] = {"host": host, "records": {}}
    # A / AAAA via stdlib (always available)
    try:
        infos = socket.getaddrinfo(host, None)
        out["records"]["A_AAAA"] = sorted({i[4][0] for i in infos})
    except socket.gaierror as e:
        out["records"]["A_AAAA"] = []
        out["records"]["resolve_error"] = str(e)
    # MX / TXT / NS / CNAME via dnspython when present
    try:
        import dns.resolver  # type: ignore
        r = dns.resolver.Resolver()
        r.lifetime = 8
        for rtype in ("MX", "TXT", "NS", "CNAME"):
            try:
                ans = r.resolve(host, rtype)
                out["records"][rtype] = [str(x).strip('"') for x in ans]
            except Exception:
                out["records"][rtype] = []
        out["records"]["via"] = "dnspython"
    except ImportError:
        out["records"]["via"] = "stdlib (install dnspython for MX/TXT/NS/CNAME)"
    await _emit(ctx, task_id, "dns_done", host=host)
    return {"ok": True, **out}


async def _whois_lookup(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    host = _host(args["target"])
    task_id = args.get("task_id")
    await _emit(ctx, task_id, "whois_start", host=host)
    # Prefer RDAP (free HTTPS, no key, no extra dep).
    result: Dict[str, Any] = {"host": host}
    try:
        async with _client(ctx) as client:
            r = await client.get(f"https://rdap.org/domain/{host}")
            if r.status_code == 200:
                d = r.json()
                result["registrar"] = (d.get("entities") or [{}])[0].get("vcardArray", [None, []])[1] if d.get("entities") else None
                result["creation"] = next((e["eventDate"] for e in d.get("events", []) if e.get("eventAction") == "registration"), None)
                result["expiry"] = next((e["eventDate"] for e in d.get("events", []) if e.get("eventAction") == "expiration"), None)
                result["status"] = d.get("status")
                result["nameservers"] = [ns.get("ldhName") for ns in d.get("nameservers", [])]
                result["via"] = "rdap.org"
            else:
                result["via"] = f"rdap.org HTTP {r.status_code}"
    except Exception as e:
        result["via"] = f"rdap failed: {e}"
    # Fallback: python-whois when installed.
    if "registrar" not in result or not result.get("registrar"):
        try:
            import whois  # type: ignore
            w = whois.whois(host)
            result["registrar"] = w.registrar
            result["creation"] = str(w.creation_date)
            result["expiry"] = str(w.expiration_date)
            result["nameservers"] = w.name_servers
            result["via"] = (result.get("via", "") + " + python-whois").strip(" +")
        except ImportError:
            pass
        except Exception as e:
            result["whois_error"] = str(e)
    await _emit(ctx, task_id, "whois_done", host=host)
    return {"ok": True, **result}


async def _tls_cert(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    host = _host(args["target"])
    port = int(args.get("port", 443))
    task_id = args.get("task_id")
    await _emit(ctx, task_id, "tls_start", host=host, port=port)
    out: Dict[str, Any] = {"host": host, "port": port}
    try:
        raw = socket.create_connection((host, port), timeout=15)
        with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as ssock:
            cert = ssock.getpeercert() or {}
            out["subject"] = dict(x[0] for x in cert.get("subject", ()))
            out["issuer"] = dict(x[0] for x in cert.get("issuer", ()))
            out["not_before"] = cert.get("notBefore")
            out["not_after"] = cert.get("notAfter")
            out["san"] = [v for t, v in cert.get("subjectAltName", ()) if t == "DNS"]
            out["tls_version"] = ssock.version()
            out["cipher"] = (ssock.cipher() or [None])[0]
    except Exception as e:
        raise BrowserError(f"TLS inspection failed: {e}", code="tls_failed") from e
    await _emit(ctx, task_id, "tls_done", host=host)
    return {"ok": True, **out}


async def _subdomain_enum(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    """Passive subdomain discovery via cert transparency (crt.sh, free)."""
    host = _host(args["target"])
    task_id = args.get("task_id")
    await _emit(ctx, task_id, "subdomain_enum_start", host=host)
    subs: List[str] = []
    try:
        async with _client(ctx, timeout=30) as client:
            r = await client.get(f"https://crt.sh/?q=%25.{host}&output=json")
            r.raise_for_status()
            for entry in r.json():
                for name in str(entry.get("name_value", "")).splitlines():
                    name = name.strip().lower()
                    if name and "*" not in name:
                        subs.append(name)
    except Exception as e:
        raise BrowserError(f"crt.sh lookup failed: {e}", code="ct_failed") from e
    subs = sorted(set(subs))
    await _emit(ctx, task_id, "subdomain_enum_done", host=host, count=len(subs))
    return {"ok": True, "host": host, "count": len(subs), "subdomains": subs[:500],
            "via": "crt.sh (certificate transparency, passive)"}


async def _wayback_urls(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    """Passive URL discovery via the Wayback Machine CDX API (free)."""
    host = _host(args["target"])
    task_id = args.get("task_id")
    limit = min(int(args.get("limit", 200)), 2000)
    await _emit(ctx, task_id, "wayback_start", host=host)
    urls: List[str] = []
    try:
        async with _client(ctx, timeout=30) as client:
            r = await client.get(
                "https://web.archive.org/cdx/search/cdx",
                params={"url": f"{host}/*", "output": "json", "fl": "original",
                        "collapse": "urlkey", "limit": limit},
            )
            r.raise_for_status()
            data = r.json()
            for row in data[1:]:
                if row and row[0]:
                    urls.append(row[0])
    except Exception as e:
        raise BrowserError(f"wayback CDX lookup failed: {e}", code="wayback_failed") from e
    await _emit(ctx, task_id, "wayback_done", host=host, count=len(urls))
    return {"ok": True, "host": host, "count": len(urls), "urls": urls,
            "via": "web.archive.org CDX (passive)"}


async def _tech_fingerprint(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    """Passive tech fingerprinting: headers, meta tags, JS libraries, cookies."""
    target = args["target"].strip()
    if "://" not in target:
        target = "https://" + target
    task_id = args.get("task_id")
    await _emit(ctx, task_id, "fingerprint_start", url=target)
    out: Dict[str, Any] = {"url": target, "server": None, "tech": [], "js_libs": [],
                           "meta": {}, "cookies": []}
    try:
        async with _client(ctx) as client:
            r = await client.get(target)
            h = {k.lower(): v for k, v in r.headers.items()}
            out["server"] = h.get("server")
            out["powered_by"] = h.get("x-powered-by")
            for sc in r.headers.get_list("set-cookie") if hasattr(r.headers, "get_list") else []:
                out["cookies"].append(sc.split(";")[0])
            soup = BeautifulSoup(r.text, "html.parser")
            gen = soup.find("meta", attrs={"name": "generator"})
            if gen and gen.get("content"):
                out["tech"].append(f"generator:{gen['content']}")
            for name in ("application-name", "framework"):
                m = soup.find("meta", attrs={"name": name})
                if m and m.get("content"):
                    out["tech"].append(f"{name}:{m['content']}")
            for script in soup.find_all("script", src=True):
                src = script["src"]
                for lib in ("jquery", "react", "vue", "angular", "bootstrap",
                            "wordpress", "drupal", "next", "nuxt", "laravel"):
                    if lib in src.lower() and lib not in out["js_libs"]:
                        out["js_libs"].append(lib)
            html = r.text.lower()
            for sig, name in (("wp-content", "wordpress"), ("drupal.js", "drupal"),
                              ("__next", "next.js"), ("ng-version", "angular")):
                if sig in html and name not in out["tech"]:
                    out["tech"].append(name)
    except Exception as e:
        raise BrowserError(f"fingerprint fetch failed: {e}", code="fetch_failed") from e
    await _emit(ctx, task_id, "fingerprint_done", url=target)
    return {"ok": True, **out}


# ---------------------------------------------------------------------------
# active tools (scope-checked, authorized targets ONLY)
# ---------------------------------------------------------------------------

_COMMON_PORTS = [21, 22, 23, 25, 53, 80, 110, 143, 443, 465, 587, 993, 995,
                 1433, 1521, 3306, 3389, 5432, 5900, 6379, 8080, 8443, 27017]


async def _port_scan(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    target = args["target"]
    ok, reason = check_target_scope(target)
    if not ok:
        return scope_denied_result("port_scan", target, reason)
    host = _host(target)
    ports = args.get("ports") or _COMMON_PORTS
    ports = [int(p) for p in ports][:100]
    timeout = min(float(args.get("timeout", 1.5)), 5.0)
    task_id = args.get("task_id")
    await _emit(ctx, task_id, "port_scan_start", host=host, ports=len(ports))

    import asyncio as _aio

    async def _probe(port: int):
        try:
            _, writer = await _aio.wait_for(_aio.open_connection(host, port), timeout)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            return port, True
        except Exception:
            return port, False

    results = await _aio.gather(*(_probe(p) for p in ports))
    open_ports = sorted(p for p, is_open in results if is_open)
    await _emit(ctx, task_id, "port_scan_done", host=host, open=open_ports)
    return {"ok": True, "tool": "port_scan", "host": host,
            "scope": reason, "open_ports": open_ports,
            "scanned": len(ports),
            "note": "TCP connect scan only; no banner grabbing or exploitation."}


_COMMON_PATHS = ["/robots.txt", "/sitemap.xml", "/.git/HEAD", "/.env",
                 "/api", "/api/v1", "/graphql", "/swagger.json",
                 "/openapi.json", "/admin", "/login", "/wp-json/",
                 "/server-status", "/.well-known/security.txt"]


async def _endpoint_discovery(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    target = args["target"]
    ok, reason = check_target_scope(target)
    if not ok:
        return scope_denied_result("endpoint_discovery", target, reason)
    base = target.strip().rstrip("/")
    if "://" not in base:
        base = "https://" + base
    paths = args.get("paths") or _COMMON_PATHS
    paths = [str(p) for p in paths][:60]
    task_id = args.get("task_id")
    await _emit(ctx, task_id, "endpoint_discovery_start", base=base, paths=len(paths))
    found: List[Dict[str, Any]] = []
    try:
        async with _client(ctx, timeout=12) as client:
            for p in paths:
                url = base + (p if p.startswith("/") else "/" + p)
                try:
                    r = await client.get(url)
                    if r.status_code < 400:
                        found.append({"path": p, "status": r.status_code,
                                      "size": len(r.content),
                                      "content_type": r.headers.get("content-type", "")})
                except Exception:
                    continue
    except Exception as e:
        raise BrowserError(f"endpoint discovery failed: {e}", code="discovery_failed") from e
    await _emit(ctx, task_id, "endpoint_discovery_done", base=base, found=len(found))
    params_hint: List[str] = []
    if args.get("params"):
        params_hint = [str(x) for x in args["params"]][:40]
    return {"ok": True, "tool": "endpoint_discovery", "base": base,
            "scope": reason, "found": found,
            "parameter_hints": params_hint,
            "note": "Lightweight GET probing of common paths only; rate-limited by client timeouts."}


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

def _schema(props: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"type": "object",
            "properties": {"task_id": {"type": "string",
                                       "description": "Optional: attach to a task's history."},
                           **props},
            "required": required}


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="dns_records",
        description="Passive DNS enumeration (A/AAAA always; MX/TXT/NS/CNAME when dnspython installed). Unrestricted.",
        schema=_schema({"target": {"type": "string", "description": "Domain or URL."}}, ["target"]),
        handler=_dns_records, tags=["recon", "passive", "no-browser-needed"])
    registry.register(
        name="whois_lookup",
        description="Passive WHOIS via free RDAP (rdap.org), python-whois fallback. Unrestricted.",
        schema=_schema({"target": {"type": "string"}}, ["target"]),
        handler=_whois_lookup, tags=["recon", "passive", "no-browser-needed"])
    registry.register(
        name="tls_cert",
        description="Passive TLS certificate inspection (subject, issuer, SANs, expiry, cipher). Unrestricted.",
        schema=_schema({"target": {"type": "string"}, "port": {"type": "integer", "default": 443}}, ["target"]),
        handler=_tls_cert, tags=["recon", "passive", "no-browser-needed"])
    registry.register(
        name="subdomain_enum",
        description="Passive subdomain enumeration via certificate transparency (crt.sh, free). Unrestricted.",
        schema=_schema({"target": {"type": "string"}}, ["target"]),
        handler=_subdomain_enum, tags=["recon", "passive", "no-browser-needed"])
    registry.register(
        name="wayback_urls",
        description="Passive archived-URL discovery via Wayback CDX API (free). Unrestricted.",
        schema=_schema({"target": {"type": "string"},
                        "limit": {"type": "integer", "default": 200, "maximum": 2000}}, ["target"]),
        handler=_wayback_urls, tags=["recon", "passive", "no-browser-needed"])
    registry.register(
        name="tech_fingerprint",
        description="Passive technology fingerprinting (headers, meta, JS libs). Unrestricted.",
        schema=_schema({"target": {"type": "string"}}, ["target"]),
        handler=_tech_fingerprint, tags=["recon", "passive", "no-browser-needed"])
    registry.register(
        name="port_scan",
        description="AUTHORIZED TARGETS ONLY (scope-checked): TCP connect scan of common ports. Refuses out-of-scope targets.",
        schema=_schema({"target": {"type": "string"},
                        "ports": {"type": "array", "items": {"type": "integer"}},
                        "timeout": {"type": "number", "default": 1.5}}, ["target"]),
        handler=_port_scan, tags=["recon", "active", "scope-checked", "no-browser-needed"])
    registry.register(
        name="endpoint_discovery",
        description="AUTHORIZED TARGETS ONLY (scope-checked): lightweight probing of common paths/params. Refuses out-of-scope targets.",
        schema=_schema({"target": {"type": "string"},
                        "paths": {"type": "array", "items": {"type": "string"}},
                        "params": {"type": "array", "items": {"type": "string"}}}, ["target"]),
        handler=_endpoint_discovery, tags=["recon", "active", "scope-checked", "no-browser-needed"])
