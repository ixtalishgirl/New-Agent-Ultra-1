import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import { getApprovals, setApprovalState, subscribe, updateEntry, getLog, type Approval } from "../store/history";
import { addEntry } from "../store/history";

export default function Approvals() {
  const [list, setList] = useState<Approval[]>(() => getApprovals());

  useEffect(() => subscribe(() => setList(getApprovals())), []);

  const decide = async (a: Approval, ok: boolean) => {
    if (!ok) {
      setApprovalState(a.id, "rejected");
      addEntry({ kind: "approval", status: "error", title: `Rejected: ${a.tool}`, detail: a.reason });
      return;
    }
    setApprovalState(a.id, "approved");
    try {
      const r = await api.executeTool(a.tool, a.args);
      const text = typeof r === "string" ? r : JSON.stringify(r, null, 2);
      addEntry({ kind: "tool", status: "ok", title: `${a.tool} (approved)`, detail: text.slice(0, 200) });
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : "Execution failed";
      addEntry({ kind: "tool", status: "error", title: `${a.tool} (approved)`, detail: msg });
    }
    // mark the original request entry resolved
    const req = getLog().find((e) => e.kind === "approval" && e.status === "pending" && e.title.includes(a.tool));
    if (req) updateEntry(req.id, { status: "ok", detail: `${a.reason} — approved` });
  };

  const pending = list.filter((a) => a.state === "pending");
  const done = list.filter((a) => a.state !== "pending");

  return (
    <div>
      <div className="card">
        <h3>Pending approvals ({pending.length})</h3>
        {pending.length === 0 ? (
          <div className="empty">Nothing waiting. State-changing tools (shell, python, file writes, …) will appear here.</div>
        ) : (
          pending.map((a) => (
            <div key={a.id} className="card" style={{ background: "var(--bg-2)" }}>
              <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 8 }}>
                <span className="mono" style={{ fontSize: 14, fontWeight: 700 }}>{a.tool}</span>
                <span className="pill warn">pending</span>
                <span style={{ marginLeft: "auto", color: "var(--faint)", fontSize: 12 }}>
                  {new Date(a.time).toLocaleString()}
                </span>
              </div>
              <p style={{ color: "var(--muted)", fontSize: 13 }}>{a.reason}</p>
              <pre className="code">{JSON.stringify(a.args, null, 2)}</pre>
              <div style={{ display: "flex", gap: 10, marginTop: 10 }}>
                <button className="btn" onClick={() => decide(a, true)}>Approve & execute</button>
                <button className="btn danger" onClick={() => decide(a, false)}>Reject</button>
              </div>
            </div>
          ))
        )}
      </div>

      {done.length > 0 && (
        <div className="card">
          <h3>Decided</h3>
          <table className="tbl">
            <thead><tr><th>Tool</th><th>Decision</th><th>When</th></tr></thead>
            <tbody>
              {done.slice(0, 20).map((a) => (
                <tr key={a.id}>
                  <td className="mono">{a.tool}</td>
                  <td>
                    <span className={`pill ${a.state === "approved" ? "ok" : "err"}`}>{a.state}</span>
                  </td>
                  <td style={{ color: "var(--muted)" }}>{new Date(a.time).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
