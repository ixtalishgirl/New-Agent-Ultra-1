"""TLS/HTTP ARMORY + parse/extract + session/identity + proxy pool.

Companion to tools.py. All imports are lazy/optional — a tool reports
"not installed" instead of crashing if its library is missing.

Directives honored:
- TLS impersonation default ON for every request (curl_cffi/primp).
- Timeouts bounded, retries bounded, response-size caps, cert validation ON.
- Free first: paid solver/proxy APIs are key-gated and honestly not-configured.
- No anti-detect browser forks (refused category) — not present here.
"""
from __future__ import annotations

import base64
import json
import os
import re
import socket
import ssl
import time
import urllib.parse


def _ok(**kw):
    kw["ok"] = True
    return kw


def _err(msg):
    return {"ok": False, "error": str(msg)[:500]}


def _need(modname, pipname=""):
    try:
        import importlib
        return importlib.import_module(modname)
    except ImportError:
        raise RuntimeError(
            f"library '{modname}' not installed"
            + (f" (pip install {pipname})" if pipname else ""))


# ---------------------------------------------------------------- TLS / HTTP

def tool_http_impersonate(url: str, impersonate: str = "chrome",
                          method: str = "GET", headers: str = "",
                          data: str = "", timeout: int = 20,
                          max_bytes: int = 2_000_000) -> dict:
    """Fetch a URL with real browser TLS/JA3 fingerprint (curl_cffi).

    impersonate default 'chrome' (per directive: ON for every request).
    Other options: safari, firefox, edge. Cert validation ON.
    Response truncated at max_bytes.
    """
    try:
        cc = _need("curl_cffi", "curl-cffi")
        hdrs = json.loads(headers) if headers.strip() else {}
        req_data = data.encode() if data else None
        t0 = time.monotonic()
        r = cc.requests.request(
            method.upper(), url, headers=hdrs, data=req_data,
            impersonate=impersonate, timeout=timeout, verify=True)
        body = r.content[:max_bytes]
        try:
            text = body.decode("utf-8", "replace")
        except Exception:
            text = ""
        return _ok(url=url, status=r.status_code,
                    impersonate=impersonate,
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                    headers=dict(r.headers),
                    truncated=len(r.content) > max_bytes,
                    text=text if len(body) < 200_000 else text[:200_000],
                    b64=base64.b64encode(body).decode()
                    if len(body) >= 200_000 else "")
    except Exception as e:
        return _err(e)


def tool_http2_fetch(url: str, timeout: int = 20,
                     max_bytes: int = 2_000_000) -> dict:
    """Fetch over HTTP/2 with httpx (ALPN negotiated, cert validation ON)."""
    try:
        httpx = _need("httpx")
        t0 = time.monotonic()
        with httpx.Client(http2=True, timeout=timeout, verify=True,
                          follow_redirects=True) as c:
            r = c.get(url)
            body = r.content[:max_bytes]
        return _ok(url=url, status=r.status_code,
                    http_version=str(r.http_version) if hasattr(r, "http_version") else "h2",
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                    headers=dict(r.headers),
                    text=body.decode("utf-8", "replace")[:200_000])
    except Exception as e:
        return _err(e)


def tool_http_async_fetch(urls: str, timeout: int = 20,
                          max_bytes: int = 1_000_000) -> dict:
    """Fetch many URLs concurrently (aiohttp). urls = JSON list of strings."""
    try:
        aiohttp = _need("aiohttp")
        import asyncio
        url_list = json.loads(urls)
        if not isinstance(url_list, list) or len(url_list) > 50:
            return _err("urls must be a JSON list of <= 50 URLs")

        async def _one(session, u):
            t0 = time.monotonic()
            try:
                async with session.get(u, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                    body = (await r.read())[:max_bytes]
                    return {"url": u, "status": r.status,
                            "ms": int((time.monotonic() - t0) * 1000),
                            "text": body.decode("utf-8", "replace")[:50_000]}
            except Exception as e:
                return {"url": u, "error": str(e)[:200]}

        async def _all():
            ssl_ctx = ssl.create_default_context()  # cert validation ON
            conn = aiohttp.TCPConnector(ssl=ssl_ctx, limit=10)
            async with aiohttp.ClientSession(connector=conn) as s:
                return await asyncio.gather(*[_one(s, u) for u in url_list])

        results = asyncio.run(_all())
        ok_n = sum(1 for x in results if "status" in x)
        return _ok(fetched=len(results), succeeded=ok_n, results=results)
    except Exception as e:
        return _err(e)


def tool_http_raw(url: str, timeout: int = 15,
                  max_bytes: int = 500_000) -> dict:
    """Raw-socket HTTP/1.1 fetch — no library, full control. Cert ON for https."""
    try:
        p = urllib.parse.urlparse(url)
        host, port = p.hostname, p.port or (443 if p.scheme == "https" else 80)
        path = p.path or "/"
        if p.query:
            path += "?" + p.query
        t0 = time.monotonic()
        raw = socket.create_connection((host, port), timeout=timeout)
        if p.scheme == "https":
            ctx = ssl.create_default_context()  # cert validation ON
            raw = ctx.wrap_socket(raw, server_hostname=host)
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
               f"User-Agent: haley-armory/1.0\r\nConnection: close\r\n\r\n")
        raw.sendall(req.encode())
        chunks, total = [], 0
        while True:
            b = raw.recv(65536)
            if not b:
                break
            chunks.append(b)
            total += len(b)
            if total > max_bytes + 100_000:
                break
        raw.close()
        data = b"".join(chunks)
        head, _, body = data.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode("latin1", "replace")
        return _ok(url=url, status_line=status,
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                    truncated=total > max_bytes,
                    text=body[:max_bytes].decode("utf-8", "replace")[:200_000])
    except Exception as e:
        return _err(e)


def tool_tls_info(url: str, timeout: int = 15) -> dict:
    """Inspect a site's TLS certificate (validation ON). Returns cert details."""
    try:
        p = urllib.parse.urlparse(url)
        host, port = p.hostname, p.port or 443
        ctx = ssl.create_default_context()
        t0 = time.monotonic()
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                cert = tls.getpeercert()
                cipher = tls.cipher()
                version = tls.version()
        return _ok(host=host, tls_version=version, cipher=cipher[0] if cipher else "",
                    cert_subject=dict(x[0] for x in cert.get("subject", ())),
                    cert_issuer=dict(x[0] for x in cert.get("issuer", ())),
                    not_before=cert.get("notBefore"), not_after=cert.get("notAfter"),
                    san=[x[1] for x in cert.get("subjectAltName", ())][:20],
                    elapsed_ms=int((time.monotonic() - t0) * 1000))
    except Exception as e:
        return _err(e)


def tool_primp_fetch(url: str, impersonate: str = "chrome",
                     timeout: int = 20, max_bytes: int = 2_000_000) -> dict:
    """Fastest impersonating fetch (primp, Rust core). impersonate default chrome."""
    try:
        primp = _need("primp")
        t0 = time.monotonic()
        r = primp.get(url, impersonate=impersonate, timeout=timeout)
        body = r.content[:max_bytes]
        return _ok(url=url, status=r.status_code, impersonate=impersonate,
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                    headers=dict(r.headers),
                    text=body.decode("utf-8", "replace")[:200_000])
    except Exception as e:
        return _err(e)


# ------------------------------------------------------- PARSE / EXTRACT

def _soup(html: str):
    bs4 = _need("bs4", "beautifulsoup4")
    return bs4.BeautifulSoup(html, "lxml")


def tool_parse_select(html: str, selector: str, mode: str = "css",
                      limit: int = 50) -> dict:
    """Extract via CSS or XPath selector. Returns text + html per match."""
    try:
        if mode == "xpath":
            lxml_html = _need("lxml.html", "lxml")
            doc = lxml_html.fromstring(html)
            els = doc.xpath(selector)[:limit]
            out = []
            for el in els:
                if isinstance(el, str):
                    out.append({"text": el.strip()[:500]})
                else:
                    out.append({"text": el.text_content().strip()[:500],
                                "html": lxml_html.tostring(el, encoding="unicode")[:2000]})
            return _ok(mode="xpath", matches=len(out), items=out)
        parsel = _need("parsel")
        sel = parsel.Selector(text=html)
        els = sel.css(selector)[:limit]
        out = [{"text": e.xpath("string(.)").get("").strip()[:500],
                "html": e.get()[:2000]} for e in els]
        return _ok(mode="css", matches=len(out), items=out)
    except Exception as e:
        return _err(e)


def tool_extract_article(source: str, is_url: bool = True,
                         timeout: int = 20) -> dict:
    """Extract main article text (trafilatura). source = URL or raw HTML."""
    try:
        trafilatura = _need("trafilatura")
        if is_url:
            downloaded = trafilatura.fetch_url(source)
            if not downloaded:
                return _err("download failed")
            html = downloaded
        else:
            html = source
        text = trafilatura.extract(html, include_comments=False,
                                   include_tables=True) or ""
        meta = trafilatura.extract_metadata(html)
        return _ok(text=text[:50_000], chars=len(text),
                    title=getattr(meta, "title", ""),
                    author=getattr(meta, "author", ""),
                    date=getattr(meta, "date", ""))
    except Exception as e:
        return _err(e)


def tool_extract_metadata(html: str) -> dict:
    """Structured metadata: JSON-LD, microdata, RDFa, OpenGraph (extruct)."""
    try:
        extruct = _need("extruct")
        from w3lib.html import get_base_url  # extruct dependency
        data = extruct.extract(html, base_url="",
                               syntaxes=["json-ld", "microdata", "rdfa",
                                         "opengraph", "dublincore"])
        # Trim for transport
        slim = {}
        for k, v in data.items():
            s = json.dumps(v)
            slim[k] = json.loads(s[:50_000])
        return _ok(**slim)
    except Exception as e:
        return _err(e)


def tool_html_tables(html: str, limit: int = 10) -> dict:
    """All HTML tables → list of {headers, rows}. No pandas needed."""
    try:
        lxml_html = _need("lxml.html", "lxml")
        doc = lxml_html.fromstring(html)
        tables = []
        for t in doc.xpath("//table")[:limit]:
            headers = [th.text_content().strip()[:100]
                       for th in t.xpath(".//th")][:20]
            rows = []
            for tr in t.xpath(".//tr")[:100]:
                cells = [td.text_content().strip()[:200]
                         for td in tr.xpath("./td|./th")]
                if cells:
                    rows.append(cells)
            if rows:
                tables.append({"headers": headers, "rows": rows[:100],
                               "n_rows": len(rows)})
        return _ok(tables=len(tables), data=tables)
    except Exception as e:
        return _err(e)


def tool_json_query(data: str, query: str) -> dict:
    """JMESPath query over JSON. data = JSON string."""
    try:
        jmespath = _need("jmespath")
        obj = json.loads(data)
        result = jmespath.search(query, obj)
        s = json.dumps(result, default=str)
        return _ok(result=json.loads(s[:200_000]), truncated=len(s) > 200_000)
    except Exception as e:
        return _err(e)


def tool_excel_read(path: str, sheet: str = "", max_rows: int = 500) -> dict:
    """Read an Excel file → rows. path inside workspace."""
    try:
        openpyxl = _need("openpyxl")
        from pathlib import Path as _P
        p = _P(os.environ.get("AGENT_WORKSPACE", "/tmp/agent-workspace")) / path
        if not p.exists():
            p = _P(path)
        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        ws = wb[sheet] if sheet else wb.active
        rows = []
        for row in ws.iter_rows(values_only=True):
            rows.append([str(c)[:200] if c is not None else "" for c in row])
            if len(rows) >= max_rows:
                break
        return _ok(sheet=ws.title, n_rows=len(rows), rows=rows)
    except Exception as e:
        return _err(e)


def tool_excel_write(path: str, rows: str, sheet: str = "Sheet1") -> dict:
    """Write rows (JSON list of lists) to an Excel file in workspace."""
    try:
        openpyxl = _need("openpyxl")
        from pathlib import Path as _P
        data = json.loads(rows)
        base = _P(os.environ.get("AGENT_WORKSPACE", "/tmp/agent-workspace"))
        base.mkdir(parents=True, exist_ok=True)
        p = base / path
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = sheet
        for r in data[:10000]:
            ws.append(r)
        wb.save(p)
        return _ok(path=str(p), n_rows=len(data))
    except Exception as e:
        return _err(e)


def tool_duckdb_query(sql: str, data: str = "", fmt: str = "json") -> dict:
    """Run SQL over JSON data with DuckDB. data = JSON (list or object)."""
    try:
        duckdb = _need("duckdb")
        con = duckdb.connect()
        if data.strip():
            obj = json.loads(data)
            con.execute("CREATE TABLE t AS SELECT * FROM (SELECT * FROM "
                        "read_json_auto(?))", ["_"])
            # simpler: register via pandas-free path
            import tempfile
            with tempfile.NamedTemporaryFile("w", suffix=".json",
                                             delete=False) as f:
                json.dump(obj, f)
                tmp = f.name
            con.execute(f"CREATE OR REPLACE TABLE t AS SELECT * FROM read_json_auto('{tmp}')")
            os.unlink(tmp)
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()[:1000]
        out = [dict(zip(cols, [str(c)[:300] for c in r])) for r in rows]
        return _ok(columns=cols, n_rows=len(out), rows=out)
    except Exception as e:
        return _err(e)


# ------------------------------------------------- SESSION / IDENTITY

_SESSION_DIR = "/tmp/agent_sessions"


def _sess_path(name: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:40]
    os.makedirs(_SESSION_DIR, exist_ok=True)
    return os.path.join(_SESSION_DIR, safe + ".json")


def tool_session_save(name: str) -> dict:
    """Save the CURRENT browser session (cookies+storage) under a name."""
    try:
        from agent import tools as _t
        def _save(state):
            path = _sess_path(name)
            state["context"].storage_state(path=path)
            return {"saved": name, "path": path}
        return _t._ok(**_t._browser_call(_save))
    except Exception as e:
        return _err(e)


def tool_session_load(name: str) -> dict:
    """Load a named session into the current browser context (isolated)."""
    try:
        from agent import tools as _t
        path = _sess_path(name)
        if not os.path.exists(path):
            return _err(f"no saved session '{name}'")
        def _load(state):
            # Add cookies into the active context without touching others.
            with open(path) as f:
                data = json.load(f)
            cookies = data.get("cookies", [])
            if cookies:
                state["context"].add_cookies(cookies)
            return {"loaded": name, "cookies": len(cookies)}
        return _t._ok(**_t._browser_call(_load))
    except Exception as e:
        return _err(e)


def tool_session_list() -> dict:
    try:
        if not os.path.isdir(_SESSION_DIR):
            return _ok(sessions=[])
        names = [f[:-5] for f in os.listdir(_SESSION_DIR) if f.endswith(".json")]
        return _ok(sessions=sorted(names))
    except Exception as e:
        return _err(e)


def tool_jwt_decode(token: str) -> dict:
    """Decode a JWT (header+payload). No signature verification — inspection only."""
    try:
        parts = token.strip().split(".")
        if len(parts) < 2:
            return _err("not a JWT")
        def _b64(s):
            s += "=" * (-len(s) % 4)
            return json.loads(base64.urlsafe_b64decode(s).decode())
        return _ok(header=_b64(parts[0]), payload=_b64(parts[1]),
                    has_signature=len(parts) == 3)
    except Exception as e:
        return _err(e)


# ------------------------------------------------- CHALLENGE / CAPTCHA

def tool_cloudscraper_fetch(url: str, timeout: int = 30,
                            max_bytes: int = 2_000_000) -> dict:
    """Fetch via cloudscraper (Cloudflare/IUAM fallback)."""
    try:
        cloudscraper = _need("cloudscraper")
        t0 = time.monotonic()
        scraper = cloudscraper.create_scraper()
        r = scraper.get(url, timeout=timeout)
        body = r.content[:max_bytes]
        return _ok(url=url, status=r.status_code,
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                    text=body.decode("utf-8", "replace")[:200_000])
    except Exception as e:
        return _err(e)


def tool_flaresolverr_solve(url: str, timeout: int = 60) -> dict:
    """Solve Cloudflare challenge via FlareSolverr server.

    Needs FLARESOLVERR_URL env (e.g. http://localhost:8191). Honest when unset.
    """
    try:
        base = os.environ.get("FLARESOLVERR_URL", "").rstrip("/")
        if not base:
            return {"ok": False, "error": "not_configured",
                    "hint": "set FLARESOLVERR_URL env to a FlareSolverr server"}
        import urllib.request
        payload = json.dumps({"cmd": "request.get", "url": url,
                              "maxTimeout": timeout * 1000}).encode()
        req = urllib.request.Request(base + "/v1",
                                     data=payload,
                                     headers={"Content-Type": "application/json"})
        t0 = time.monotonic()
        with urllib.request.urlopen(req, timeout=timeout + 10) as r:
            data = json.load(r)
        sol = data.get("solution", {})
        return _ok(url=sol.get("url", url), status=sol.get("status", 0),
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                    cookies=len(sol.get("cookies", [])),
                    text=str(sol.get("response", ""))[:200_000])
    except Exception as e:
        return _err(e)


def tool_captcha_solvers_status() -> dict:
    """Which paid CAPTCHA-solver APIs are configured (keys in env)."""
    keys = {
        "capsolver": "CAPSOLVER_API_KEY",
        "twocaptcha": "TWOCAPTCHA_API_KEY",
        "anticaptcha": "ANTICAPTCHA_API_KEY",
        "nopecha": "NOPECHA_API_KEY",
    }
    status = {name: bool(os.environ.get(env)) for name, env in keys.items()}
    status["human_assist"] = True  # browser_solve_captcha + challenge pause flow
    return _ok(solvers=status,
               note="paid solvers need API keys in env; human-assist always available")


# ------------------------------------------------- PROXY POOL (free first)

_PROXY_CACHE = {"at": 0.0, "proxies": []}


def tool_proxy_pool_refresh(max_proxies: int = 100) -> dict:
    """Refresh free proxy pool (ProxyScrape + Geonode free APIs)."""
    import urllib.request
    found = []
    sources = [
        ("https://api.proxyscrape.com/v2/?request=displayproxies"
         "&protocol=http&timeout=5000&country=all&ssl=all&anonymity=all"),
        ("https://proxylist.geonode.com/api/proxy-list?limit=100&page=1"
         "&sort_by=lastChecked&sort_type=desc"),
    ]
    for src in sources:
        try:
            with urllib.request.urlopen(src, timeout=20) as r:
                body = r.read().decode("utf-8", "replace")
            if "geonode" in src:
                for item in json.loads(body).get("data", []):
                    found.append(f"{item['ip']}:{item['port']}")
            else:
                for line in body.splitlines():
                    line = line.strip()
                    if re.match(r"^\d+\.\d+\.\d+\.\d+:\d+$", line):
                        found.append(line)
        except Exception:
            continue
    found = list(dict.fromkeys(found))[:max_proxies]
    _PROXY_CACHE.update({"at": time.time(), "proxies": found})
    return _ok(n=len(found), sources_ok=True)


def tool_proxy_pool_get() -> dict:
    """Get next proxy from the pool (round-robin). Refresh if empty/stale."""
    if (not _PROXY_CACHE["proxies"]
            or time.time() - _PROXY_CACHE["at"] > 3600):
        r = tool_proxy_pool_refresh()
        if not r.get("ok") or not _PROXY_CACHE["proxies"]:
            return _err("proxy pool empty — free sources unreachable")
    proxies = _PROXY_CACHE["proxies"]
    p = proxies.pop(0)
    proxies.append(p)
    return _ok(proxy=f"http://{p}", remaining=len(proxies))


# ------------------------------------------------- SMART FETCH (fallback chain)

def tool_smart_fetch(url: str, timeout: int = 20,
                     chain: str = "normal,germany,virginia,residential",
                     max_bytes: int = 2_000_000) -> dict:
    """Fetch with toggle-able fallback chain.

    chain: comma list among normal, germany, virginia, residential.
    Skips modes with no proxy configured. Tor excluded until phone-Tor is live.
    Never silently fails — reports each hop's result.
    """
    hops = []
    for mode in [m.strip() for m in chain.split(",") if m.strip()]:
        if mode == "normal":
            r = tool_http_impersonate(url, timeout=timeout, max_bytes=max_bytes)
            hops.append({"mode": "normal", **{k: v for k, v in r.items()
                                              if k in ("ok", "status", "error", "elapsed_ms")}})
            if r.get("ok"):
                r["chain"] = hops
                return r
        elif mode in ("germany", "virginia"):
            env = "GERMANY_PROXY_URL" if mode == "germany" else "VIRGINIA_PROXY_URL"
            proxy = os.environ.get(env, "")
            if not proxy:
                hops.append({"mode": mode, "ok": False,
                             "error": f"not_configured ({env} unset) — skipped"})
                continue
            try:
                cc = _need("curl_cffi", "curl-cffi")
                t0 = time.monotonic()
                r = cc.requests.get(url, proxies={"http": proxy, "https": proxy},
                                    impersonate="chrome", timeout=timeout,
                                    verify=True)
                body = r.content[:max_bytes]
                hops.append({"mode": mode, "ok": True, "status": r.status_code,
                             "elapsed_ms": int((time.monotonic() - t0) * 1000)})
                return _ok(url=url, status=r.status_code, mode=mode,
                            text=body.decode("utf-8", "replace")[:200_000],
                            chain=hops)
            except Exception as e:
                hops.append({"mode": mode, "ok": False,
                             "error": str(e)[:200]})
        elif mode == "residential":
            pr = tool_proxy_pool_get()
            if not pr.get("ok"):
                hops.append({"mode": "residential", "ok": False,
                             "error": pr.get("error")})
                continue
            try:
                cc = _need("curl_cffi", "curl-cffi")
                t0 = time.monotonic()
                r = cc.requests.get(url,
                                    proxies={"http": pr["proxy"],
                                             "https": pr["proxy"]},
                                    impersonate="chrome", timeout=timeout,
                                    verify=True)
                body = r.content[:max_bytes]
                hops.append({"mode": "residential", "ok": True,
                             "status": r.status_code,
                             "proxy": pr["proxy"],
                             "elapsed_ms": int((time.monotonic() - t0) * 1000)})
                return _ok(url=url, status=r.status_code, mode="residential",
                            text=body.decode("utf-8", "replace")[:200_000],
                            chain=hops)
            except Exception as e:
                hops.append({"mode": "residential", "ok": False,
                             "proxy": pr.get("proxy"), "error": str(e)[:200]})
        else:
            hops.append({"mode": mode, "ok": False, "error": "unknown mode"})
    return {"ok": False, "error": "all chain hops failed", "chain": hops}


# ------------------------------------------------- STEM (Tor control, future)

def tool_tor_control_status() -> dict:
    """Tor control-port status (for phone-Tor future). Honest when unreachable."""
    try:
        stem = _need("stem")
        from stem.control import Controller
        host = os.environ.get("TOR_CONTROL_HOST", "127.0.0.1")
        port = int(os.environ.get("TOR_CONTROL_PORT", "9051"))
        try:
            with Controller.from_port(address=host, port=port) as c:
                c.authenticate()
                ver = c.get_version().version_str
                circ = len(c.get_circuits())
                return _ok(connected=True, version=ver, circuits=circ)
        except Exception as e:
            return {"ok": False, "connected": False,
                    "error": str(e)[:200],
                    "hint": "phone-Tor not connected yet"}
    except Exception as e:
        return _err(e)


ARMORY_REGISTRY = {
    "http_impersonate": (tool_http_impersonate,
        "Fetch URL with real browser TLS/JA3 fingerprint (curl_cffi). Impersonation default ON.",
        {"url": "str (required)", "impersonate": "chrome|safari|firefox|edge, default chrome",
         "method": "GET|POST, default GET", "headers": "JSON str", "data": "str",
         "timeout": "int, default 20", "max_bytes": "int, default 2000000"}),
    "http2_fetch": (tool_http2_fetch,
        "Fetch over HTTP/2 (httpx, ALPN, cert validation ON).",
        {"url": "str (required)", "timeout": "int, default 20", "max_bytes": "int"}),
    "http_async_fetch": (tool_http_async_fetch,
        "Fetch up to 50 URLs concurrently (aiohttp). urls = JSON list.",
        {"urls": "JSON list str (required)", "timeout": "int", "max_bytes": "int"}),
    "http_raw": (tool_http_raw,
        "Raw-socket HTTP/1.1 fetch, full control, cert ON for https.",
        {"url": "str (required)", "timeout": "int", "max_bytes": "int"}),
    "tls_info": (tool_tls_info,
        "Inspect a site's TLS certificate (validation ON).",
        {"url": "str (required)", "timeout": "int"}),
    "primp_fetch": (tool_primp_fetch,
        "Fastest impersonating fetch (primp Rust core). Default chrome.",
        {"url": "str (required)", "impersonate": "str, default chrome",
         "timeout": "int", "max_bytes": "int"}),
    "parse_select": (tool_parse_select,
        "Extract via CSS or XPath selector from HTML.",
        {"html": "str (required)", "selector": "str (required)",
         "mode": "css|xpath, default css", "limit": "int"}),
    "extract_article": (tool_extract_article,
        "Main article text via trafilatura (URL or raw HTML).",
        {"source": "str (required)", "is_url": "bool, default true", "timeout": "int"}),
    "extract_metadata": (tool_extract_metadata,
        "JSON-LD/microdata/RDFa/OpenGraph metadata (extruct).",
        {"html": "str (required)"}),
    "html_tables": (tool_html_tables,
        "All HTML tables → headers+rows (no pandas needed).",
        {"html": "str (required)", "limit": "int"}),
    "json_query": (tool_json_query,
        "JMESPath query over JSON string.",
        {"data": "JSON str (required)", "query": "JMESPath str (required)"}),
    "excel_read": (tool_excel_read,
        "Read Excel file from workspace → rows.",
        {"path": "str (required)", "sheet": "str", "max_rows": "int"}),
    "excel_write": (tool_excel_write,
        "Write JSON list-of-lists to Excel in workspace.",
        {"path": "str (required)", "rows": "JSON str (required)", "sheet": "str"}),
    "duckdb_query": (tool_duckdb_query,
        "SQL over JSON data with DuckDB.",
        {"sql": "str (required)", "data": "JSON str", "fmt": "str"}),
    "session_save": (tool_session_save,
        "Save current browser session (cookies+storage) under a name.",
        {"name": "str (required)"}),
    "session_load": (tool_session_load,
        "Load a named session into the current browser context.",
        {"name": "str (required)"}),
    "session_list": (tool_session_list, "List saved browser sessions.", {}),
    "jwt_decode": (tool_jwt_decode,
        "Decode JWT header+payload (inspection only, no verify).",
        {"token": "str (required)"}),
    "cloudscraper_fetch": (tool_cloudscraper_fetch,
        "Fetch via cloudscraper (Cloudflare fallback).",
        {"url": "str (required)", "timeout": "int", "max_bytes": "int"}),
    "flaresolverr_solve": (tool_flaresolverr_solve,
        "Solve Cloudflare via FlareSolverr server (needs FLARESOLVERR_URL env).",
        {"url": "str (required)", "timeout": "int"}),
    "captcha_solvers_status": (tool_captcha_solvers_status,
        "Which paid CAPTCHA-solver APIs are configured (env keys).", {}),
    "proxy_pool_refresh": (tool_proxy_pool_refresh,
        "Refresh free proxy pool (ProxyScrape + Geonode).",
        {"max_proxies": "int, default 100"}),
    "proxy_pool_get": (tool_proxy_pool_get,
        "Next proxy from pool (round-robin).", {}),
    "smart_fetch": (tool_smart_fetch,
        "Fetch with toggle-able fallback chain: normal,germany,virginia,residential.",
        {"url": "str (required)", "timeout": "int",
         "chain": "str, default normal,germany,virginia,residential",
         "max_bytes": "int"}),
    "tor_control_status": (tool_tor_control_status,
        "Tor control-port status (phone-Tor future; honest when unreachable).", {}),
}
