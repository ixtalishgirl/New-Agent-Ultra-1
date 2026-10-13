# Test Plan — Germany web-access option

How to read the **Run** column:

* `LOCAL` — runnable right now from any machine, no German infra needed.
* `LIVE-GERMAN` — needs a real German egress (VPS tunnel or German proxy)
  configured first. Cannot be faked; do not mark pass without it.
* `LIVE-BROWSER` — needs the actual browser service running with a
  `/status` endpoint. This subagent cannot operate a live browser, so these
  are delegated.

## A. verify_german_ip.py

| # | Test | Run | Expect |
|---|---|---|---|
| A1 | `python3 verify_german_ip.py` on a machine with **no** German routing | LOCAL ✅ done 2026-10-13 | `appears German [backend_local]: no`, exit code 1, evidence shows real country/city/ASN |
| A2 | Same, with `--json` | LOCAL ✅ done 2026-10-13 | Valid JSON; `targets.backend_local.appears_german == false` |
| A3 | `--status-url http://localhost:9` (nothing listening) | LOCAL ✅ done 2026-10-13 | Browser row: `UNKNOWN — browser /status unreachable`; exit code 1; no crash |
| A4 | `--status-url` pointing at a stub server returning `{"egress_ip": "203.0.113.7"}` | LOCAL | Script geolocates the stub IP (documentation range → lookup fails gracefully, reported UNKNOWN, not German) |
| A5 | `python3 verify_german_ip.py` with WireGuard tunnel **up** to German VPS | LIVE-GERMAN | `appears German [backend_local]: yes`, city in Germany, ASN = VPS provider, exit code 0 |
| A6 | A5, then `wg-quick down`, re-run immediately | LIVE-GERMAN | Flips to `no` — proves the verdict tracks reality, not config |
| A7 | `--status-url <real browser service>` with German proxy configured on the browser | LIVE-BROWSER + LIVE-GERMAN | **Browser** row: `appears German: yes`; backend row may differ — that difference is the point of the check |
| A8 | DNS check with `GERMAN_DNS_PRIMARY` set but unreachable | LOCAL/LIVE-GERMAN | DNS verdict FAIL with actionable message (not a silent pass) |
| A9 | Rate-limit behavior: run 3x in quick succession | LOCAL | All succeed; ≤3 requests/run stays far under ip-api.com's 45 req/min free limit |

## B. report_location.py

| # | Test | Run | Expect |
|---|---|---|---|
| B1 | `python3 report_location.py` (no status URL) | LOCAL ✅ done 2026-10-13 | Backend row populated honestly; browser row UNKNOWN with guidance text |
| B2 | `--json` | LOCAL | Valid JSON with `rows` array |
| B3 | With browser service on German egress, backend direct | LIVE-BROWSER + LIVE-GERMAN | Rows show DIFFERENT countries; footer says "judge by the BROWSER row only" |

## C. Config / WireGuard templates

| # | Test | Run | Expect |
|---|---|---|---|
| C1 | `config/.env.germany.example` parses as env file (no secrets present) | LOCAL | `grep -c '<\|user:pass@' ` shows only placeholders/examples; file contains zero real credentials |
| C2 | `wg-germany.conf.example` — every placeholder uses `<...>` | LOCAL | `grep -o '<[^>]*>'` lists only placeholders; `wg-quick` would refuse it unfilled (by design) |
| C3 | Full tunnel up → `curl ifconfig.me` from browser machine shows VPS IP; `verify_german_ip.py` agrees | LIVE-GERMAN | Both agree on the same German IP (cross-check, not self-report) |
| C4 | `wg-quick down` → traffic returns to direct; verify script flips to `no` | LIVE-GERMAN | Clean teardown, no leftover routes (`ip route` inspected) |

## D. Honesty / negative tests (must never "pass" by faking)

| # | Test | Run | Expect |
|---|---|---|---|
| D1 | Point `BROWSER_HTTP_PROXY` at a **non**-German proxy, run verify | LIVE-GERMAN (proxy) | Verdict `no` with the proxy country's evidence — the script must not be fooled by the URL scheme |
| D2 | Confirm no code path claims identity/residency | LOCAL | `grep -ri 'residen\|citizen\|identity' *.py` returns only the disclaimer lines |
| D3 | Confirm no real keys/credentials in repo | LOCAL ✅ done 2026-10-13 | `grep -rniE 'PRIVATE KEY\|api_key' config/ tests/ *.py` → only instructional comments, no key material |

## Live-run checklist (for the parent agent / ahsan)

- [ ] A1–A3, B1: done (this VM, 2026-10-13) — logic verified, honest negatives confirmed.
- [ ] **Blocked on ahsan's decision:** procure German VPS (~€4–6/mo) or German proxy — no purchase made by this build.
- [ ] After procurement: A5–A7, B3, C3–C4 (need the tunnel/proxy live).
- [ ] After browser service exposes `/status` with `egress_ip`: A7, B3 (needs live browser delegation — subagents can't run it).
- [ ] Optional hardening: run `verify_german_ip.py` from the browser service's health check on startup.
