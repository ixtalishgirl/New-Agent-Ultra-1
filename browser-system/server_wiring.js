/**
 * server_wiring.js — mounts the browser system into the Halye Express app.
 *
 * Call mountBrowserSystem(app, deps) once from server.ts:
 *   import { mountBrowserSystem } from './browser-system/server_wiring.js';
 *   mountBrowserSystem(app, { legacyInspect: executeWebEyes });
 *
 * What it wires:
 *  - /api/browser/*            (browserRoutes.js: status, screenshot, tabs, history, navigate, permissions)
 *  - /api/browser/health       (deep health: sidecar + service + permissions + honest limits)
 *  - /api/browser/tool         (generic named-tool call with permission + scope gates)
 *  - /api/recon/run            (recon tools; active ones scope-checked)
 *  - /api/sectest/run          (security-testing tools; scope-checked + confirm-gated)
 *  - chat helpers: detectBrowserIntent() + runBrowserForChat()
 *
 * Free-tier honesty: Render sleeps after 15min idle — the sidecar respawns
 * lazily on the next browser request (cold start ~5-60s). No 24/7 promise.
 */

import browserRoutes from './node-integration/browserRoutes.js';
import {
  executeBrowserAction,
  BROWSER_ACTIONS_LIST,
  buildObservationContext,
  getBrowserPermissions,
} from './node-integration/browserActions.js';
import {
  ensureBrowserService,
  getSidecarStatus,
  resolveServiceUrl,
} from './browser_sidecar.js';
import {
  getPermissions,
  checkTargetScope,
  checkDestructiveConfirmed,
  checkInfraAction,
  checkShellAction,
  auditSensitiveAction,
} from './permissions.js';

const SERVICE_TIMEOUT = Number(process.env.BROWSER_TIMEOUT_MS || 25000);

// Tools that must never run without an authorized target (also enforced
// service-side in scope_guard.py — defense in depth).
const SCOPE_CHECKED_TOOLS = new Set([
  'port_scan', 'endpoint_discovery',
  'xss_probe', 'sqli_probe', 'ssrf_check', 'idor_check', 'csrf_check',
  'nmap_scan', 'zap_scan',
]);

// ---------------------------------------------------------------------------
// Chat intent detection (Roman Urdu + English, the way ahsan talks).
// Returns { action, args } or null.
// ---------------------------------------------------------------------------

const URL_RE = /https?:\/\/[^\s"'<>]+/i;

export function detectBrowserIntent(prompt) {
  const p = String(prompt || '');
  const low = p.toLowerCase();
  const urls = [...p.matchAll(/https?:\/\/[^\s"'<>]+/gi)].map((m) => m[0]).slice(0, 5);

  // Explicit slash commands (deterministic).
  const slash = low.match(/^\/(browse|search|open|extract|screenshot|shot|compare|recon)\b\s*(.*)/s);
  if (slash) {
    const [, cmd, rest] = slash;
    const restUrls = [...rest.matchAll(/https?:\/\/[^\s"'<>]+/gi)].map((m) => m[0]).slice(0, 5);
    if (cmd === 'search' || cmd === 'browse') return { action: 'browser_search', args: { query: rest.trim() || p } };
    if (cmd === 'screenshot' || cmd === 'shot') {
      if (restUrls[0]) return { action: 'browser_screenshot', args: { url: restUrls[0] } };
      return null;
    }
    if (cmd === 'compare') {
      if (restUrls.length >= 2) return { action: 'browser_compare', args: { urls: restUrls } };
      return null;
    }
    if (restUrls[0]) return { action: 'browser_open', args: { url: restUrls[0] } };
    return null;
  }

  const wantsSearch =
    /(search karo|dhoondho|dhoondo|talash karo|job listing|germany ki|websites search|\bsearch\b.*\bkaro\b)/.test(low);
  const wantsOpen =
    /(open kar|kholo|is link ko|detail samjhao|ye link|ye website|website par)/.test(low);
  const wantsShot =
    /(screenshot|screen shot|kya dikh raha|dikhao|live dekh|live view)/.test(low);
  const wantsCompare =
    /(compare|muqabla|mawazna|farq batao|in \d+ pages|kitni websites)/.test(low);

  if (wantsCompare && urls.length >= 2) return { action: 'browser_compare', args: { urls } };
  if (wantsShot && urls[0]) return { action: 'browser_screenshot', args: { url: urls[0] } };
  if (wantsOpen && urls[0]) {
    return /detail|samjhao|extract|read/i.test(p)
      ? { action: 'browser_extract', args: { url: urls[0] } }
      : { action: 'browser_open', args: { url: urls[0] } };
  }
  if (wantsSearch) {
    // Strip the command words to get a clean query.
    const query = p
      .replace(/(search karo|dhoondho|dhoondo|talash karo|please|barae meherbani)/gi, '')
      .trim();
    if (query.length >= 2) return { action: 'browser_search', args: { query } };
  }
  // Bare URL with no other instruction → open it.
  if (urls.length === 1 && p.trim().length < urls[0].length + 30) {
    return { action: 'browser_open', args: { url: urls[0] } };
  }
  return null;
}

// ---------------------------------------------------------------------------
// Run a browser action for a chat prompt (server-side tool selection).
// deps: { legacyInspect }
// ---------------------------------------------------------------------------

export async function runBrowserForChat(prompt, deps = {}) {
  const intent = detectBrowserIntent(prompt);
  if (!intent) return { intent: null };

  // Lazy sidecar: first browser request boots the Python service.
  const svc = await ensureBrowserService();
  if (svc.ok && svc.url) {
    process.env.BROWSER_SERVICE_URL = svc.url;
  }
  const { toolCall, observation } = await executeBrowserAction(intent.action, intent.args, {
    legacyInspect: deps.legacyInspect,
  });
  return {
    intent,
    toolCall,
    observation,
    observationContext: buildObservationContext(observation),
    serviceMode: svc.ok ? svc.mode : 'offline',
    serviceError: svc.ok ? null : svc.error,
  };
}

// ---------------------------------------------------------------------------
// Generic tool proxy with permission + scope gates.
// ---------------------------------------------------------------------------

async function proxyToolCall(tool, args, res, { timeoutMs = SERVICE_TIMEOUT } = {}) {
  const perms = getPermissions();
  const url = resolveServiceUrl();
  if (!url) {
    return res.status(503).json({
      success: false,
      error: 'Browser service is not running. Send any /api/browser request first (lazy sidecar boots it), then retry.',
    });
  }
  if (SCOPE_CHECKED_TOOLS.has(tool)) {
    const target = String(args?.target || args?.url || '');
    const scope = checkTargetScope(target);
    if (!scope.ok) {
      auditSensitiveAction('tool_scope_denied', { tool, target });
      return res.status(403).json({ success: false, tool, error: 'scope_denied', reason: scope.reason });
    }
    if (perms.requireConfirmConsequential && args?.confirmed !== true && tool !== 'csrf_check') {
      return res.status(409).json({
        success: false,
        tool,
        needsConfirmation: true,
        error: 'Security testing needs explicit confirmation. Re-send with confirmed:true after the user approves the authorized target.',
        scope: scope.matched || scope.host,
      });
    }
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const r = await fetch(url + '/tools/call', {
      method: 'POST',
      signal: controller.signal,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: tool, arguments: args || {} }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) return res.status(502).json({ success: false, tool, error: `service HTTP ${r.status}`, detail: data });
    return res.json({ success: true, tool, ...data });
  } catch (err) {
    return res.status(503).json({ success: false, tool, error: String(err?.message || err) });
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Build a readable chat reply from a browser observation when the model is
 * offline — facts + clickable sources + timestamp, honestly labeled.
 */
export function formatBrowserOnlyReply(browser) {
  const obs = browser?.observation;
  if (!obs) return 'Browser se koi result nahi mila.';
  const lines = [];
  if (obs.title) lines.push(`**${obs.title}**`);
  if (obs.url) lines.push(obs.url);
  if (obs.facts && obs.facts.length) {
    lines.push('');
    for (const f of obs.facts.slice(0, 15)) {
      lines.push(`- ${f.label}: ${String(f.value).slice(0, 300)}`);
    }
  }
  if (obs.sources && obs.sources.length) {
    lines.push('', 'Sources:');
    for (const s of obs.sources.slice(0, 8)) {
      lines.push(`- [${s.title || s.url}](${s.url})`);
    }
  }
  if (obs.accessedAt) {
    try {
      lines.push('', `_Accessed: ${new Date(obs.accessedAt).toLocaleString('en-PK', { timeZone: 'Asia/Karachi' })}_`);
    } catch { lines.push(`_Accessed: ${obs.accessedAt}_`); }
  }
  if (obs.fallbackUsed) lines.push('', '_Note: live browser offline tha — legacy fetch view._');
  if (!obs.success && obs.error) lines.push('', `Error: ${obs.error}`);
  return lines.join('\n') || 'Koi result nahi mila.';
}

// ---------------------------------------------------------------------------
// Mount everything.
// ---------------------------------------------------------------------------

export function mountBrowserSystem(app, deps = {}) {
  // Live-browser passthrough routes.
  app.use('/api/browser', browserRoutes);

  // Deep health: sidecar + service + permissions + honest free-tier limits.
  app.get('/api/browser/health', async (_req, res) => {
    const sidecar = getSidecarStatus();
    let service = { online: false };
    const url = resolveServiceUrl();
    if (url) {
      try {
        const ctrl = new AbortController();
        const t = setTimeout(() => ctrl.abort(), 5000);
        const r = await fetch(url + '/status', { signal: ctrl.signal });
        clearTimeout(t);
        service = { online: r.ok, detail: await r.json().catch(() => ({})) };
      } catch (err) {
        service = { online: false, error: String(err?.message || err) };
      }
    }
    res.json({
      success: true,
      sidecar,
      service,
      permissions: getPermissions(),
      browserPermissions: getBrowserPermissions(),
      limits: {
        note: 'Render Free: 0.1 CPU / 512MB RAM, sleeps after ~15min idle. No 24/7 uptime promised.',
        browser: 'Chromium launches lazily on first use; fetch-only mode when it cannot start.',
        model: 'Kaggle GPU model is separate — this endpoint does not imply the model is online.',
      },
    });
  });

  // Generic named-tool call (browser + recon + sectest tools).
  app.post('/api/browser/tool', async (req, res) => {
    const { tool, args } = req.body || {};
    if (!tool || typeof tool !== 'string') {
      return res.status(400).json({ success: false, error: 'tool (string) is required.' });
    }
    return proxyToolCall(tool, args || {}, res);
  });

  // Recon tools.
  app.post('/api/recon/run', async (req, res) => {
    const { tool, ...args } = req.body || {};
    const RECON_TOOLS = new Set([
      'dns_records', 'whois_lookup', 'tls_cert', 'subdomain_enum',
      'wayback_urls', 'tech_fingerprint', 'port_scan', 'endpoint_discovery',
    ]);
    if (!RECON_TOOLS.has(tool)) {
      return res.status(400).json({ success: false, error: `Unknown recon tool: ${tool}` });
    }
    return proxyToolCall(tool, args, res);
  });

  // Security-testing tools (scope-checked + confirm-gated; service enforces too).
  app.post('/api/sectest/run', async (req, res) => {
    const { tool, ...args } = req.body || {};
    const SECTEST_TOOLS = new Set([
      'xss_probe', 'sqli_probe', 'ssrf_check', 'idor_check',
      'csrf_check', 'nmap_scan', 'zap_scan',
    ]);
    if (!SECTEST_TOOLS.has(tool)) {
      return res.status(400).json({ success: false, error: `Unknown sectest tool: ${tool}` });
    }
    return proxyToolCall(tool, args, res, { timeoutMs: 120000 });
  });

  // Permission + scope introspection (for UI / debugging).
  app.get('/api/permissions', (_req, res) => {
    res.json({ success: true, permissions: getPermissions() });
  });
  app.post('/api/permissions/check-scope', (req, res) => {
    const scope = checkTargetScope(String(req.body?.target || ''));
    res.json({ success: true, ...scope });
  });

  return { detectBrowserIntent, runBrowserForChat };
}

export default {
  mountBrowserSystem,
  detectBrowserIntent,
  runBrowserForChat,
  formatBrowserOnlyReply,
  checkInfraAction,
  checkShellAction,
  checkTargetScope,
  checkDestructiveConfirmed,
  auditSensitiveAction,
};
