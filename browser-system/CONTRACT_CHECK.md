# CONTRACT_CHECK.md — Node integration ↔ Python browser service

**Verdict: ✅ RESOLVED (2026-10-13) — Option A implemented.**
`browser_service/flat_shim.py` adds the 9 flat Side A endpoints
(`GET /status`, `POST /navigate|/search|/extract|/screenshot|/detect_blocks|/download`,
`GET /tabs`, `GET /history`) on top of the existing `/tools/*` registry.
Each flat call auto-manages a per-request task (create → act → close) and
returns Side A's exact field shapes (`screenshot_base64`,
`block_kind` vocabulary mapping, `facts`, `entries`, `egress_ip` on
`/status` for the Germany verifier). The `/tools/*` contract is untouched;
`browserActions.js`/`browserRoutes.js` work **unchanged**.
Default port: **8080** everywhere (`BROWSER_SERVICE_PORT=8080`,
`BROWSER_SERVICE_URL=http://127.0.0.1:8080`; Node docs' `8765` was only an
example — the URL is env-configurable).

The original mismatch analysis is preserved below for the record.

---

## The two contracts

**Side A — what `node-integration/` expects** (used by `browserActions.js`,
`browserRoutes.js`; documented in `INTEGRATION_PATCH.md`). Flat REST on
`BROWSER_SERVICE_URL` (example given: `http://127.0.0.1:8765`):

| # | Method & path | Body | Expected success shape |
|---|---|---|---|
| 1 | `GET /status` | — | `{ ok, service, playwright }` |
| 2 | `POST /navigate` | `{ url }` | `{ ok, url, title, blocked?, block_kind? }` |
| 3 | `POST /search` | `{ query, max_results }` | `{ ok, results: [{ title, url, snippet }] }` |
| 4 | `POST /extract` | `{ url }` | `{ ok, url, title, facts: [...], text, accessed_at }` |
| 5 | `POST /screenshot` | `{ url }` | `{ ok, url, title, screenshot_base64, mime }` |
| 6 | `POST /detect_blocks` | `{ url }` | `{ ok, blocked, block_kind }` (`block_kind`: `captcha` \| `cloudflare` \| `waf` \| `paywall` \| `login`) |
| 7 | `GET /tabs` | — | `{ ok, tabs: [...] }` |
| 8 | `POST /download` | `{ url }` | `{ ok, path?, note? }` |
| 9 | `GET /history` | — | `{ ok, entries: [...] }` |

No `task_id` lifecycle — every call is stateless (pass `url`, get result).

**Side B — what `browser_service/` actually implements** (`API.md`, `app.py`),
default `http://127.0.0.1:8080`:

| Side A call | What Side B has instead | Difference |
|---|---|---|
| `GET /status` | `GET /health` → `{ status, service, version, headless, proxy, history_enabled, tasks, tools }` | **Path missing entirely** (`/status` 404s); shape different |
| `POST /navigate` | `POST /tools/navigate` → `{ ok, tool, task_id, data: { task_id, url, title, http_status, tab_id } }` | Path differs; envelope nests real data under `data`; no `blocked`/`block_kind` fields (block detection is a *separate* tool call); returns `task_id` the caller must keep |
| `POST /search` | `POST /tools/search` → `{ ok, tool, task_id: null, data: { engine, query, count, results } }` | Path differs; results nested at `data.results`, not top-level |
| `POST /extract` | `POST /tools/extract` **requires `task_id`** → `{ ok, tool, task_id, data: { url, title, text, headings, links, tables, forms } }` | Requires task_id (Side A sends none); no `facts` array, no `blocked` field, no `accessed_at` |
| `POST /screenshot` | `POST /tools/screenshot` **requires `task_id`** → `{ ok, tool, task_id, data: { url, scope, mime, bytes, data_base64, ... } }` | Requires task_id; image field is `data_base64` inside `data`, **not** `screenshot_base64` at top level (Side A's `doScreenshot` reads `d.screenshot_base64` → would get `undefined`, `hasImage: false`) |
| `POST /detect_blocks` | `POST /tools/detect_blocks` **requires `task_id`** → `{ ok, tool, task_id, data: { blocked, block_type, signals, text_sample, action } }` | Requires task_id; field is `block_type` not `block_kind`; vocabulary differs: `captcha \| bot_check \| login_wall \| blocked \| rate_limited \| null` vs Side A's `captcha \| cloudflare \| waf \| paywall \| login` |
| `GET /tabs` | `POST /tools/list_tabs` **requires `task_id` in body** | GET→POST change; task_id required; `GET /tabs` 404s |
| `POST /download` | `POST /tools/download_file` **requires `task_id`** | Path renamed; task_id required |
| `GET /history` | `GET /history?task_id=...&limit=...` → `{ observations: [...] }` (only with `BROWSER_ENABLE_HISTORY=1`) | Closest match, but returns `observations`, not `entries`; task-scoped; needs history enabled |

## What breaks in practice if wired as-is

1. **Every Node call 404s.** Side A calls `/navigate`, `/search`, `/extract`,
   …; Side B only serves `/tools/*` + `/health`. (Exception: `GET /history`
   exists but returns `{ observations }` instead of `{ entries }`.)
2. **Even at correct paths, field reads fail.** Side A's code reads
   `r.data.results`, `d.screenshot_base64`, `d.blocked`, `d.facts` — Side B
   nests everything under `data` and uses different field names
   (`data_base64`, `block_type`). Results would silently come back empty.
3. **Missing task lifecycle.** Side B creates a `task_id` on the first
   `navigate`/`search` and demands it on all later tool calls. Side A never
   creates or passes one — no Side A flow could do extract-after-navigate.
4. **Default ports disagree.** Side A's docs use `BROWSER_SERVICE_URL=
   http://127.0.0.1:8765`; Side B defaults to `8080`. Port is configurable
   on both sides, so this is minor — but out-of-box they would not connect.
5. **Block-detection behavior differs.** Side A expects `navigate` itself to
   return `blocked/block_kind` and treats it as the honest-failure path.
   Side B requires a separate `detect_blocks` call after navigation.
   The RESPONSE_CONTRACT honesty behavior still holds either way, but the
   *code path* that produces it does not match.
6. **Germany script needs `/status` too.** `germany/verify_german_ip.py`
   expects `GET <status-url>/status` → `{ egress_ip }` for browser-egress
   verification. Side B has `GET /health` (no `/status`, no `egress_ip`
   field). The Germany TEST_PLAN.md already flags this as a pending addition.

## Reconciliation options (parent decides — no code changed by this doc)

**Option A — shim in the Python service (recommended, least churn).**
Add flat routes to `browser_service/app.py` (`/status`, `/navigate`,
`/search`, `/extract`, `/screenshot`, `/detect_blocks`, `/tabs`,
`/download`, `/history`) that internally call the existing tool registry
with an auto-managed per-request task, and return Side A's flat shapes
(including `screenshot_base64`, `block_kind` vocabulary mapping, `facts`
derived from extract). Also include `egress_ip` on `/status` so the
Germany verifier works. ~150 lines, additive, no registry changes;
`browserActions.js`/`browserRoutes.js` then work **unchanged**.

**Option B — rewrite the Node layer to Side B's contract.**
Teach `browserActions.js`/`browserRoutes.js` the `/tools/*` endpoints,
the `{ ok, tool, task_id, data }` envelope, and a task_id lifecycle
(create on first call, pass through, delete at end). Larger change;
touches already-reviewed code.

**Option C — meet in the middle.**
Side B adds `/status` + `egress_ip` only (needed for Germany regardless);
Side A is rewritten for `/tools/*`. Still requires the Node rewrite.

## Checklist for whichever option is chosen — ✅ all done via Option A (flat_shim.py)

- [x] `GET /status` answers with service liveness (Node `/api/browser/status` route depends on it) — plus `egress_ip` for the Germany verifier
- [x] `POST /navigate` returns `url`, `title`, plus block info in ONE response (best-effort `detect_blocks` bundled; strict check remains at `POST /detect_blocks`)
- [x] `screenshot_base64` field name agreed (shim maps Side B's `data_base64` → `screenshot_base64`)
- [x] `block_kind` vs `block_type` vocabulary agreed and mapped 1:1 (`captcha→captcha`, `bot_check→cloudflare`, `login_wall→login`, `blocked|rate_limited→waf`)
- [x] task_id lifecycle owned by exactly one side: **shim-managed** (per-request task, closed after each flat call)
- [x] `GET /history` shape agreed: returns `entries` (mapped from `observations`); graceful `{ok, entries: []}` when history disabled
- [x] `GET /status` exposes browser `egress_ip` for the Germany verifier (cached 120s, omitted on failure — verifier handles it honestly)
- [x] Default port documented in one place: **8080** everywhere
- [ ] Re-run `tests/test_screenshot_url.py` + `tests/test_service.py` after reconciliation — **needs live run** (Playwright Chromium not installed in this environment); offline shim tests pass 18/18 (`browser_service/tests/test_flat_shim.py`)
