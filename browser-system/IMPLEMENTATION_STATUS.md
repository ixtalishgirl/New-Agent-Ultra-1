# IMPLEMENTATION_STATUS.md — honest per-feature status

As of 2026-10-13. Status words mean exactly this:

- **WORKING** — code complete and actually exercised (unit tests run, or
  builder-recorded local runs). Not the same as "deployed".
- **NEEDS-LIVE-RUN** — written, not yet run against a live browser/network.
- **NEEDS-EXTERNAL** — blocked on ahsan's decision, procurement, credential,
  or an action only he (or the parent orchestrator) can take.

---

## WORKING

| Feature | Evidence |
|---|---|
| Vision pipeline — text-only fallback + injection guard | `vision/tests/test_pipeline.py`: **10 mocked tests PASS, 2 live skipped** (re-run by the docs assembler 2026-10-13 via `python3 -m unittest discover -s tests`: OK). Covers: empty-URL rejection, non-PNG rejection, text-only fallback labeling, `untrusted_page_content` tagging, injection-guard prompt, adapter error degradation, DOM element preservation |
| Germany scripts — local logic | Builder-recorded LOCAL runs 2026-10-13: A1 (honest `appears German: no`, exit 1), A2 (`--json`), A3 (unreachable status URL → UNKNOWN, no crash), B1 (report_location backend row), D3 (no real credentials in repo). See `germany/tests/TEST_PLAN.md` |
| Browser service — code complete | `app.py`, `registry.py`, `browser_manager.py`, `db.py`, 13 tool modules, Dockerfile, API.md — all written; imports verified structurally (not executed: needs live Chromium) |
| Node integration — code complete | `browserActions.js` + `browserRoutes.js` + `.d.ts` shims + 6-hunk `INTEGRATION_PATCH.md` — written; **no test file exists for this layer** (see below) |
| Chat panel — component complete | `BrowserPanel.tsx` (175 lines) + `CHAT_PATCH.md` — written; renders null-safe |
| Docs | This folder: ARCHITECTURE, CONFIG, DEPENDENCIES, SECURITY, SETUP, TROUBLESHOOTING, CONTRACT_CHECK, RESPONSE_CONTRACT (pre-existing) |

## NEEDS-LIVE-RUN

| Feature | What it needs |
|---|---|
| `browser_service/tests/test_service.py` (207 lines, all 15 tools) | Running service + Playwright Chromium + network (example.com, duckduckgo.com). Marked NEEDS LIVE RUN by its own header |
| `tests/test_screenshot_url.py` (new, this build) | Running service + network; captures real PNG + timestamped sidecar |
| `tests/E2E_GERMAN_JOBS.md` (new, this build) | Reconciled contract + German egress + live browser delegation (subagents can't run a live browser) |
| Vision live-model tests (2 tests) | `RUN_LIVE_VISION_TESTS=1` + a served VLM (`VISION_MODEL_URL`) |
| Node integration behavior | No test file was written for `browserActions.js`/`browserRoutes.js`; needs a live service (post-reconciliation) to validate fallback, SSRF guard, permission toggles |
| Germany TEST_PLAN remaining LOCAL items | A4, A8, A9, B2, C1, C2, D2 — runnable without German infra, not yet recorded |

## NEEDS-EXTERNAL (ahsan's decision / procurement / credentials)

| Item | Blocked on |
|---|---|
| **Contract reconciliation** | Parent orchestrator decision: Option A (Python shim), B (Node rewrite), or C — see `CONTRACT_CHECK.md`. Then a builder implements it |
| **German egress (real German IP)** | ahsan procures a German VPS (~€4–6/mo) or German proxy/VPN — or defers. Nothing purchased by this build. After procurement: TEST_PLAN A5–A7, B3, C3–C4 |
| **Vision model serving** | His decision: Kaggle Qwen2.5-VL serving notebook (standing Kaggle boundary — only what he orders; his datasets/sessions untouched) **or** local Ollama `qwen2.5vl:7b`. Until then: text-only fallback (honest, labeled) |
| **Applying the Node patch to the live repo** | Fresh GitHub PAT from him (pending — same PAT also gates the English-only fix). Patch targets `New-Agent-ulta/src/server.ts` |
| **Applying the chat patch** | Same PAT; plus his decision on the wiring gap (see below) |
| **Render redeploy** | After patches merge; note the browser service itself **cannot** run on Render free (see TROUBLESHOOTING.md) |
| **`/status` + `egress_ip` on the browser service** | Needed by `verify_german_ip.py --status-url` (Germany A7). Part of reconciliation (Option A includes it) |
| **Model endpoint for reasoning** | `HALEY_API_URL` (Kaggle) is still dead per the app status — the planner's reasoning calls depend on it. Separate from this system but the architecture assumes it |

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
