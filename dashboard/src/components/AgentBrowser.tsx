import { useEffect, useRef, useState } from "react";

/**
 * AgentBrowser — the agent's live browser, like the assistant's.
 * A real-time MJPEG stream of a real Chromium page. Click directly on the
 * page, type with your keyboard, scroll with the wheel. No option panels.
 */
export default function AgentBrowser({ conn }: { conn: string }) {
  const imgRef = useRef<HTMLImageElement>(null);
  const [url, setUrl] = useState("");
  const [tor, setTor] = useState(false);
  const [loading, setLoading] = useState(false);
  const base = conn === "offline" ? "" : conn;
  const streamUrl = `${base}/api/browser/live`;

  // Restart the MJPEG stream when it drops.
  useEffect(() => {
    const img = imgRef.current;
    if (!img) return;
    let t: ReturnType<typeof setTimeout>;
    const kick = () => {
      t = setTimeout(() => {
        if (imgRef.current) {
          imgRef.current.src = `${streamUrl}?t=${Date.now()}`;
        }
      }, 3000);
    };
    img.onerror = kick;
    return () => clearTimeout(t);
  }, [streamUrl]);

  const post = async (path: string, body: object) => {
    await fetch(`${base}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  };

  const go = async () => {
    const u = url.trim();
    if (!u) return;
    setLoading(true);
    try {
      await post("/api/browser/navigate", { url: u });
    } finally {
      setLoading(false);
    }
  };

  const rel = (e: React.MouseEvent) => {
    const r = (e.target as HTMLElement).getBoundingClientRect();
    return {
      x: (e.clientX - r.left) / r.width,
      y: (e.clientY - r.top) / r.height,
    };
  };

  const onClick = (e: React.MouseEvent) => {
    const { x, y } = rel(e);
    post("/api/browser/input", { type: "click", x, y });
  };

  const onWheel = (e: React.WheelEvent) => {
    post("/api/browser/input", {
      type: "scroll",
      dx: e.deltaX / 400,
      dy: e.deltaY / 400,
    });
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    // Let the browser page receive real typing.
    const k = e.key;
    if (k === "Enter" && (e.target as HTMLElement).tagName === "INPUT") {
      go();
      return;
    }
    e.preventDefault();
    if (k.length === 1) {
      post("/api/browser/input", { type: "type", text: k });
    } else {
      post("/api/browser/input", { type: "key", key: k });
    }
  };

  const toggleTor = async () => {
    const next = !tor;
    setTor(next);
    await post("/api/browser/tor", { enabled: next });
  };

  return (
    <div
      className="agent-browser"
      style={{ display: "flex", flexDirection: "column", height: "100%" }}
      tabIndex={0}
      onKeyDown={onKeyDown}
    >
      {/* Minimal browser chrome — URL bar + Tor, nothing else */}
      <div
        style={{
          display: "flex",
          gap: 8,
          padding: "8px 10px",
          alignItems: "center",
          borderBottom: "1px solid #2a2a3a",
          background: "#16161f",
        }}
      >
        <span title="Germany · Frankfurt" style={{ fontSize: 18 }}>🇩🇪</span>
        <input
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.stopPropagation();
              go();
            }
          }}
          placeholder="Search or type a URL…"
          style={{
            flex: 1,
            background: "#0e0e14",
            border: "1px solid #2a2a3a",
            borderRadius: 8,
            color: "#eee",
            padding: "8px 12px",
            fontSize: 14,
            outline: "none",
          }}
        />
        <button
          onClick={go}
          disabled={loading}
          style={{
            background: "#4a5cff",
            border: "none",
            borderRadius: 8,
            color: "#fff",
            padding: "8px 16px",
            fontSize: 14,
            cursor: "pointer",
          }}
        >
          {loading ? "…" : "Go"}
        </button>
        <button
          onClick={toggleTor}
          title={tor ? "Tor ON — onion routing" : "Tor OFF"}
          style={{
            background: tor ? "#7b2ff7" : "#23232f",
            border: "1px solid #2a2a3a",
            borderRadius: 8,
            color: "#fff",
            padding: "8px 12px",
            fontSize: 14,
            cursor: "pointer",
          }}
        >
          🧅 {tor ? "ON" : "OFF"}
        </button>
      </div>

      {/* Live page — click, type, scroll directly */}
      <div style={{ flex: 1, position: "relative", background: "#000", overflow: "hidden" }}>
        <img
          ref={imgRef}
          src={streamUrl}
          onClick={onClick}
          onWheel={onWheel}
          alt="Live browser"
          draggable={false}
          style={{
            width: "100%",
            height: "100%",
            objectFit: "contain",
            cursor: "default",
            userSelect: "none",
          }}
        />
      </div>
    </div>
  );
}
