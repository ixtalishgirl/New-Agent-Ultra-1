import { useEffect, useMemo, useState } from "react";
import { clearLog, getLog, subscribe } from "../store/history";

export default function History() {
  const [log, setLog] = useState(() => getLog());
  const [filter, setFilter] = useState("all");

  useEffect(() => subscribe(() => setLog(getLog())), []);

  const rows = useMemo(
    () => (filter === "all" ? log : log.filter((e) => e.kind === filter)),
    [log, filter]
  );

  return (
    <div className="card">
      <div className="toolbar">
        <h3 style={{ margin: 0 }}>Execution history</h3>
        <div className="spacer" style={{ flex: 1 }} />
        <select value={filter} onChange={(e) => setFilter(e.target.value)} style={{ width: 160 }}>
          <option value="all">All</option>
          <option value="chat">Agent runs</option>
          <option value="tool">Tool executions</option>
          <option value="approval">Approvals</option>
        </select>
        <button className="btn ghost small" onClick={() => { if (confirm("Clear local history?")) clearLog(); }}>
          Clear
        </button>
      </div>
      {rows.length === 0 ? (
        <div className="empty">No history yet.</div>
      ) : (
        <table className="tbl">
          <thead><tr><th>Type</th><th>Status</th><th>Summary</th><th>Detail</th><th>Time</th></tr></thead>
          <tbody>
            {rows.slice(0, 100).map((e) => (
              <tr key={e.id}>
                <td><span className="pill info">{e.kind}</span></td>
                <td>
                  <span className={`pill ${e.status === "ok" ? "ok" : e.status === "error" ? "err" : "warn"}`}>
                    {e.status}
                  </span>
                </td>
                <td>{e.title}</td>
                <td style={{ color: "var(--muted)", maxWidth: 320, overflow: "hidden", textOverflow: "ellipsis" }}>
                  {e.detail}
                </td>
                <td style={{ color: "var(--muted)", whiteSpace: "nowrap" }}>
                  {new Date(e.time).toLocaleString()}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
