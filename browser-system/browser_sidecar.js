/**
 * browser_sidecar.js — runs the Python browser service INSIDE the Halye app
 * process tree (Render free has one container; no second service possible).
 *
 * Strategy (free-tier honest):
 *  - LAZY: the Python service spawns on the first /api/browser request, not
 *    at boot. Idle cost = 0 RAM.
 *  - The service itself lazy-launches Chromium (browser_manager.ensure_started)
 *    — no 300MB browser RAM until a real page is opened.
 *  - If Chromium can't start (missing system deps / OOM on 512MB), the service
 *    stays up in fetch-only mode: search, extract, recon, sectest all work;
 *    screenshot/navigate fail honestly with code='browser_unavailable'.
 *  - Auto-restart with backoff if the child crashes; gives up after 3 rapid
 *    crashes and reports clearly (no silent death).
 *
 * Env:
 *   BROWSER_SERVICE_URL      — if set, sidecar never spawns; uses external URL.
 *   BROWSER_SIDECAR          — '0' disables auto-spawn (default '1').
 *   BROWSER_SERVICE_PORT     — default 8080.
 *   BROWSER_SERVICE_HOST     — default 127.0.0.1.
 *   BROWSER_BOOTSTRAP_DEPS   — '1' (default) pip-installs missing py deps.
 */

import { spawn } from 'child_process';
import fs from 'fs';
import path from 'path';

const PROJECT_ROOT = process.cwd();
const SERVICE_DIR = path.join(PROJECT_ROOT, 'browser-system', 'browser_service');

const HOST = process.env.BROWSER_SERVICE_HOST || '127.0.0.1';
const PORT = Number(process.env.BROWSER_SERVICE_PORT || 8080);
const EXTERNAL_URL = (process.env.BROWSER_SERVICE_URL || '').replace(/\/+$/, '');
const SIDECAR_ENABLED = process.env.BROWSER_SIDECAR !== '0';
const BOOTSTRAP_DEPS = process.env.BROWSER_BOOTSTRAP_DEPS !== '0';

let child = null;
let lastError = null;
let restarts = 0;
let lastStartAt = 0;
let rapidCrashes = 0;
let readyUrl = EXTERNAL_URL || '';

const MIN_PY_DEPS = ['fastapi', 'uvicorn', 'httpx', 'bs4', 'pydantic'];

function log(...a) {
  console.log('[browser-sidecar]', ...a);
}

function pickPython() {
  return new Promise((resolve) => {
    const tryBin = (bin, cb) => {
      const p = spawn(bin, ['--version'], { stdio: 'ignore' });
      p.on('error', () => cb(false));
      p.on('close', (code) => cb(code === 0));
    };
    tryBin('python3', (ok) => {
      if (ok) return resolve('python3');
      tryBin('python', (ok2) => resolve(ok2 ? 'python' : null));
    });
  });
}

function runCmd(bin, args, opts = {}) {
  return new Promise((resolve) => {
    const p = spawn(bin, args, { stdio: 'pipe', ...opts });
    let out = '';
    p.stdout?.on('data', (d) => (out += d));
    p.stderr?.on('data', (d) => (out += d));
    const timer = setTimeout(() => {
      try { p.kill('SIGKILL'); } catch {}
      resolve({ code: -1, out: out + '\n[TIMEOUT]' });
    }, opts.timeoutMs || 120000);
    p.on('error', (e) => { clearTimeout(timer); resolve({ code: -1, out: String(e) }); });
    p.on('close', (code) => { clearTimeout(timer); resolve({ code, out }); });
  });
}

async function bootstrapDeps(pyBin) {
  if (!BOOTSTRAP_DEPS) return true;
  const missing = [];
  for (const mod of MIN_PY_DEPS) {
    const r = await runCmd(pyBin, ['-c', `import ${mod}`], { timeoutMs: 15000 });
    if (r.code !== 0) missing.push(mod === 'bs4' ? 'beautifulsoup4' : mod);
  }
  if (!missing.length) return true;
  log('installing missing python deps:', missing.join(', '));
  const pipArgs = ['-m', 'pip', 'install', '-q', '--user', ...missing];
  let r = await runCmd(pyBin, pipArgs, { timeoutMs: 300000 });
  if (r.code !== 0 && /externally-managed-environment|PEP 668/i.test(r.out)) {
    // Container/system Pythons (Debian, Render) need this flag — safe in containers.
    log('retrying pip with --break-system-packages (PEP 668)');
    r = await runCmd(pyBin, ['-m', 'pip', 'install', '-q', '--user', '--break-system-packages', ...missing], { timeoutMs: 300000 });
  }
  if (r.code !== 0) {
    lastError = 'pip install failed: ' + r.out.slice(-500);
    return false;
  }
  return true;
}

async function waitForHealth(timeoutMs = 45000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      const ctrl = new AbortController();
      const t = setTimeout(() => ctrl.abort(), 3000);
      const r = await fetch(`${readyUrl}/status`, { signal: ctrl.signal });
      clearTimeout(t);
      if (r.ok) return true;
    } catch {}
    await new Promise((r) => setTimeout(r, 1000));
  }
  return false;
}

/**
 * Ensure the browser service is up. Spawns it lazily on first call.
 * Returns { ok, url } or { ok:false, error }.
 */
export async function ensureBrowserService() {
  if (EXTERNAL_URL) {
    readyUrl = EXTERNAL_URL;
    return { ok: true, url: EXTERNAL_URL, mode: 'external' };
  }
  if (!SIDECAR_ENABLED) {
    return { ok: false, error: 'Sidecar disabled (BROWSER_SIDECAR=0) and BROWSER_SERVICE_URL unset.' };
  }
  if (child && child.exitCode === null) {
    if (await waitForHealth(5000)) return { ok: true, url: readyUrl, mode: 'sidecar' };
  }
  if (rapidCrashes >= 3) {
    return {
      ok: false,
      error: `Browser service crashed ${rapidCrashes}x rapidly — not retrying. Last error: ${lastError || 'unknown'}. ` +
             'Fetch-only mode remains available via legacy web inspection.',
    };
  }
  if (!fs.existsSync(path.join(SERVICE_DIR, 'app.py'))) {
    return { ok: false, error: `Browser service not found at ${SERVICE_DIR}` };
  }
  const pyBin = await pickPython();
  if (!pyBin) return { ok: false, error: 'No python3/python found for the browser sidecar.' };
  if (!(await bootstrapDeps(pyBin))) {
    return { ok: false, error: lastError || 'Python dependency bootstrap failed.' };
  }

  readyUrl = `http://${HOST}:${PORT}`;
  lastStartAt = Date.now();
  log(`spawning browser service: ${pyBin} -m uvicorn app:app (${readyUrl})`);
  child = spawn(pyBin, ['-m', 'uvicorn', 'app:app', '--host', HOST, '--port', String(PORT)], {
    cwd: SERVICE_DIR,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: {
      ...process.env,
      BROWSER_SERVICE_HOST: HOST,
      BROWSER_SERVICE_PORT: String(PORT),
      PYTHONUNBUFFERED: '1',
    },
  });
  child.stdout.on('data', (d) => log('[svc]', String(d).trim().slice(0, 300)));
  child.stderr.on('data', (d) => log('[svc:err]', String(d).trim().slice(0, 300)));
  child.on('exit', (code, sig) => {
    log(`browser service exited (code=${code} sig=${sig})`);
    const sinceStart = Date.now() - lastStartAt;
    rapidCrashes = sinceStart < 60000 ? rapidCrashes + 1 : 0;
    restarts += 1;
    child = null;
  });
  child.on('error', (e) => {
    lastError = String(e);
    child = null;
  });

  if (await waitForHealth(60000)) {
    rapidCrashes = 0;
    log('browser service ready at', readyUrl);
    return { ok: true, url: readyUrl, mode: 'sidecar' };
  }
  lastError = 'Service did not become healthy within 60s.';
  try { child?.kill('SIGTERM'); } catch {}
  child = null;
  return { ok: false, error: lastError };
}

export function resolveServiceUrl() {
  return readyUrl || EXTERNAL_URL || (process.env.BROWSER_SERVICE_URL || '').replace(/\/+$/, '');
}

export function getSidecarStatus() {
  return {
    enabled: SIDECAR_ENABLED,
    externalUrl: EXTERNAL_URL || null,
    running: Boolean(child && child.exitCode === null),
    pid: child?.pid || null,
    url: readyUrl || null,
    restarts,
    rapidCrashes,
    lastError,
  };
}

/** Call from server shutdown. */
export function stopSidecar() {
  try { child?.kill('SIGTERM'); } catch {}
  child = null;
}

process.on('exit', stopSidecar);

export default { ensureBrowserService, getSidecarStatus, stopSidecar, resolveServiceUrl };
