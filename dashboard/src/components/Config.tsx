import { useState } from "react";
import { getStoredPassword, getStoredUrl, resolveBaseUrl, storeSettings } from "../api/client";
import { clearApprovals, clearLog } from "../store/history";

/**
 * Agent configuration UI.
 * - Backend URL + app password (persisted in localStorage)
 * - Connection info
 * - Local data management
 * No personality text — functional settings only.
 */
export default function Config({ onSaved }: { onSaved: () => void }) {
  const [url, setUrl] = useState(getStoredUrl());
  const [password, setPassword] = useState(getStoredPassword());
  const [saved, setSaved] = useState(false);

  const save = () => {
    storeSettings(url, password);
    setSaved(true);
    onSaved();
    setTimeout(() => setSaved(false), 2000);
  };

  return (
    <div>
      <div className="card">
        <h3>Backend connection</h3>
        <label>API base URL (leave empty to use the same origin the dashboard is served from)</label>
        <input
          type="text"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder={window.location.origin}
        />
        <label>App password (x-app-password header)</label>
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="Set on the server via APP_PASSWORD"
          autoComplete="off"
        />
        <div style={{ marginTop: 14, display: "flex", gap: 10, alignItems: "center" }}>
          <button className="btn" onClick={save}>Save & reconnect</button>
          {saved && <span style={{ color: "var(--green)", fontSize: 13 }}>Saved.</span>}
        </div>
        <p style={{ color: "var(--faint)", fontSize: 12, marginTop: 12 }}>
          Effective base URL: <span className="mono">{resolveBaseUrl()}</span>
        </p>
      </div>

      <div className="card">
        <h3>Agent</h3>
        <dl className="kv">
          <dt>Project</dt><dd className="mono">New-Agent-Ultra-1</dd>
          <dt>Runtime</dt><dd>Python 3 · stdlib HTTP API</dd>
          <dt>Endpoints</dt>
          <dd className="mono">POST /api/chat · GET /api/tools · POST /api/tools/execute · GET /api/tunnel/status</dd>
          <dt>Auth</dt><dd>x-app-password header (or ?password= query)</dd>
        </dl>
      </div>

      <div className="card">
        <h3>Local data</h3>
        <p style={{ color: "var(--muted)", fontSize: 13 }}>
          Chat history, tool runs and approvals are stored in this browser only.
        </p>
        <div style={{ display: "flex", gap: 10 }}>
          <button className="btn ghost small" onClick={() => { if (confirm("Clear activity history?")) clearLog(); }}>
            Clear history
          </button>
          <button className="btn ghost small" onClick={() => { if (confirm("Clear all approvals?")) clearApprovals(); }}>
            Clear approvals
          </button>
        </div>
      </div>
    </div>
  );
}
