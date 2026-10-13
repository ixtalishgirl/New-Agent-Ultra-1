# DEPENDENCIES.md — every dependency, per component

**Constraint honored: everything here is free and open-source. No paid
services, no paid APIs, no license keys required for the system itself.**
(The optional German VPS in `germany/` is infrastructure he may rent —
~€4–6/month — documented as his procurement decision, not a dependency.)

---

## 1. browser_service/ (Python)

From `browser_service/requirements.txt` (pinned):

| Package | Version | Why |
|---|---|---|
| `fastapi` | 0.115.6 | REST API |
| `uvicorn[standard]` | 0.34.0 | ASGI server |
| `playwright` | 1.49.1 | Real Chromium browser automation |
| `beautifulsoup4` | 4.12.3 | HTML parsing helpers |
| `httpx` | 0.28.1 | Async HTTP (search, geolocation, tests) |
| `pydantic` | 2.10.4 | Request/response models |
| Python | 3.11 | See Dockerfile (`python:3.11-slim`) |

System deps (Chromium): installed via `playwright install --with-deps
chromium` (the Dockerfile runs this; ~400–500 MB layer). No manual apt
list needed — Playwright resolves it.

Test-only: `httpx` (already above). `tests/test_service.py` and
`tests/test_screenshot_url.py` use stdlib + `httpx`.

## 2. vision/ (Python)

From `vision/requirements.txt`:

| Package | Version | Why |
|---|---|---|
| `requests` | ≥ 2.31 | Calls the OpenAI-compatible VLM endpoint |

Test-only: `pytest` ≥ 8.0 (not needed at runtime; stdlib `unittest` also
works — `python3 -m unittest discover -s tests`).

The vision *model* itself is not a pip dependency — it is a served VLM
behind `VISION_MODEL_URL` (see CONFIG.md / VISION_MODELS.md):
- **Ollama** locally (free, open-source) + `qwen2.5vl:7b` weights, or
- **vLLM** serving `Qwen/Qwen2.5-VL-7B-Instruct` on his Kaggle GPU, or
- any OpenAI-compatible endpoint he already runs.
With none configured, the pipeline runs in text-only fallback — no extra
dependency at all.

## 3. node-integration/ (Node)

**No new npm dependencies.** `browserActions.js` and `browserRoutes.js`
use only:

- `fetch` + `AbortController` (Node 18+ built-in)
- `dns/promises` (built-in, for the SSRF guard)
- `express` (already a dependency of his app — the router mounts on the
  existing app)
- `Buffer` (built-in, for screenshot proxying)

The adjacent `.d.ts` shims keep `tsc --noEmit` happy with no new types.

## 4. chat-ui/ (React)

`chat-ui/BrowserPanel.tsx` — presentational component. Assumes the app's
existing React + TypeScript toolchain; no new npm packages listed by the
builder. (If the project's tsconfig balks at importing outside `src/`,
copy the file into `src/components/` per `CHAT_PATCH.md`.)

## 5. germany/ (Python + OS)

| Dependency | Why |
|---|---|
| Python 3 stdlib only (`urllib`, `json`, `argparse`) | `verify_german_ip.py`, `report_location.py` — no pip packages |
| `ip-api.com` free tier (HTTP) | Geolocation evidence for the PASS/FAIL verdict (45 req/min free; scripts make ~2–3/run) |
| `wireguard` / `wg-quick` (OS package) | Only for Path A (tunnel to his German VPS) |
| A German egress itself | His procurement decision — VPS (~€4–6/mo) or commercial German proxy |

## 6. Deployment targets

| Target | Needs |
|---|---|
| His PC (Linux) | Python 3.11, `pip`, Playwright Chromium (`playwright install --with-deps chromium`, needs sudo for system deps) |
| VPS / Germany VPS | Docker (recommended — `browser_service/Dockerfile` bundles everything) **or** same as PC; ≥ 2 GB RAM (4 GB comfortable for multi-tab) |
| Render (Node app only) | No new deps — but **the browser service cannot run here** (free plan: 0.1 CPU / 512 MB; Chromium needs ~300–600 MB per tab). Render hosts the Node app; the service lives on his PC/VPS. |

## License notes

All packages above are OSI-approved open-source (MIT/Apache-2.0/BSD).
Chromium is BSD-licensed. WireGuard is GPLv2 (OS package, not linked).
Qwen2.5-VL weights are Apache 2.0. No copyleft code is linked into his app.
