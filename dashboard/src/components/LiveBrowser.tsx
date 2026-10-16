import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import { addEntry } from "../store/history";
import type { ConnState } from "./Connection";

/**
 * Live Browser — watch the agent's own Chromium like ahsan watches Muse's.
 * - Navigate to any URL (browser_navigate)
 * - Live screenshots, auto-refreshing (browser_screenshot via /api/browser/shot)
 * - Extract page text (browser_extract)
 * - 24/7 Vision mode: continuous capture loop ("the agent's eyes")
 * - Location selector: Germany (Frankfurt, DE exit IP) or USA (Virginia, US exit IP)
 */
const LOCATIONS = [
  { id: "auto", label: "Auto (this server)", base: "", ip: "" },
  { id: "de", label: "🇩🇪 Germany · Frankfurt", base: "https://haley-agent-de.onrender.com", ip: "74.220.51.162" },
  { id: "us", label: "🇺🇸 USA · Virginia", base: "https://haley-agent.onrender.com", ip: "74.220.49.7" },
] as const;

export default function LiveBrowser({ conn }: { conn: ConnState }) {
  const [url, setUrl] = useState("https://example.com");
  const [location, setLocation] = useState<string>("auto");
  const [shot, setShot] = useState("");
  const [title, setTitle] = useState("");
  const [pageText, setPageText] = useState("");
  const [showText, setShowText] = useState(false);
  const [loading, setLoading] = useState(false);
  const [vision, setVision] = useState(false);
  const [intervalMs, setIntervalMs] = useState(5000);
  const [lastShot, setLastShot] = useState("");
  const [error, setError] = useState("");
  const [shots, setShots] = useState(0);
  const timer = useRef<number | null>(null);
  const urlRef = useRef(url);
  urlRef.current = url;
  const locRef = useRef(location);
  locRef.current = location;
  const locBase = (id?: string) => {
    const l = LOCATIONS.find((x) => x.id === (id ?? locRef.current));
    return l && l.base ? l.base : undefined;
  };

  const capture = useCallback(async (target?: string) => {
    const u = (target ?? urlRef.current).trim();
    if (!u) return;
    setLoading(true);
    setError("");
    try {
      const r = await api.browserShot(u, locBase());
      setShot(r.data_uri);
      setLastShot(new Date().toLocaleTimeString());
      setShots((s) => s + 1);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Screenshot failed");
      setVision(false);
    } finally {
      setLoading(false);
    }
  }, []);

  const navigate = async () => {
    const u = url.trim();
    if (!u) return;
    setLoading(true);
    setError("");
    setPageText("");
    try {
      const out = (await api.executeTool("browser_navigate", { url: u }, locBase())) as string;
      try {
        const j = JSON.parse(out);
        setTitle(j.title || "");
        setPageText(j.text || "");
      } catch {
        setPageText(out);
      }
      addEntry({ kind: "tool", status: "ok", title: "browser_navigate", detail: u });
      await capture(u);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Navigation failed");
    } finally {
      setLoading(false);
    }
  };

  const extract = async () => {
    const u = url.trim();
    if (!u) return;
    setLoading(true);
    try {
      const out = (await api.executeTool("browser_extract", { url: u }, locBase())) as string;
      setPageText(String(out));
      setShowText(true);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Extract failed");
    } finally {
      setLoading(false);
    }
  };

  // 24/7 vision loop
  useEffect(() => {
    if (vision) {
      capture();
      timer.current = window.setInterval(() => capture(), Math.max(2000, intervalMs));
    }
    return () => {
      if (timer.current) {
        clearInterval(timer.current);
        timer.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [vision, intervalMs]);

  if (conn !== "online") return <div className="empty">Backend offline — live browser unavailable.</div>;

  return (
    <div>
      <div className="card">
        <h3>
          Agent Browser {vision && <span className="pill ok">● 24/7 vision on</span>}
          {loading && <span style={{ color: "var(--muted)", fontSize: 12 }}> — capturing…</span>}
        </h3>
        <div style={{ display: "flex", gap: 10, marginBottom: 12, flexWrap: "wrap", alignItems: "center" }}>
          <label style={{ display: "flex", alignItems: "center", gap: 8, margin: 0, fontSize: 12, color: "var(--muted)" }}>
            Location
            <select
              value={location}
              onChange={(e) => setLocation(e.target.value)}
              style={{ width: "auto", padding: "6px 8px" }}
              title="Browser exit location: Germany via Frankfurt service, USA via Virginia service"
            >
              {LOCATIONS.map((l) => (
                <option key={l.id} value={l.id}>{l.label}</option>
              ))}
            </select>
          </label>
          {(() => {
            const l = LOCATIONS.find((x) => x.id === location);
            return l && l.ip ? (
              <span className="pill ok" style={{ fontSize: 11 }} title="Verified exit IP">exit IP {l.ip}</span>
            ) : null;
          })()}
        </div>
        <div style={{ display: "flex", gap: 10, marginBottom: 12 }}>
          <input
            type="text"
            placeholder="https://…"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && navigate()}
            className="mono"
            style={{ flex: 1 }}
          />
          <button className="btn small" onClick={navigate} disabled={loading}>
            Go
          </button>
          <button className="btn ghost small" onClick={() => capture()} disabled={loading}>
            📸 Shot
          </button>
          <button className="btn ghost small" onClick={extract} disabled={loading}>
            Extract text
          </button>
        </div>
        <div style={{ display: "flex", gap: 16, alignItems: "center", flexWrap: "wrap", fontSize: 12, color: "var(--muted)" }}>
          <label style={{ display: "flex", alignItems: "center", gap: 8, margin: 0, cursor: "pointer" }}>
            <input type="checkbox" checked={vision} onChange={(e) => setVision(e.target.checked)} style={{ width: "auto" }} />
            24/7 Vision — continuous screenshots (the agent's eyes)
          </label>
          <label style={{ display: "flex", alignItems: "center", gap: 8, margin: 0 }}>
            Refresh every
            <select value={intervalMs} onChange={(e) => setIntervalMs(Number(e.target.value))} style={{ width: "auto", padding: "6px 8px" }}>
              <option value={3000}>3s</option>
              <option value={5000}>5s</option>
              <option value={10000}>10s</option>
              <option value={30000}>30s</option>
            </select>
          </label>
          {lastShot && <span>Last capture: {lastShot} · {shots} shots this session</span>}
        </div>
        {error && <div style={{ marginTop: 10, color: "var(--red)", fontSize: 12 }}>{error}</div>}
      </div>

      <div className="card">
        <h3>
          Live view {title && <span style={{ color: "var(--muted)", fontWeight: 400 }}>— {title}</span>}
        </h3>
        {shot ? (
          <img
            src={shot}
            alt="Agent browser screenshot"
            style={{ width: "100%", borderRadius: 8, border: "1px solid var(--border)", background: "#fff" }}
          />
        ) : (
          <div className="empty">
            No screenshot yet. Enter a URL and press Go — or enable 24/7 Vision.
            <br />
            <span style={{ fontSize: 11 }}>Requires Playwright + Chromium on the agent host.</span>
          </div>
        )}
      </div>

      {(showText || pageText) && (
        <div className="card">
          <h3>
            Page content
            <button className="btn ghost small" style={{ marginLeft: 12 }} onClick={() => setShowText(!showText)}>
              {showText ? "Hide" : "Show"}
            </button>
          </h3>
          {showText && <pre className="code" style={{ maxHeight: 400 }}>{pageText || "(empty)"}</pre>}
        </div>
      )}
    </div>
  );
}
