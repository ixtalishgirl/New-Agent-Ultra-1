/**
 * browserRoutes.js — Express router for /api/browser/* (registered in server.ts).
 *
 * Passthroughs to the Python browser service (BROWSER_SERVICE_URL) with
 * permission toggles + SSRF guard. When the service is offline, endpoints
 * return honest 503s instead of fake data.
 *
 * Env:
 *   BROWSER_SERVICE_URL                 e.g. http://127.0.0.1:8765
 *   BROWSER_TIMEOUT_MS                  default 25000
 *   BROWSER_ALLOW_PUBLIC                default '1'
 *   BROWSER_ALLOW_DOWNLOADS             default '0'
 *   BROWSER_ALLOW_FORMS                 default '0'  (no automatic form-filling)
 *   BROWSER_REQUIRE_CONFIRM_CONSEQUENTIAL default '1'
 */

import express from 'express';
import { lookup as dnsLookup } from 'dns/promises';

const router = express.Router();

const TIMEOUT_MS = Number(process.env.BROWSER_TIMEOUT_MS || 25000);

/** Resolved per call — the sidecar sets process.env.BROWSER_SERVICE_URL lazily. */
function serviceUrl() {
  return (process.env.BROWSER_SERVICE_URL || '').replace(/\/+$/, '');
}

export function getBrowserPermissions() {
  return {
    allowPublic: process.env.BROWSER_ALLOW_PUBLIC !== '0',
    allowDownloads: process.env.BROWSER_ALLOW_DOWNLOADS === '1',
    allowForms: process.env.BROWSER_ALLOW_FORMS === '1',
    requireConfirmConsequential: process.env.BROWSER_REQUIRE_CONFIRM_CONSEQUENTIAL !== '0',
    serviceConfigured: Boolean(serviceUrl()),
  };
}

// ---------------------------------------------------------------------------
// SSRF guard: only public http(s) hosts. Blocks private ranges, loopback,
// link-local (incl. cloud metadata 169.254.169.254), and non-http schemes.
// ---------------------------------------------------------------------------

function ipToInt(ip) {
  const parts = String(ip).split('.');
  if (parts.length !== 4) return null;
  let n = 0;
  for (const p of parts) {
    const v = Number(p);
    if (!Number.isInteger(v) || v < 0 || v > 255) return null;
    n = (n << 8) + v;
  }
  return n >>> 0;
}

function isPrivateIp(ip) {
  const n = ipToInt(ip);
  if (n === null) return true; // not plain IPv4 -> treat as unsafe for this check
  const inRange = (a, b) => n >= a && n <= b;
  return (
    inRange(ipToInt('10.0.0.0'), ipToInt('10.255.255.255')) ||
    inRange(ipToInt('172.16.0.0'), ipToInt('172.31.255.255')) ||
    inRange(ipToInt('192.168.0.0'), ipToInt('192.168.255.255')) ||
    inRange(ipToInt('127.0.0.0'), ipToInt('127.255.255.255')) ||
    inRange(ipToInt('169.254.0.0'), ipToInt('169.254.255.255')) ||
    inRange(ipToInt('0.0.0.0'), ipToInt('0.255.255.255'))
  );
}

async function assertSafePublicUrl(raw) {
  if (typeof raw !== 'string' || !/^(https?:\/\/)/i.test(raw.trim())) {
    throw Object.assign(new Error('Only http(s) URLs are allowed.'), { status: 400 });
  }
  let parsed;
  try {
    parsed = new URL(raw.trim());
  } catch {
    throw Object.assign(new Error('Invalid URL.'), { status: 400 });
  }
  const host = parsed.hostname.toLowerCase();
  if (host === 'localhost' || host.endsWith('.local') || host.endsWith('.internal')) {
    throw Object.assign(new Error('Internal hostnames are not allowed.'), { status: 403 });
  }
  if (ipToInt(host) !== null) {
    if (isPrivateIp(host)) throw Object.assign(new Error('Private IP targets are not allowed.'), { status: 403 });
    return parsed.toString();
  }
  try {
    const { address } = await dnsLookup(host);
    if (isPrivateIp(address)) {
      throw Object.assign(new Error('Hostname resolves to a private IP.'), { status: 403 });
    }
  } catch (err) {
    if (err && err.status) throw err;
    throw Object.assign(new Error(`Could not resolve host: ${host}`), { status: 400 });
  }
  return parsed.toString();
}

async function proxyToService(path, payload, res, { timeoutMs = TIMEOUT_MS } = {}) {
  const SERVICE_URL = serviceUrl();
  if (!SERVICE_URL) {
    return res.status(503).json({
      success: false,
      error: 'Browser service is not configured (BROWSER_SERVICE_URL unset). Legacy fetch-only mode has no live browser.',
      fallback: 'legacy_fetch',
    });
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const r = await fetch(SERVICE_URL + path, {
      method: payload ? 'POST' : 'GET',
      signal: controller.signal,
      headers: { 'Content-Type': 'application/json' },
      body: payload ? JSON.stringify(payload) : undefined,
    });
    const text = await r.text();
    let data;
    try {
      data = text ? JSON.parse(text) : {};
    } catch {
      data = { raw: text.slice(0, 4000) };
    }
    if (!r.ok) return res.status(502).json({ success: false, error: `browser service HTTP ${r.status}`, detail: data });
    return res.json({ success: true, ...(typeof data === 'object' && data !== null ? data : { data }) });
  } catch (err) {
    return res.status(503).json({
      success: false,
      error: `Browser service unreachable: ${err instanceof Error ? err.message : String(err)}`,
      fallback: 'legacy_fetch',
    });
  } finally {
    clearTimeout(timer);
  }
}

// ---------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------

/** Health: is the live service up? What is permitted? */
router.get('/status', async (_req, res) => {
  const perms = getBrowserPermissions();
  const SERVICE_URL = serviceUrl();
  if (!SERVICE_URL) {
    return res.json({
      success: true,
      service: { configured: false, online: false },
      permissions: perms,
      fallback: 'legacy_fetch',
      note: 'Live browser offline — inspect_web legacy fetch is the only web capability.',
    });
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 5000);
  const started = Date.now();
  try {
    const r = await fetch(SERVICE_URL + '/status', { signal: controller.signal });
    const data = await r.json().catch(() => ({}));
    return res.json({
      success: true,
      service: { configured: true, online: r.ok, latencyMs: Date.now() - started, detail: data },
      permissions: perms,
      fallback: 'legacy_fetch',
    });
  } catch (err) {
    return res.json({
      success: true,
      service: { configured: true, online: false, error: err instanceof Error ? err.message : String(err) },
      permissions: perms,
      fallback: 'legacy_fetch',
    });
  } finally {
    clearTimeout(timer);
  }
});

/** Current permission toggles (for the chat UI / admin panel). */
router.get('/permissions', (_req, res) => {
  res.json({ success: true, permissions: getBrowserPermissions() });
});

/** Proxy a live screenshot from the service. 503 when offline — never a fake image. */
router.get('/screenshot', async (req, res) => {
  try {
    const url = await assertSafePublicUrl(String(req.query.url || ''));
    const perms = getBrowserPermissions();
    const SERVICE_URL = serviceUrl();
    if (!perms.allowPublic) return res.status(403).json({ success: false, error: 'Public browsing is disabled (BROWSER_ALLOW_PUBLIC=0).' });
    if (!SERVICE_URL) {
      return res.status(503).json({
        success: false,
        error: 'Browser service offline — screenshots require the live rendered browser; legacy fetch-only mode cannot take them.',
      });
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
    try {
      const r = await fetch(SERVICE_URL + '/screenshot', {
        method: 'POST',
        signal: controller.signal,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok || !data?.screenshot_base64) {
        return res.status(502).json({ success: false, error: 'Screenshot failed.', detail: data?.error || data });
      }
      if (req.query.format === 'json') {
        return res.json({ success: true, url, title: data.title || url, image: `data:${data.mime || 'image/png'};base64,${data.screenshot_base64}` });
      }
      const buf = Buffer.from(data.screenshot_base64, 'base64');
      res.setHeader('Content-Type', data.mime || 'image/png');
      res.setHeader('Cache-Control', 'no-store');
      return res.send(buf);
    } finally {
      clearTimeout(timer);
    }
  } catch (err) {
    return res.status(err.status || 500).json({ success: false, error: err.message });
  }
});

/** Browsing history from the service (empty + honest when offline). */
router.get('/history', async (_req, res) => {
  return proxyToService('/history', null, res);
});

/** List live tabs on the service. */
router.get('/tabs', async (_req, res) => {
  return proxyToService('/tabs', null, res);
});

/**
 * Navigate passthrough. Form submissions are refused unless BROWSER_ALLOW_FORMS=1.
 * Consequential actions (download, or anything flagged by the caller) require an
 * explicit `confirmed: true` body flag while BROWSER_REQUIRE_CONFIRM_CONSEQUENTIAL=1.
 */
router.post('/navigate', async (req, res) => {
  const perms = getBrowserPermissions();
  try {
    const url = await assertSafePublicUrl(String(req.body?.url || ''));
    const { formFill, download, confirmed, ...rest } = req.body || {};
    if (!perms.allowPublic) return res.status(403).json({ success: false, error: 'Public browsing is disabled.' });
    if (formFill && !perms.allowForms) {
      return res.status(403).json({
        success: false,
        error: 'Automatic form-filling is disabled (BROWSER_ALLOW_FORMS=0). A human must fill forms manually.',
      });
    }
    const consequential = Boolean(download || req.body?.consequential);
    if (consequential) {
      if (download && !perms.allowDownloads) {
        return res.status(403).json({ success: false, error: 'Downloads are disabled (BROWSER_ALLOW_DOWNLOADS=0).' });
      }
      if (perms.requireConfirmConsequential && !confirmed) {
        return res.status(409).json({
          success: false,
          needsConfirmation: true,
          error: 'This is a consequential action (purchase, download, deletion, public post). Re-send with confirmed:true after the user approves.',
        });
      }
    }
    return proxyToService('/navigate', { url, ...rest }, res);
  } catch (err) {
    return res.status(err.status || 500).json({ success: false, error: err.message });
  }
});

export default router;
