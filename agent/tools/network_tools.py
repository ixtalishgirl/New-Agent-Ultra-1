"""Network tool module — real HTTP, DNS, TCP, ICMP and TLS inspection.

All operations use the standard library (urllib, socket, ssl, subprocess)
with explicit timeouts and bounded response sizes. Every method returns a
:class:`NetResult` (ok, output, parsed, error) — never raises to the caller
for ordinary network failures.
"""
from __future__ import annotations

import json as _json
import re
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


DEFAULT_TIMEOUT = 20
DEFAULT_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) HaleyAgent/1.0"
MAX_BODY_BYTES = 2_000_000  # 2 MB cap on fetched bodies


@dataclass
class NetResult:
    """Structured result of a network operation."""
    ok: bool
    output: str = ""
    parsed: dict = field(default_factory=dict)
    error: str = ""
    duration_ms: float = 0.0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_request(url: str, method: str = "GET",
                   headers: dict | None = None,
                   data: bytes | None = None) -> urllib.request.Request:
    hdrs = {"User-Agent": DEFAULT_USER_AGENT}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    return req


def _read_bounded(resp, cap: int = MAX_BODY_BYTES) -> tuple[bytes, bool]:
    """Read at most ``cap`` bytes; return (body, truncated)."""
    chunks: list[bytes] = []
    total = 0
    truncated = False
    while True:
        chunk = resp.read(min(65536, cap - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            chunks.append(chunk[:cap - (total - len(chunk))])
            truncated = True
            break
        chunks.append(chunk)
    return b"".join(chunks), truncated


def _decode_body(raw: bytes, content_type: str = "") -> tuple[str, str]:
    """Decode bytes to text, honoring charset when present."""
    charset = "utf-8"
    m = re.search(r"charset=([\w-]+)", content_type, re.IGNORECASE)
    if m:
        charset = m.group(1)
    for enc in (charset, "utf-8", "latin-1"):
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace"), "utf-8-replace"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirect handler that records instead of following."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        return None


# ---------------------------------------------------------------------------
# Minimal DER certificate parser (dependency-free fallback)
# ---------------------------------------------------------------------------

def _der_tlvs(data: bytes) -> list[tuple[int, bytes]]:
    """Split DER bytes into (tag, value) pairs (single-byte tags only,
    which covers everything in a typical X.509 certificate)."""
    out: list[tuple[int, bytes]] = []
    i, end = 0, len(data)
    while i < end:
        tag = data[i]
        i += 1
        if i >= end:
            break
        lb = data[i]
        i += 1
        if lb & 0x80:
            n = lb & 0x7F
            if n == 0 or i + n > end:
                break
            length = int.from_bytes(data[i:i + n], "big")
            i += n
        else:
            length = lb
        if i + length > end:
            break
        out.append((tag, data[i:i + length]))
        i += length
    return out


_CN_OID = bytes([0x55, 0x04, 0x03])  # 2.5.4.3 commonName


def _der_name_cn(name_der: bytes) -> str:
    """Extract the CN from a DER-encoded X.509 Name."""
    for tag, rdn in _der_tlvs(name_der):
        if tag != 0x31:  # SET
            continue
        for _t2, atv in _der_tlvs(rdn):
            parts = _der_tlvs(atv)
            if len(parts) >= 2 and parts[0][0] == 0x06 \
                    and parts[0][1] == _CN_OID:
                return parts[1][1].decode("utf-8", "replace")
    return ""


def _der_time(value: bytes, tag: int) -> str:
    """DER UTCTime/GeneralizedTime -> getpeercert()-style date string."""
    s = value.decode("ascii", "replace")
    if tag == 0x17 and len(s) >= 12:  # UTCTime YYMMDDHHMMSSZ
        year = int(s[0:2])
        year += 2000 if year < 50 else 1900
        dt = datetime(year, int(s[2:4]), int(s[4:6]),
                      int(s[6:8]), int(s[8:10]), int(s[10:12]),
                      tzinfo=timezone.utc)
    elif tag == 0x18 and len(s) >= 14:  # GeneralizedTime YYYYMMDDHHMMSSZ
        dt = datetime(int(s[0:4]), int(s[4:6]), int(s[6:8]),
                      int(s[8:10]), int(s[10:12]), int(s[12:14]),
                      tzinfo=timezone.utc)
    else:
        raise ValueError(f"unrecognized time encoding: {s!r}")
    return dt.strftime("%b %d %H:%M:%S %Y GMT")


def parse_der_cert(der: bytes) -> dict | None:
    """Parse DER X.509 bytes into a getpeercert()-shaped dict.

    Used as a fallback when the TLS context runs with CERT_NONE (where
    getpeercert() returns nothing). Returns None on parse failure.
    """
    try:
        outer = _der_tlvs(der)
        if not outer or outer[0][0] != 0x30:
            return None
        # Certificate ::= SEQ { tbsCertificate, signatureAlgorithm, signatureValue }
        cert_children = _der_tlvs(outer[0][1])
        if not cert_children or cert_children[0][0] != 0x30:
            return None
        tbs = _der_tlvs(cert_children[0][1])
        idx = 0
        if tbs and tbs[0][0] == 0xA0:  # [0] explicit version
            idx = 1
        # tbs: serial, signature alg, issuer, validity, subject, ...
        if len(tbs) < idx + 5:
            return None
        serial_hex = tbs[idx][1].hex().upper().lstrip("0") or "0"
        issuer_cn = _der_name_cn(tbs[idx + 2][1])
        validity = _der_tlvs(tbs[idx + 3][1])
        if len(validity) < 2:
            return None
        not_before = _der_time(validity[0][1], validity[0][0])
        not_after = _der_time(validity[1][1], validity[1][0])
        subject_cn = _der_name_cn(tbs[idx + 4][1])
        return {
            "subject": ((("commonName", subject_cn),),) if subject_cn else (),
            "issuer": ((("commonName", issuer_cn),),) if issuer_cn else (),
            "notBefore": not_before,
            "notAfter": not_after,
            "subjectAltName": (),
            "serialNumber": serial_hex,
        }
    except (IndexError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class NetworkTools:
    """Real network operations with timeouts and bounded reads."""

    def __init__(self, timeout: int = DEFAULT_TIMEOUT,
                 user_agent: str = DEFAULT_USER_AGENT,
                 proxy: str = ""):
        self.timeout = timeout
        self.user_agent = user_agent
        self.proxy = proxy

    # -- HTTP ----------------------------------------------------------------

    def _opener(self, follow_redirects: bool = True):
        handlers: list = []
        if not follow_redirects:
            handlers.append(_NoRedirect())
        if self.proxy:
            handlers.append(urllib.request.ProxyHandler(
                {"http": self.proxy, "https": self.proxy}))
        return urllib.request.build_opener(*handlers)

    def http_get(self, url: str, headers: dict | None = None,
                 params: dict | None = None,
                 follow_redirects: bool = True,
                 max_bytes: int = MAX_BODY_BYTES,
                 timeout: int | None = None) -> NetResult:
        """GET a URL. Returns status, headers, decoded body (bounded)."""
        if params:
            sep = "&" if "?" in url else "?"
            url = url + sep + urllib.parse.urlencode(params)
        hdrs = {"User-Agent": self.user_agent}
        if headers:
            hdrs.update(headers)
        t0 = time.time()
        try:
            req = _build_request(url, "GET", hdrs)
            with self._opener(follow_redirects).open(
                    req, timeout=timeout or self.timeout) as resp:
                raw, truncated = _read_bounded(resp, max_bytes)
                ctype = resp.headers.get("Content-Type", "")
                text, charset = _decode_body(raw, ctype)
                parsed = {
                    "url": resp.geturl(),
                    "requested_url": url,
                    "status": resp.status,
                    "reason": resp.reason,
                    "headers": dict(resp.headers),
                    "content_type": ctype,
                    "charset": charset,
                    "bytes": len(raw),
                    "truncated": truncated,
                    "body": text,
                    "redirected": resp.geturl() != url,
                }
                dur = (time.time() - t0) * 1000
                out = (f"HTTP {resp.status} {resp.reason} "
                       f"({len(raw)} bytes, {dur:.0f} ms)\n{resp.geturl()}")
                return NetResult(ok=200 <= resp.status < 400, output=out,
                                 parsed=parsed, duration_ms=dur)
        except urllib.error.HTTPError as e:
            dur = (time.time() - t0) * 1000
            try:
                raw, _ = _read_bounded(e, max_bytes)
                text, _ = _decode_body(raw, e.headers.get("Content-Type", ""))
            except Exception:
                text = ""
            return NetResult(ok=False,
                             output=f"HTTP {e.code} {e.reason} for {url}\n{text[:2000]}",
                             parsed={"url": url, "status": e.code,
                                     "reason": e.reason,
                                     "headers": dict(e.headers or {}),
                                     "body": text[:2000]},
                             error=f"HTTP {e.code}: {e.reason}",
                             duration_ms=dur)
        except (urllib.error.URLError, socket.timeout, TimeoutError,
                ssl.SSLError, OSError) as e:
            dur = (time.time() - t0) * 1000
            return NetResult(ok=False, output=f"Request failed: {e}",
                             parsed={"url": url}, error=str(e),
                             duration_ms=dur)

    def http_post(self, url: str, data: dict | None = None,
                  json_data: Any = None, headers: dict | None = None,
                  timeout: int | None = None) -> NetResult:
        """POST JSON (``json_data``) or form-encoded (``data``) payload."""
        hdrs = {"User-Agent": self.user_agent}
        body: bytes
        if json_data is not None:
            body = _json.dumps(json_data).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        elif data is not None:
            body = urllib.parse.urlencode(data).encode("utf-8")
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = b""
        if headers:
            hdrs.update(headers)
        t0 = time.time()
        try:
            req = _build_request(url, "POST", hdrs, data=body)
            with self._opener().open(req, timeout=timeout or self.timeout) as resp:
                raw, truncated = _read_bounded(resp)
                ctype = resp.headers.get("Content-Type", "")
                text, charset = _decode_body(raw, ctype)
                parsed = {
                    "url": resp.geturl(), "status": resp.status,
                    "reason": resp.reason, "headers": dict(resp.headers),
                    "bytes": len(raw), "truncated": truncated, "body": text,
                }
                dur = (time.time() - t0) * 1000
                return NetResult(ok=200 <= resp.status < 300,
                                 output=f"HTTP {resp.status} ({len(raw)} bytes)\n{text[:4000]}",
                                 parsed=parsed, duration_ms=dur)
        except urllib.error.HTTPError as e:
            dur = (time.time() - t0) * 1000
            return NetResult(ok=False, output=f"HTTP {e.code} {e.reason}",
                             parsed={"url": url, "status": e.code},
                             error=f"HTTP {e.code}: {e.reason}",
                             duration_ms=dur)
        except (urllib.error.URLError, socket.timeout, TimeoutError,
                ssl.SSLError, OSError) as e:
            dur = (time.time() - t0) * 1000
            return NetResult(ok=False, output=f"Request failed: {e}",
                             parsed={"url": url}, error=str(e),
                             duration_ms=dur)

    def http_head(self, url: str, headers: dict | None = None,
                  timeout: int | None = None) -> NetResult:
        """HEAD request — headers only, no body."""
        hdrs = {"User-Agent": self.user_agent}
        if headers:
            hdrs.update(headers)
        t0 = time.time()
        try:
            req = _build_request(url, "HEAD", hdrs)
            with self._opener().open(req, timeout=timeout or self.timeout) as resp:
                parsed = {
                    "url": resp.geturl(), "status": resp.status,
                    "reason": resp.reason, "headers": dict(resp.headers),
                    "content_length": resp.headers.get("Content-Length"),
                    "content_type": resp.headers.get("Content-Type"),
                    "last_modified": resp.headers.get("Last-Modified"),
                }
                dur = (time.time() - t0) * 1000
                lines = [f"HTTP {resp.status} {resp.reason}"]
                for k, v in resp.headers.items():
                    lines.append(f"{k}: {v}")
                return NetResult(ok=200 <= resp.status < 400,
                                 output="\n".join(lines), parsed=parsed,
                                 duration_ms=dur)
        except urllib.error.HTTPError as e:
            return NetResult(ok=False, output=f"HTTP {e.code} {e.reason}",
                             parsed={"url": url, "status": e.code},
                             error=f"HTTP {e.code}")
        except (urllib.error.URLError, socket.timeout, TimeoutError,
                ssl.SSLError, OSError) as e:
            return NetResult(ok=False, output=f"Request failed: {e}",
                             parsed={"url": url}, error=str(e))

    def headers_inspect(self, url: str,
                        timeout: int | None = None) -> NetResult:
        """Fetch headers plus security-header analysis (HSTS, CSP, X-Frame…)."""
        r = self.http_head(url, timeout=timeout)
        if not r.ok and r.parsed.get("status") is None:
            return r
        headers = {k.lower(): v for k, v in r.parsed.get("headers", {}).items()}
        security = {
            "strict-transport-security": headers.get("strict-transport-security", ""),
            "content-security-policy": headers.get("content-security-policy", ""),
            "x-frame-options": headers.get("x-frame-options", ""),
            "x-content-type-options": headers.get("x-content-type-options", ""),
            "referrer-policy": headers.get("referrer-policy", ""),
            "permissions-policy": headers.get("permissions-policy", ""),
        }
        missing = [k for k, v in security.items() if not v]
        parsed = dict(r.parsed)
        parsed["security_headers"] = security
        parsed["missing_security_headers"] = missing
        out = r.output + "\n\nSecurity headers: " + (
            "all present" if not missing else "missing: " + ", ".join(missing))
        return NetResult(ok=r.ok, output=out, parsed=parsed,
                         error=r.error, duration_ms=r.duration_ms)

    def download_file(self, url: str, dest: str | os.PathLike,
                      timeout: int = 120,
                      max_bytes: int = 200 * 1024 * 1024,
                      headers: dict | None = None) -> NetResult:
        """Stream-download a URL to ``dest`` with a size cap."""
        d = Path(dest).expanduser().resolve()
        hdrs = {"User-Agent": self.user_agent}
        if headers:
            hdrs.update(headers)
        t0 = time.time()
        try:
            d.parent.mkdir(parents=True, exist_ok=True)
            req = _build_request(url, "GET", hdrs)
            total = 0
            with self._opener().open(req, timeout=timeout) as resp, \
                    open(d, "wb") as f:
                if not (200 <= resp.status < 400):
                    return NetResult(ok=False,
                                     output=f"HTTP {resp.status} {resp.reason}",
                                     parsed={"url": url, "status": resp.status},
                                     error=f"HTTP {resp.status}")
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise OSError(f"Download exceeds cap of {max_bytes} bytes")
                    f.write(chunk)
            dur = (time.time() - t0) * 1000
            return NetResult(ok=True,
                             output=f"Downloaded {total} bytes to {d} in {dur:.0f} ms",
                             parsed={"url": url, "dest": str(d),
                                     "bytes": total, "content_type":
                                     resp.headers.get("Content-Type", "")},
                             duration_ms=dur)
        except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout,
                TimeoutError, ssl.SSLError, OSError) as e:
            return NetResult(ok=False, output=f"Download failed: {e}",
                             parsed={"url": url, "dest": str(d)},
                             error=str(e))

    def check_urls(self, urls: list[str],
                   timeout: int | None = None) -> NetResult:
        """HEAD-check a list of URLs; report status per URL."""
        results = []
        for u in urls:
            r = self.http_head(u, timeout=timeout)
            results.append({
                "url": u, "ok": r.ok,
                "status": r.parsed.get("status"),
                "final_url": r.parsed.get("url", u),
                "error": r.error,
                "duration_ms": round(r.duration_ms, 1),
            })
        ok_count = sum(1 for x in results if x["ok"])
        lines = [f"{ok_count}/{len(results)} URLs reachable"]
        for x in results:
            mark = "OK " if x["ok"] else "FAIL"
            lines.append(f"  [{mark}] {x['status'] or '-'} {x['url']}"
                         + (f" -> {x['final_url']}" if x["final_url"] != x["url"] else ""))
        return NetResult(ok=True, output="\n".join(lines),
                         parsed={"results": results,
                                 "ok_count": ok_count,
                                 "total": len(results)})

    # -- URL parsing -----------------------------------------------------------

    def url_parse(self, url: str) -> NetResult:
        """Detailed URL breakdown: scheme, host, port, path, query params,
        fragment, credentials presence, IDNA/punycode, etc."""
        try:
            p = urllib.parse.urlparse(url)
        except ValueError as e:
            return NetResult(ok=False, output=f"Invalid URL: {e}",
                             parsed={}, error=str(e))
        query_params: dict[str, Any] = {}
        for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True):
            if k in query_params:
                if isinstance(query_params[k], list):
                    query_params[k].append(v)
                else:
                    query_params[k] = [query_params[k], v]
            else:
                query_params[k] = v
        try:
            hostname = p.hostname or ""
            port = p.port  # may raise ValueError on bad port
        except ValueError:
            hostname, port = p.hostname or "", None
        parsed = {
            "url": url,
            "scheme": p.scheme,
            "username": p.username,
            "has_password": p.password is not None,
            "hostname": hostname,
            "port": port,
            "explicit_port": port is not None,
            "path": p.path,
            "path_segments": [s for s in p.path.split("/") if s],
            "params": p.params,
            "query": p.query,
            "query_params": query_params,
            "fragment": p.fragment,
            "netloc": p.netloc,
            "is_absolute": bool(p.scheme and p.netloc),
            "is_https": p.scheme.lower() == "https",
            "punycode": "xn--" in (hostname or ""),
        }
        lines = [f"scheme={p.scheme} host={hostname} port={port or '(default)'}",
                 f"path={p.path or '/'}",
                 f"query_params={len(query_params)} fragment={p.fragment or '(none)'}",
                 f"https={parsed['is_https']} credentials_in_url={bool(p.username)}"]
        return NetResult(ok=True, output="\n".join(lines), parsed=parsed)

    def url_join(self, base: str, relative: str) -> NetResult:
        """Resolve a relative URL against a base (RFC 3986)."""
        joined = urllib.parse.urljoin(base, relative)
        return NetResult(ok=True, output=f"{base} + {relative} = {joined}",
                         parsed={"base": base, "relative": relative,
                                 "joined": joined})

    # -- DNS -------------------------------------------------------------------

    def dns_lookup(self, hostname: str,
                   timeout: int | None = None) -> NetResult:
        """Resolve a hostname to IP addresses (A/AAAA via getaddrinfo)."""
        host = hostname.strip()
        # Strip scheme if someone passes a URL.
        if "://" in host:
            host = urllib.parse.urlparse(host).hostname or host
        t0 = time.time()
        old_timeout = socket.getdefaulttimeout()
        try:
            socket.setdefaulttimeout(timeout or self.timeout)
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except Exception as e:  # gaierror, timeout, OSError, UnicodeError (bad input)
            return NetResult(ok=False, output=f"DNS lookup failed for {host}: {e}",
                             parsed={"hostname": host}, error=str(e))
        finally:
            socket.setdefaulttimeout(old_timeout)
        v4, v6 = [], []
        for fam, _t, _p, _canon, sockaddr in infos:
            ip = sockaddr[0]
            if fam == socket.AF_INET and ip not in v4:
                v4.append(ip)
            elif fam == socket.AF_INET6 and ip not in v6:
                v6.append(ip)
        dur = (time.time() - t0) * 1000
        parsed = {"hostname": host, "ipv4": v4, "ipv6": v6,
                  "all": v4 + v6}
        lines = [f"{host}:"]
        lines += [f"  A    {ip}" for ip in v4] or ["  (no A records)"]
        lines += [f"  AAAA {ip}" for ip in v6] or ["  (no AAAA records)"]
        return NetResult(ok=bool(v4 or v6), output="\n".join(lines),
                         parsed=parsed, duration_ms=dur)

    def reverse_dns(self, ip: str,
                    timeout: int | None = None) -> NetResult:
        """Reverse-resolve an IP to a hostname (PTR)."""
        old_timeout = socket.getdefaulttimeout()
        try:
            socket.setdefaulttimeout(timeout or self.timeout)
            host, aliases, _addrs = socket.gethostbyaddr(ip)
        except Exception as e:  # herror, gaierror, timeout, bad input
            return NetResult(ok=False, output=f"Reverse DNS failed for {ip}: {e}",
                             parsed={"ip": ip}, error=str(e))
        finally:
            socket.setdefaulttimeout(old_timeout)
        return NetResult(ok=True, output=f"{ip} -> {host}",
                         parsed={"ip": ip, "hostname": host,
                                 "aliases": aliases})

    # -- TCP / ports -------------------------------------------------------------

    def tcp_connect_test(self, host: str, port: int,
                         timeout: float = 5.0) -> NetResult:
        """Test whether a TCP connection to host:port succeeds."""
        t0 = time.time()
        try:
            with socket.create_connection((host, port), timeout=timeout) as sock:
                peer = sock.getpeername()
                local = sock.getsockname()
        except (socket.timeout, ConnectionRefusedError, OSError) as e:
            dur = (time.time() - t0) * 1000
            return NetResult(ok=False,
                             output=f"TCP {host}:{port} unreachable: {e}",
                             parsed={"host": host, "port": port,
                                     "open": False, "error": str(e)},
                             error=str(e), duration_ms=dur)
        dur = (time.time() - t0) * 1000
        return NetResult(ok=True,
                         output=f"TCP {host}:{port} OPEN ({dur:.0f} ms)",
                         parsed={"host": host, "port": port, "open": True,
                                 "peer": str(peer), "local": str(local)},
                         duration_ms=dur)

    def port_check(self, host: str, ports: list[int],
                   timeout: float = 3.0) -> NetResult:
        """Check multiple TCP ports on a host. Returns open/closed per port."""
        results = []
        for port in ports:
            r = self.tcp_connect_test(host, int(port), timeout=timeout)
            results.append({
                "port": int(port), "open": r.ok,
                "duration_ms": round(r.duration_ms, 1),
                "error": r.error if not r.ok else "",
            })
        open_ports = [x["port"] for x in results if x["open"]]
        lines = [f"{host}: {len(open_ports)}/{len(results)} ports open"]
        for x in results:
            lines.append(f"  {'OPEN  ' if x['open'] else 'closed'} {x['port']}")
        return NetResult(ok=True, output="\n".join(lines),
                         parsed={"host": host, "ports": results,
                                 "open_ports": open_ports})

    def ping_host(self, host: str, count: int = 3,
                  timeout_sec: int = 5) -> NetResult:
        """ICMP ping via the system ``ping`` binary; parses loss & RTT."""
        import shutil as _shutil
        ping_bin = _shutil.which("ping")
        if not ping_bin:
            return NetResult(ok=False, output="ping binary not found",
                             parsed={}, error="ping not installed")
        count = max(1, min(count, 10))
        try:
            proc = subprocess.run(
                [ping_bin, "-c", str(count), "-W", str(timeout_sec), host],
                capture_output=True, text=True,
                timeout=timeout_sec * count + 10)
        except subprocess.TimeoutExpired:
            return NetResult(ok=False, output=f"ping timed out for {host}",
                             parsed={"host": host}, error="timeout")
        out = proc.stdout + proc.stderr
        loss = None
        m = re.search(r"(\d+(?:\.\d+)?)% packet loss", out)
        if m:
            loss = float(m.group(1))
        rtt: dict[str, float] = {}
        m = re.search(r"rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)", out)
        if m:
            rtt = {"min": float(m.group(1)), "avg": float(m.group(2)),
                   "max": float(m.group(3)), "mdev": float(m.group(4))}
        reachable = proc.returncode == 0 or (loss is not None and loss < 100)
        lines = [f"ping {host}: {'reachable' if reachable else 'UNREACHABLE'}"]
        if loss is not None:
            lines.append(f"packet_loss={loss}%")
        if rtt:
            lines.append(f"rtt avg={rtt['avg']} ms (min={rtt['min']} max={rtt['max']})")
        return NetResult(ok=reachable, output="\n".join(lines) + "\n" + out.strip()[-1500:],
                         parsed={"host": host, "reachable": reachable,
                                 "packet_loss_pct": loss, "rtt_ms": rtt,
                                 "raw": out.strip()[-1500:]})

    # -- TLS ---------------------------------------------------------------------

    def ssl_info(self, hostname: str, port: int = 443,
                 timeout: float = 10.0, verify: bool = True) -> NetResult:
        """Fetch the TLS certificate: subject, issuer, validity dates,
        days-to-expiry, SANs, protocol version.

        ``verify`` defaults to True (proper chain verification). Set False
        only for testing against self-signed/lab certificates.
        """
        ctx = ssl.create_default_context()
        if not verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        t0 = time.time()
        try:
            with socket.create_connection((hostname, port), timeout=timeout) as sock:
                with ctx.wrap_socket(sock, server_hostname=hostname) as tls:
                    cert = tls.getpeercert()
                    cert_der = tls.getpeercert(binary_form=True)
                    version = tls.version()
                    cipher = tls.cipher()
        except (socket.timeout, ConnectionRefusedError, ssl.SSLError,
                OSError) as e:
            return NetResult(ok=False,
                             output=f"TLS handshake failed for {hostname}:{port}: {e}",
                             parsed={"hostname": hostname, "port": port},
                             error=str(e))
        dur = (time.time() - t0) * 1000
        if not cert:
            # CERT_NONE contexts (verify=False) don't populate getpeercert();
            # parse the DER form directly instead.
            if cert_der:
                cert = parse_der_cert(cert_der)
        if not cert:
            return NetResult(ok=False, output="No certificate presented",
                             parsed={"hostname": hostname}, error="no cert")
        def _name(ntuple):
            return ", ".join(f"{k}={v}" for seq in ntuple for k, v in seq)
        not_after = cert.get("notAfter", "")
        not_before = cert.get("notBefore", "")
        days_left: float | None = None
        expired = False
        try:
            exp = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(
                tzinfo=timezone.utc)
            days_left = (exp - datetime.now(timezone.utc)).total_seconds() / 86400
            expired = days_left < 0
        except (ValueError, TypeError):
            pass
        sans = [v for t, v in cert.get("subjectAltName", []) if t == "DNS"]
        parsed = {
            "hostname": hostname, "port": port,
            "subject": _name(cert.get("subject", ())),
            "issuer": _name(cert.get("issuer", ())),
            "not_before": not_before, "not_after": not_after,
            "days_until_expiry": round(days_left, 1) if days_left is not None else None,
            "expired": expired,
            "expiring_soon": days_left is not None and 0 <= days_left < 30,
            "san_dns": sans,
            "serial": cert.get("serialNumber", ""),
            "tls_version": version,
            "cipher": cipher[0] if cipher else "",
        }
        lines = [f"TLS {hostname}:{port} {version} cipher={parsed['cipher']}",
                 f"subject: {parsed['subject']}",
                 f"issuer:  {parsed['issuer']}",
                 f"valid:   {not_before} -> {not_after}",
                 f"expires in {parsed['days_until_expiry']} days"
                 + (" (EXPIRED!)" if expired else
                    " (expiring soon!)" if parsed["expiring_soon"] else ""),
                 f"SANs: {', '.join(sans[:8])}" + (" ..." if len(sans) > 8 else "")]
        return NetResult(ok=not expired, output="\n".join(lines),
                         parsed=parsed, duration_ms=dur)

    # -- local / misc --------------------------------------------------------------

    def local_interfaces(self) -> NetResult:
        """List local IP addresses (hostname resolution + socket trick)."""
        addrs = set()
        try:
            host = socket.gethostname()
            for info in socket.getaddrinfo(host, None):
                ip = info[4][0]
                if not ip.startswith("127."):
                    addrs.add(ip)
        except OSError:
            host = "unknown"
        # UDP trick to discover the default-route source address.
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(2)
            s.connect(("8.8.8.8", 80))
            addrs.add(s.getsockname()[0])
            s.close()
        except OSError:
            pass
        addrs = sorted(addrs)
        return NetResult(ok=True,
                         output=f"hostname={host}\n" + "\n".join(f"  {a}" for a in addrs),
                         parsed={"hostname": host, "addresses": addrs})

    def public_ip(self, timeout: int | None = None) -> NetResult:
        """Discover the public egress IP via an external echo service."""
        for svc in ("https://api.ipify.org", "https://ifconfig.me/ip"):
            r = self.http_get(svc, timeout=timeout or 10)
            if r.ok:
                ip = r.parsed.get("body", "").strip()
                if re.match(r"^[\d.a-fA-F:]+$", ip):
                    return NetResult(ok=True, output=f"public_ip={ip} (via {svc})",
                                     parsed={"public_ip": ip, "service": svc})
        return NetResult(ok=False, output="Could not determine public IP",
                         parsed={}, error="all echo services failed")

    def resolve_and_check(self, url: str,
                          timeout: int | None = None) -> NetResult:
        """End-to-end check: parse URL -> DNS -> TCP -> TLS -> HEAD."""
        steps: dict[str, Any] = {}
        up = self.url_parse(url)
        if not up.ok:
            return up
        steps["parse"] = up.parsed
        host = up.parsed["hostname"]
        port = up.parsed["port"] or (443 if up.parsed["is_https"] else 80)
        dns = self.dns_lookup(host, timeout=timeout)
        steps["dns"] = {"ok": dns.ok, "ipv4": dns.parsed.get("ipv4", []),
                        "error": dns.error}
        if not dns.ok:
            return NetResult(ok=False, output=f"DNS failed for {host}: {dns.error}",
                             parsed={"steps": steps}, error=dns.error)
        tcp = self.tcp_connect_test(host, port, timeout=timeout or 5.0)
        steps["tcp"] = {"ok": tcp.ok, "duration_ms": round(tcp.duration_ms, 1),
                        "error": tcp.error}
        if not tcp.ok:
            return NetResult(ok=False,
                             output=f"TCP {host}:{port} failed: {tcp.error}",
                             parsed={"steps": steps}, error=tcp.error)
        if up.parsed["is_https"]:
            tls = self.ssl_info(host, port)
            steps["tls"] = {"ok": tls.ok,
                            "days_until_expiry": tls.parsed.get("days_until_expiry"),
                            "expired": tls.parsed.get("expired"),
                            "error": tls.error}
        head = self.http_head(url, timeout=timeout)
        steps["http"] = {"ok": head.ok, "status": head.parsed.get("status"),
                         "final_url": head.parsed.get("url")}
        tls_line = "n/a (http)"
        if up.parsed["is_https"]:
            days = steps["tls"].get("days_until_expiry")
            tls_line = f"expires in {days}d" if days is not None else "checked"
        lines = [f"URL check: {url}",
                 f"  DNS:  {'OK ' + ','.join(steps['dns']['ipv4'][:2]) if dns.ok else 'FAIL'}",
                 f"  TCP:  {'OPEN ' + str(port) if tcp.ok else 'FAIL'}",
                 f"  TLS:  {tls_line}",
                 f"  HTTP: {steps['http']['status']}"]
        all_ok = dns.ok and tcp.ok and head.ok and (
            not up.parsed["is_https"] or steps["tls"]["ok"])
        return NetResult(ok=all_ok, output="\n".join(lines),
                         parsed={"url": url, "steps": steps})


__all__ = ["NetworkTools", "NetResult", "DEFAULT_TIMEOUT"]
