import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { api, ApiError, DESTRUCTIVE_TOOLS, toolCategory, type ToolInfo } from "../api/client";
import { addApproval, addEntry } from "../store/history";
import type { ConnState } from "./Connection";

/** Render data:image URIs embedded in tool output as real images (screenshots). */
function renderResultRich(text: string): ReactNode {
  const re = /(data:image\/[a-zA-Z+]+;base64,[A-Za-z0-9+/=\s]+)/g;
  const parts: ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  let k = 0;
  while ((m = re.exec(text)) !== null) {
    const uri = m[1].replace(/\s+/g, "");
    if (uri.length < 100) continue; // not a real image
    if (m.index > last) parts.push(<span key={`t${k++}`}>{text.slice(last, m.index)}</span>);
    parts.push(
      <img
        key={`i${k++}`}
        src={uri}
        alt="tool output"
        style={{ maxWidth: "100%", borderRadius: 8, margin: "8px 0", border: "1px solid #333", display: "block" }}
      />
    );
    last = m.index + m[0].length;
  }
  if (last < text.length) parts.push(<span key={`t${k++}`}>{text.slice(last)}</span>);
  return parts.length > 0 ? parts : text;
}

export default function ToolRegistry({ conn, onApproval }: { conn: ConnState; onApproval: () => void }) {
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [catFilter, setCatFilter] = useState("all");
  const [selected, setSelected] = useState<ToolInfo | null>(null);
  const [argsText, setArgsText] = useState("{}");
  const [result, setResult] = useState("");
  const [running, setRunning] = useState(false);
  const [showBuilder, setShowBuilder] = useState(false);
  const [buildName, setBuildName] = useState("");
  const [buildDesc, setBuildDesc] = useState("");
  const [buildCode, setBuildCode] = useState('def run(args):\n    """Tool entry point. args is a dict."""\n    return {"ok": True, "echo": args}\n');
  const [buildMsg, setBuildMsg] = useState("");
  const [fetchUrl, setFetchUrl] = useState("");
  const [fetchMsg, setFetchMsg] = useState("");
  const [hfId, setHfId] = useState("");
  const [hfInfo, setHfInfo] = useState("");

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

  const categories = useMemo(() => {
    const m = new Map<string, number>();
    for (const t of tools) m.set(toolCategory(t.name), (m.get(toolCategory(t.name)) || 0) + 1);
    return [...m.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [tools]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const list = tools.filter(
      (t) =>
        (catFilter === "all" || toolCategory(t.name) === catFilter) &&
        (!q ||
          t.name.toLowerCase().includes(q) ||
          (t.description || "").toLowerCase().includes(q) ||
          toolCategory(t.name).toLowerCase().includes(q))
    );
    const groups = new Map<string, ToolInfo[]>();
    for (const t of list) {
      const c = toolCategory(t.name);
      if (!groups.has(c)) groups.set(c, []);
      groups.get(c)!.push(t);
    }
    return [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [tools, query, catFilter]);

  const visibleCount = filtered.reduce((n, [, l]) => n + l.length, 0);

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

  const buildTool = async () => {
    const name = buildName.trim().replace(/[^a-zA-Z0-9_]/g, "_");
    if (!name || !buildCode.trim()) {
      setBuildMsg("Name and code are required.");
      return;
    }
    setRunning(true);
    setBuildMsg("Building…");
    try {
      const out = (await api.executeTool("tool_create", {
        name,
        description: buildDesc.trim() || `Dashboard-built tool: ${name}`,
        code: buildCode,
      })) as string;
      setBuildMsg(`Created:\n${out}`);
      addEntry({ kind: "tool", status: "ok", title: "tool_create", detail: name });
      const t = await api.listTools();
      setTools(t);
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : "Build failed";
      // tool_create is destructive → may be held for approval
      if (msg.includes("approval") || msg.includes("Held")) {
        addApproval("tool_create", { name, description: buildDesc, code: buildCode }, `Dashboard tool builder: ${name}`);
        setBuildMsg("Held for approval — see the Approvals tab.");
        onApproval();
      } else {
        setBuildMsg(`Error: ${msg}`);
      }
    } finally {
      setRunning(false);
    }
  };

  /** Fetch a raw file from GitHub and install it as a tool. */
  const fetchFromGitHub = async () => {
    const u = fetchUrl.trim();
    if (!u) {
      setFetchMsg("Paste a GitHub raw URL (e.g. https://raw.githubusercontent.com/…/tool.py).");
      return;
    }
    setFetchMsg("Fetching…");
    try {
      const out = (await api.executeTool("http_get", { url: u })) as string;
      const code = String(out).slice(0, 20000);
      if (!code || code.length < 10) {
        setFetchMsg("Fetch returned empty content.");
        return;
      }
      const base = u.split("/").pop()?.replace(/[^a-zA-Z0-9_]/g, "_").replace(/_py$/, "") || "github_tool";
      setBuildName(base);
      setBuildDesc(`Fetched from GitHub: ${u}`);
      setBuildCode(code);
      setFetchMsg(`Fetched ${code.length} chars into the builder below — review and press "Build tool".`);
    } catch (e) {
      setFetchMsg(`Error: ${e instanceof ApiError ? e.message : "fetch failed"}`);
    }
  };

  /** Fetch model/repo info from HuggingFace and show it (install as data via http_get). */
  const fetchFromHF = async () => {
    const id = hfId.trim();
    if (!id) {
      setHfInfo("Enter a HuggingFace id (e.g. google/gemma-2b).");
      return;
    }
    setHfInfo("Fetching…");
    try {
      const out = (await api.executeTool("http_get", { url: `https://huggingface.co/api/models/${id}` })) as string;
      try {
        const j = JSON.parse(String(out));
        setHfInfo(
          `id: ${j.id}\nlikes: ${j.likes}\ndownloads: ${j.downloads}\nlicense: ${j.license}\ntags: ${(j.tags || []).slice(0, 10).join(", ")}\nsha: ${j.sha}\n---\nCopy what you need, or build a tool around https://huggingface.co/${id}`
        );
      } catch {
        setHfInfo(String(out).slice(0, 2000));
      }
    } catch (e) {
      setHfInfo(`Error: ${e instanceof ApiError ? e.message : "fetch failed"}`);
    }
  };

  return (
    <div>
      <div className="toolbar">
        <div className="search" style={{ marginBottom: 0, flex: 1, minWidth: 220 }}>
          <input
            type="text"
            placeholder={`Search ${tools.length} tools…`}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <select value={catFilter} onChange={(e) => setCatFilter(e.target.value)} style={{ width: "auto" }}>
          <option value="all">All categories ({categories.length})</option>
          {categories.map(([c, n]) => (
            <option key={c} value={c}>
              {c} ({n})
            </option>
          ))}
        </select>
        <span style={{ color: "var(--muted)", fontSize: 12 }}>
          {visibleCount} of {tools.length} tools
        </span>
        <button className="btn small" onClick={() => setShowBuilder(!showBuilder)}>
          {showBuilder ? "Hide builder" : "🛠 Build new tool"}
        </button>
      </div>

      {showBuilder && (
        <div className="card" style={{ borderColor: "var(--accent)" }}>
          <h3>Tool Builder</h3>
          <div className="grid2">
            <div>
              <label>Tool name</label>
              <input type="text" className="mono" placeholder="my_tool" value={buildName} onChange={(e) => setBuildName(e.target.value)} />
              <label>Description</label>
              <input type="text" placeholder="What the tool does…" value={buildDesc} onChange={(e) => setBuildDesc(e.target.value)} />
            </div>
            <div>
              <label>Fetch from GitHub (raw URL)</label>
              <div style={{ display: "flex", gap: 8 }}>
                <input type="text" className="mono" placeholder="https://raw.githubusercontent.com/…/tool.py" value={fetchUrl} onChange={(e) => setFetchUrl(e.target.value)} />
                <button className="btn ghost small" onClick={fetchFromGitHub} style={{ whiteSpace: "nowrap" }}>
                  Fetch
                </button>
              </div>
              {fetchMsg && <div style={{ fontSize: 12, color: "var(--muted)", marginTop: 6, whiteSpace: "pre-wrap" }}>{fetchMsg}</div>}
              <label>HuggingFace (model or dataset id)</label>
              <div style={{ display: "flex", gap: 8 }}>
                <input type="text" className="mono" placeholder="google/gemma-2b" value={hfId} onChange={(e) => setHfId(e.target.value)} />
                <button className="btn ghost small" onClick={fetchFromHF} style={{ whiteSpace: "nowrap" }}>
                  Info
                </button>
              </div>
              {hfInfo && <pre className="code" style={{ marginTop: 6, maxHeight: 160 }}>{hfInfo}</pre>}
            </div>
          </div>
          <label>Python code — must define <span className="mono">run(args)</span></label>
          <textarea className="mono" rows={10} value={buildCode} onChange={(e) => setBuildCode(e.target.value)} style={{ fontSize: 12 }} />
          <div style={{ marginTop: 10, display: "flex", gap: 10 }}>
            <button className="btn small" onClick={buildTool} disabled={running}>
              {running ? "Building…" : "Build tool"}
            </button>
          </div>
          {buildMsg && <pre className="code" style={{ marginTop: 10, maxHeight: 220 }}>{buildMsg}</pre>}
        </div>
      )}

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
          {result && (
            <div style={{ marginTop: 12 }}>
              <div className="result-body">{renderResultRich(result)}</div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
