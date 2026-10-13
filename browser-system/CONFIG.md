# CONFIG.md — complete environment variable reference

All configuration is via environment variables. Copy each component's
`.env.example` → `.env` and fill in. **Never commit real `.env` files.**

Legend: **[R]** required · **[O]** optional (has a safe default) ·
**[S]** secret — keep in `chmod 600` files, never paste into chat.

---

## 1. Shared: where the browser service lives

| Var | Default | Req | Used by | Meaning |
|---|---|---|---|---|
| `BROWSER_SERVICE_URL` | *(empty)* | **[R]** to enable live browsing | Node app (`browserActions.js`, `browserRoutes.js`), test scripts, `verify_german_ip.py --status-url` | Base URL of the Python browser service, e.g. `http://127.0.0.1:8080`. Empty/unset = legacy fetch-only mode (honest, degraded) |
| `BROWSER_SERVICE_API_KEY` | *(empty)* | [O] **[S]** | Python service, Node callers, tests | Shared secret; when set on the service, every request must carry `X-API-Key`. **Node integration does not send it yet** (see CONTRACT_CHECK.md) — set it only after the Node layer adds the header, or Node calls will 401 |

## 2. browser_service/ (Python service)

| Var | Default | Req | Meaning |
|---|---|---|---|
| `BROWSER_SERVICE_HOST` | `127.0.0.1` | [O] | Bind address (`0.0.0.0` in Docker) |
| `BROWSER_SERVICE_PORT` | `8080` | [O] | Canonical default port (decided 2026-10-13, see CONTRACT_CHECK.md). Node docs' `8765` was only an example — always set `BROWSER_SERVICE_URL=http://127.0.0.1:8080` to match |
| `BROWSER_HEADLESS` | `true` | [O] | `false` = visible browser (needs a display) |
| `BROWSER_SLOW_MO_MS` | `0` | [O] | e.g. `100` for human-like pacing when headful |
| `BROWSER_TIMEOUT_MS` | `30000` | [O] | Default per-action timeout (ms) |
| `BROWSER_NAV_TIMEOUT_MS` | `45000` | [O] | Navigation timeout (ms) |
| `BROWSER_MAX_TABS` | `5` | [O] | Max tabs per task |
| `BROWSER_MAX_TASKS` | `20` | [O] | Max concurrent tasks |
| `BROWSER_TASK_IDLE_TTL_SEC` | `1800` | [O] | Evict tasks idle longer than this |
| `BROWSER_HTTP_PROXY` | *(empty)* | [O] **[S]** | e.g. `http://user:pass@host:port` — Germany routing, Path B |
| `BROWSER_HTTPS_PROXY` | *(empty)* | [O] **[S]** | Same for HTTPS |
| `BROWSER_PROXY_USERNAME` | *(empty)* | [O] **[S]** | If not embedded in the proxy URL |
| `BROWSER_PROXY_PASSWORD` | *(empty)* | [O] **[S]** | If not embedded in the proxy URL |
| `BROWSER_DOWNLOAD_DIR` | `./downloads` | [O] | Where permitted downloads land |
| `BROWSER_MAX_DOWNLOAD_MB` | `100` | [O] | Download size cap |
| `BROWSER_RESPECT_ROBOTS` | `true` | [O] | Refuse robots-disallowed URLs |
| `BROWSER_MIN_REQUEST_INTERVAL_MS` | `1500` | [O] | Politeness delay between navigations |
| `BROWSER_USER_AGENT` | *(empty)* | [O] | Custom UA; empty = Playwright default |
| `BROWSER_BLOCKED_HOSTS` | *(empty)* | [O] | Comma-separated hosts to never visit |
| `BROWSER_SCREENSHOT_MAX_KB` | `2048` | [O] | Screenshot size cap |
| `BROWSER_ENABLE_HISTORY` | `false` | [O] | `true` = SQLite research history (`GET /history`) |
| `BROWSER_HISTORY_DB` | `./history.db` | [O] | SQLite path |

## 3. Node app — browser permission toggles (`node-integration/`)

| Var | Default | Req | Meaning |
|---|---|---|---|
| `BROWSER_TIMEOUT_MS` | `25000` | [O] | Per-call timeout from Node → service (ms) |
| `BROWSER_FALLBACK_LEGACY` | `1` | [O] | `1` = fall back to legacy fetch when the service is offline (open/extract/compare); `0` = hard-fail instead |
| `BROWSER_ALLOW_PUBLIC` | `1` | [O] | `0` blocks all public browsing |
| `BROWSER_ALLOW_DOWNLOADS` | `0` | [O] | `1` allows downloads (still needs `confirmed: true` per call) |
| `BROWSER_ALLOW_FORMS` | `0` | [O] | `1` allows automatic form-filling |
| `BROWSER_REQUIRE_CONFIRM_CONSEQUENTIAL` | `1` | [O] | `1` = purchases/downloads/deletions/public posts need explicit `confirmed: true` |

Defaults are the safe posture: read-only browsing, no downloads, no
auto form-filling, consequential actions gated.

## 4. vision/ (pipeline)

| Var | Default | Req | Meaning |
|---|---|---|---|
| `VISION_ADAPTER` | *(empty)* | [O] | `openai_compat` (covers vLLM/Ollama/LocalAI/llama.cpp). Empty = **text-only fallback** ("vision unavailable — text-only") |
| `VISION_MODEL_URL` | *(empty)* | **[R]** if `VISION_ADAPTER` set | e.g. `http://localhost:11434/v1/chat/completions` (Ollama) or `http://<kaggle-tunnel>/v1/chat/completions` |
| `VISION_MODEL_NAME` | *(empty)* | **[R]** if `VISION_ADAPTER` set | e.g. `qwen2.5vl:7b` or `Qwen/Qwen2.5-VL-7B-Instruct` |
| `VISION_MODEL_KEY` | *(empty)* | [O] **[S]** | Bearer token if the endpoint needs one (Ollama/vLLM ignore it) |
| `VISION_TIMEOUT` | `60` | [O] | HTTP timeout, seconds |
| `VISION_MAX_TOKENS` | `512` | [O] | Max description tokens |
| `RUN_LIVE_VISION_TESTS` | *(unset)* | [O] | `1` = run the live-model vision tests (skipped otherwise) |

## 5. germany/ (German egress)

From `germany/config/.env.germany.example` (copy to `.env.germany`, `chmod 600`):

| Var | Default | Req | Meaning |
|---|---|---|---|
| `BROWSER_HTTP_PROXY` / `BROWSER_HTTPS_PROXY` | *(empty)* | **[R]** for Path B | **Must point at a German exit node** (his VPS or German proxy). Prefer `socks5h://` so DNS goes through the proxy too |
| `ALL_PROXY` | *(empty)* | [O] | Proxy for other outbound traffic from the agent process |
| `GERMAN_DNS_PRIMARY` / `GERMAN_DNS_SECONDARY` | *(empty)* | [O] | German resolver for leak prevention (WireGuard path only) |
| `WIREGUARD_CONFIG_PATH` | *(empty)* | **[R]** for Path A | Path to the client config built from `wg-germany.conf.example` |
| `BROWSER_SERVICE_STATUS_URL` | *(empty)* | [O] | Base URL the verifier checks for **browser** egress (e.g. `http://localhost:8080`) |
| `ON_VERIFICATION_DEMAND` | `report` | [O] | Only supported value: `report` — surface residency/KYC demands honestly; there is no "fake it" option |
| `EXPECTED_COUNTRY_CODE` | `DE` | [O] | PASS/FAIL verdict target |

## 6. Test scripts only

| Var | Meaning |
|---|---|
| `USE_FLAT_SHIM` | `1` = `tests/test_screenshot_url.py` exercises the flat shim contract (CONTRACT_CHECK.md Option A) instead of the implemented `/tools/*` API |
| `RUN_LIVE_VISION_TESTS` | `1` = run live VLM tests in `vision/tests/` |

## 7. Persistent memory — Supabase (optional, recommended)

| Var | Meaning |
|---|---|
| `SUPABASE_URL` | Supabase project URL, e.g. `https://xyz.supabase.co` — enables cloud memory |
| `SUPABASE_ANON_KEY` | Supabase anon (public) key — used for `agent_memory` reads/writes |
| `SUPABASE_SERVICE_KEY` | Optional. Add later manually for privileged ops (never required) |
| `SUPABASE_DB_URL` | **Optional.** Direct Postgres connection string — unlocks DDL (`CREATE TABLE` etc.) via `querySql()` / `POST /api/db/query`. The anon key cannot do DDL. **Set it yourself in Render env vars — never paste it in chat.** |
| `SUPABASE_TIMEOUT_MS` | REST/SQL timeout, default `10000` |

When unset: memory writes are skipped (one warning) and the Python service's
SQLite history (`browser_service/db.py`) remains the local fallback — the app
never crashes. See `SUPABASE.md`. Set these in the Render dashboard
(Environment tab) — never commit keys to git.

## Minimal working configurations

**Local dev, no Germany, no vision (fastest honest start):**
```bash
# browser_service/.env
BROWSER_SERVICE_HOST=127.0.0.1
BROWSER_SERVICE_PORT=8080
# Node app env:
BROWSER_SERVICE_URL=http://127.0.0.1:8080
```

**Add Germany (Path B):**
```bash
# germany/config/.env.germany  (chmod 600)
BROWSER_HTTPS_PROXY=socks5h://user:pass@de-vps.example:1080
BROWSER_SERVICE_STATUS_URL=http://127.0.0.1:8080
```
Load it when starting the browser service, then
`python3 germany/verify_german_ip.py` must say `appears German: yes`.

**Add vision (Ollama):**
```bash
# vision/.env
VISION_ADAPTER=openai_compat
VISION_MODEL_URL=http://localhost:11434/v1/chat/completions
VISION_MODEL_NAME=qwen2.5vl:7b
```
