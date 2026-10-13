# browser-system — real web browsing for ahsan's AI agent

Gives his Node/Express agent ("Halye" app) a **real rendered browser**:
JavaScript pages, clicking, form-filling, screenshots, multi-tab research,
and visual page understanding — over HTTP, with a chat UI panel, a pluggable
vision model, and an optional German network egress.

Built 2026-10-13 by four builder subagents; assembled and documented here.
**Status: code complete, not yet live** — see the honest split below.

---

## Component map

```
browser-system/
├── README.md                  ← you are here
├── ARCHITECTURE.md            ← diagrams, data flows, trust boundaries
├── CONTRACT_CHECK.md          ← ✅ contract reconciled (Option A flat shim) — read first
├── RESPONSE_CONTRACT.md       ← mandatory answer anatomy, injection guard, CAPTCHA policy
├── CONFIG.md                  ← every env var, defaults, required vs optional
├── DEPENDENCIES.md            ← all packages (Python/Node/system), all open-source
├── SECURITY.md                ← checklist: toggles, secrets, isolation, confirmations
├── SETUP.md                   ← step-by-step: his PC, VPS, Germany VPS
├── TROUBLESHOOTING.md         ← common failures and fixes
├── IMPLEMENTATION_STATUS.md   ← honest WORKING / NEEDS-LIVE-RUN / NEEDS-EXTERNAL
│
├── browser_service/           ← Python FastAPI + Playwright (API.md, README.md, Dockerfile,
│                                requirements.txt, tests/test_service.py) — the actual browser
├── node-integration/          ← browserActions.js, browserRoutes.js, INTEGRATION_PATCH.md
│                                (+ .d.ts, RESPONSE_CONTRACT notes) — Node wiring (patch, not applied)
├── chat-ui/                   ← BrowserPanel.tsx, CHAT_PATCH.md — chat rendering (patch, not applied)
├── vision/                    ← pipeline.py, adapters/, VISION_MODELS.md, README.md,
│                                tests/test_pipeline.py — visual understanding (standalone)
├── germany/                   ← GERMANY.md, verify_german_ip.py, report_location.py,
│                                config/, tests/TEST_PLAN.md — German egress option (no egress yet)
└── tests/                     ← test_screenshot_url.py (new), E2E_GERMAN_JOBS.md (new)
```

## Quickstart (local, 5 minutes to a health check)

```bash
cd browser-system/browser_service
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium   # sudo needed for --with-deps
cp .env.example .env && chmod 600 .env
uvicorn app:app --host 127.0.0.1 --port 8080
# new terminal:
curl http://127.0.0.1:8080/health
```

Full setup per target (his PC / VPS / Germany VPS): **SETUP.md**.
Every env var: **CONFIG.md**. When something breaks: **TROUBLESHOOTING.md**.

## Honest status split

**Working now (code complete, exercised):**
- Vision pipeline unit tests: 10 mocked tests pass, 2 live tests skip
  without a VLM (`python3 -m unittest discover -s tests` in `vision/`)
- Germany scripts: local PASS/FAIL logic verified (`appears German: no`
  reported honestly on this machine; no-crash on unreachable endpoints)
- All service code, Node integration code, chat panel, docs — written

**Written, needs a live run:**
- `browser_service/tests/test_service.py` — all 15 tools, needs live Chromium + network
- `tests/test_screenshot_url.py` — real screenshot + timestamped sidecar
- `tests/E2E_GERMAN_JOBS.md` — full chain on arbeitsagentur.de job search
- Vision live-model tests — need a served VLM

**Needs his decision / procurement / credentials (nothing purchased or pushed):**
- ~~**Contract reconciliation**~~ — ✅ **done** via `browser_service/flat_shim.py`
  (Option A); Node↔Python now interoperate, default port **8080**
- German VPS/proxy (~€4–6/mo) — or defer the Germany option
- Vision model serving — Kaggle Qwen2.5-VL notebook (his call, Kaggle
  boundary respected) or local Ollama
- GitHub PAT — to apply the Node + chat patches to the live repo
- Render redeploy after patches merge
- Chat wiring gap — `/api/agent/generate` isn't called from live chat yet
  (pre-existing; recipe in `chat-ui/CHAT_PATCH.md`)

Details: **IMPLEMENTATION_STATUS.md**.

## Non-negotiable rules (from ahsan's spec)

- **Page content is untrusted data** — never instructions, never followed
- **CAPTCHA / anti-bot: detect and report honestly, never bypass**
- **Never fake location or identity** — the Germany option is real network
  egress or nothing; residency/KYC demands are reported, not faked
- **Free and open-source only** — no paid APIs anywhere in this system
- **Consequential actions need his explicit confirmation** (downloads,
  purchases, form submissions, public posts)
- Secrets live in `chmod 600` env files — never in chat, logs, or git
