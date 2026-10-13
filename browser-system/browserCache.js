/**
 * browserCache.js — free-tier optimization: response caching + request dedup.
 *
 * - In-memory LRU cache with TTL for idempotent browser reads
 *   (search, extract, page text). Default TTL 10 min, max 200 entries.
 * - In-flight dedup: concurrent identical requests share one promise —
 *   the agent's parallel "compare 10 pages" doesn't fetch the same URL twice.
 * - Screenshots are NEVER cached (live visual state must be fresh).
 * - Cache is per-process; Render sleep naturally clears it (honest staleness).
 */

const DEFAULT_TTL_MS = Number(process.env.BROWSER_CACHE_TTL_MS || 10 * 60 * 1000);
const MAX_ENTRIES = Number(process.env.BROWSER_CACHE_MAX || 200);

const cache = new Map(); // key -> { value, expiresAt, hits }
const inflight = new Map(); // key -> Promise

function keyOf(namespace, params) {
  const sorted = Object.keys(params || {})
    .sort()
    .map((k) => `${k}=${JSON.stringify(params[k])}`)
    .join('&');
  return `${namespace}?${sorted}`;
}

function prune() {
  const now = Date.now();
  for (const [k, v] of cache) {
    if (v.expiresAt <= now) cache.delete(k);
  }
  while (cache.size > MAX_ENTRIES) {
    const oldest = cache.keys().next().value;
    cache.delete(oldest);
  }
}

export function cacheGet(namespace, params) {
  const key = keyOf(namespace, params);
  const hit = cache.get(key);
  if (hit && hit.expiresAt > Date.now()) {
    hit.hits += 1;
    return { hit: true, value: hit.value, key };
  }
  cache.delete(key);
  return { hit: false, key };
}

export function cacheSet(key, value, ttlMs = DEFAULT_TTL_MS) {
  prune();
  cache.set(key, { value, expiresAt: Date.now() + ttlMs, hits: 0 });
}

/**
 * Dedup wrapper: fn() runs once per key; concurrent callers share the promise.
 * Successful results for cacheable namespaces are stored.
 */
export async function deduped(namespace, params, fn, { cacheable = true, ttlMs } = {}) {
  if (cacheable) {
    const cached = cacheGet(namespace, params);
    if (cached.hit) return cached.value;
  }
  const key = keyOf(namespace, params);
  if (inflight.has(key)) return inflight.get(key);
  const p = (async () => {
    try {
      const value = await fn();
      if (cacheable && value && value.success !== false) cacheSet(key, value, ttlMs);
      return value;
    } finally {
      inflight.delete(key);
    }
  })();
  inflight.set(key, p);
  return p;
}

export function cacheStats() {
  prune();
  let hits = 0;
  for (const v of cache.values()) hits += v.hits;
  return { entries: cache.size, totalHits: hits, inflight: inflight.size, ttlMs: DEFAULT_TTL_MS };
}

export function cacheClear() {
  cache.clear();
  inflight.clear();
}

/** Namespaces that are safe to cache. Screenshots excluded deliberately. */
export const CACHEABLE = new Set(['browser_search', 'browser_extract', 'browser_open']);

export default { cacheGet, cacheSet, deduped, cacheStats, cacheClear, CACHEABLE };
