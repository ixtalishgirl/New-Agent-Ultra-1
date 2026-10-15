import { useState } from "react";
import {
  getStoredPassword,
  getStoredUrl,
  resolveBaseUrl,
  storeSettings,
  api,
  ApiError,
  ACCENT_PRESETS,
  getAccent,
  setAccent,
} from "../api/client";
import { clearApprovals, clearLog } from "../store/history";
import PermissionToggles from "./PermissionToggles";
import type { ConnState } from "./Connection";

/**
 * Options — the agent's control panel.
 * Sections: backend, permissions (live toggles), theme, session & model,
 * danger zone, local data. No personality text — functional settings only.
 */
export default function Config({ onSaved, conn }: { onSaved: () => void; conn: ConnState }) {
  const [url, setUrl] = useState(getStoredUrl());
  const [password, setPassword] = useState(getStoredPassword());
  const [saved, setSaved] = useState(false);
  const [accent, setAccentState] = useState(getAccent());
  const [sessionInfo, setSessionInfo] = useState("");
  const [tunnel, setTunnel] = useState("");
  const [loading, setLoading] = useState(false);
  const [dangerMsg, setDangerMsg] = useState("");

  const save = () => {
    storeSettings(url, password);
    setSaved(true);
    onSaved();
    setTimeout(() => setSaved(false), 2000);
  };

  const pickTheme = (a: string, b: string) => {
    setAccent(a, b);
    setAccentState({ a, b });
  };

  const loadSession = async () => {
    setLoading(true);
    setSessionInfo("");
    setTunnel("");
    try {
      const [s, t] = await Promise.all([
        api.sessionInfo().catch((e) => `session_info failed: ${e instanceof ApiError ? e.message : e}`),
        api.tunnelStatus().then((r) => JSON.stringify(r, null, 2)).catch((e) => `tunnel failed: ${e instanceof ApiError ? e.message : e}`),
      ]);
      setSessionInfo(s);
      setTunnel(t);
    } finally {
      setLoading(false);
    }
  };

  const emergencyReset = async () => {
    if (!confirm("Emergency reset? This re-asserts safe defaults: 475 permissions ON, 5 denials locked.")) return;
    setDangerMsg("Running…");
    try {
      const out = await api.setSafeMode(true);
      setDangerMsg(out.slice(0, 300));
    } catch (e) {
      setDangerMsg(`Failed: ${e instanceof ApiError ? e.message : "error"}`);
    }
  };

  return (
    <div>
      {/* ---------- backend ---------- */}
      <div className="card">
        <h3>🔌 Backend connection</h3>
        <label>API base URL (leave empty to use the same origin the dashboard is served from)</label>
        <input
          type="text"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder={typeof window !== "undefined" ? window.location.origin : ""}
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

      {/* ---------- permissions ---------- */}
      <div className="card">
        <h3>🔑 Permissions — flip any power on/off</h3>
        <p style={{ color: "var(--muted)", fontSize: 13, marginTop: 0 }}>
          Changes go through the agent's own perm_grant / perm_revoke tools and are audit-logged.
          The 5 locked denials cannot be flipped.
        </p>
        <PermissionToggles conn={conn} compact />
      </div>

      {/* ---------- theme ---------- */}
      <div className="card">
        <h3>🎨 Theme — accent color</h3>
        <p style={{ color: "var(--muted)", fontSize: 13, marginTop: 0 }}>
          Applies instantly. Tip: you can also ask the agent in Chat to change the accent color
          via self_modify on <span className="mono">dashboard/src/styles.css</span> — a live self-mod demo.
        </p>
        <div className="swatches">
          {ACCENT_PRESETS.map((p) => {
            const active = accent.a === p.a && accent.b === p.b;
            return (
              <button
                key={p.name}
                className={`swatch ${active ? "active" : ""}`}
                title={p.name}
                onClick={() => pickTheme(p.a, p.b)}
                style={{ background: `linear-gradient(135deg, ${p.a}, ${p.b})` }}
              >
                <span>{p.name}</span>
              </button>
            );
          })}
        </div>
        <div className="grid2" style={{ marginTop: 12 }}>
          <div>
            <label>Custom accent</label>
            <input
              type="color"
              value={accent.a}
              onChange={(e) => pickTheme(e.target.value, accent.b)}
              style={{ width: "100%", height: 40, padding: 4, cursor: "pointer" }}
            />
          </div>
          <div>
            <label>Custom accent 2</label>
            <input
              type="color"
              value={accent.b}
              onChange={(e) => pickTheme(accent.a, e.target.value)}
              style={{ width: "100%", height: 40, padding: 4, cursor: "pointer" }}
            />
          </div>
        </div>
      </div>

      {/* ---------- session & model ---------- */}
      <div className="card">
        <h3>🖥️ Session & model</h3>
        <button className="btn ghost small" onClick={loadSession} disabled={loading}>
          {loading ? "Loading…" : "Load session info"}
        </button>
        {sessionInfo && (
          <>
            <label>Agent session (session_info tool)</label>
            <pre className="code">{sessionInfo.slice(0, 2000)}</pre>
          </>
        )}
        {tunnel && (
          <>
            <label>Model tunnel (/api/tunnel/status)</label>
            <pre className="code">{tunnel.slice(0, 2000)}</pre>
          </>
        )}
      </div>

      {/* ---------- danger zone ---------- */}
      <div className="card danger">
        <h3>⚠️ Danger zone</h3>
        <p style={{ color: "var(--muted)", fontSize: 13, marginTop: 0 }}>
          Emergency reset re-asserts safe defaults: 475 permissions ON, 5 denials locked.
          It can never disable the 5 denials.
        </p>
        <button className="btn danger" onClick={emergencyReset}>
          Emergency reset — restore safe defaults
        </button>
        {dangerMsg && <pre className="code" style={{ marginTop: 10 }}>{dangerMsg}</pre>}
      </div>

      {/* ---------- local data ---------- */}
      <div className="card">
        <h3>💾 Local data</h3>
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
