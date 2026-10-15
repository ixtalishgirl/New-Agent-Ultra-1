# Deploying the rebuilt Haley agent (100% Python) to Render

## What changed
The old deployment ran a Node.js/TypeScript server (`server.ts`, 5,184 lines).
The rebuild is **100% Python** — the entry point is now:

```
python3 -m agent.api.server
```

## Render settings
- **Runtime:** Python 3.11
- **Build command:** `pip install -r requirements.txt`
- **Start command:** `python3 -m agent.api.server`
- The server reads the `PORT` env var automatically (Render sets it).

Or use `render.yaml` (in repo root) with Render Blueprints.

## Required env vars (set in Render dashboard)
| Var | Purpose |
|---|---|
| `HALEY_API_URL` | Qwen 7B model endpoint (ngrok URL when GPU is on) |
| `APP_PASSWORD` | Password for `/api/*` auth (`x-app-password` header) |
| `SUPABASE_URL` | Supabase project URL (memory) |
| `SUPABASE_KEY` | Supabase anon/service key |
| `SUPABASE_DB_URL` | Postgres connection string (superuser) |
| `GERMANY_PROXY_URL` | Germany egress proxy (optional) |

## Endpoints
- `GET /api/health` — no auth → `{"ok": true, "agent": "haley-py"}`
- `GET /api/tools` — auth → list of 13 tools
- `POST /api/chat` — auth → agentic ReAct loop
- `POST /api/tools/execute` — auth → run one tool

## Note
Do NOT deploy until `HALEY_API_URL` points at a live model endpoint.
The old ngrok URL is stale (GPU session was stopped 2026-10-15).
Start the Kaggle GPU notebook first, then set the fresh URL.
