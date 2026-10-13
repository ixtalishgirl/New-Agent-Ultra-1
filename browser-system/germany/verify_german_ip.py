#!/usr/bin/env python3
"""
verify_german_ip.py — Verify that browser/backend egress traffic appears German.

What it does:
  (a) Queries free geolocation endpoints (ip-api.com free tier first,
      respecting its 45 req/min limit; ipinfo.io free as fallback).
  (b) Reports country / city / ASN / ISP / IP.
  (c) Runs a DNS consistency check (configured German DNS vs system DNS).
  (d) Checks IPv4 / IPv6 routing state.
  (e) Prints a clear PASS/FAIL summary: "appears German: yes/no" + evidence.

Two modes:
  * Local mode (default): checks THIS machine's egress IP.
  * Browser mode (--status-url http://host:port): fetches the browser
    service's /status endpoint and verifies the BROWSER's egress IP,
    not just the backend's. This is the mode that matters for ahsan's
    agent: the backend and the browser can exit from different places.

Browser service /status contract (simple on purpose):
    GET <status-url>/status  ->  JSON, one of:
        {"egress_ip": "203.0.113.10"}        (preferred)
        {"public_ip": "203.0.113.10"}
        {"ip": "203.0.113.10"}
    ...or a plain-text body containing just an IP address.
If /status is unreachable or has no IP, the script says so honestly and
only reports the local result.

Honesty rules baked in:
  * A German IP is reported as exactly that — a German IP. It is NEVER
    reported as German identity/residency. See GERMANY.md.
  * ip-api.com free tier is HTTP-only and rate-limited (45 req/min/IP).
    This script makes at most ~3 requests per run: well within limits.

Usage:
    python3 verify_german_ip.py
    python3 verify_german_ip.py --status-url http://localhost:8000
    python3 verify_german_ip.py --country DE        # expected country code
    python3 verify_german_ip.py --json              # machine-readable output
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error

EXPECTED_DEFAULT = os.environ.get("EXPECTED_COUNTRY_CODE", "DE")
IP_API_URL = "http://ip-api.com/json/{ip}?fields=status,message,country,countryCode,regionName,city,isp,org,as,query"
IPINFO_URL = "https://ipinfo.io/{ip}/json"
TEST_HOSTNAME = "example.com"   # stable, neutral test name for DNS checks

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "ahsan-agent-verify/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def load_dotenv_germany():
    """Load config/.env.germany if present (never required)."""
    here = os.path.dirname(os.path.abspath(__file__))
    for name in (".env.germany",):
        path = os.path.join(here, "config", name)
        if os.path.exists(path):
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


def system_dns_servers():
    servers = []
    try:
        with open("/etc/resolv.conf") as f:
            for line in f:
                m = re.match(r"\s*nameserver\s+(\S+)", line)
                if m:
                    servers.append(m.group(1))
    except OSError:
        pass
    return servers


def dig_query(server, hostname, timeout=8):
    """Resolve hostname via a specific DNS server. Returns (answers, error)."""
    for tool in (["dig", "+short", "+time=5", "+tries=1", f"@{server}", hostname],
                 ["nslookup", hostname, server]):
        try:
            out = subprocess.run(tool, capture_output=True, text=True, timeout=timeout)
            answers = [l.strip() for l in out.stdout.splitlines()
                       if l.strip() and not l.strip().startswith(";")]
            if answers:
                return answers, None
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        except Exception as e:  # noqa: BLE001 - report, don't crash
            return [], str(e)
    return [], "no dig/nslookup available"


def has_ipv6_connectivity():
    try:
        s = socket.create_connection(("2606:4700:4700::1111", 53), timeout=5)
        s.close()
        return True
    except OSError:
        return False

# --------------------------------------------------------------------------
# geolocation
# --------------------------------------------------------------------------

def geolocate_ipapi(ip):
    """Primary source: ip-api.com free tier. Respects its 45 req/min limit
    (we issue at most a couple of requests per run)."""
    try:
        data = json.loads(http_get(IP_API_URL.format(ip=ip or "")))
    except Exception as e:  # noqa: BLE001
        return None, f"ip-api.com unreachable: {e}"
    if data.get("status") != "success":
        return None, f"ip-api.com: {data.get('message', 'lookup failed')}"
    return {
        "source": "ip-api.com",
        "ip": data.get("query"),
        "country": data.get("country"),
        "country_code": data.get("countryCode"),
        "region": data.get("regionName"),
        "city": data.get("city"),
        "isp": data.get("isp"),
        "org": data.get("org"),
        "asn": data.get("as"),
    }, None


def geolocate_ipinfo(ip):
    """Fallback: ipinfo.io free tier (no token)."""
    try:
        data = json.loads(http_get(IPINFO_URL.format(ip=ip or "")))
    except Exception as e:  # noqa: BLE001
        return None, f"ipinfo.io unreachable: {e}"
    if "bogon" in data:
        return None, "ipinfo.io: bogon/private IP"
    return {
        "source": "ipinfo.io",
        "ip": data.get("ip"),
        "country": None,
        "country_code": data.get("country"),
        "region": data.get("region"),
        "city": data.get("city"),
        "isp": data.get("org"),
        "org": data.get("org"),
        "asn": data.get("org"),
    }, None


def geolocate(ip, country_hint=""):
    info, err = geolocate_ipapi(ip)
    if info:
        return info, None
    # Only fall back on failure — never double-query just for fun.
    time.sleep(1)  # be polite even on the fallback path
    info2, err2 = geolocate_ipinfo(ip)
    if info2:
        info2["note"] = f"primary failed ({err}); used fallback"
        return info2, None
    return None, f"both sources failed: [{err}] / [{err2}]"

# --------------------------------------------------------------------------
# egress IP discovery
# --------------------------------------------------------------------------

def local_egress_ip():
    """This machine's public IPv4 as seen by the geolocation service itself."""
    info, err = geolocate_ipapi("")
    if info:
        return info["ip"], None
    info, err = geolocate_ipinfo("")
    if info:
        return info["ip"], None
    return None, f"could not determine local egress IP: {err}"


def browser_egress_ip(status_url):
    """Ask the browser service for ITS egress IP via /status."""
    url = status_url.rstrip("/") + "/status"
    try:
        body = http_get(url)
    except Exception as e:  # noqa: BLE001
        return None, f"browser /status unreachable at {url}: {e}"
    try:
        data = json.loads(body)
        for key in ("egress_ip", "public_ip", "ip"):
            if data.get(key):
                return data[key], None
        return None, f"/status returned JSON but no egress_ip/public_ip/ip field: {body[:200]}"
    except json.JSONDecodeError:
        m = re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", body)
        if m:
            return m.group(0), None
        return None, f"/status returned non-JSON body with no IP: {body[:200]}"

# --------------------------------------------------------------------------
# DNS check
# --------------------------------------------------------------------------

def dns_check():
    """Heuristic DNS consistency check.

    A 'leak' here means: DNS answers come from a resolver OUTSIDE the
    German path while traffic exits inside it (or vice versa) — which can
    betray the real location to DNS-based checks.
    """
    result = {
        "system_dns": system_dns_servers(),
        "configured_german_dns": [d for d in
            (os.environ.get("GERMAN_DNS_PRIMARY"), os.environ.get("GERMAN_DNS_SECONDARY"))
            if d],
        "queries": {},
        "verdict": "UNKNOWN",
        "detail": "",
    }
    # Query the test hostname through each resolver we know about.
    targets = {}
    for i, srv in enumerate(result["system_dns"][:2]):
        targets[f"system[{i}]={srv}"] = srv
    for i, srv in enumerate(result["configured_german_dns"]):
        targets[f"german[{i}]={srv}"] = srv
    for label, server in targets.items():
        answers, err = dig_query(server, TEST_HOSTNAME)
        result["queries"][label] = {"answers": answers[:4], "error": err}

    german_cfg = result["configured_german_dns"]
    if not german_cfg:
        result["verdict"] = "WARN"
        result["detail"] = ("No German DNS configured (GERMAN_DNS_PRIMARY empty). "
                            "With socks5h or a full-tunnel WireGuard setup DNS follows "
                            "the tunnel/proxy automatically — verify that instead. "
                            "System resolver(s): " + (", ".join(result["system_dns"]) or "unknown"))
        return result

    ok = [l for l in result["queries"]
          if l.startswith("german[") and result["queries"][l]["answers"]]
    if ok:
        result["verdict"] = "PASS"
        result["detail"] = ("Configured German DNS answered the test query "
                            f"({', '.join(ok)}). DNS path looks consistent.")
    else:
        result["verdict"] = "FAIL"
        result["detail"] = ("Configured German DNS did NOT answer the test query. "
                            "DNS may be leaking via the system resolver — fix DNS "
                            "before relying on the German route.")
    return result

# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Verify German egress for ahsan's agent browser.")
    ap.add_argument("--status-url", default=os.environ.get("BROWSER_SERVICE_STATUS_URL", ""),
                    help="Browser service base URL (checks BROWSER egress via /status).")
    ap.add_argument("--country", default=EXPECTED_DEFAULT,
                    help="Expected ISO country code (default: DE).")
    ap.add_argument("--json", action="store_true", help="Machine-readable JSON output.")
    args = ap.parse_args()

    load_dotenv_germany()
    expected = (args.country or "DE").upper()

    report = {"expected_country": expected, "targets": {}}

    # ---- target 1: browser egress (if a status URL was given) ----
    if args.status_url:
        ip, err = browser_egress_ip(args.status_url)
        report["targets"]["browser"] = {"via": args.status_url + "/status",
                                        "egress_ip": ip, "error": err}
    else:
        report["targets"]["browser"] = {
            "via": None,
            "egress_ip": None,
            "error": ("no --status-url / BROWSER_SERVICE_STATUS_URL given; "
                      "browser egress NOT verified (only backend/local below)")}

    # ---- target 2: local/backend egress ----
    ip, err = local_egress_ip()
    report["targets"]["backend_local"] = {"via": "this machine",
                                          "egress_ip": ip, "error": err}

    # ---- geolocate each discovered IP ----
    for name, t in report["targets"].items():
        if not t["egress_ip"]:
            t["geo"] = None
            continue
        info, err = geolocate(t["egress_ip"])
        t["geo"] = info
        t["geo_error"] = err
        t["appears_german"] = bool(info and (info.get("country_code") or "").upper() == expected)

    # ---- DNS + IP version checks (properties of THIS machine) ----
    report["dns"] = dns_check()
    report["ipv6_connectivity"] = has_ipv6_connectivity()
    try:
        report["local_addresses"] = socket.getaddrinfo(socket.gethostname(), None) and True
    except socket.gaierror:
        report["local_addresses"] = False

    # ---- verdict ----
    verdicts = []
    for name, t in report["targets"].items():
        if t["egress_ip"] and t.get("geo"):
            verdicts.append((name, t["appears_german"], t["geo"]))

    if args.json:
        print(json.dumps(report, indent=2))
        return 0 if all(v[1] for v in verdicts if v[0] == "browser") else 1

    # ---- human-readable output ----
    print("=" * 64)
    print("GERMAN EGRESS VERIFICATION")
    print("=" * 64)
    for name, t in report["targets"].items():
        label = "BROWSER (agent web traffic)" if name == "browser" else "BACKEND/LOCAL (this machine)"
        print(f"\n--- {label} ---")
        print(f"via:        {t['via']}")
        if t["error"]:
            print(f"egress IP:  UNKNOWN — {t['error']}")
            continue
        print(f"egress IP:  {t['egress_ip']}")
        g = t.get("geo")
        if not g:
            print(f"geolocate:  FAILED — {t.get('geo_error')}")
            continue
        print(f"source:     {g['source']}")
        print(f"country:    {g.get('country')} ({g.get('country_code')})")
        print(f"city:       {g.get('city')}, {g.get('region')}")
        print(f"ASN/ISP:    {g.get('asn')} / {g.get('isp')}")
        mark = "YES" if t["appears_german"] else "NO"
        print(f"appears German: {mark}")
        if g.get("note"):
            print(f"note:       {g['note']}")

    d = report["dns"]
    print("\n--- DNS CHECK ---")
    print(f"system DNS:           {', '.join(d['system_dns']) or 'unknown'}")
    print(f"configured German DNS:{', '.join(d['configured_german_dns']) or '(none)'}")
    for label, q in d["queries"].items():
        ans = ", ".join(q["answers"][:2]) if q["answers"] else f"ERROR: {q['error']}"
        print(f"  {label}: {ans}")
    print(f"DNS verdict: {d['verdict']} — {d['detail']}")

    print("\n--- IP VERSIONS ---")
    print(f"IPv6 connectivity from here: {'yes' if report['ipv6_connectivity'] else 'no'}")
    if report["ipv6_connectivity"]:
        print("note: with IPv6 active, confirm the German path covers IPv6 too")
        print("      (AllowedIPs includes ::/0, or the proxy handles AAAA).")

    print("\n" + "=" * 64)
    print("SUMMARY")
    print("=" * 64)
    any_german = False
    for name, t in report["targets"].items():
        if t["egress_ip"] and t.get("geo"):
            yn = "yes" if t["appears_german"] else "no"
            any_german = any_german or t["appears_german"]
            print(f"appears German [{name}]: {yn}   (IP {t['egress_ip']}, "
                  f"{t['geo'].get('country_code')}, {t['geo'].get('city')})")
        else:
            print(f"appears German [{name}]: UNKNOWN — {t['error']}")
    print(f"\nDNS: {d['verdict']}")
    print("\nA 'yes' means: the egress IP geolocates to Germany. It does NOT")
    print("mean German identity, residency, or account standing. If a site")
    print("demands more than an IP, report the requirement — never fake it.")
    print("=" * 64)

    # exit code: 0 only if the BROWSER target (when checked) appears German
    checked = [t for n, t in report["targets"].items()
               if t["egress_ip"] and t.get("geo") and (n == "browser" or not args.status_url)]
    return 0 if checked and all(t["appears_german"] for t in checked) else 1


if __name__ == "__main__":
    sys.exit(main())
