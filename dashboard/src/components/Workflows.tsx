import { useEffect, useState } from "react";
import { api, ApiError, DESTRUCTIVE_TOOLS } from "../api/client";
import { addApproval, addEntry } from "../store/history";
import type { ConnState } from "./Connection";

interface Step {
  id: string;
  tool: string;
  argsText: string;
}

interface Workflow {
  id: string;
  name: string;
  steps: Step[];
  created: number;
}

interface StepResult {
  stepId: string;
  status: "pending" | "running" | "ok" | "error" | "held";
  output: string;
}

const LS_KEY = "nau1.workflows";

function loadSaved(): Workflow[] {
  try {
    const raw = localStorage.getItem(LS_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
}

const uid = () => `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;

const TEMPLATES: Array<{ name: string; steps: Array<{ tool: string; args: Record<string, unknown> }> }> = [
  {
    name: "Repo health check",
    steps: [
      { tool: "git_status", args: {} },
      { tool: "sysinfo", args: {} },
      { tool: "disk_usage", args: { path: "." } },
    ],
  },
  {
    name: "Web recon",
    steps: [
      { tool: "web_search", args: { query: "example" } },
      { tool: "dns_lookup", args: { host: "example.com" } },
      { tool: "http_get", args: { url: "https://example.com" } },
    ],
  },
];

export default function Workflows({ conn }: { conn: ConnState }) {
  const [workflows, setWorkflows] = useState<Workflow[]>(() => loadSaved());
  const [active, setActive] = useState<Workflow | null>(null);
  const [results, setResults] = useState<StepResult[]>([]);
  const [running, setRunning] = useState(false);
  const [, setStopFlag] = useState(false); // stop signal for workflow runs
  const [newName, setNewName] = useState("");
  const [editSteps, setEditSteps] = useState<Step[]>([]);
  const [editName, setEditName] = useState("");

  useEffect(() => {
    localStorage.setItem(LS_KEY, JSON.stringify(workflows));
  }, [workflows]);

  const startNew = () => {
    setActive(null);
    setEditName(newName.trim() || `Workflow ${workflows.length + 1}`);
    setEditSteps([{ id: uid(), tool: "shell", argsText: '{"command": "echo hello"}' }]);
    setResults([]);
  };

  const loadTemplate = (t: (typeof TEMPLATES)[number]) => {
    setActive(null);
    setEditName(t.name);
    setEditSteps(t.steps.map((s) => ({ id: uid(), tool: s.tool, argsText: JSON.stringify(s.args, null, 1) })));
    setResults([]);
  };

  const editExisting = (w: Workflow) => {
    setActive(null);
    setEditName(w.name);
    setEditSteps(w.steps.map((s) => ({ ...s, id: uid() })));
    setResults([]);
  };

  const saveWorkflow = () => {
    const w: Workflow = { id: uid(), name: editName || "Untitled", steps: editSteps, created: Date.now() };
    setWorkflows((ws) => [w, ...ws]);
    setActive(w);
    setEditSteps([]);
    setEditName("");
    addEntry({ kind: "tool", status: "ok", title: "workflow_saved", detail: w.name });
  };

  const deleteWorkflow = (id: string) => {
    setWorkflows((ws) => ws.filter((w) => w.id !== id));
    if (active?.id === id) setActive(null);
  };

  const runWorkflow = async (w: Workflow) => {
    setRunning(true);
    setStopFlag(false);
    const res: StepResult[] = w.steps.map((s) => ({ stepId: s.id, status: "pending", output: "" }));
    setResults(res);
    let stopped = false;
    for (let i = 0; i < w.steps.length; i++) {
      if (stopped) break;
      const step = w.steps[i];
      setResults((r) => r.map((x) => (x.stepId === step.id ? { ...x, status: "running" } : x)));
      let args: Record<string, unknown> = {};
      try {
        args = JSON.parse(step.argsText || "{}");
      } catch {
        setResults((r) => r.map((x) => (x.stepId === step.id ? { ...x, status: "error", output: "Invalid JSON args" } : x)));
        continue;
      }
      if (DESTRUCTIVE_TOOLS.has(step.tool)) {
        addApproval(step.tool, args, `Workflow "${w.name}" step ${i + 1}`);
        setResults((r) => r.map((x) => (x.stepId === step.id ? { ...x, status: "held", output: "Held for approval — approve in Approvals tab, then re-run." } : x)));
        continue;
      }
      try {
        const out = await api.executeTool(step.tool, args);
        const text = typeof out === "string" ? out : JSON.stringify(out, null, 2);
        setResults((r) => r.map((x) => (x.stepId === step.id ? { ...x, status: "ok", output: text.slice(0, 3000) } : x)));
      } catch (e) {
        setResults((r) =>
          r.map((x) => (x.stepId === step.id ? { ...x, status: "error", output: e instanceof ApiError ? e.message : "failed" } : x))
        );
      }
      // allow stop between steps
      await new Promise((r) => setTimeout(r, 50));
      setStopFlag((f) => {
        stopped = f;
        return f;
      });
    }
    setRunning(false);
    addEntry({ kind: "tool", status: stopped ? "error" : "ok", title: `workflow_run: ${w.name}`, detail: `${w.steps.length} steps` });
  };

  const updateStep = (id: string, patch: Partial<Step>) =>
    setEditSteps((ss) => ss.map((s) => (s.id === id ? { ...s, ...patch } : s)));

  if (conn !== "online") return <div className="empty">Backend offline — automation unavailable.</div>;

  return (
    <div>
      <div className="toolbar">
        <input
          type="text"
          placeholder="New workflow name…"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          style={{ maxWidth: 280 }}
        />
        <button className="btn small" onClick={startNew}>
          + New workflow
        </button>
        {TEMPLATES.map((t) => (
          <button key={t.name} className="btn ghost small" onClick={() => loadTemplate(t)}>
            Template: {t.name}
          </button>
        ))}
      </div>

      <div className="grid2">
        <div className="card">
          <h3>Saved workflows ({workflows.length})</h3>
          {workflows.length === 0 ? (
            <div className="empty">No workflows yet — create one or load a template.</div>
          ) : (
            <table className="tbl">
              <tbody>
                {workflows.map((w) => (
                  <tr key={w.id}>
                    <td>
                      <span className="mono">{w.name}</span>
                      <div style={{ fontSize: 11, color: "var(--faint)" }}>
                        {w.steps.length} steps · {new Date(w.created).toLocaleString()}
                      </div>
                    </td>
                    <td style={{ whiteSpace: "nowrap", textAlign: "right" }}>
                      <button className="btn small" onClick={() => { setActive(w); setResults([]); }} style={{ marginRight: 6 }}>
                        Open
                      </button>
                      <button className="btn ghost small" onClick={() => editExisting(w)} style={{ marginRight: 6 }}>
                        Edit
                      </button>
                      <button className="btn danger small" onClick={() => deleteWorkflow(w.id)}>
                        Delete
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div className="card">
          <h3>{active ? `Run: ${active.name}` : editSteps.length ? `Editing: ${editName}` : "Workflow"}</h3>
          {!active && editSteps.length === 0 && <div className="empty">Open a saved workflow or create a new one.</div>}

          {editSteps.length > 0 && (
            <>
              {editSteps.map((s, i) => (
                <div key={s.id} style={{ border: "1px solid var(--border)", borderRadius: 8, padding: 12, marginBottom: 10 }}>
                  <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8 }}>
                    <span className="pill info">Step {i + 1}</span>
                    <input
                      type="text"
                      value={s.tool}
                      onChange={(e) => updateStep(s.id, { tool: e.target.value })}
                      placeholder="tool name (e.g. shell)"
                      className="mono"
                      style={{ maxWidth: 220 }}
                    />
                    <div style={{ flex: 1 }} />
                    <button className="btn ghost small" onClick={() => setEditSteps((ss) => ss.filter((x) => x.id !== s.id))}>
                      Remove
                    </button>
                  </div>
                  <textarea
                    className="mono"
                    rows={3}
                    value={s.argsText}
                    onChange={(e) => updateStep(s.id, { argsText: e.target.value })}
                    placeholder='{"command": "echo hi"}'
                    style={{ fontSize: 12 }}
                  />
                </div>
              ))}
              <div style={{ display: "flex", gap: 8 }}>
                <button className="btn ghost small" onClick={() => setEditSteps((ss) => [...ss, { id: uid(), tool: "shell", argsText: "{}" }])}>
                  + Add step
                </button>
                <button className="btn small" onClick={saveWorkflow}>
                  Save workflow
                </button>
              </div>
            </>
          )}

          {active && (
            <>
              <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
                <button className="btn small" onClick={() => runWorkflow(active)} disabled={running}>
                  {running ? "Running…" : "▶ Run workflow"}
                </button>
                {running && (
                  <button className="btn danger small" onClick={() => setStopFlag(true)}>
                    Stop
                  </button>
                )}
              </div>
              {active.steps.map((s, i) => {
                const r = results.find((x) => x.stepId === s.id);
                return (
                  <div key={s.id} style={{ border: "1px solid var(--border)", borderRadius: 8, padding: 12, marginBottom: 10 }}>
                    <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                      <span className="mono" style={{ fontWeight: 700 }}>
                        {i + 1}. {s.tool}
                      </span>
                      {r && (
                        <span className={`pill ${r.status === "ok" ? "ok" : r.status === "error" ? "err" : r.status === "held" ? "warn" : "info"}`}>
                          {r.status}
                        </span>
                      )}
                    </div>
                    <div className="mono" style={{ fontSize: 11, color: "var(--muted)", marginTop: 6 }}>{s.argsText}</div>
                    {r && r.output && (
                      <pre className="code" style={{ marginTop: 8, maxHeight: 200 }}>
                        {r.output}
                      </pre>
                    )}
                  </div>
                );
              })}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
