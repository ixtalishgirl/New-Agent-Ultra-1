# New-Agent-Ultra-1 — Web Dashboard

TypeScript + React (Vite) console for the Python agent backend.

## Tabs

| Tab | What it does |
|---|---|
| **Chat** | Send tasks to the agent (`POST /api/chat`), view replies + tool calls |
| **Tasks** | Session activity stats, recent runs, model tunnel status |
| **Tools** | Searchable registry of the 45 backend tools; execute read-only tools directly |
| **Approvals** | State-changing tools (shell, python, file writes, …) are held here until approved |
| **History** | Full local log of chats, tool runs and approvals |
| **Settings** | Backend URL + app password, connection info, clear local data |

## Develop

```bash
cd dashboard
npm install
npm run dev        # http://localhost:5173 — /api is proxied to http://127.0.0.1:8000
```

## Build

```bash
cd dashboard
npm install
npm run build      # → dashboard/dist/
```

## Serving from the Python backend

`agent/api/server.py` serves the built dashboard automatically — no extra
process needed:

- If `dashboard/dist/` exists next to the repo root, every non-`/api/` request
  is served from it (`index.html` for SPA routes).
- API routes are unchanged; the dashboard itself needs no auth, the API calls
  from it use the `x-app-password` header you configure in Settings.

Deploy flow (Render):

```bash
npm --prefix dashboard install && npm --prefix dashboard run build
python3 -m agent.api.server   # serves API + dashboard on $PORT
```

## API contract (backend: `agent/api/server.py`)

- `GET  /api/health` — no auth → `{ ok, agent }`
- `POST /api/chat` — auth → body `{ prompt }` → `{ success, reply?, tool_calls?, error? }`
- `GET  /api/tools` — auth → `{ tools: [{ name, description, status }] }`
- `POST /api/tools/execute` — auth → body `{ name, args }` → `{ success, result?, error? }`
- `GET  /api/tunnel/status` — auth → `{ tunnel, note }`

Auth: `x-app-password` header (or `?password=` query). Set `APP_PASSWORD`
env var on the server; enter the same value in the dashboard Settings tab.
If the backend is offline, the UI shows an Offline pill and blocks sends —
nothing breaks.

## Notes

- Chat history, tool runs and approvals live in the browser's localStorage only.
- No personality text in the UI (per ahsan's order) — functional labels only.
- Destructive tools (`shell`, `python`, `pip`, `self_modify`, `tool_create`,
  `file_write`, `text_replace`, `archive_extract`) always require approval.
