# TROUBLESHOOTING.md — common failures and fixes

## Service unreachable

**Symptom:** Node logs `browser service unreachable`, `/api/browser/status`
reports `online: false`, or `curl <service>/health` fails.

- Is the service running? `ps aux | grep uvicorn` / `docker ps | grep browser-service`
- Right host/port? `BROWSER_SERVICE_URL` must match `BROWSER_SERVICE_HOST:PORT`
  (remember the CONTRACT_CHECK.md port note: service default is `8080`)
- Firewall / security group blocking the port? (VPS)
- Node on Render + service on his PC: Render cannot reach his PC's
  `127.0.0.1` — use a tunnel URL (see SETUP.md Target 1) or host both on the VPS
- Check service logs: `docker logs browser-service | tail -50`
- **Fallback behavior is honest, not broken:** with the service down,
  `browser_open`/`browser_extract`/`browser_compare` fall back to legacy
  fetch (banner shown); `browser_search`/`browser_screenshot` fail honestly.
  Set `BROWSER_FALLBACK_LEGACY=0` to hard-fail instead of falling back

## Chromium missing / won't launch

**Symptom:** `playwright` errors on first navigate; `Executable doesn't exist`.

- Run `playwright install --with-deps chromium` (needs sudo for `--with-deps`)
- Docker: the Dockerfile does this at build time — rebuild if the layer failed
- Headless server + `BROWSER_HEADLESS=false`: needs a display (Xvfb or real);
  keep `true` on servers
- `/dev/shm` too small in Docker → the Dockerfile already passes
  `--disable-dev-shm-usage`; if you run without Docker, launch Chromium with
  `--disable-dev-shm-usage` on small VMs

## `401 invalid or missing X-API-Key`

**Symptom:** every service call returns 401.

- `BROWSER_SERVICE_API_KEY` is set on the service but the caller isn't
  sending `X-API-Key`
- **Known gap:** the Node layer does not send the header yet (SECURITY.md).
  Until reconciliation adds it: either unset the key (loopback-only
  deployments) or call the service with the header manually
  (`curl -H 'X-API-Key: <key>' ...`)

## Proxy misconfiguration (Germany path)

**Symptom:** `verify_german_ip.py` says `appears German: no`, or browser
traffic times out.

- Is the proxy URL correct and reachable? `curl -x <proxy-url> ifconfig.me`
- `socks5://` vs `socks5h://`: without the `h`, DNS resolves locally and
  leaks your real resolver's location — prefer `socks5h://`
- Credentials with special characters must be URL-encoded in the proxy URL
- WireGuard: `sudo wg show` — handshake recent? `wg-quick down` then `up`
  re-tests cleanly; check `ip route` for leftover routes after down
- A proxy URL alone doesn't make traffic German — the **exit node's**
  location does (see `germany/GERMANY.md` §3). Re-run the verifier after
  every change

## Vision model down / not configured

**Symptom:** observations labeled `"vision unavailable — text-only"`.

- This is the **designed fallback**, not an error — DOM/text observations
  still work; visual details are never fabricated
- To enable: check `VISION_ADAPTER`, `VISION_MODEL_URL`, `VISION_MODEL_NAME`
  in `vision/.env`; is Ollama/vLLM actually serving?
  `curl <VISION_MODEL_URL>` should answer (it expects POST, but a 404/405
  proves the server is up; connection-refused means it's down)
- Ollama: `ollama list` — is `qwen2.5vl:7b` pulled? `ollama serve` running?
- Kaggle tunnel URL changed? Tunnels (localtunnel etc.) get new URLs on
  restart — update `VISION_MODEL_URL`

## Render sleep (Node app)

**Symptom:** first request after ~15 min idle takes ~1 min or times out.

- Normal on the free plan — the service sleeps after ~15 min idle
- The browser service **must not** run on Render free (0.1 CPU / 512 MB;
  Chromium needs ~300–600 MB per tab) — host it on his PC/VPS per SETUP.md
- If the Node app sleeps, `/api/browser/*` calls made during wake-up may hit
  `BROWSER_TIMEOUT_MS` — the client sees an honest timeout, not fake data

## Rate limits / blocks

**Symptom:** `429`, `detect_blocks` → `blocked: true`, empty results.

- `429 too_many_tasks`/`too_many_tabs`: close tasks (`DELETE /tasks/{id}`);
  raise `BROWSER_MAX_TASKS`/`BROWSER_MAX_TABS` only if the host has RAM
- `block_type: rate_limited`: back off — `BROWSER_MIN_REQUEST_INTERVAL_MS`
  (default 1500 ms) is the politeness floor, not a guarantee
- `block_type: captcha` / `cloudflare` / `bot_check` / `login_wall`:
  **stop and report honestly** — never bypass, never retry rapidly hoping
  it clears. Record the `block_type` and move on
- `403 robots_disallowed`: the URL is disallowed by robots.txt — respect it
  (operator-only `force_robots` override exists; keep it operator-only)
- DuckDuckGo search failing: `search_failed` (502) — DDG HTML endpoints
  occasionally throttle; wait and retry once, then report honestly

## Downloads failing

- `403 forbidden_file_type`: the file is an executable or not on the
  allow-list — by design, never overridden
- `413 file_too_large`: over `BROWSER_MAX_DOWNLOAD_MB` (default 100)
- Service-side: is `BROWSER_DOWNLOAD_DIR` writable? (Docker: volume mounted?)

## Contract mismatch errors (after wiring)

**Symptom:** Node calls return 404, or observations come back empty despite
HTTP 200.

- You are hitting the pre-reconciliation mismatch — see `CONTRACT_CHECK.md`.
  Do not "fix" it by guessing field names; implement the decided
  reconciliation (Option A/B/C), then re-run `tests/test_service.py` and
  `tests/test_screenshot_url.py`

## History empty

- `GET /history` needs `BROWSER_ENABLE_HISTORY=1` (default `false`) —
  otherwise 404 `history not enabled`
- History stores short observations only (url, title, summary, timestamp) —
  never form values; an "empty" history with the flag off is correct

## Still stuck?

1. Capture: service logs, the exact request/response (redact secrets!),
   `curl <service>/health` output, and `verify_german_ip.py` output if
   network-related
2. Check `IMPLEMENTATION_STATUS.md` — the feature may be NEEDS-LIVE-RUN or
   NEEDS-EXTERNAL rather than broken
3. Report to the parent agent with the evidence — do not invent a diagnosis
