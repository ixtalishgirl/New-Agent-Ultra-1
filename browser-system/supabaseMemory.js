/**
 * supabaseMemory.js — persistent memory backend for the Halye agent (Supabase).
 *
 * Plain-fetch REST client (no npm deps, keeps the Render build simple).
 * Table: public.agent_memory (id uuid PK, created_at timestamptz, kind text, content jsonb).
 *
 * Env: SUPABASE_URL, SUPABASE_ANON_KEY (SUPABASE_SERVICE_KEY optional, for
 * privileged ops later — not required).
 *
 * Design rules:
 *  - Never throws: every function catches internally and returns { ok:false, ... }.
 *  - Never blocks the request path: callers fire-and-forget (no await needed).
 *  - When env is absent/unreachable: logs once + no-ops. The Python service's
 *    SQLite history (db.py) remains the local fallback — untouched.
 */

const SUPABASE_TIMEOUT_MS = Number(process.env.SUPABASE_TIMEOUT_MS || 10000);

let _warned = false;
function warnOnce(msg) {
  if (_warned) return;
  _warned = true;
  console.warn(`[supabaseMemory] ${msg}`);
}

export function supabaseConfig() {
  const url = String(process.env.SUPABASE_URL || '').trim().replace(/\/+$/, '');
  const anonKey = String(process.env.SUPABASE_ANON_KEY || '').trim();
  const serviceKey = String(process.env.SUPABASE_SERVICE_KEY || '').trim();
  return { url, anonKey, serviceKey };
}

export function isSupabaseConfigured() {
  const { url, anonKey } = supabaseConfig();
  return Boolean(url && /^https?:\/\//i.test(url) && anonKey);
}

function headers(json = true) {
  const { anonKey, serviceKey } = supabaseConfig();
  const key = serviceKey || anonKey; // service key only if he adds it later
  const h = {
    apikey: anonKey,
    Authorization: `Bearer ${key}`,
  };
  if (json) h['Content-Type'] = 'application/json';
  return h;
}

/**
 * saveMemory(kind, content) -> { ok, skipped?, error? }
 * kind: 'chat' | 'research' | 'note' | ... (free-form text)
 * content: any JSON-serializable object
 */
export async function saveMemory(kind, content) {
  if (!isSupabaseConfigured()) {
    warnOnce('SUPABASE_URL / SUPABASE_ANON_KEY not set — memory writes skipped (SQLite fallback active).');
    return { ok: false, skipped: true, error: 'supabase_not_configured' };
  }
  const { url } = supabaseConfig();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), SUPABASE_TIMEOUT_MS);
  try {
    const r = await fetch(`${url}/rest/v1/agent_memory`, {
      method: 'POST',
      signal: controller.signal,
      headers: { ...headers(), Prefer: 'return=minimal' },
      body: JSON.stringify({ kind: String(kind || 'note'), content: content ?? {} }),
    });
    if (!r.ok) {
      const t = await r.text().catch(() => '');
      console.warn(`[supabaseMemory] save failed HTTP ${r.status}: ${t.slice(0, 200)}`);
      return { ok: false, error: `http_${r.status}` };
    }
    return { ok: true };
  } catch (err) {
    console.warn(`[supabaseMemory] save failed: ${String(err?.message || err).slice(0, 200)}`);
    return { ok: false, error: 'unreachable' };
  } finally {
    clearTimeout(timer);
  }
}

/**
 * getMemory(kind, limit) -> { ok, entries:[{id,created_at,kind,content}], error? }
 */
export async function getMemory(kind, limit = 20) {
  if (!isSupabaseConfigured()) {
    return { ok: false, skipped: true, entries: [], error: 'supabase_not_configured' };
  }
  const { url } = supabaseConfig();
  const lim = Math.max(1, Math.min(Number(limit) || 20, 200));
  const qs = new URLSearchParams({
    kind: `eq.${kind}`,
    order: 'created_at.desc',
    limit: String(lim),
    select: 'id,created_at,kind,content',
  });
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), SUPABASE_TIMEOUT_MS);
  try {
    const r = await fetch(`${url}/rest/v1/agent_memory?${qs}`, {
      signal: controller.signal,
      headers: headers(false),
    });
    if (!r.ok) {
      return { ok: false, entries: [], error: `http_${r.status}` };
    }
    const entries = await r.json().catch(() => []);
    return { ok: true, entries: Array.isArray(entries) ? entries : [] };
  } catch (err) {
    console.warn(`[supabaseMemory] read failed: ${String(err?.message || err).slice(0, 200)}`);
    return { ok: false, entries: [], error: 'unreachable' };
  } finally {
    clearTimeout(timer);
  }
}

/** getMemoryStatus() -> { configured, reachable|null } for health endpoints. */
export async function getMemoryStatus() {
  const configured = isSupabaseConfigured();
  if (!configured) return { configured: false, backend: 'sqlite-fallback', reachable: null };
  const { url } = supabaseConfig();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 5000);
  try {
    const r = await fetch(`${url}/rest/v1/agent_memory?limit=1&select=id`, {
      signal: controller.signal,
      headers: headers(false),
    });
    return { configured: true, backend: 'supabase', reachable: r.ok };
  } catch {
    return { configured: true, backend: 'supabase', reachable: false };
  } finally {
    clearTimeout(timer);
  }
}

/** Fire-and-forget wrapper — never throws, never blocks. */
export function saveMemoryAsync(kind, content) {
  saveMemory(kind, content).catch(() => {});
}

// ---------------------------------------------------------------------------
// Direct Postgres (optional): SUPABASE_DB_URL
//
// The anon key cannot do DDL (CREATE TABLE etc.). When SUPABASE_DB_URL is set
// (direct Postgres connection string — he sets it himself in Render env vars),
// the agent gets querySql() for arbitrary SQL including CREATE TABLE.
// The anon-key REST path above stays the default for memory reads/writes.
// ---------------------------------------------------------------------------

let _pool = null;
let _poolFailed = false;

export function isDbUrlConfigured() {
  const u = String(process.env.SUPABASE_DB_URL || '').trim();
  return /^postgres(ql)?:\/\//i.test(u);
}

async function getPool() {
  if (_pool) return _pool;
  if (_poolFailed) return null;
  const connStr = String(process.env.SUPABASE_DB_URL || '').trim();
  if (!isDbUrlConfigured()) return null;
  try {
    const { Pool } = await import('pg'); // dynamic: module loads even if pg missing
    _pool = new Pool({
      connectionString: connStr,
      max: 3,
      idleTimeoutMillis: 30000,
      connectionTimeoutMillis: 10000,
      ssl: { rejectUnauthorized: false }, // Supabase requires SSL; pooler/SNI setups
    });
    _pool.on('error', (e) => console.warn(`[supabaseMemory] pg pool error: ${String(e?.message || e).slice(0, 150)}`));
    return _pool;
  } catch (err) {
    _poolFailed = true;
    console.warn(`[supabaseMemory] pg unavailable: ${String(err?.message || err).slice(0, 150)}`);
    return null;
  }
}

/**
 * querySql(sql, params) -> { ok, rows, rowCount, error? }
 * Arbitrary SQL against the Supabase Postgres (DDL included). Never throws.
 * Safety: parameterized via params array; destructive statements are the
 * caller's responsibility (the /api/db/query endpoint gates them).
 */
export async function querySql(sql, params = []) {
  if (!isDbUrlConfigured()) {
    return { ok: false, error: 'db_url_not_configured' };
  }
  const pool = await getPool();
  if (!pool) {
    return { ok: false, error: 'pg_pool_unavailable' };
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), Number(process.env.SUPABASE_TIMEOUT_MS || 10000));
  try {
    const res = await pool.query({ text: String(sql), values: Array.isArray(params) ? params : [], signal: controller.signal });
    return { ok: true, rows: res.rows || [], rowCount: res.rowCount ?? 0, command: res.command || null };
  } catch (err) {
    const msg = String(err?.message || err).slice(0, 300);
    console.warn(`[supabaseMemory] querySql failed: ${msg}`);
    return { ok: false, error: 'query_failed', detail: msg };
  } finally {
    clearTimeout(timer);
  }
}

export default { isSupabaseConfigured, saveMemory, getMemory, getMemoryStatus, saveMemoryAsync, supabaseConfig, isDbUrlConfigured, querySql };
