import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, DESTRUCTIVE_TOOLS } from "../api/client";
import { addApproval, addEntry } from "../store/history";
import type { ConnState } from "./Connection";

interface TreeNode {
  name: string;
  path: string;
  isDir: boolean;
  size?: string;
}

/** Parse the text output of the `file_tree` tool into nodes. */
function parseTree(output: string, basePath: string): TreeNode[] {
  const lines = output.split("\n").slice(1); // skip header line
  const nodes: TreeNode[] = [];
  const seen = new Set<string>();
  for (const raw of lines) {
    const line = raw.replace(/^[\s│├└─]*/, "");
    const isDir = line.includes("📁");
    const isFile = line.includes("📄");
    if (!isDir && !isFile) continue;
    const m = line.match(/[📁📄]\s+([^\s(]+)/);
    if (!m) continue;
    const name = m[1];
    const sizeM = line.match(/\(([^,]+),/);
    const full = basePath === "." ? name : `${basePath}/${name}`;
    if (seen.has(full)) continue;
    seen.add(full);
    nodes.push({ name, path: full, isDir, size: sizeM ? sizeM[1].trim() : undefined });
  }
  nodes.sort((a, b) => (a.isDir === b.isDir ? a.name.localeCompare(b.name) : a.isDir ? -1 : 1));
  return nodes;
}

function parentOf(p: string): string {
  if (p === "." || p === "/" || !p.includes("/")) return ".";
  return p.split("/").slice(0, -1).join("/") || ".";
}

export default function FileManager({ conn }: { conn: ConnState }) {
  const [cwd, setCwd] = useState(".");
  const [nodes, setNodes] = useState<TreeNode[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<TreeNode | null>(null);
  const [content, setContent] = useState("");
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState("");
  const [clipboard, setClipboard] = useState<{ path: string; content: string; cut: boolean } | null>(null);
  const [newName, setNewName] = useState("");
  const [showNew, setShowNew] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  const load = useCallback(
    async (path: string) => {
      if (conn !== "online") return;
      setLoading(true);
      setError("");
      setSelected(null);
      setContent("");
      setEditing(false);
      try {
        const out = (await api.executeTool("file_tree", { path, max_depth: 1 })) as string;
        setNodes(parseTree(String(out), path));
        setCwd(path);
      } catch (e) {
        setError(e instanceof ApiError ? e.message : "Failed to list directory");
      } finally {
        setLoading(false);
      }
    },
    [conn]
  );

  useEffect(() => {
    load(".");
  }, [load]);

  const openNode = async (n: TreeNode) => {
    if (n.isDir) {
      load(n.path);
      return;
    }
    setSelected(n);
    setEditing(false);
    setMsg("");
    setBusy("Reading…");
    try {
      const out = (await api.executeTool("file_read", { path: n.path })) as string;
      setContent(String(out));
      setDraft(String(out));
    } catch (e) {
      setContent("");
      setMsg(`Error: ${e instanceof ApiError ? e.message : "read failed"}`);
    } finally {
      setBusy("");
    }
  };

  const saveFile = async () => {
    if (!selected || selected.isDir) return;
    if (DESTRUCTIVE_TOOLS.has("file_write")) {
      addApproval("file_write", { path: selected.path, content: draft }, `Dashboard file save: ${selected.path}`);
      setMsg("Held for approval — see the Approvals tab.");
      return;
    }
    await doWrite(selected.path, draft);
  };

  const doWrite = async (path: string, text: string) => {
    setBusy("Saving…");
    try {
      await api.executeTool("file_write", { path, content: text });
      setContent(text);
      setEditing(false);
      setMsg("Saved.");
      addEntry({ kind: "tool", status: "ok", title: "file_write", detail: path });
    } catch (e) {
      setMsg(`Error: ${e instanceof ApiError ? e.message : "write failed"}`);
    } finally {
      setBusy("");
    }
  };

  const copyNode = async (cut: boolean) => {
    if (!selected || selected.isDir) return;
    setClipboard({ path: selected.path, content: content || draft, cut });
    setMsg(cut ? `Cut: ${selected.name}` : `Copied: ${selected.name}`);
  };

  const paste = async () => {
    if (!clipboard) return;
    const dest = `${cwd === "." ? "" : cwd + "/"}${clipboard.path.split("/").pop()}`;
    if (clipboard.cut && clipboard.path === dest) {
      setMsg("Source and destination are the same.");
      return;
    }
    await doWrite(dest, clipboard.content);
    if (clipboard.cut) {
      try {
        await api.executeTool("shell", { command: `rm -f ${JSON.stringify(clipboard.path)}` });
      } catch {
        /* best effort */
      }
      setClipboard(null);
    }
    setMsg(`Pasted to ${dest}`);
    load(cwd);
  };

  const createFile = async () => {
    const name = newName.trim();
    if (!name) return;
    const path = `${cwd === "." ? "" : cwd + "/"}${name}`;
    await doWrite(path, "");
    setNewName("");
    setShowNew(false);
    load(cwd);
  };

  const uploadFile = async (f: File) => {
    const text = await f.text();
    const path = `${cwd === "." ? "" : cwd + "/"}${f.name}`;
    await doWrite(path, text);
    setMsg(`Uploaded ${f.name}`);
    load(cwd);
  };

  const downloadFile = () => {
    if (!selected || selected.isDir) return;
    const blob = new Blob([content], { type: "text/plain" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = selected.name;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  if (conn !== "online") return <div className="empty">Backend offline — file manager unavailable.</div>;

  return (
    <div>
      <div className="toolbar">
        <button className="btn ghost small" onClick={() => load(parentOf(cwd))} disabled={cwd === "."}>
          ↑ Up
        </button>
        <span className="mono" style={{ color: "var(--muted)" }}>
          /{cwd === "." ? "" : cwd}
        </span>
        <div className="spacer" style={{ flex: 1 }} />
        <button className="btn ghost small" onClick={() => setShowNew(!showNew)}>
          + New file
        </button>
        <button className="btn ghost small" onClick={() => fileInput.current?.click()}>
          Upload
        </button>
        <input
          ref={fileInput}
          type="file"
          style={{ display: "none" }}
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) uploadFile(f);
            e.target.value = "";
          }}
        />
        <button className="btn ghost small" onClick={paste} disabled={!clipboard}>
          Paste{clipboard ? ` (${clipboard.cut ? "cut" : "copy"}: ${clipboard.path.split("/").pop()})` : ""}
        </button>
        <button className="btn ghost small" onClick={() => load(cwd)}>
          Refresh
        </button>
      </div>

      {showNew && (
        <div className="card">
          <h3>New file in /{cwd === "." ? "" : cwd}</h3>
          <div style={{ display: "flex", gap: 10 }}>
            <input type="text" placeholder="filename.ext" value={newName} onChange={(e) => setNewName(e.target.value)} />
            <button className="btn small" onClick={createFile}>
              Create
            </button>
          </div>
        </div>
      )}

      {error && (
        <div className="empty" style={{ color: "var(--red)" }}>
          {error}
        </div>
      )}

      <div className="grid2">
        <div className="card">
          <h3>Directory listing</h3>
          {loading ? (
            <div className="empty">Loading…</div>
          ) : nodes.length === 0 ? (
            <div className="empty">Empty directory</div>
          ) : (
            <table className="tbl">
              <tbody>
                {nodes.map((n) => (
                  <tr
                    key={n.path}
                    onClick={() => openNode(n)}
                    style={{
                      cursor: "pointer",
                      background: selected?.path === n.path ? "rgba(110,155,255,0.08)" : undefined,
                    }}
                  >
                    <td style={{ width: 28 }}>{n.isDir ? "📁" : "📄"}</td>
                    <td className="mono">{n.name}</td>
                    <td style={{ color: "var(--faint)", fontSize: 12, textAlign: "right" }}>{n.size || ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div className="card">
          <h3>{selected ? (selected.isDir ? "Directory" : `File: ${selected.name}`) : "File preview"}</h3>
          {!selected ? (
            <div className="empty">Select a file to view or edit it.</div>
          ) : selected.isDir ? (
            <div className="empty">Double-click a folder in the list to open it.</div>
          ) : (
            <>
              <div className="toolbar">
                {!editing ? (
                  <button className="btn small" onClick={() => setEditing(true)}>
                    Edit
                  </button>
                ) : (
                  <>
                    <button className="btn small" onClick={saveFile} disabled={!!busy}>
                      {busy || "Save"}
                    </button>
                    <button
                      className="btn ghost small"
                      onClick={() => {
                        setDraft(content);
                        setEditing(false);
                      }}
                    >
                      Cancel
                    </button>
                  </>
                )}
                <button className="btn ghost small" onClick={() => copyNode(false)}>
                  Copy
                </button>
                <button className="btn ghost small" onClick={() => copyNode(true)}>
                  Cut
                </button>
                <button className="btn ghost small" onClick={downloadFile}>
                  Download
                </button>
              </div>
              {editing ? (
                <textarea
                  className="mono"
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  rows={22}
                  style={{ fontSize: 12 }}
                />
              ) : (
                <pre className="code" style={{ maxHeight: 480 }}>
                  {busy || content || "(empty)"}
                </pre>
              )}
              {msg && <div style={{ marginTop: 10, fontSize: 12, color: "var(--muted)" }}>{msg}</div>}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
