import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";

interface LiveBrowserProps {
  visible: boolean;
  onClose: () => void;
}

/**
 * Live browser view — like the assistant's browser.
 * Appears automatically inside the chat when the agent browses.
 * Shows the real-time page via MJPEG stream. No buttons to click,
 * no manual panel — it just appears when there's something to see.
 */
export default function LiveBrowser({ visible, onClose }: LiveBrowserProps) {
  const [url, setUrl] = useState("");
  const [inputUrl, setInputUrl] = useState("");
  const [loading, setLoading] = useState(false);
  const imgRef = useRef<HTMLImageElement>(null);
  const [streamKey, setStreamKey] = useState(0);
  const [settings, setSettings] = useState<any>(null);
  const [showSettings, setShowSettings] = useState(false);
  const [settingsMsg, setSettingsMsg] = useState("");

  const loadSettings = () => {
    fetch("/api/browser/settings")
      .then((r) => r.json())
      .then((d) => setSettings(d))
      .catch(() => {});
  };

  const postSettings = async (body: any) => {
    setSettingsMsg("");
    try {
      const r = await fetch("/api/browser/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const d = await r.json();
      if (d.ok) {
        setSettingsMsg("✓ Saved");
        loadSettings();
      } else {
        setSettingsMsg("✕ " + (d.error || "failed"));
      }
    } catch (e) {
      setSettingsMsg("✕ network error");
    }
  };

  // Fetch current URL when becoming visible
  useEffect(() => {
    if (!visible) return;
    setStreamKey((k) => k + 1); // force stream refresh
    loadSettings();
    fetch("/api/browser/current")
      .then((r) => r.json())
      .then((d: any) => {
        if (d.url) {
          setUrl(d.url);
          setInputUrl(d.url);
        } else {
          // No page loaded — auto-open Google so it's never a black screen
          navigate("https://www.google.com");
        }
      })
      .catch(() => {
        // If fetch fails, still try to open Google
        navigate("https://www.google.com");
      });
  }, [visible]);

  if (!visible) return null;

  const navigate = async (target: string) => {
    const u = target.trim();
    if (!u) return;
    setLoading(true);
    try {
      const full = u.startsWith("http") ? u : "https://" + u;
      await api.executeTool("browser_navigate", { url: full });
      setUrl(full);
      setInputUrl(full);
      setStreamKey((k) => k + 1);
    } catch (e) {
      console.error("navigate failed", e);
    } finally {
      setLoading(false);
    }
  };

  const streamUrl = `/api/browser/stream?k=${streamKey}`;

  return (
    <div
      style={{
        border: "1px solid #2a2a2a",
        borderRadius: 12,
        overflow: "hidden",
        margin: "12px 0",
        background: "#0a0a0a",
      }}
    >
      {/* Header — URL bar like a real browser */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "8px 12px",
          background: "#111",
          borderBottom: "1px solid #222",
        }}
      >
        <input
          value={inputUrl}
          onChange={(e) => setInputUrl(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") navigate(inputUrl);
          }}
          placeholder="URL likho..."
          style={{
            flex: 1,
            background: "#1a1a1a",
            border: "1px solid #333",
            borderRadius: 6,
            padding: "6px 10px",
            color: "#eee",
            fontSize: 13,
            outline: "none",
          }}
        />
        <button
          onClick={() => navigate(inputUrl)}
          disabled={loading}
          style={{
            background: "#1a1a1a",
            color: "#eee",
            border: "1px solid #333",
            borderRadius: 6,
            padding: "6px 12px",
            cursor: "pointer",
            fontSize: 13,
          }}
        >
          {loading ? "…" : "Go"}
        </button>
        <button
          onClick={() => {
            setShowSettings((s) => !s);
            if (!showSettings) loadSettings();
          }}
          title="Browser settings — search engine + network"
          style={{
            background: "#1a1a1a",
            color: "#eee",
            border: "1px solid #333",
            borderRadius: 6,
            padding: "6px 10px",
            cursor: "pointer",
            fontSize: 13,
          }}
        >
          ⚙
        </button>
        <button
          onClick={onClose}
          title="Band karo"
          style={{
            background: "transparent",
            color: "#666",
            border: "none",
            cursor: "pointer",
            fontSize: 16,
            padding: "4px 8px",
          }}
        >
          ✕
        </button>
      </div>

      {/* Settings panel — search engine + network mode (independent) */}
      {showSettings && (
        <div
          style={{
            padding: "12px",
            background: "#0d0d0d",
            borderBottom: "1px solid #222",
            fontSize: 13,
            color: "#ccc",
          }}
        >
          <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
            <div>
              <div style={{ marginBottom: 4, color: "#888" }}>🔍 Search engine</div>
              <select
                value={settings?.default_engine?.id || ""}
                onChange={(e) =>
                  postSettings({ action: "set_engine", engine: e.target.value })
                }
                style={{
                  background: "#1a1a1a", color: "#eee", border: "1px solid #333",
                  borderRadius: 6, padding: "6px 10px", fontSize: 13,
                }}
              >
                {settings &&
                  Object.entries(settings.engines || {}).map(([id, e]: any) => (
                    <option key={id} value={id}>
                      {e.name}
                    </option>
                  ))}
              </select>
            </div>
            <div>
              <div style={{ marginBottom: 4, color: "#888" }}>🌐 Network</div>
              <select
                value={settings?.network?.active || "normal"}
                onChange={(e) =>
                  postSettings({ action: "set_network", mode: e.target.value })
                }
                style={{
                  background: "#1a1a1a", color: "#eee", border: "1px solid #333",
                  borderRadius: 6, padding: "6px 10px", fontSize: 13,
                }}
              >
                {settings &&
                  Object.entries(settings.network?.modes || {}).map(
                    ([mode, m]: any) => (
                      <option key={mode} value={mode}>
                        {m.available ? "● " : "○ "}{m.name}
                      </option>
                    )
                  )}
              </select>
            </div>
          </div>
          {/* Per-mode status — never silent about problems */}
          {settings && (
            <div style={{ marginTop: 8, fontSize: 12, color: "#888" }}>
              {Object.entries(settings.network?.modes || {}).map(([mode, m]: any) => (
                <div key={mode} style={{ marginTop: 2 }}>
                  <span style={{ color: m.available ? "#4a4" : "#a44" }}>
                    {m.available ? "●" : "○"}
                  </span>{" "}
                  {m.name}
                  {!m.available && m.reason && (
                    <span style={{ color: "#a44" }}> — {m.reason}</span>
                  )}
                  {settings.network?.active === mode && (
                    <span style={{ color: "#6af" }}> ← active</span>
                  )}
                </div>
              ))}
            </div>
          )}
          {settingsMsg && (
            <div style={{ marginTop: 6, fontSize: 12, color: "#888" }}>{settingsMsg}</div>
          )}
        </div>
      )}

      {/* Live page view */}
      <div
        style={{
          position: "relative",
          background: "#000",
          minHeight: 300,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        <img
          key={streamKey}
          ref={imgRef}
          src={streamUrl}
          alt="Live browser"
          style={{
            width: "100%",
            display: "block",
            maxHeight: 600,
            objectFit: "contain",
          }}
          onError={() => console.error("stream failed")}
        />
      </div>

      {/* Status bar */}
      <div
        style={{
          padding: "6px 12px",
          background: "#111",
          borderTop: "1px solid #222",
          fontSize: 11,
          color: "#666",
          display: "flex",
          justifyContent: "space-between",
        }}
      >
        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: "55%" }}>
          {url || "…"}
        </span>
        <span style={{ color: "#888" }}>
          🔍 {settings?.default_engine?.name || "…"} · 🌐{" "}
          {settings?.network?.modes?.[settings?.network?.active]?.name || "…"}
        </span>
        <span>🔴 LIVE</span>
      </div>
    </div>
  );
}
