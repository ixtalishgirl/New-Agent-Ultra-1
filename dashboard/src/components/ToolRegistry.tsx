import { useEffect, useMemo, useState } from "react";
import { api, ApiError, DESTRUCTIVE_TOOLS, toolCategory, type ToolInfo } from "../api/client";
import { addApproval, addEntry } from "../store/history";
import type { ConnState } from "./Connection";

export default function ToolRegistry({ conn, onApproval }: { conn: ConnState; onApproval: () => void }) {
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<ToolInfo | null>(null);
  const [argsText, setArgsText] = useState("{}");
  const [result, setResult] = useState("");
  const [running, setRunning] = useState(false);

  useEffect(() => {
    if (conn !== "online") {
      setLoading(false);
      return;
    }
    setLoading(true);
    api
      .listTools()
      .then((t) => {
        setTools(t);
        setError("");
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : "Failed to load tools"))
      .finally(() => setLoading(false));
  }, [conn]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const list = q
      ? tools.filter(
          (t) =>
            t.name.toLowerCase().includes(q) ||
            (t.description || "").toLowerCase().includes(q) ||
            toolCategory(t.name).toLowerCase().includes(q)
        )
      : tools;
    const groups = new Map<string, ToolInfo[]>();
    for (const t of list) {
      const c = toolCategory(t.name);
      if (!groups.has(c)) groups.set(c, []);
      groups.get(c)!.push(t);
    }
    return [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [tools, query]);

  const run = async () => {
    if (!selected) return;
    let args: Record<string, unknown> = {};
    try {
      args = JSON.parse(argsText || "{}");
    } catch {
      setResult("Invalid JSON in arguments.");
      return;
    }
    if (DESTRUCTIVE_TOOLS.has(selected.name)) {
      addApproval(selected.name, args, `Manual execution requested from Tool Registry (${selected.name})`);
      setResult(`Held for approval — see the Approvals tab. (${selected.name} is state-changing)`);
      onApproval();
      return;
    }
    setRunning(true);
    setResult("");
    try {
      const r = await api.executeTool(selected.name, args);
      const text = typeof r === "string" ? r : JSON.stringify(r, null, 2);
      setResult(text.slice(0, 8000));
      addEntry({ kind: "tool", status: "ok", title: selected.name, detail: JSON.stringify(args).slice(0, 120) });
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : "Execution failed";
      setResult(`Error: ${msg}`);
      addEntry({ kind: "tool", status: "error", title: selected.name, detail: msg });
    } finally {
      setRunning(false);
    }
  };

  if (conn !== "online") return <div className="empty">Backend offline — tool registry unavailable.</div>;
  if (loading) return <div className="empty">Loading tools…</div>;
  if (error) return <div className="empty" style={{ color: "var(--red)" }}>{error}</div>;

  return (
    <div>
      <div className="toolbar">
        <div className="search" style={{ marginBottom: 0, flex: 1, maxWidth: 420 }}>
          <input
            type="text"
            placeholder={`Search ${tools.length} tools…`}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <span style={{ color: "var(--muted)", fontSize: 12 }}>{tools.length} tools registered</span>
      </div>

      {filtered.map(([cat, list]) => (
        <div className="card" key={cat}>
          <h3>{cat} <span style={{ color: "var(--faint)", fontWeight: 400 }}>({list.length})</span></h3>
          <table className="tbl">
            <tbody>
              {list.map((t) => (
                <tr key={t.name} onClick={() => { setSelected(t); setArgsText("{}"); setResult(""); }} style={{ cursor: "pointer" }}>
                  <td className="mono" style={{ whiteSpace: "nowrap" }}>
                    {t.name}{" "}
                    {DESTRUCTIVE_TOOLS.has(t.name) && <span className="pill warn">approval</span>}
                  </td>
                  <td style={{ color: "var(--muted)" }}>{t.description || "—"}</td>
                  <td><span className="pill info">{t.status || "active"}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}

      {selected && (
        <div className="card" style={{ borderColor: "var(--accent)" }}>
          <h3>Execute: <span className="mono">{selected.name}</span></h3>
          <p style={{ color: "var(--muted)", fontSize: 13 }}>{selected.description}</p>
          <label>Arguments (JSON)</label>
          <textarea value={argsText} onChange={(e) => setArgsText(e.target.value)} rows={4} className="mono" />
          <div style={{ marginTop: 12, display: "flex", gap: 10 }}>
            <button className="btn" onClick={run} disabled={running}>
              {running ? "Running…" : DESTRUCTIVE_TOOLS.has(selected.name) ? "Request approval" : "Execute"}
            </button>
            <button className="btn ghost" onClick={() => setSelected(null)}>Close</button>
          </div>
          {result && <pre className="code" style={{ marginTop: 12 }}>{result}</pre>}
        </div>
      )}
    </div>
  );
}
