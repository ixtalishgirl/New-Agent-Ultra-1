import React from 'react';
import { Globe, ExternalLink, AlertTriangle, CheckCircle2, Clock, Camera, ListChecks, Brain, ShieldAlert } from 'lucide-react';

/**
 * BrowserObservation — the shape produced by node-integration/browserActions.js
 * and returned by /api/agent/generate as `browserObservation`.
 */
export interface BrowserObservation {
  kind: 'browser_observation';
  action: string;
  url?: string | null;
  title?: string | null;
  screenshot?: string | null; // data URL
  accessedAt: string; // ISO
  facts: Array<{ label: string; value: string }>;
  interpretation?: string | null; // agent's own reading — explicitly NOT page content
  success: boolean;
  error?: string | null;
  needsConfirmation?: boolean;
  fallbackUsed?: boolean;
  fallback?: string | null; // 'legacy_fetch'
  sources: Array<{ url: string; title: string; accessedAt: string }>;
}

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString('en-PK', { timeZone: 'Asia/Karachi', hour12: true });
  } catch {
    return iso;
  }
}

const ACTION_LABELS: Record<string, string> = {
  browser_search: 'Web Search',
  browser_open: 'Page Opened',
  browser_extract: 'Facts Extracted',
  browser_screenshot: 'Screenshot Captured',
  browser_compare: 'Pages Compared',
};

export function BrowserPanel({ observation }: { observation: BrowserObservation | null | undefined }) {
  if (!observation) return null;
  const obs = observation;
  const actionLabel = ACTION_LABELS[obs.action] || obs.action;

  return (
    <div className="mt-3 rounded-xl bg-zinc-950 border border-cyan-500/30 p-3 space-y-2.5 font-mono text-[11px]">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-zinc-900 pb-1.5">
        <div className="flex items-center gap-2">
          <Globe className="w-3.5 h-3.5 text-cyan-400" />
          <span className="font-bold text-white text-xs">Live Browser Observation — {actionLabel}</span>
        </div>
        <span
          className={`px-2 py-0.5 rounded text-[10px] font-bold flex items-center gap-1 ${
            obs.success ? 'bg-emerald-500/15 text-emerald-300' : 'bg-rose-500/15 text-rose-300'
          }`}
        >
          {obs.success ? <CheckCircle2 className="w-3 h-3" /> : <AlertTriangle className="w-3 h-3" />}
          {obs.success ? 'Observed' : 'Failed'}
        </span>
      </div>

      {/* Fallback banner */}
      {obs.fallbackUsed && (
        <div className="rounded-lg bg-amber-500/10 border border-amber-500/40 px-2.5 py-1.5 text-amber-200 text-[10px] flex items-center gap-1.5">
          <ShieldAlert className="w-3.5 h-3.5 shrink-0" />
          <span>
            Live browser service offline — showing legacy fetch-only view (no JS rendering, no screenshots).
            Facts below come from raw HTML only.
          </span>
        </div>
      )}

      {/* URL + title + timestamp */}
      <div className="space-y-1">
        {obs.url && (
          <a
            href={obs.url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-cyan-300 hover:text-cyan-200 underline underline-offset-2 break-all flex items-center gap-1"
          >
            <ExternalLink className="w-3 h-3 shrink-0" />
            {obs.title || obs.url}
          </a>
        )}
        <div className="flex items-center gap-1.5 text-zinc-500 text-[10px]">
          <Clock className="w-3 h-3" />
          <span>Accessed: {formatTime(obs.accessedAt)} (PKT)</span>
        </div>
      </div>

      {/* Screenshot */}
      {obs.screenshot && (
        <div className="rounded-lg overflow-hidden border border-zinc-800">
          <div className="px-2 py-1 bg-zinc-900 text-zinc-400 text-[10px] flex items-center gap-1.5">
            <Camera className="w-3 h-3" /> Rendered page screenshot
          </div>
          <img src={obs.screenshot} alt={`Screenshot of ${obs.title || obs.url}`} className="w-full" />
        </div>
      )}

      {/* Honest failure */}
      {!obs.success && (
        <div className="rounded-lg bg-rose-950/25 border border-rose-900/50 p-2.5 text-rose-200 text-[10px] leading-relaxed">
          <div className="font-bold text-rose-300 mb-1 flex items-center gap-1.5">
            <AlertTriangle className="w-3.5 h-3.5" /> What happened (honest report)
          </div>
          <div>{obs.error || 'The browser observation failed for an unknown reason.'}</div>
          {obs.needsConfirmation && (
            <div className="mt-1.5 text-amber-200">
              This action needs your explicit confirmation before it can run.
            </div>
          )}
          <div className="mt-1.5 text-zinc-400">
            Nothing was bypassed or clicked through — anti-bot challenges are reported, never solved.
          </div>
        </div>
      )}

      {/* FACTS — verbatim page content */}
      {obs.facts.length > 0 && (
        <div className="rounded-lg bg-black/60 border border-zinc-900 p-2.5">
          <div className="font-bold text-emerald-300 text-[10px] mb-1.5 flex items-center gap-1.5">
            <ListChecks className="w-3.5 h-3.5" /> Facts from the page ({obs.facts.length})
            <span className="font-normal text-zinc-500">— verbatim, not my words</span>
          </div>
          <ul className="space-y-1 max-h-56 overflow-y-auto">
            {obs.facts.map((f, i) => (
              <li key={i} className="text-[10px] text-zinc-300 leading-relaxed break-words">
                <span className="text-cyan-400 font-bold">{f.label}:</span> {f.value}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* INTERPRETATION — agent's own reading, clearly separated */}
      {obs.interpretation && (
        <div className="rounded-lg bg-violet-950/25 border border-violet-900/50 p-2.5">
          <div className="font-bold text-violet-300 text-[10px] mb-1 flex items-center gap-1.5">
            <Brain className="w-3.5 h-3.5" /> Halye's interpretation
            <span className="font-normal text-zinc-500">— my reading, not page content</span>
          </div>
          <div className="text-[10px] text-zinc-300 leading-relaxed whitespace-pre-wrap">{obs.interpretation}</div>
        </div>
      )}

      {/* Sources */}
      {obs.sources.length > 0 && (
        <div className="pt-1 border-t border-zinc-900">
          <div className="text-[10px] text-zinc-500 font-bold mb-1">Sources</div>
          <ul className="space-y-0.5">
            {obs.sources.map((s, i) => (
              <li key={i} className="text-[10px]">
                <a
                  href={s.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-cyan-400/90 hover:text-cyan-300 underline underline-offset-2 break-all"
                >
                  {s.title || s.url}
                </a>
                <span className="text-zinc-600"> · {formatTime(s.accessedAt)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

export default BrowserPanel;
