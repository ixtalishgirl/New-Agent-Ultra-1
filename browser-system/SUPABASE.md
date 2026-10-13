# Supabase — Persistent Memory Backend

The agent's long-term memory lives in Supabase (Postgres), so chat history and
research results survive Render restarts/redeploys. Local SQLite
(`browser_service/db.py`) remains the fallback when Supabase is not configured.

## Project

- URL: `https://zkdgyvxesolwxnhijqzh.supabase.co` (Frankfurt)
- Table: `public.agent_memory`
  - `id` uuid PK (default `gen_random_uuid()`)
  - `created_at` timestamptz (default `now()`)
  - `kind` text — `'chat'` | `'research'` | `'note'` | ...
  - `content` jsonb — the payload
- RLS is disabled on this table (single-user agent; the anon key is the gate).

## Setup (2 minutes, Render dashboard)

1. Supabase dashboard → Project Settings → API → copy the **anon public** key.
2. Render dashboard → `haley-agent` → Environment → add:
   - `SUPABASE_URL` = `https://zkdgyvxesolwxnhijqzh.supabase.co`
   - `SUPABASE_ANON_KEY` = `<paste the anon key>`
3. Save — Render redeploys automatically. Done.

Optional later: `SUPABASE_SERVICE_KEY` for privileged ops (not required; add
manually if ever needed — never commit keys to git).

## What gets stored

| kind | content | written by |
|---|---|---|
| `chat` | `{session_id, role: 'user'\|'assistant', content}` | `POST /api/model/chat` (fire-and-forget) |
| `research` | `{source, tool/action, target/url, summary/facts, accessed_at}` | browser intents + `/api/recon/run`, `/api/sectest/run`, `/api/browser/tool` |

All writes are fire-and-forget with a 10s timeout — a slow/dead Supabase can
never block or crash a chat response. Reads: `getMemory(kind, limit)` in
`browser-system/supabaseMemory.js`.

## DDL: the agent creates its OWN tables (optional)

The anon key can only read/write rows — it **cannot** run `CREATE TABLE`.
For that, the agent needs a direct Postgres connection:

1. Supabase dashboard → **Project Settings → Database** → find the
   **connection string** (use the "Transaction pooler" / port 6543 one if
   offered — it survives serverless well; otherwise the direct port 5432).
   If you never set a database password, click **Reset database password**
   first, then copy the string.
2. Render dashboard → `haley-agent` → Environment → add
   `SUPABASE_DB_URL` = `postgresql://postgres:<your-password>@<host>:<port>/postgres`
3. **Never paste this string in chat** — it is a full-access credential.
   Set it only in Render env vars (and keep a copy somewhere safe yourself).

When `SUPABASE_DB_URL` is present:
- `querySql(sql, params)` in `browser-system/supabaseMemory.js` runs arbitrary
  SQL (SELECT / INSERT / CREATE TABLE / ...). Never throws; `{ok, rows, rowCount}`.
- `POST /api/db/query` `{sql, params?}` — same, over HTTP. Every call is
  audit-logged; `DROP` / `TRUNCATE` / `DELETE` need `confirmed:true`.
- The anon-key REST path stays the **default** for chat/research memory.

Without it: everything still works via the anon key + SQLite fallback; DDL
just isn't available to the agent.

## Health

`GET /api/browser/health` → `memory: { configured, backend: 'supabase'|'sqlite-fallback', reachable }`.

## Code map

- `browser-system/supabaseMemory.js` — `saveMemory(kind, content)`, `getMemory(kind, limit)`, `getMemoryStatus()`, `saveMemoryAsync()` (never throws)
- `browser-system/server_wiring.js` — exports the above; saves research results; health status
- `server.ts` — `/api/model/chat` saves user + assistant turns (`session_id` from body, default `'default'`)
