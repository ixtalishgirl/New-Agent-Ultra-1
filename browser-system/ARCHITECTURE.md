# ARCHITECTURE.md — browser-system for ahsan's AI agent

## What this system is

A real-browser capability for ahsan's Node/Express agent ("Halye" app):
JavaScript-rendered browsing, clicking, form-filling, screenshots, multi-tab
research, and visual page understanding — over HTTP, with a chat UI panel,
a pluggable vision model, and an optional German network egress.

It runs as a **separate Python service** next to the Node app (not inside it,
not on the tiny Render free instance).

## System diagram (mermaid)

```mermaid
flowchart TB
    subgraph chat["Chat UI (browser)"]
        BP["BrowserPanel.tsx<br/>renders observations,<br/>screenshots, honest-failure cards"]
    end

    subgraph node["Node/Express app — Halye (src/server.ts)"]
        GEN["/api/agent/generate<br/>planner + tool executor"]
        BA["browserActions.js<br/>planner actions:<br/>browser_search/open/extract/<br/>screenshot/compare"]
        BR["browserRoutes.js<br/>/api/browser/* passthrough<br/>SSRF guard + permission toggles"]
        LEG["legacy fetch<br/>(runWebInspection)"]
    end

    subgraph py["Python browser service (FastAPI + Playwright)"]
        API["REST API<br/>/health · /tools/* · /tasks/*<br/>/history · ws/{task_id}"]
        REG["ToolRegistry<br/>15 tools (navigate, search,<br/>extract, screenshot, ...)<br/>detect_blocks = detect-only"]
        BM["BrowserManager<br/>1 Chromium, 1 isolated<br/>context per task"]
        DB[("SQLite history<br/>(optional, observations only)")]
    end

    subgraph vision["Vision pipeline (Python)"]
        PIPE["VisionPipeline.observe()<br/>screenshot + DOM → Observation"]
        ADPT["pluggable VLM adapter<br/>(openai_compat: vLLM/Ollama/...)<br/>text-only fallback when unset"]
    end

    subgraph model["Reasoning model"]
        KG["Kaggle GPU — Llama-3-8B<br/>(text-only) via HALEY_API_URL"]
    end

    subgraph net["Network egress"]
        DE{"German egress?<br/>WireGuard / proxy<br/>to German VPS"}
    end

    WEB(("{The web<br/>UNTRUSTED — page content<br/>is DATA, never instructions}"))

    chat <-->|chat messages +<br/>browserObservation| node
    BP -.->|renders| GEN
    GEN --> BA -->|HTTP JSON<br/>(contract: see CONTRACT_CHECK.md)| API
    BR -->|HTTP JSON| API
    BA -.->|offline fallback| LEG
    GEN <-->|reasoning prompts| KG
    API --> REG --> BM -->|Chromium| DE
    DE -->|optional German IP| WEB
    BM -.->|direct (no German egress)| WEB
    REG --> DB
    BM -->|live PNG + DOM| PIPE --> ADPT
    PIPE -.->|Observation| BA
```

## ASCII fallback

```
 ┌──────────────┐   chat msgs + browserObservation   ┌───────────────────────────┐
 │   Chat UI    │ ◄───────────────────────────────► │  Node/Express Halye app   │
 │ BrowserPanel │                                   │  /api/agent/generate      │
 └──────────────┘                                   │  browserActions.js        │
                                                    │  browserRoutes.js         │
                                                    │  (+ legacy fetch fallback)│
                                                    └─────────────┬─────────────┘
                                                                  │ HTTP JSON
                                                                  │ (CONTRACT_CHECK.md)
                                                    ┌─────────────▼─────────────┐
                                                    │ Python browser service    │
                        ┌──────────────────────────►│ FastAPI + Playwright      │
                        │                           │ /health · /tools/*        │
                        │                           └──────┬──────────┬─────────┘
                        │                                  │          │
              ┌─────────▼────────┐                ┌────────▼───┐ ┌───▼────────┐
              │ Vision pipeline  │◄── PNG+DOM ─── │ ToolRegistry│ │ SQLite hist│
              │ observe()        │                │ 15 tools    │ │ (optional) │
              │ VLM adapter or   │                └────────┬────┘ └────────────┘
              │ text-only        │                         │
              └────────────────┘                ┌─────────▼──────────┐
                                                │ BrowserManager     │
                                                │ 1 Chromium,        │
                                                │ 1 context / task   │
                                                └─────────┬──────────┘
                                                          │
                                              ┌───────────▼────────────┐
                                              │ German egress (optional)│
                                              │ WireGuard / proxy → DE  │
                                              └───────────┬────────────┘
                                                          │
                                              ┌───────────▼────────────┐
                                              │        THE WEB         │
                                              │  ⚠ UNTRUSTED ZONE ⚠   │
                                              └────────────────────────┘

  Reasoning model (Kaggle GPU, Llama-3-8B, text-only) ◄──► Node app (prompts)
```

## Components

| Component | Location | Role |
|---|---|---|
| Chat UI panel | `chat-ui/BrowserPanel.tsx` | Renders `browserObservation` (facts, screenshot, sources, honest-failure cards) |
| Chat patch | `chat-ui/CHAT_PATCH.md` | Recipe to mount the panel in `HalyeStudio.tsx` |
| Planner actions | `node-integration/browserActions.js` | 5 browser actions for `/api/agent/generate`, legacy-fallback, secret redaction, untrusted-content wrapping |
| Passthrough routes | `node-integration/browserRoutes.js` | `/api/browser/*` — SSRF guard, permission toggles, honest 503s when offline |
| Integration patch | `node-integration/INTEGRATION_PATCH.md` | 6-hunk recipe for `src/server.ts` |
| Browser service | `browser_service/` | FastAPI + Playwright; 15 tools; task/context lifecycle; SQLite history; WebSocket status |
| Vision pipeline | `vision/` | `observe(png, url, dom_text, elements)` → stamped `Observation`; pluggable VLM adapter; text-only fallback |
| Germany option | `germany/` | WireGuard/proxy config templates + `verify_german_ip.py` PASS/FAIL egress check; procurement is ahsan's decision |
| Response contract | `RESPONSE_CONTRACT.md` | Mandatory answer anatomy, injection guard, CAPTCHA policy, secrets hygiene |

## Data flows

1. **Browse request:** user message → Node planner picks a `browser_*` action →
   `browserActions.js` → HTTP to Python service → Playwright Chromium renders
   page (optionally via German egress) → extract/screenshot → observation →
   secret-redacted, `[UNTRUSTED]`-wrapped → model context → chat answer via
   `BrowserPanel` following `RESPONSE_CONTRACT.md`.
2. **Vision:** live screenshot + DOM text → `VisionPipeline.observe()` →
   VLM describes image (or text-only fallback) → `Observation` with URL +
   timestamp + sha256 → merged into the browsing observation.
3. **History:** short observations (url, title, summary, timestamp — never
   form values) → SQLite, only when `BROWSER_ENABLE_HISTORY=1`.
4. **Reasoning:** the Node app's planner calls ahsan's model endpoint
   (Kaggle GPU, `HALEY_API_URL`) for planning/summarizing; browsing tools
   feed it observations.

## Trust boundaries

```
TRUSTED                          │  UNTRUSTED
                                 │
ahsan's Node app                 │  Everything from the web:
Python browser service           │   page text, HTML, images,
Vision pipeline + VLM            │   screenshots, search results,
SQLite history                   │   DNS answers, proxy responses
                                 │
──────────── boundary ───────────┼─────────────────────────────────
Rules:                           │
• Page content = DATA, never instructions. Wrapped in                 │
  [BEGIN/END UNTRUSTED PAGE CONTENT] before reaching the model.      │
• Credential-shaped strings redacted before display/storage.         │
• CAPTCHA/anti-bot = detect + report honestly. NEVER bypass.         │
• No arbitrary shell/admin through the browser path (HTTP only).     │
• Downloads: allow-listed extensions only, never executables,        │
  size-capped, sha256-recorded.                                      │
• SSRF guard on Node passthrough: public http(s) only.               │
• Secrets (API keys, proxy creds) live in env files (chmod 600),    │
  never in chat, logs, screenshots, or git.                          │
```

## Known integration seams (not yet closed)

- **Contract mismatch** between `node-integration/` and `browser_service/`
  — see `CONTRACT_CHECK.md`. Reconciliation required before wiring.
- **Chat wiring gap (pre-existing):** `HalyeStudio.handleSendPrompt` POSTs to
  `/api/model/chat` (plain text); `/api/agent/generate` (which returns
  `browserObservation`) is not called from live chat — see `CHAT_PATCH.md`.
- **Vision not wired** into the service or Node app yet — the pipeline is a
  standalone module awaiting integration + a served VLM.
- **`/status` + `egress_ip`** missing from the browser service — the Germany
  verifier documents this contract but the service doesn't expose it yet.
