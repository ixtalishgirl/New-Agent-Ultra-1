#!/usr/bin/env node
/**
 * E2E live test: chat command → intent → browser service → tools → chat response.
 *
 * Usage:
 *   APP_URL=http://127.0.0.1:3000 node browser-system/tests/e2e_live.mjs
 *   # or against production:
 *   APP_URL=https://haley-agent.onrender.com node browser-system/tests/e2e_live.mjs
 *
 * What it verifies (honestly — failures are reported, never faked):
 *  1. /api/health ................. app is up
 *  2. /api/browser/health ......... sidecar + service + permissions
 *  3. chat "search karo" .......... intent → browser_search → observation in reply
 *  4. chat bare URL ............... intent → browser_open (+ legacy fallback note)
 *  5. /api/recon/run dns_records .. passive recon works
 *  6. /api/sectest/run xss_probe .. unauthorized target → scope_denied (must NOT run)
 *  7. /api/permissions ............ full grant set visible
 *
 * Live-browser checks (screenshot, real Chromium) are marked separately —
 * they need a host where Chromium can actually launch (not Render free).
 */

const APP = (process.env.APP_URL || 'http://127.0.0.1:3000').replace(/\/+$/, '');
const results = [];

async function check(name, fn) {
  const started = Date.now();
  try {
    const detail = await fn();
    results.push({ name, pass: true, ms: Date.now() - started, detail });
    console.log(`  [PASS] ${name} (${Date.now() - started}ms)${detail ? ' — ' + detail : ''}`);
  } catch (err) {
    results.push({ name, pass: false, ms: Date.now() - started, error: String(err?.message || err) });
    console.log(`  [FAIL] ${name} — ${err?.message || err}`);
  }
}

async function post(path, body, timeoutMs = 120000) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(APP + path, {
      method: 'POST', signal: ctrl.signal,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    return { status: r.status, data: await r.json().catch(() => ({})) };
  } finally {
    clearTimeout(t);
  }
}

async function get(path, timeoutMs = 30000) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(APP + path, { signal: ctrl.signal });
    return { status: r.status, data: await r.json().catch(() => ({})) };
  } finally {
    clearTimeout(t);
  }
}

console.log(`E2E live test against ${APP}\n`);

await check('app /api/health', async () => {
  const { status, data } = await get('/api/health');
  if (status !== 200 || data.status !== 'ONLINE') throw new Error(`status=${status}`);
  return `memory ${data.memoryMb}MB`;
});

await check('browser /api/browser/health', async () => {
  const { status, data } = await get('/api/browser/health');
  if (status !== 200 || !data.success) throw new Error(`status=${status}`);
  const s = data.sidecar || {};
  const svc = data.service || {};
  return `sidecar=${s.running ? 'running' : 'lazy'} service=${svc.online ? 'online' : 'offline'} ` +
         `playwright=${svc.detail?.playwright === true ? 'YES' : 'no'}`;
});

await check('chat search intent → browser_search', async () => {
  const { data } = await post('/api/model/chat', { prompt: 'germany ki job websites search karo', max_tokens: 200 });
  if (!data.success) throw new Error('chat failed: ' + (data.error || 'unknown'));
  const obs = data.browserObservation;
  if (!obs || obs.action !== 'browser_search') throw new Error('no browser_search observation in reply');
  if (obs.success && obs.facts.length > 0) return `${obs.facts.length} facts, ${obs.sources.length} sources`;
  return `honest failure: ${(obs.error || '').slice(0, 80)}`;
});

await check('chat bare URL → browser_open', async () => {
  const { data } = await post('/api/model/chat', { prompt: 'https://example.com', max_tokens: 200 });
  if (!data.success) throw new Error('chat failed');
  const obs = data.browserObservation;
  if (!obs || obs.action !== 'browser_open') throw new Error('no browser_open observation');
  return obs.success ? `opened: ${obs.title}` : `fallback=${obs.fallback} err=${(obs.error || '').slice(0, 60)}`;
});

await check('recon dns_records (passive)', async () => {
  const { status, data } = await post('/api/recon/run', { tool: 'dns_records', target: 'example.com' }, 60000);
  if (status !== 200 || !data.success) throw new Error(`status=${status} err=${data.error || ''}`);
  const recs = data.data?.records || data.records || {};
  return `A/AAAA: ${(recs.A_AAAA || []).join(',') || 'none'}`;
});

await check('sectest xss_probe unauthorized → scope_denied', async () => {
  const { status, data } = await post(
    '/api/sectest/run',
    { tool: 'xss_probe', url: 'https://unauthorized-example-12345.com/?q=1', confirmed: true },
    60000
  );
  if (status === 403 && (data.reason || data.error || '').includes('scope')) {
    return 'correctly refused (scope_denied)';
  }
  throw new Error(`expected 403 scope_denied, got status=${status}`);
});

await check('/api/permissions shows full grant', async () => {
  const { data } = await get('/api/permissions');
  const p = data.permissions || {};
  const need = ['allowRootAdmin', 'allowUnrestrictedShell', 'allowUnrestrictedFilesystem', 'allowSecTesting'];
  const missing = need.filter((k) => p[k] !== true);
  if (missing.length) throw new Error('missing grants: ' + missing.join(','));
  return 'all grants present';
});

const passed = results.filter((r) => r.pass).length;
console.log(`\n== ${passed}/${results.length} passed ==`);
const liveBrowser = results.find((r) => r.name.includes('browser/health'));
console.log('Live Chromium on this host:', /playwright=YES/.test(liveBrowser?.detail || '') ? 'YES' : 'NO (fetch-only mode — honest)');
process.exit(passed === results.length ? 0 : 1);
