import { useEffect, useState } from "react";
import { api } from "../api/client";
import { getLog, subscribe, type LogEntry } from "../store/history";
import type { ConnState } from "./Connection";

function ago(t: number) {
  const s = Math.floor((Date.now() - t) / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

function pill(s: LogEntry["status"]) {
  return <span className={`pill ${s === "ok" ? "ok" : s === "error" ? "err" : "warn"}`}>{s}</span>;
}

export default function TaskMonitor({ conn }: { conn: ConnState }) {
  const [log, setLog] = useState<LogEntry[]>(() => getLog());
  const [tunnel, setTunnel] = useState<Record<string, unknown> | null>(null);
  const [tunnelErr, setTunnelErr] = useState("");

  useEffect(() => subscribe(() => setLog(getLog())), []);

  useEffect(() => {
    if (conn !== "online") return;
    api
      .tunnelStatus()
      .then(setTunnel)
      .catch((e) => setTunnelErr(e.message));
  }, [conn]);

  const runs = log.filter((e) => e.kind === "chat");
  const tools = log.filter((e) => e.kind === "tool");
  const okRuns = runs.filter((e) => e.status === "ok").length;
  const errRuns = runs.filter((e) => e.status === "error").length;

  return (
    <div>
      <div className="grid2">
        <div className="card">
          <h3>Session activity</h3>
          <div style={{ display: "flex", gap: 24 }}>
            <div className="stat"><span className="v">{runs.length}</span><span className="k">Agent runs</span></div>
            <div className="stat"><span className="v" style={{ color: "var(--green)" }}>{okRuns}</span><span className="k">Succeeded</span></div>
            <div className="stat"><span className="v" style={{ color: "var(--red)" }}>{errRuns}</span><span className="k">Failed</span></div>
            <div className="stat"><span className="v">{tools.length}</span><span className="k">Tool executions</span></div>
          </div>
        </div>
        <div className="card">
          <h3>Model tunnel</h3>
          {conn !== "online" ? (
            <p style={{ color: "var(--muted)" }}>Backend offline — tunnel status unavailable.</p>
          ) : tunnelErr ? (
            <p style={{ color: "var(--red)" }}>{tunnelErr}</p>
          ) : tunnel ? (
            <dl className="kv">
              {Object.entries(tunnel).map(([k, v]) => (
                <div key={k} style={{ display: "contents" }}>
                  <dt>{k}</dt>
                  <dd className="mono">{String(v)}</dd>
                </div>
              ))}
            </dl>
          ) : (
            <p style={{ color: "var(--muted)" }}>Loading…</p>
          )}
        </div>
      </div>

      <div className="card">
        <h3>Recent runs</h3>
        {runs.length === 0 ? (
          <div className="empty">No agent runs yet — use the Chat tab to start one.</div>
        ) : (
          <table className="tbl">
            <thead>
              <tr><th>Status</th><th>Task</th><th>Detail</th><th>When</th></tr>
            </thead>
            <tbody>
              {runs.slice(0, 25).map((e) => (
                <tr key={e.id}>
                  <td>{pill(e.status)}</td>
                  <td>{e.title}</td>
                  <td style={{ color: "var(--muted)" }}>{e.detail}</td>
                  <td style={{ color: "var(--muted)", whiteSpace: "nowrap" }}>{ago(e.time)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
