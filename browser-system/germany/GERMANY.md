# Germany Web-Access Option — honest guide

**Status of this module:** plumbing + verification implemented. **No German
egress exists yet** — getting a real German IP requires ahsan to procure
infrastructure (his decision). This document explains exactly what works,
what doesn't, and what to do when sites ask for more than an IP.

---

## 1. What this gives you

When configured, the agent's **browser traffic** exits the internet from a
German IP address (e.g. a VPS in Frankfurt/Nuremberg). Websites that do
plain IP geolocation will see: country = Germany.

That's it. That is the entire promise. Read section 4 before relying on it.

## 2. What actually produces a real German IP

Only an exit node physically/contractually in Germany, controlled by you:

| Option | Typical cost | Notes |
|---|---|---|
| **Your own VPS in Germany** (recommended) | ~€4–6/month | Full control; run WireGuard (`config/wireguard/`) or a proxy on it. Datacenter IPs are sometimes blocked by picky sites — honest signal, not a failure. |
| **Commercial German proxy/VPN provider** | varies (€3–10/month) | Less setup, less control; check their logging policy and ToS. |
| WireGuard tunnel to the VPS | included above | `config/wireguard/wg-germany.conf.example` + `SETUP.md` |

**Procurement is ahsan's decision.** Nothing here buys, rents, or signs up
for anything. No accounts were created, no payment made.

## 3. What does NOT give you a German IP

* **Free VPNs / free proxies** — unreliable, frequently IP-blocked by
  major sites, often log traffic, some inject ads or worse. Not used here,
  not recommended. If a site works "sometimes" through one, that's luck,
  not infrastructure.
* **Setting a proxy URL alone** — the URL is just an address; the *exit
  node's location* is what counts. A `socks5://` URL pointing at a US
  server gives you a US IP with extra steps.
* **DNS changes alone** — DNS doesn't change your IP. (It matters for
  leak-prevention, which is why `verify_german_ip.py` checks it.)
* **Browser locale / timezone / language spoofing** — cosmetic only, and
  explicitly out of scope: this project never fakes physical location or
  identity signals beyond the real network egress.

## 4. The honesty rule (from ahsan's spec)

> NEVER fake physical location or identity. If a site demands residency,
> an account, or extra verification beyond IP, report the actual
> requirement honestly.

In practice:

* **IP-only checks** (most geo-fenced content, price localization): the
  German egress handles these legitimately.
* **Residency / KYC / local phone / German payment method / account
  history checks**: these are *identity* checks, not network checks. The
  agent must surface them to ahsan as-is: *"Site X requires [specific
  thing]. I cannot provide this."* No fake documents, no fake numbers,
  no borrowed identities — ever.
* `ON_VERIFICATION_DEMAND=report` in `.env.germany.example` encodes this:
  the only configured behavior is to report.

## 5. Setup paths

### Path A — WireGuard tunnel (recommended, full control)

1. Procure the German VPS (your decision).
2. Follow `config/wireguard/SETUP.md` (server side + client side).
3. Copy `config/.env.germany.example` → `config/.env.germany`, fill in
   values, `chmod 600` it.
4. `sudo wg-quick up wg-germany`
5. `python3 verify_german_ip.py` → expect `appears German: yes`.

### Path B — Proxy only (simpler, less leak-proof)

1. Get a German proxy endpoint (your VPS running tinyproxy/squid, or a
   commercial German proxy).
2. Fill `BROWSER_HTTP_PROXY` / `BROWSER_HTTPS_PROXY` in
   `config/.env.germany` (prefer `socks5h://` so DNS goes through the
   proxy too).
3. Start the browser service with the env file loaded.
4. `python3 verify_german_ip.py --status-url <browser-service-url>` →
   the **browser** row must say `appears German: yes`.

## 6. Verification

| Script | Checks |
|---|---|
| `verify_german_ip.py` | Egress IP → geolocation (ip-api.com free, ipinfo.io fallback) → PASS/FAIL `appears German: yes/no` with country/city/ASN evidence; DNS consistency; IPv4/IPv6. Verifies the **browser's** egress via `--status-url` (the browser service's `/status`), not just the backend. |
| `report_location.py` | Side-by-side: backend location vs browser location. Judge the "Germany option" by the **browser** row — that's what sites see. |

ip-api.com free tier: HTTP only, **45 requests/minute per IP** — the
scripts make ~2–3 requests per run, well within limits. Don't loop them in
a tight cron.

**Verify after every change** (tunnel up/down, proxy swap, VPS reboot).
A stale "it was German yesterday" is how leaks go unnoticed.

## 7. When a site still says no

1. Re-run `verify_german_ip.py` — confirm the egress is *actually* German
   right now.
2. If yes and the site still refuses: it's likely blocking datacenter IPs
   or demanding identity verification. **Report it** (site, page, exact
   requirement) — don't escalate to shadier infrastructure.
3. If the requirement is residency/account/KYC: stop, report, wait for
   ahsan. There is no technical bypass for "prove you're German" that
   isn't fraud.

## 8. ToS & legal notes (plain language, not legal advice)

* Using your own VPS/VPN to route traffic is ordinary network operation.
  Circumventing a site's geo-restriction may violate that site's Terms of
  Service — read the ToS of sites that matter to you.
* You are responsible for traffic exiting your VPS (abuse complaints go
  to the VPS owner — ahsan). Keep it patched, keep keys private.
* Nothing here helps impersonate a German resident. Identity fraud is a
  crime in Germany and Pakistan; this project refuses that entire
  category by design.

## 9. File map

```
germany/
├── GERMANY.md                  # this guide
├── verify_german_ip.py         # PASS/FAIL German-egress verification
├── report_location.py          # browser-vs-backend location report
├── config/
│   ├── .env.germany.example    # proxy/DNS/tunnel settings (template)
│   └── wireguard/
│       ├── wg-germany.conf.example  # client config TEMPLATE (placeholders)
│       └── SETUP.md                # VPS + client setup instructions
└── tests/
    └── TEST_PLAN.md            # what to run, incl. live-run requirements
```

## 10. What still needs ahsan

* [ ] **Decision:** procure a German VPS (~€4–6/mo) or a commercial German
  proxy/VPN — or defer the Germany option entirely.
* [ ] If yes: run Path A or B above, then the verification scripts.
* [ ] Optional: wire `verify_german_ip.py` into the browser service's
  startup/health check so a dead tunnel fails loudly instead of silently
  exiting from the wrong country.
