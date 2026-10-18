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
  const [torOn, setTorOn] = useState(false);
  const [loading, setLoading] = useState(false);
  const imgRef = useRef<HTMLImageElement>(null);
  const [streamKey, setStreamKey] = useState(0);

  // Fetch current URL when becoming visible
  useEffect(() => {
    if (!visible) return;
    setStreamKey((k) => k + 1); // force stream refresh
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
        if (typeof d.tor === "boolean") setTorOn(d.tor);
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

  const toggleTor = async () => {
    try {
      const d: any = await api.executeTool("browser_tor", { enabled: !torOn });
      setTorOn(!!(d && (d as any).tor));
      setStreamKey((k) => k + 1);
    } catch (e) {
      console.error("tor toggle failed", e);
    }
  };

  const streamUrl = `/api/browser/live?k=${streamKey}`;

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
        <button
          onClick={toggleTor}
          title="Tor"
          style={{
            background: torOn ? "#7c3aed" : "#1a1a1a",
            color: torOn ? "#fff" : "#888",
            border: "1px solid #333",
            borderRadius: 6,
            padding: "4px 8px",
            cursor: "pointer",
            fontSize: 13,
          }}
        >
          🧅
        </button>
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
        {torOn && (
          <div
            style={{
              position: "absolute",
              top: 8,
              right: 8,
              background: "rgba(124,58,237,0.9)",
              color: "#fff",
              padding: "4px 10px",
              borderRadius: 6,
              fontSize: 12,
            }}
          >
            🧅 Tor ON
          </div>
        )}
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
        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: "70%" }}>
          {url || "…"}
        </span>
        <span>🔴 LIVE</span>
      </div>
    </div>
  );
}
