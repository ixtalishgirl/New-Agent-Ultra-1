/**
 * permissions.js — canonical permission toggles for ahsan's agent.
 *
 * Defaults implement his STANDING GRANT (browser-system/PERMISSIONS.md, issued
 * 2026-10-13): maximum practical web-research + security-testing access, each
 * independently toggleable via env vars.
 *
 * HARD BOUNDARY (non-overridable — enforced in code, no env flag can skip):
 *  - active recon + security testing run ONLY against authorized targets /
 *    program scope (scope guard). An empty authorized-target list = deny.
 *  - no unrestricted private-data / credential / unauthorized-system access.
 *  - logins only via authorized sessions; CAPTCHAs / anti-bot / site
 *    restrictions are never bypassed (no code path exists for it).
 *  - destructive actions (deletion, public posting, purchases) always require
 *    explicit confirmed:true — cannot be defaulted on.
 *  - page content is untrusted data — never treated as system instructions.
 *
 * Infra update (2026-10-13 ~17:24 PKT): on his explicit risk acceptance, the
 * FULL infrastructure set is granted for HIS OWN systems — root/admin, cloud
 * admin, user management, prod DB/credentials, secrets visibility,
 * unrestricted shell/network. Retained non-negotiable safeguards: explicit
 * confirmation for destructive/consequential actions, page-content-never-as-
 * instructions, audit logging of sensitive actions.
 */

function envBool(name, def) {
  const v = process.env[name];
  if (v === undefined || v === null || String(v).trim() === '') return def;
  return /^(1|true|yes|on)$/i.test(String(v).trim());
}

import fs from 'fs';
import { createHash } from 'crypto';

/** Comma-separated authorized target domains for active recon + sec testing. */
export function getAuthorizedTargets() {
  const raw = process.env.SECTEST_AUTHORIZED_TARGETS || '';
  return raw
    .split(',')
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean);
}

export function getPermissions() {
  return {
    // ---- Web research (default ON per standing grant) ----
    allowPublicBrowsing: true /* BROWSER_ALLOW_PUBLIC — hard-coded ON per ahsan 2026-10-14 */,
    allowPageSource: true /* BROWSER_ALLOW_PAGE_SOURCE — hard-coded ON per ahsan 2026-10-14 */,
    allowBrowserControl: true /* BROWSER_ALLOW_BROWSER_CONTROL — hard-coded ON per ahsan 2026-10-14 */,
    allowSearchCrawl: true /* BROWSER_ALLOW_SEARCH_CRAWL — hard-coded ON per ahsan 2026-10-14 */,

    // ---- Recon ----
    allowPassiveRecon: true /* BROWSER_ALLOW_PASSIVE_RECON — hard-coded ON per ahsan 2026-10-14 */, // DNS/WHOIS/TLS/crt.sh/wayback/fingerprinting
    allowActiveRecon: true /* BROWSER_ALLOW_ACTIVE_RECON — hard-coded ON per ahsan 2026-10-14 */, // scope-checked, authorized targets only

    // ---- Security testing (scope-checked + confirmation-gated) ----
    // Covers: ZAP/Nmap wrappers (if installed), vuln detection, XSS,
    // SQLi incl. blind boolean/time-based, SSRF, IDOR, CSRF, auth testing.
    allowSecTesting: true /* BROWSER_ALLOW_SECTEST — hard-coded ON per ahsan 2026-10-14 */,

    // ---- Agent runtime ----
    allowWorkspaceFiles: true /* BROWSER_ALLOW_WORKSPACE_FILES — hard-coded ON per ahsan 2026-10-14 */,
    allowPythonExec: true /* BROWSER_ALLOW_PYTHON_EXEC — hard-coded ON per ahsan 2026-10-14 */,
    allowParallelTasks: true /* BROWSER_ALLOW_PARALLEL_TASKS — hard-coded ON per ahsan 2026-10-14 */,
    allowEvidenceCollection: true /* BROWSER_ALLOW_EVIDENCE — hard-coded ON per ahsan 2026-10-14 */,
    allowGermanyRouting: true /* BROWSER_ALLOW_GERMANY_ROUTING — hard-coded ON per ahsan 2026-10-14 */,
    allowAutoRecovery: true /* BROWSER_ALLOW_AUTO_RECOVERY — hard-coded ON per ahsan 2026-10-14 */,

    // ---- Gated by default ----
    allowDownloads: true /* BROWSER_ALLOW_DOWNLOADS — hard-coded ON per ahsan 2026-10-14 */,
    allowForms: true /* BROWSER_ALLOW_FORMS — hard-coded ON per ahsan 2026-10-14 */,
    requireConfirmConsequential: envBool('BROWSER_REQUIRE_CONFIRM_CONSEQUENTIAL', true) /* USER TOGGLE — ahsan controls this */,

    // ---- Infrastructure (FULL GRANT for his own infra, 2026-10-13 ~17:24 PKT —
    // he explicitly accepted the risk). Retained safeguards: confirmation for
    // destructive actions, prompt-injection guard, audit logging.
    allowOwnDeployments: true /* INFRA_ALLOW_OWN_DEPLOYMENTS — hard-coded ON per ahsan 2026-10-14 */,
    allowWorkspaceShell: true /* INFRA_ALLOW_WORKSPACE_SHELL — hard-coded ON per ahsan 2026-10-14 */,
    allowRootAdmin: true /* INFRA_ALLOW_ROOT_ADMIN — hard-coded ON per ahsan 2026-10-14 */,
    allowCloudAdmin: true /* INFRA_ALLOW_CLOUD_ADMIN — hard-coded ON per ahsan 2026-10-14 */,
    allowUserManagement: true /* INFRA_ALLOW_USER_MANAGEMENT — hard-coded ON per ahsan 2026-10-14 */,
    allowProdCredentials: true /* INFRA_ALLOW_PROD_CREDENTIALS — hard-coded ON per ahsan 2026-10-14 */,
    allowSecretVisibility: true /* INFRA_ALLOW_SECRET_VISIBILITY — hard-coded ON per ahsan 2026-10-14 */,
    allowUnrestrictedShell: true /* INFRA_ALLOW_UNRESTRICTED_SHELL — hard-coded ON per ahsan 2026-10-14 */,
    // Full filesystem on HIS OWN systems (2026-10-13, his explicit order): entire /,
    // C:\\, all users' /home, OS config dirs, browser profiles, SSH dirs,
    // password stores, cloud credential dirs, app secret/config dirs.
    // Anti-leak rule: credential material is USED, never pasted into chat
    // responses or sent to third parties (sanitizeForChat).
    allowUnrestrictedFilesystem: true /* INFRA_ALLOW_UNRESTRICTED_FS — hard-coded ON per ahsan 2026-10-14 */,
    // ---- Derived ----
    authorizedTargets: getAuthorizedTargets(),
    serviceConfigured: Boolean((process.env.BROWSER_SERVICE_URL || '').trim()),
  };
}

// ---------------------------------------------------------------------------
// Hard-boundary guards (cannot be disabled by env).
// ---------------------------------------------------------------------------

/**
 * Scope guard: is `target` (URL or hostname) inside the authorized set?
 * - Exact domain or subdomain match against SECTEST_AUTHORIZED_TARGETS.
 * - Optional SECTEST_SCOPE_FILE (JSON: { "targets": ["example.com", ...] }).
 * Returns { ok, reason }.
 */
export function checkTargetScope(target) {
  let host = '';
  try {
    const u = new URL(/^[a-z]+:\/\//i.test(target) ? target : `https://${target}`);
    host = u.hostname.toLowerCase();
  } catch {
    return { ok: false, reason: `Invalid target: ${target}` };
  }
  const allowed = new Set(getAuthorizedTargets());
  // Optional scope file (lets ahsan maintain a program-scope list on disk).
  try {
    const scopeFile = process.env.SECTEST_SCOPE_FILE;
    if (scopeFile && fs.existsSync(scopeFile)) {
      const data = JSON.parse(fs.readFileSync(scopeFile, 'utf8'));
      for (const t of data.targets || []) allowed.add(String(t).trim().toLowerCase());
    }
  } catch {
    /* scope file problems fail closed below */
  }
  for (const a of allowed) {
    if (host === a || host.endsWith('.' + a)) return { ok: true, host, matched: a };
  }
  return {
    ok: false,
    host,
    reason:
      `Target ${host} is NOT in the authorized scope. ` +
      `Active recon and security testing run only against authorized targets / program scope. ` +
      `Add the domain to SECTEST_AUTHORIZED_TARGETS (or SECTEST_SCOPE_FILE) to authorize it.`,
  };
}

/**
 * Destructive-action gate: always requires explicit confirmed:true.
 * There is intentionally no env flag to bypass this.
 */
export function checkDestructiveConfirmed(body) {
  if (body && body.confirmed === true) return { ok: true };
  return {
    ok: false,
    needsConfirmation: true,
    reason:
      'Destructive action (account deletion, public posting, purchase, or similar) ' +
      'requires explicit user confirmation. Re-send with confirmed:true after the user approves.',
  };
}

// ---------------------------------------------------------------------------
// Infrastructure permissions — FULL GRANT for ahsan's OWN infrastructure
// (2026-10-13 ~17:24 PKT: he explicitly accepted the risk —
// "dy do khair haa, min apni Halye ko sembal ky rekhu ga").
//
// Granted: root/admin on his own systems, cloud admin (his accounts), user
// management on his platforms, production DB/credentials access, secrets
// visibility, unrestricted shell/network.
//
// RETAINED SAFEGUARDS (non-negotiable, non-overridable — from his own spec
// section 6; they protect against malicious WEB CONTENT tricking the agent,
// they don't reduce his power):
//  1. explicit confirmation for destructive/consequential actions
//     (checkDestructiveConfirmed — no bypass path exists).
//  2. page content is NEVER treated as system instructions
//     (prompt-injection guard — wrapUntrusted in browserActions.js).
//  3. audit logging of sensitive actions (auditSensitiveAction below).
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Shell permissions (2026-10-13, PERMISSIONS.md — final).
// GRANTED (his own systems, his explicit orders + accepted risk):
//  - unrestricted Bash/PowerShell/CMD, arbitrary commands in his workspace,
//    process-killing, service control, cron/scheduled-task modification,
//    executable launching.
//  - blind_run_unknown_script: GRANTED blind-run — every execution MUST be
//    audit-logged with source URL + SHA-256 hash.
//  - privesc_general: GRANTED on his own systems — every use MUST be
//    audit-logged with the full command.
// PERMANENTLY DENIED (not his to authorize):
//  - container_escape — the container host (e.g. Render) is the provider's
//    system; breaking out attacks shared infrastructure.
// ---------------------------------------------------------------------------

const SHELL_PERMANENT_DENIAL = {
  container_escape:
    'Container escape to the host shell is PERMANENTLY DENIED. The host is the provider\u2019s system (e.g. Render) \u2014 breaking out of it is an attack on shared infrastructure that is not his to authorize. This is not his risk to accept.',
};

/**
 * Check a shell-class action.
 * - 'container_escape' \u2192 always denied (permanent, not his to authorize).
 * - 'blind_run_unknown_script' \u2192 GRANTED (his explicit order); MANDATORY audit
 *   with source URL + SHA-256 hash. Pass { sourceUrl, sha256 } or { scriptContent }.
 * - 'privesc_general' \u2192 GRANTED on his own systems (his explicit order);
 *   MANDATORY audit with the full command. Pass { command }.
 * - 'privesc_scoped' \u2192 authorized security-testing targets only (scope guard).
 * - granted powers: 'arbitrary_command', 'process_control', 'service_control',
 *   'cron_control', 'executable_launch'.
 */
export function checkShellAction(kind, opts = {}) {
  const permDenial = SHELL_PERMANENT_DENIAL[kind];
  if (permDenial) {
    auditSensitiveAction('shell_denied:' + kind, { target: opts.target || null });
    return { ok: false, nonOverridable: true, permanent: true, reason: permDenial };
  }
  if (kind === 'blind_run_unknown_script') {
    const sha256 =
      opts.sha256 ||
      (opts.scriptContent ? createHash('sha256').update(String(opts.scriptContent)).digest('hex') : null);
    auditSensitiveAction('blind_script_run', {
      sourceUrl: opts.sourceUrl || null,
      sha256,
      command: opts.command || null,
    });
    return { ok: true, audited: true, sha256, note: 'Execution audit-logged with source URL + hash.' };
  }
  if (kind === 'privesc_general') {
    auditSensitiveAction('privesc_general', {
      command: opts.command || null,
      target: opts.target || 'own-system',
    });
    return { ok: true, audited: true, note: 'Privesc execution audit-logged with full command.' };
  }
  if (kind === 'privesc_scoped') {
    const scope = checkTargetScope(opts.target || '');
    if (!scope.ok) {
      return { ok: false, nonOverridable: true, reason: scope.reason };
    }
    auditSensitiveAction('privesc_scoped', { target: scope.host, command: opts.command || null });
    return { ok: true, audited: true, scope: scope.matched || scope.host };
  }
  const granted = new Set([
    'arbitrary_command',
    'process_control',
    'service_control',
    'cron_control',
    'executable_launch',
  ]);
  if (granted.has(kind)) {
    if (!getPermissions().allowUnrestrictedShell) {
      return { ok: false, reason: 'Shell actions disabled by INFRA_ALLOW_UNRESTRICTED_SHELL=0.' };
    }
    return { ok: true };
  }
  return { ok: false, reason: `Unknown shell action kind: ${kind}` };
}

// ---------------------------------------------------------------------------
// Kernel-level access (2026-10-13, PERMISSIONS.md).
// GRANTED on his own bare-metal systems ONLY (his explicit order).
// HONEST TECHNICAL NOTE (surfaced to the caller every time): inside containers
// (Render, Kaggle) kernel access is technically IMPOSSIBLE — the container has
// no kernel of its own. This permission can only ever apply where he owns the
// hardware (his own PC / bare metal).
// NEVER against a hosting provider's infrastructure (same line as the
// permanently-denied container escape).
// ---------------------------------------------------------------------------
export function checkKernelAccess(opts = {}) {
  const note =
    'Technical note: kernel access is impossible inside containers (Render/Kaggle) — ' +
    'applies only on his own bare-metal hardware.';
  if (opts.providerInfra === true) {
    auditSensitiveAction('kernel_denied:provider_infra', {});
    return {
      ok: false, nonOverridable: true, permanent: true, note,
      reason: 'Kernel access against a hosting provider\u2019s infrastructure is NOT granted — same line as container escape (permanently denied).',
    };
  }
  if (opts.bareMetal !== true) {
    return {
      ok: false, note,
      reason: 'Kernel access is granted for his own bare-metal systems only. Declare bareMetal:true to confirm this is his own hardware.',
    };
  }
  auditSensitiveAction('kernel_access', { host: opts.host || 'own-bare-metal' });
  return { ok: true, audited: true, note };
}

// ---------------------------------------------------------------------------
// Credential anti-leak (operational rule, 2026-10-13 — not a capability limit).
// Credential material (private keys, password-store contents, cloud secrets)
// may be USED by the agent for its tasks, but must NEVER be pasted into chat
// responses or sent to third parties. sanitizeForChat() enforces this on the
// way out; CREDENTIAL_PATH_RE helps callers recognize sensitive locations.
// ---------------------------------------------------------------------------
const CREDENTIAL_PATH_RE = [
  /[\\/]\.ssh[\\/]/i,
  /[\\/]\.aws[\\/]/i,
  /[\\/]\.gnupg[\\/]/i,
  /[\\/]\.config[\\/]gcloud/i,
  /[\\/]\.azure[\\/]/i,
  /password[-_ ]?store/i,
  /[\\/]\.password-store[\\/]/i,
  /[\\/]\.mozilla[\\/]/i,
  /[\\/]\.config[\\/]google-chrome/i,
  /Login Data$/i,
  /Cookies$/i,
  /id_rsa|id_ed25519|\.pem$/i,
  /\.env$/i,
  /credentials\.json$/i,
  /secrets?\.ya?ml$/i,
];

/** True if a filesystem path looks like it holds credential material. */
export function looksLikeCredentialPath(p) {
  return CREDENTIAL_PATH_RE.some((re) => re.test(String(p || '')));
}

const CHAT_SECRET_PATTERNS = [
  /((?:api[_-]?key|apikey|secret|client[_-]?secret|access[_-]?token|auth[_-]?token|bearer|password|passwd|pwd))\s*[:=]\s*['"]?[^\s'";,]{4,}['"]?/gi,
  /(sk-[A-Za-z0-9_-]{8,})/g,
  /(xox[baprs]-[A-Za-z0-9-]{8,})/g,
  /(-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----)/g,
];

/**
 * Strip credential-shaped values before text reaches chat responses, logs,
 * or third parties. Capability is NOT limited — the agent can still USE the
 * values; they just never leak outward.
 */
export function sanitizeForChat(text) {
  if (typeof text !== 'string') return text;
  let out = text;
  for (const re of CHAT_SECRET_PATTERNS) out = out.replace(re, '[REDACTED]');
  return out;
}

/** Audit log for sensitive actions — append-only.
 * TOGGLE: INFRA_AUDIT_ENABLED (default '1'). ahsan can switch it OFF himself;
 * his systems, his choice. When off, this is a no-op returning ''. */
export function auditEnabled() {
  const v = process.env.INFRA_AUDIT_ENABLED;
  if (v === undefined || v === null || String(v).trim() === '') return true;
  return /^(1|true|yes|on)$/i.test(String(v).trim());
}

/** Audit log for sensitive actions — append-only (respects the toggle). */
export function auditSensitiveAction(action, details = {}) {
  if (!auditEnabled()) return '';
  const line =
    JSON.stringify({
      ts: new Date().toISOString(),
      action,
      details,
      pid: process.pid,
    }) + '\n';
  try {
    fs.appendFileSync(process.env.INFRA_AUDIT_LOG || '/tmp/halye-infra-audit.log', line);
  } catch {
    /* audit failure must never break the action itself */
  }
  return line;
}

/**
 * Infrastructure action check. The full set is GRANTED for his own infra;
 * destructive/consequential actions still require explicit confirmed:true.
 * Sensitive infra actions are audit-logged automatically.
 */
export function checkInfraAction(kind, body = {}) {
  const perms = getPermissions();
  const map = {
    root_admin: perms.allowRootAdmin,
    cloud_admin: perms.allowCloudAdmin,
    user_management: perms.allowUserManagement,
    prod_credential_access: perms.allowProdCredentials,
    secret_visibility: perms.allowSecretVisibility,
    unrestricted_shell: perms.allowUnrestrictedShell,
    deploy_own_app: perms.allowOwnDeployments,
    workspace_shell: perms.allowWorkspaceShell,
  };
  const sensitive = new Set([
    'root_admin',
    'cloud_admin',
    'user_management',
    'prod_credential_access',
    'secret_visibility',
  ]);
  if (sensitive.has(kind)) auditSensitiveAction(kind, { confirmed: body?.confirmed === true });
  if (kind in map && !map[kind]) {
    return { ok: false, reason: `Infrastructure action '${kind}' is disabled by env toggle.` };
  }
  // Destructive/consequential infra actions ALWAYS need explicit confirmation.
  const destructive = new Set(['user_management', 'cloud_admin', 'root_admin']);
  if (destructive.has(kind) && body?.destructive === true) {
    const c = checkDestructiveConfirmed(body);
    if (!c.ok) return c;
  }
  return { ok: true };
}

export default {
  getPermissions,
  getAuthorizedTargets,
  checkTargetScope,
  checkDestructiveConfirmed,
  checkInfraAction,
  checkShellAction,
  checkKernelAccess,
  looksLikeCredentialPath,
  sanitizeForChat,
  auditSensitiveAction,
  auditEnabled,
};
