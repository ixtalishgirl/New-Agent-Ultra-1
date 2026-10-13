# IMPLEMENTATION_STATUS.md — honest per-feature status

As of 2026-10-13 (deep in-app integration pass). Status words mean exactly this:

- **WORKING** — code complete and actually exercised (tests run, or live local runs).
- **NEEDS-LIVE-RUN** — written, not yet run against a live browser / real network.
- **NEEDS-EXTERNAL** — blocked on ahsan's decision, procurement, or an action only he can take.

---

## WORKING (verified this pass)

| Feature | Evidence |
|---|---|
| Server wiring — /api/browser/*, /api/recon/run, /api/sectest/run, /api/permissions, /api/browser/health | `browser-system/server_wiring.js` mounted in root `server.ts`; live boot on :18099 — all endpoints respond |
| Lazy Python sidecar (in-app, Render-free compatible) | `browser_sidecar.js`: PEP 668 handled, pip bootstrap, health-gated spawn, auto-restart w/ backoff; live run spawned service on :8080 |
| Python browser service boots WITHOUT playwright | Optional playwright import; `/status` → `{playwright:false, browser_lazy:true}`; 30 tools registered (15 browser + 8 recon + 7 sectest) |
| Lazy Chromium launch | `browser_manager.ensure_started()` — no browser RAM at boot; `browser_unavailable` honest error when it can't start |
| httpx 0.28 compat + proxy hardening | `client_kwargs()` helper; `trust_env=False` (explicit config only); malformed proxy URLs dropped, not crashed |
| Chat intent routing (server-side tool selection) | `detectBrowserIntent`: 7/7 unit cases pass (Roman Urdu + English + slash commands); `/api/model/chat` runs browser tools, enriches model prompt, serves browser results when model offline |
| Legacy fetch fallback (widened) | Falls back when service offline OR browser unavailable; `fallbackUsed` labeled honestly |
| Response cache + request dedup | `browserCache.js`: dedup + TTL cache verified (screenshots never cached) |
| Scope guard (web targets) | Unauthorized sectest/recon → `scope_denied` (live-verified via /api/sectest/run → 403) |
| Permission model (final) | `permissions.js` + `scope_guard.py`: full infra grant, shell model (container escape permanently denied), kernel bare-metal-only w/ honest container note, audit toggle (default ON), credential anti-leak (`sanitizeForChat`, `looksLikeCredentialPath`) |
| Recon tools (8) | Registered; passive trio live-tested (DNS/TLS/fingerprint paths); active two scope-gated |
| Sectest tools (7) | Registered; XSS/SQLi (error+blind boolean+blind time)/SSRF/IDOR/CSRF/Nmap/ZAP wrappers; all scope-checked |
| Chat UI | `BrowserPanel.tsx` in `src/components/`, `browserObservation` on ChatMessage, render block + send-handler wiring; `tsc` + full `npm run build` pass |
| Tool schemas for the model | 18 new schemas in `NATIVE_TOOL_SCHEMAS` (browser/recon/sectest) — the model sees what it can do |
| Permissions in system prompt | `PERMISSIONS_SUMMARY.txt` injected via `composeSystemInstruction` — the model KNOWS its capabilities |
| Flat shim contract | 18/18 offline tests pass (`tests/test_flat_shim.py`) |
| E2E suite | `tests/e2e_live.mjs`: **7/7 pass** against live local app (health, browser health, search intent, open intent, recon, scope denial, permissions) |
| Vision pipeline | 10 mocked tests pass (unchanged); Germany verify scripts live-tested (unchanged) |

## NEEDS-LIVE-RUN (written; needs real browser/network)

| Feature | What it needs |
|---|---|
| Real Chromium rendering (navigate JS pages, screenshots) | A host where Chromium can launch: system deps + ~300MB RAM. NOT Render free (see verdict below). Try: his PC, Germany VPS |
| `browser_service/tests/test_service.py` (all tools live) | Running service + Chromium + clean egress network |
| `tests/test_screenshot_url.py` | Running service + Chromium |
| `tests/E2E_GERMAN_JOBS.md` (arbeitsagentur.de) | German egress + live browser |
| Vision against a real VLM | `VISION_MODEL_URL` (Kaggle Qwen2.5-VL or local Ollama) |
| Sandbox-network flakiness | This dev sandbox's egress proxy breaks httpx intermittently (DDG 0-result flakes); Render has clean egress — re-verify there |

## NEEDS-EXTERNAL (ahsan's decisions)

| Item | Blocked on |
|---|---|
| German egress (real German IP) | He procures German VPS (~€4–6/mo) or proxy/VPN — or defers. Plumbing + `verify_german_ip.py` ready |
| Vision model serving | His call: Kaggle Qwen2.5-VL notebook or local Ollama. Until then: text-only fallback (honest) |
| Chromium-capable host | His call: his PC or a VPS. Render free cannot reliably run Chromium (see verdict) |
| Push + Render redeploy | Parent pushes the local commits (no PAT on builder side); Render auto-deploys from main |

## Honest verdicts

**Chromium on Render free (0.1 CPU / 512MB): NOT RECOMMENDED.** The stack is built for graceful degradation: the service boots without playwright (~80MB), fetch-based tools (search/extract/recon/sectest) work fully, and browser tools fail honestly with `browser_unavailable` + legacy-fetch fallback for reading pages. Real screenshots/JS rendering need ~300MB + system libs — get a VPS or run the browser service on his PC (`BROWSER_SERVICE_URL` points the app at it, no code change).

**Render sleep:** the sidecar respawns lazily on the next browser request after sleep (cold start 5–60s for pip check + service boot). No 24/7 promise — documented in `/api/browser/health`.

**Model:** `/api/model/chat` now serves browser results even when the Kaggle model is offline (honest `modelOffline` flag) — the agent stays useful without the GPU.

---

## Previous build notes (kept for history)

## Known gaps vs ahsan's spec (said plainly)

1. **The Node↔Python contract does not match.** `node-integration/` was
   built against a flat REST contract (`/navigate`, `/search`, …) that
   `browser_service/` does not implement (it serves `/tools/*` with a
   task_id envelope). Wiring them today = every call 404s. Full detail in
   `CONTRACT_CHECK.md`.
2. **Pre-existing chat wiring gap** (not introduced by this build, noted in
   `chat-ui/CHAT_PATCH.md`): `HalyeStudio.handleSendPrompt` POSTs to
   `/api/model/chat` (plain text response). The only `/api/agent/generate`
   caller is the realtime builder, which ignores extra fields. Until the
   generate response is mapped onto the assistant message (`toolCalls`,
   `webInspection`, `browserObservation`), `BrowserPanel` never renders —
   even after both patches are applied.
3. **Vision is not wired into anything yet.** The pipeline is standalone;
   neither the browser service nor the Node app calls it. Integration +
   a served VLM are both outstanding.
4. **No live browser has run in this build.** Every Playwright path —
   navigate, extract, screenshot, detect_blocks — is written but
   unexecuted. The honest claim today is "code complete", not "verified".
5. **His "all powers" requirement** (pip, shell, python, bash, Terminal,
   Playwright) — the Playwright/browser power exists here as a service,
   but it is not yet inside his live agent. The rest (shell/python/pip)
   was verified live on Render separately and is out of scope for this doc.
