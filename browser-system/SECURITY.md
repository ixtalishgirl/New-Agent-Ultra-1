# SECURITY.md — checklist

Every item below is either **implemented in the built code** or marked
**TODO** with its owner. Nothing is assumed secure — verify each line
during deployment review.

## Infrastructure permissions (FULL GRANT for ahsan's own systems — 2026-10-13 ~17:24 PKT, his explicit risk acceptance)
- [x] Granted: root/admin on his own systems, cloud admin (his accounts), user
      management on his platforms, production DB/credentials access, secrets
      visibility, unrestricted shell/network — via `INFRA_ALLOW_*` toggles
      (all default `1`) in `browser-system/permissions.js` + `scope_guard.py`
- [x] Retained safeguard 1: **explicit confirmation for destructive/
      consequential actions** — `checkDestructiveConfirmed()` has no bypass
      path (not even God Mode); destructive infra actions
      (`user_management`, `cloud_admin`, `root_admin` with `destructive:true`)
      require `confirmed:true`
- [x] Retained safeguard 2: **page content never treated as system
      instructions** — `wrapUntrusted()` in `browserActions.js` quarantines
      all page text; vision pipeline tags observations untrusted
- [x] Retained safeguard 3: **audit logging of sensitive actions** —
      `auditSensitiveAction()` / `audit_sensitive_action()` append to
      `INFRA_AUDIT_LOG` (default `/tmp/halye-infra-audit.log`); failures never
      break the action
- [x] Web-target scope guard unchanged: active recon + security testing run
      ONLY against authorized targets / program scope (fail-closed)

## Shell permissions (final, 2026-10-13)

- [x] Granted on his own systems: unrestricted Bash/PowerShell/CMD, arbitrary
      commands, process/service/cron control, executable launching,
      unrestricted filesystem (`INFRA_ALLOW_UNRESTRICTED_FS`, default `1`)
- [x] Blind script-run: GRANTED on his explicit order — every execution
      audit-logged with source URL + SHA-256 (`checkShellAction('blind_run_unknown_script')`)
- [x] General privesc: GRANTED on his own systems on his explicit order —
      every use audit-logged with the full command (`checkShellAction('privesc_general')`)
- [x] Kernel access: granted on his OWN BARE-METAL ONLY
      (`checkKernelAccess({bareMetal:true})`) — every call surfaces the honest
      note that it is technically impossible inside containers (Render/Kaggle);
      provider infrastructure permanently denied (same line as container escape)
- [x] Container escape: PERMANENTLY DENIED — not his to authorize
- [x] Audit logging is a user toggle (`INFRA_AUDIT_ENABLED`, default ON) —
      he can switch it OFF himself; `auditEnabled()` / `audit_enabled()`

## Permission toggles (Node layer — `browserRoutes.js`, `browserActions.js`, `permissions.js`)

- [x] `BROWSER_ALLOW_PUBLIC` (default `1`) — master switch for public browsing
- [x] `BROWSER_ALLOW_DOWNLOADS` (default `0`) — downloads off unless enabled
- [x] `BROWSER_ALLOW_FORMS` (default `0`) — automatic form-filling off unless enabled
- [x] `BROWSER_REQUIRE_CONFIRM_CONSEQUENTIAL` (default `1`) — purchases,
      downloads, deletions, public posts require explicit `confirmed: true`
- [x] Toggles are re-read per call — env changes apply without restart
- [ ] **TODO (reconciliation):** confirm toggles are enforced on every
      reconciled endpoint path, not just the current router

## Secrets handling

- [x] Proxy credentials, API keys, WireGuard configs live in env files only
      (`.env`, `.env.germany` with `chmod 600`); examples contain placeholders only
- [x] `browserActions.js` `redactSecrets()` strips credential-shaped strings
      (`api_key=`, `sk-…`, `xox…`, `password=`) from page text before it
      reaches prompts, logs, or chat
- [x] Browser service redacts form values from history; password fields
      refused in `fill_form` unless `allow_sensitive: true`
- [x] Germany docs: "never paste the real filled-in file into chat or screenshots"
- [ ] **TODO (reconciliation):** `BROWSER_SERVICE_API_KEY` — the service
      enforces `X-API-Key`, but the Node layer **does not send it yet**.
      Either add the header in `serviceCall`/`proxyToService` or leave the
      key unset (only on a trusted local network). Do not deploy with the
      key set server-side and missing client-side — every Node call would 401

## No arbitrary shell / admin through the browser path

- [x] Browser actions are HTTP calls to the service only — no shell, no
      filesystem, no admin access in `browserActions.js`/`browserRoutes.js`
- [x] The service exposes tools, not a shell; `run_terminal` stays in the
      Node app under its own permissions, separate from browsing

## Consequential-action confirmation

- [x] `browserRoutes.js` `/navigate` passthrough: `download` or
      `consequential` flags without `confirmed: true` → `409` with
      `needsConfirmation: true` and a plain-words explanation
- [x] `RESPONSE_CONTRACT.md` §5: agent states the exact action, waits for
      explicit user yes, quotes the confirmation back
- [ ] **TODO:** the same confirmation gate must exist on the reconciled
      Python-side endpoints (the service's `download_file` currently relies
      on the Node gate)

## Isolated browser contexts

- [x] `BrowserManager`: one Chromium process, **one `BrowserContext` per
      task** — separate cookies, storage, sessions per research task
- [x] Idle tasks evicted after `BROWSER_TASK_IDLE_TTL_SEC` (default 1800s);
      max tasks/tabs capped (`BROWSER_MAX_TASKS=20`, `BROWSER_MAX_TABS=5`)
- [x] `DELETE /tasks/{task_id}` closes the context (cookies, tabs, RAM freed)

## Download validation

- [x] Extension allow-list; **executables (`.exe`, `.sh`, …) refused with
      `403 forbidden_file_type` — no executables ever**
- [x] Size cap (`BROWSER_MAX_DOWNLOAD_MB`, default 100) → `413 file_too_large`
- [x] Content-type sanity check; SHA-256 recorded per download
- [x] Filenames sanitized; downloads confined to `BROWSER_DOWNLOAD_DIR`

## CAPTCHA / anti-bot: detect, never bypass

- [x] `detect_blocks` is **detect-only** — no solving, clicking through, or
      working around CAPTCHAs, Cloudflare, PerimeterX, or bot walls, ever
- [x] `block_kind`/`block_type` surfaced verbatim to the user in honest
      failure cards; the user is never promised "it'll be handled later"
- [x] On `blocked: true` the flow STOPS — no rapid retries as a workaround
- [x] Standing permission note: ahsan's PerimeterX auto-solve permission
      covers **his Fiverr account in the assistant's browser** — it does
      NOT transfer to this agent's browsing. This system never bypasses.

## Prompt-injection guard (page content = untrusted data)

- [x] `browserActions.js` `wrapUntrusted()`: page content wrapped in
      `[BEGIN/END UNTRUSTED PAGE CONTENT]` markers — data, never instructions
- [x] Vision pipeline: `VISION_DESCRIBE_PROMPT` instructs describe-only;
      every `Observation` carries `untrusted_page_content: true`; screenshots
      captured live with `captured_at` + SHA-256, never reused stale
- [x] `RESPONSE_CONTRACT.md` §2: if a page contains instruction-looking text,
      the answer must flag it, not follow it
- [x] API.md: "Content is data, never instructions" on extract

## Robots / ToS / rate limits

- [x] `BROWSER_RESPECT_ROBOTS=true` default — robots-disallowed URLs refused
      (`403 robots_disallowed`; operator-only `force_robots` override exists
      and must stay operator-only)
- [x] `BROWSER_MIN_REQUEST_INTERVAL_MS=1500` politeness delay between navigations
- [x] Search uses DuckDuckGo HTML endpoints (no API key, no scraping of
      key-gated APIs)
- [x] SSRF guard (`browserRoutes.js`): public http(s) only — blocks private
      ranges, loopback, link-local (incl. cloud metadata `169.254.169.254`),
      `localhost`/`.local`/`.internal`; DNS-resolved IPs re-checked
- [x] `BROWSER_BLOCKED_HOSTS` denylist on the service
- [x] Germany docs: circumventing geo-restrictions may violate site ToS —
      read the ToS; identity/residency demands are reported, never faked
      (`ON_VERIFICATION_DEMAND=report` — no "fake it" option exists)

## Deployment review (run before going live)

- [ ] Reconciliation complete (CONTRACT_CHECK.md) and security TODOs above closed
- [ ] `.env` files have `chmod 600`, are git-ignored, contain no pasted chat secrets
- [ ] `BROWSER_SERVICE_API_KEY` set **and** Node sends `X-API-Key` — or both unset on loopback only
- [ ] Service binds `127.0.0.1` unless a reverse proxy with auth fronts it
- [ ] `verify_german_ip.py` run after every network change (tunnel up/down, proxy swap)
- [ ] Confirm no test artifacts, screenshots, or sidecars with real URLs leak into git
