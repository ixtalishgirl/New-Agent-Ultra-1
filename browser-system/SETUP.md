# SETUP.md — step-by-step setup

Three targets. Pick one. In all cases the **Node app stays where it is**
(Render); the Python browser service runs on a machine with enough RAM.

> **Prerequisite for all targets:** `CONTRACT_CHECK.md` reconciliation must
> be implemented first — otherwise the Node app and the service cannot talk
> to each other. Everything below assumes the reconciled contract.

---

## Target 1 — his PC (Linux, fastest start)

**You need:** Python 3.11, `pip`, sudo (once, for Chromium system deps).

```bash
# 1. Install
cd ~/workspace/agent-fix/New-Agent-ulta/browser-system/browser_service
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium   # needs sudo for --with-deps

# 2. Configure
cp .env.example .env
# edit .env: set BROWSER_SERVICE_PORT (use 8080 everywhere), history on/off
chmod 600 .env

# 3. Start
uvicorn app:app --host 127.0.0.1 --port 8080

# 4. Verify (new terminal)
curl http://127.0.0.1:8080/health
# expect: {"status":"ok","service":"browser-service",...}

# 5. Run the service test suite (NEEDS LIVE RUN)
BROWSER_SERVICE_URL=http://127.0.0.1:8080 python tests/test_service.py
# then the screenshot test:
python ../tests/test_screenshot_url.py https://example.com
```

**Wire the Node app** (his dev machine or Render — same env vars):

```bash
# .env of the Node app
BROWSER_SERVICE_URL=http://127.0.0.1:8080   # reachable from the Node host!
BROWSER_TIMEOUT_MS=25000
BROWSER_FALLBACK_LEGACY=1
BROWSER_ALLOW_PUBLIC=1
BROWSER_ALLOW_DOWNLOADS=0
BROWSER_ALLOW_FORMS=0
BROWSER_REQUIRE_CONFIRM_CONSEQUENTIAL=1
```

⚠️ If the Node app runs on Render and the service runs on his PC, Render
cannot reach `127.0.0.1` on his PC. Options: run the Node app locally too
during development, or expose the service via a tunnel (e.g. localtunnel —
the same pattern as his `haley-serve-live` Kaggle notebook) and set
`BROWSER_SERVICE_URL` to the tunnel URL. If you use a tunnel, set
`BROWSER_SERVICE_API_KEY` on both sides.

**Verify wiring:** `GET /api/browser/status` on the Node app should report
`service.online: true`.

## Target 2 — VPS (Docker, recommended for 24/7)

**You need:** a VPS with ≥ 2 GB RAM (4 GB comfortable), Docker installed.

```bash
# 1. Copy the service dir to the VPS
scp -r browser_service user@vps:/opt/ahsan-browser-service
ssh user@vps

# 2. Configure
cd /opt/ahsan-browser-service
cp .env.example .env && chmod 600 .env
# edit .env — set BROWSER_SERVICE_HOST=0.0.0.0? No: keep 127.0.0.1 and
# put a reverse proxy (caddy/nginx) with auth in front, OR bind 0.0.0.0
# only behind a firewall that allows the Node host.

# 3. Build + run
docker build -t ahsan-browser-service .
docker run -d --name browser-service --restart unless-stopped \
  -p 127.0.0.1:8080:8080 \
  --env-file .env -v browser-downloads:/app/downloads \
  ahsan-browser-service

# 4. Verify
curl http://127.0.0.1:8080/health
docker logs browser-service | tail -20
```

**Wire the Node app:** `BROWSER_SERVICE_URL=http://<vps-ip>:8080`
(or the reverse-proxy URL), plus the permission toggles from Target 1.
Set `BROWSER_SERVICE_API_KEY` on both sides — the service is now on a network.

## Target 3 — Germany VPS (German egress)

**You need:** Target 2 done on a VPS physically in Germany (his procurement
decision — ~€4–6/month; nothing here buys it).

**Path A — WireGuard tunnel (recommended, full control):**

```bash
# 1. On the VPS: install WireGuard, create server config (see
#    germany/config/wireguard/SETUP.md for the server side)
# 2. On the client (his PC or the browser-service host):
cp germany/config/wireguard/wg-germany.conf.example /etc/wireguard/wg-germany.conf
# fill in PrivateKey, PublicKey, Endpoint (YOUR VPS values — never commit)
chmod 600 /etc/wireguard/wg-germany.conf
sudo wg-quick up wg-germany

# 3. Tell the verifier about it
cp germany/config/.env.germany.example germany/config/.env.germany
chmod 600 germany/config/.env.germany
# set WIREGUARD_CONFIG_PATH=/etc/wireguard/wg-germany.conf
# set BROWSER_SERVICE_STATUS_URL=http://127.0.0.1:8080

# 4. Verify — expect "appears German: yes"
python3 germany/verify_german_ip.py
```

**Path B — proxy only (simpler, less leak-proof):**

```bash
# 1. Get a German proxy endpoint (his VPS running tinyproxy/squid,
#    or a commercial German proxy)
# 2. In germany/config/.env.germany (chmod 600):
BROWSER_HTTPS_PROXY=socks5h://user:pass@de-proxy.example:1080
BROWSER_SERVICE_STATUS_URL=http://127.0.0.1:8080
# 3. Start the browser service with this env file loaded
#    (docker: add --env-file /path/to/.env.germany)
# 4. Verify BOTH rows — judge by the BROWSER row:
python3 germany/verify_german_ip.py --status-url http://127.0.0.1:8080
python3 germany/report_location.py --status-url http://127.0.0.1:8080
```

**Re-verify after every change** (tunnel up/down, proxy swap, VPS reboot).
A stale "it was German yesterday" is how leaks go unnoticed.

## Optional: vision

```bash
# Option B (simplest): Ollama on the same machine as the service
ollama pull qwen2.5vl:7b
ollama serve   # :11434

# vision/.env
VISION_ADAPTER=openai_compat
VISION_MODEL_URL=http://localhost:11434/v1/chat/completions
VISION_MODEL_NAME=qwen2.5vl:7b

# test (live VLM needed for the 2 live tests; 10 mocked tests run without):
cd vision && python3 -m unittest discover -s tests -v
RUN_LIVE_VISION_TESTS=1 python3 -m unittest discover -s tests -v
```

Option A (Kaggle Qwen2.5-VL via vLLM + tunnel) is his decision per the
standing Kaggle boundary — see `vision/VISION_MODELS.md`.

## Applying the app patches (needs his GitHub PAT)

1. `node-integration/INTEGRATION_PATCH.md` → 6 hunks into
   `New-Agent-ulta/src/server.ts` (additive; no existing logic rewritten)
2. `chat-ui/CHAT_PATCH.md` → 3 hunks into `HalyeStudio.tsx` (+ `src/types.ts`)
3. `npm run build` (esbuild) + `npm run lint` (tsc --noEmit) must pass
4. Commit, push, redeploy on Render
5. Decide the chat wiring gap (IMPLEMENTATION_STATUS.md §2): map
   `/api/agent/generate`'s `browserObservation` onto the assistant message

## Final verification checklist

- [ ] `curl <service>/health` → 200
- [ ] `tests/test_service.py` → all PASS
- [ ] `tests/test_screenshot_url.py` → PNG + sidecar written
- [ ] Node `GET /api/browser/status` → `service.online: true`
- [ ] Node `GET /api/browser/permissions` → toggles as configured
- [ ] (Germany) `verify_german_ip.py` → `appears German: yes` on the browser row
- [ ] (Vision) live tests pass, or fallback label confirmed
- [ ] SECURITY.md deployment review completed
