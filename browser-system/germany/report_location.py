#!/usr/bin/env python3
"""
report_location.py — Report the ACTUAL network location of the agent's
browser traffic vs the backend, side by side.

Why two separate numbers matter:
  The backend (this machine / the agent process) and the browser service
  (the thing that actually loads web pages) can exit the internet from
  DIFFERENT places. A German VPS proxy on the backend means nothing if the
  browser still exits directly. This script shows both, honestly, so ahsan
  never mistakes one for the other.

Usage:
    python3 report_location.py
    python3 report_location.py --status-url http://localhost:8000
    python3 report_location.py --json

The browser service is expected to expose GET <status-url>/status returning
JSON with an "egress_ip" (or "public_ip"/"ip") field. If it doesn't, the
browser column is reported UNKNOWN — never guessed.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from verify_german_ip import (  # noqa: E402
    browser_egress_ip,
    local_egress_ip,
    geolocate,
    load_dotenv_germany,
)


def describe(name, ip, err, expected="DE"):
    row = {"name": name, "egress_ip": ip, "error": err,
           "location": None, "appears_german": None}
    if err or not ip:
        return row
    geo, gerr = geolocate(ip)
    if not geo:
        row["error"] = f"geolocation failed: {gerr}"
        return row
    row["location"] = {
        "country": geo.get("country"),
        "country_code": geo.get("country_code"),
        "city": geo.get("city"),
        "region": geo.get("region"),
        "asn": geo.get("asn"),
        "isp": geo.get("isp"),
        "source": geo.get("source"),
    }
    row["appears_german"] = (geo.get("country_code") or "").upper() == expected.upper()
    return row


def main():
    ap = argparse.ArgumentParser(description="Browser vs backend location report.")
    ap.add_argument("--status-url", default=os.environ.get("BROWSER_SERVICE_STATUS_URL", ""))
    ap.add_argument("--country", default=os.environ.get("EXPECTED_COUNTRY_CODE", "DE"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    load_dotenv_germany()

    rows = []

    # Backend / local machine
    ip, err = local_egress_ip()
    rows.append(describe("backend (agent process / this machine)", ip, err, args.country))

    # Browser service
    if args.status_url:
        ip, err = browser_egress_ip(args.status_url)
        rows.append(describe("browser (web traffic egress)", ip, err, args.country))
    else:
        rows.append({"name": "browser (web traffic egress)", "egress_ip": None,
                     "error": ("not checked — pass --status-url or set "
                               "BROWSER_SERVICE_STATUS_URL"),
                     "location": None, "appears_german": None})

    if args.json:
        print(json.dumps({"expected_country": args.country.upper(), "rows": rows}, indent=2))
        return 0

    print("=" * 64)
    print("NETWORK LOCATION REPORT — browser vs backend")
    print("=" * 64)
    for r in rows:
        print(f"\n[{r['name']}]")
        if r["error"]:
            print(f"  location: UNKNOWN — {r['error']}")
            continue
        loc = r["location"]
        print(f"  egress IP:    {r['egress_ip']}")
        print(f"  country:      {loc['country']} ({loc['country_code']})")
        print(f"  city/region:  {loc['city']}, {loc['region']}")
        print(f"  ASN/ISP:      {loc['asn']} / {loc['isp']}")
        print(f"  source:       {loc['source']}")
        print(f"  appears German: {'YES' if r['appears_german'] else 'NO'}")

    b, br = rows[0], rows[1]
    print("\n" + "-" * 64)
    if b["location"] and br["location"]:
        same = b["egress_ip"] == br["egress_ip"]
        print("backend and browser exit from the "
              + ("SAME" if same else "DIFFERENT") + " egress IP.")
        if not same:
            print("Judge the 'Germany option' by the BROWSER row only — that is")
            print("what websites actually see.")
    else:
        print("Could not compare both paths (see UNKNOWN rows above).")
    print("-" * 64)
    print("\nReminder: a German IP is a network fact, not an identity.")
    print("Residency/account demands from sites must be reported, not faked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
