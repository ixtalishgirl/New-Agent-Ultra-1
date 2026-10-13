import React, { useState, useEffect } from 'react';
import {
  Crown,
  Activity,
  Cpu,
  HardDrive,
  Wifi,
  WifiOff,
  Zap,
  RefreshCw,
  Loader2,
  CheckCircle2,
  AlertCircle,
  Terminal,
  Clock,
} from 'lucide-react';

interface SystemStatus {
  success: boolean;
  timestamp: number;
  godMode: boolean;
  model: {
    configured: boolean;
    url: string | null;
    alive: boolean | null;
    latencyMs: number | null;
  };
  server: {
    uptimeSec: number;
    nodeVersion: string;
    platform: string;
    memoryMb: number;
    heapMb: number;
  };
  tools: Record<string, boolean>;
}

export const GodModePanel: React.FC = () => {
  const [godMode, setGodMode] = useState(false);
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [toggling, setToggling] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchStatus = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch('/api/system/status');
      const data = await res.json();
      if (data.success) {
        setStatus(data);
        setGodMode(!!data.godMode);
      } else {
        setError('Status load failed');
      }
    } catch (err: any) {
      setError(err.message || 'Failed to load system status');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchStatus();
  }, []);

  const toggleGodMode = async () => {
    setToggling(true);
    try {
      const res = await fetch('/api/godmode', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: !godMode }),
      });
      const data = await res.json();
      if (data.success) {
        setGodMode(data.enabled);
        fetchStatus();
      }
    } catch (err: any) {
      setError(err.message || 'Toggle failed');
    } finally {
      setToggling(false);
    }
  };

  const fmtUptime = (sec: number) => {
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    return h > 0 ? `${h}h ${m}m` : `${m}m`;
  };

  return (
    <div className="flex-1 overflow-y-auto p-4 space-y-4 bg-black">
      {/* God Mode Toggle Card */}
      <div className={`rounded-2xl border p-4 ${godMode ? 'border-amber-500/50 bg-amber-950/20' : 'border-zinc-800 bg-zinc-950'}`}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className={`w-10 h-10 rounded-xl flex items-center justify-center ${godMode ? 'bg-amber-500/20 border border-amber-500/40' : 'bg-zinc-900 border border-zinc-800'}`}>
              <Crown className={`w-5 h-5 ${godMode ? 'text-amber-400' : 'text-zinc-500'}`} />
            </div>
            <div>
              <div className="text-sm font-extrabold text-white flex items-center gap-2">
                GOD MODE
                {godMode && <span className="px-2 py-0.5 rounded-full bg-amber-500/20 text-amber-400 border border-amber-500/30 text-[9px] font-mono">ACTIVE</span>}
              </div>
              <p className="text-[11px] text-zinc-400 mt-0.5">
                Power-user UX: tools bina confirmation, lambi terminal timeouts, batch file ops
              </p>
            </div>
          </div>
          <button
            onClick={toggleGodMode}
            disabled={toggling}
            className={`px-4 py-2 rounded-xl font-extrabold text-xs transition cursor-pointer active:scale-95 flex items-center gap-2 ${
              godMode
                ? 'bg-amber-500 hover:bg-amber-400 text-black'
                : 'bg-zinc-900 hover:bg-zinc-800 text-zinc-300 border border-zinc-800'
            }`}
          >
            {toggling ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Zap className="w-3.5 h-3.5" />}
            {godMode ? 'ON' : 'OFF'}
          </button>
        </div>
        {godMode && (
          <div className="mt-3 pt-3 border-t border-amber-500/20 grid grid-cols-2 gap-2 text-[11px]">
            <div className="flex items-center gap-1.5 text-amber-300"><CheckCircle2 className="w-3.5 h-3.5" /> Tools: no confirm prompts</div>
            <div className="flex items-center gap-1.5 text-amber-300"><CheckCircle2 className="w-3.5 h-3.5" /> Terminal: 55s max (serverless limit)</div>
            <div className="flex items-center gap-1.5 text-amber-300"><CheckCircle2 className="w-3.5 h-3.5" /> Batch file ops enabled</div>
            <div className="flex items-center gap-1.5 text-amber-300"><CheckCircle2 className="w-3.5 h-3.5" /> Full system dashboard</div>
          </div>
        )}
      </div>

      {/* System Status Dashboard */}
      <div className="rounded-2xl border border-zinc-800 bg-zinc-950 p-4">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <Activity className="w-4 h-4 text-cyan-400" />
            <span className="text-sm font-extrabold text-white">System Status</span>
          </div>
          <button
            onClick={fetchStatus}
            disabled={loading}
            className="p-1.5 rounded-lg bg-zinc-900 hover:bg-zinc-800 text-zinc-400 border border-zinc-800 transition cursor-pointer"
            title="Refresh"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
          </button>
        </div>

        {error && (
          <div className="mb-3 p-2.5 rounded-xl bg-rose-950/40 border border-rose-800/50 text-rose-300 text-xs font-mono flex items-center gap-2">
            <AlertCircle className="w-4 h-4 shrink-0" /> {error}
          </div>
        )}

        {loading && !status ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="w-6 h-6 text-cyan-400 animate-spin" />
          </div>
        ) : status ? (
          <div className="space-y-3">
            {/* Model */}
            <div className="rounded-xl bg-black border border-zinc-900 p-3">
              <div className="flex items-center justify-between mb-2">
                <span className="text-xs font-bold text-zinc-300 flex items-center gap-1.5">
                  <Cpu className="w-3.5 h-3.5 text-cyan-400" /> Haley Model API
                </span>
                {status.model.alive === true ? (
                  <span className="flex items-center gap-1 text-emerald-400 text-[10px] font-mono font-bold"><Wifi className="w-3 h-3" /> ALIVE</span>
                ) : status.model.alive === false ? (
                  <span className="flex items-center gap-1 text-rose-400 text-[10px] font-mono font-bold"><WifiOff className="w-3 h-3" /> DOWN</span>
                ) : (
                  <span className="text-amber-400 text-[10px] font-mono font-bold">NOT CONFIGURED</span>
                )}
              </div>
              {status.model.configured ? (
                <div className="text-[10px] font-mono text-zinc-500 space-y-1">
                  <div className="break-all">URL: <span className="text-zinc-300">{status.model.url}</span></div>
                  {status.model.latencyMs !== null && (
                    <div>Latency: <span className="text-emerald-400">{status.model.latencyMs}ms</span></div>
                  )}
                </div>
              ) : (
                <div className="text-[11px] text-amber-300 font-mono">⚠️ HALEY_API_URL set karo — Vercel env vars me add karo.</div>
              )}
            </div>

            {/* Server */}
            <div className="rounded-xl bg-black border border-zinc-900 p-3">
              <div className="text-xs font-bold text-zinc-300 mb-2 flex items-center gap-1.5">
                <HardDrive className="w-3.5 h-3.5 text-emerald-400" /> Server
              </div>
              <div className="grid grid-cols-2 gap-2 text-[10px] font-mono">
                <div className="text-zinc-500">Uptime: <span className="text-zinc-300">{fmtUptime(status.server.uptimeSec)}</span></div>
                <div className="text-zinc-500">Node: <span className="text-zinc-300">{status.server.nodeVersion}</span></div>
                <div className="text-zinc-500">Memory: <span className="text-zinc-300">{status.server.memoryMb} MB</span></div>
                <div className="text-zinc-500">Heap: <span className="text-zinc-300">{status.server.heapMb} MB</span></div>
              </div>
            </div>

            {/* Tools */}
            <div className="rounded-xl bg-black border border-zinc-900 p-3">
              <div className="text-xs font-bold text-zinc-300 mb-2 flex items-center gap-1.5">
                <Terminal className="w-3.5 h-3.5 text-purple-400" /> Tools
              </div>
              <div className="flex flex-wrap gap-1.5">
                {Object.entries(status.tools).map(([name, on]) => (
                  <span
                    key={name}
                    className={`px-2 py-1 rounded-lg text-[10px] font-mono font-bold border ${
                      on ? 'bg-emerald-950/40 text-emerald-300 border-emerald-800/50' : 'bg-zinc-900 text-zinc-500 border-zinc-800'
                    }`}
                  >
                    {on ? '✓' : '✗'} {name}
                  </span>
                ))}
              </div>
            </div>

            <div className="text-[10px] text-zinc-600 font-mono flex items-center gap-1.5">
              <Clock className="w-3 h-3" /> Updated: {new Date(status.timestamp).toLocaleTimeString()}
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
};
