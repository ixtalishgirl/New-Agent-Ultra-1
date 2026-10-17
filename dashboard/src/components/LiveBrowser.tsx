import { useCallback, useEffect, useRef, useState } from "react";
import type { MouseEvent as ReactMouseEvent } from "react";
import { api, ApiError, type IpInfo } from "../api/client";
import { addEntry } from "../store/history";
import type { ConnState } from "./Connection";

/**
 * Live Browser — drive the agent's own Chromium like a remote desktop.
 * - Click the screenshot: coordinates are scaled from the displayed size to
 *   the real 1280x720 viewport and sent via browser_mouse_click, then the
 *   view refreshes.
 * - Type box sends keystrokes into the focused page element (key_type).
 * - URL bar + Go (browser_open), back / forward / reload.
 * - Screenshot poll loop (~2s) while the view is open + refresh after
 *   every action.
 * - Location shown is LIVE from GET /api/ip — never hardcoded.
 */

const VIEW_W = 1280;
const VIEW_H = 720;
const POLL_MS = 2000;

const BACKENDS = [
  { id: "de", label: "Germany", base: "https://haley-agent-de.onrender.com" },
] as const; // Germany ONLY (user order 2026-10-17)

function baseFor(id: string): string | undefined {
  const b = BACKENDS.find((x) => x.id === id);
  return b && b.base ? b.base : undefined;
}

/** browser_tab_screenshot returns "screenshot -> /tmp/...\n![screenshot](data:...)" */
function extractShotUri(out: unknown): string {
  const m = String(out ?? "").match(
    /!\[screenshot\]\((data:image\/png;base64,[A-Za-z0-9+/=]+)\)/
  );
  return m ? m[1] : "";
}

export default function LiveBrowser({ conn }: { conn: ConnState }) {
  const [backend] = useState<string>("de"); // LOCKED: Germany only (user order 2026-10-17)
  const [ip, setIp] = useState<IpInfo | null>(null);
  const [url, setUrl] = useState("https://example.com");
  const [pageUrl, setPageUrl] = useState("");
  const [pageTitle, setPageTitle] = useState("");
  const [shot, setShot] = useState("");
  const [shotAt, setShotAt] = useState("");
  const [shots, setShots] = useState(0);
  const [stale, setStale] = useState(false);
  const [pageText, setPageText] = useState("");
  const [showText, setShowText] = useState(false);
  const [typeText, setTypeText] = useState("");
  const [busy, setBusy] = useState(false);
  const [pollOn, setPollOn] = useState(true);
  const [lastClick, setLastClick] = useState<{ x: number; y: number } | null>(null);
  const [error, setError] = useState("");

  const backendRef = useRef(backend);
  backendRef.current = backend;
  const shotBusyRef = useRef(false);
  const actBusyRef = useRef(false);

  /** Grab one fresh frame from the active tab (daemon screenshot). */
  const shotNow = useCallback(async () => {
    if (shotBusyRef.current) return;
    shotBusyRef.current = true;
    try {
      const out = await api.executeTool(
        "browser_tab_screenshot",
        {},
        baseFor(backendRef.current)
      );
      const uri = extractShotUri(out);
      if (uri) {
        setShot(uri);
        setShotAt(new Date().toLocaleTimeString());
        setShots((s) => s + 1);
        setStale(false);
        setError("");
      } else if (!shot) {
        setError("Screenshot came back empty — is the browser daemon running?");
      }
    } catch {
      // Poll failures shouldn't nuke a good frame; mark it stale instead.
      setStale(true);
    } finally {
      shotBusyRef.current = false;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** Read back the active tab's URL + title (cheap, no screenshot). */
  const refreshState = useCallback(async () => {
    try {
      const out = await api.executeTool(
        "browser_get_page_state",
        {},
        baseFor(backendRef.current)
      );
      const st = JSON.parse(String(out ?? "{}")) as {
        url?: string;
        title?: string;
      };
      if (st.url) {
        setPageUrl(st.url);
        setUrl(st.url);
      }
      if (st.title) setPageTitle(st.title);
    } catch {
      /* non-fatal — the frame still tells the story */
    }
  }, []);

  /** Run one browser action, then refresh the frame. */
  const act = async (
    name: string,
    args: Record<string, unknown>,
    label: string
  ) => {
    if (actBusyRef.current) return;
    actBusyRef.current = true;
    setBusy(true);
    setError("");
    try {
      await api.executeTool(name, args, baseFor(backendRef.current));
      addEntry({ kind: "tool", status: "ok", title: name, detail: label });
      await refreshState();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : `${name} failed`);
    } finally {
      actBusyRef.current = false;
      setBusy(false);
      await shotNow();
    }
  };

  const [torMode, setTorMode] = useState(false); // Tor onion browsing toggle

  const go = async () => {
    const raw = url.trim();
    if (!raw || actBusyRef.current) return;
    // Smart target: .onion -> Tor; plain text -> DuckDuckGo search; else URL.
    const isOnion = /\.onion(\/|$)/i.test(raw);
    const looksUrl = /^[a-z0-9-]+(\.[a-z0-9-]+)+/i.test(raw.replace(/^https?:\/\//i, ""));
    const hasSpace = /\s/.test(raw);
    let target: string;
    let useTor = false;
    if (isOnion) {
      target = /^https?:\/\//i.test(raw) ? raw : `http://${raw}`;
      useTor = true;
    } else if (!looksUrl || hasSpace) {
      target = `https://duckduckgo.com/?q=${encodeURIComponent(raw)}`;
    } else {
      target = /^https?:\/\//i.test(raw) ? raw : `https://${raw}`;
    }
    if (torMode && !isOnion) useTor = false; // Tor toggle only affects .onion here
    setUrl(target);
    actBusyRef.current = true;
    setBusy(true);
    setError("");
    try {
      if (useTor || (torMode && isOnion)) {
        const out = await api.executeTool(
          "tor_browse",
          { url: target },
          baseFor(backendRef.current)
        );
        const m = String(out ?? "").match(/data:image\/png;base64,[A-Za-z0-9+/=]+/);
        if (m) {
          setShot(`data:${m[0].split("data:")[1]}`);
          setShotAt(new Date().toLocaleTimeString());
          setShots((s) => s + 1);
          setStale(false);
          setError("");
        }
        addEntry({ kind: "tool", status: "ok", title: "tor_browse", detail: target });
      } else {
        await api.executeTool(
          "browser_open",
          { url: target },
          baseFor(backendRef.current)
        );
        // Pin the viewport so click coordinates map 1:1 onto 1280x720.
        try {
          await api.executeTool(
            "browser_viewport",
            { width: VIEW_W, height: VIEW_H },
            baseFor(backendRef.current)
          );
        } catch {
          /* viewport pin is best-effort */
        }
        addEntry({ kind: "tool", status: "ok", title: "browser_open", detail: target });
      }
      await refreshState();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Navigation failed");
    } finally {
      actBusyRef.current = false;
      setBusy(false);
      await shotNow();
    }
  };

  const onShotClick = async (e: ReactMouseEvent<HTMLImageElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    if (rect.width < 1 || rect.height < 1) return;
    const x = Math.min(
      VIEW_W,
      Math.max(0, Math.round(((e.clientX - rect.left) / rect.width) * VIEW_W))
    );
    const y = Math.min(
      VIEW_H,
      Math.max(0, Math.round(((e.clientY - rect.top) / rect.height) * VIEW_H))
    );
    setLastClick({ x, y });
    await act("browser_mouse_click", { x, y }, `click (${x}, ${y})`);
  };

  const typeIntoPage = async () => {
    const t = typeText;
    if (!t || actBusyRef.current) return;
    // key_type types into whatever element currently has focus in the page.
    // (browser_type needs a selector; this is the selector-free keyboard path.)
    await act("key_type", { text: t }, `typed ${t.length} char(s)`);
    setTypeText("");
  };

  const pressEnter = () => act("key_press", { key: "Enter" }, "pressed Enter");

  const extractText = async () => {
    if (actBusyRef.current) return;
    actBusyRef.current = true;
    setBusy(true);
    setError("");
    try {
      const out = await api.executeTool(
        "browser_tab_text",
        {},
        baseFor(backendRef.current)
      );
      setPageText(String(out ?? ""));
      setShowText(true);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Extract failed");
    } finally {
      actBusyRef.current = false;
      setBusy(false);
    }
  };

  // Live egress location of the selected backend — what /api/ip really says.
  useEffect(() => {
    let alive = true;
    setIp(null);
    api
      .ipInfo(baseFor(backend))
      .then((d) => {
        if (alive) setIp(d);
      })
      .catch(() => {
        if (alive) setIp(null);
      });
    return () => {
      alive = false;
    };
  }, [backend]);

  // First frame + tab state when the view opens / backend changes.
  useEffect(() => {
    if (conn !== "online") return;
    void shotNow();
    void refreshState();
  }, [backend, conn, shotNow, refreshState]);

  // ~2s poll loop while the view is open.
  useEffect(() => {
    if (!pollOn || conn !== "online") return;
    let alive = true;
    let timer = 0;
    const tick = async () => {
      if (!alive) return;
      await shotNow();
      if (alive) timer = window.setTimeout(tick, POLL_MS);
    };
    void tick();
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [pollOn, conn, shotNow]);

  if (conn !== "online")
    return <div className="empty">Backend offline — live browser unavailable.</div>;

  const locLabel = ip?.country
    ? `${ip.country}${ip.city ? ` · ${ip.city}` : ""}`
    : "locating…";

  return (
    <div className="lbrowser">
      <div className="card lb-toolbar">
        <div className="lb-row">
          <span className="lb-loc" title="Live egress location — GET /api/ip on the selected backend">
            <span className={`lb-dot ${ip ? "on" : ""}`} />
            <strong>{locLabel}</strong>
            {ip?.ip && <span className="mono lb-ip">{ip.ip}</span>}
          </span>
          <span className="lb-select" title="Browser location locked to Germany (user order)">
            🇩🇪 Germany
          </span>
          <label className="lb-poll">
            <input
              type="checkbox"
              checked={pollOn}
              onChange={(e) => setPollOn(e.target.checked)}
            />
            live · 2s
          </label>
          {busy && <span className="lb-busy">working…</span>}
        </div>

        <div className="lb-row">
          <button className="icon-btn" title="Back" onClick={() => act("browser_back", {}, "back")} disabled={busy}>
            ←
          </button>
          <button className="icon-btn" title="Forward" onClick={() => act("browser_forward", {}, "forward")} disabled={busy}>
            →
          </button>
          <button className="icon-btn" title="Reload" onClick={() => act("browser_refresh", {}, "reload")} disabled={busy}>
            ⟳
          </button>
          <input
            type="text"
            className="mono lb-urlbar"
            placeholder=https://… or search… (.onion = Tor)"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && go()}
          />
          <button className="btn small" onClick={go} disabled={busy}>
            Go
          </button>
          <button
            className={`btn small ${torMode ? "tor-on" : ""}`}
            title="Tor onion browsing — .onion sites open through Tor"
            onClick={() => setTorMode((v) => !v)}
            disabled={busy}
            style={torMode ? { background: "#7c3aed", color: "#fff" } : undefined}
          >
            🧅 Tor {torMode ? "ON" : "OFF"}
          </button>
        </div>

        <div className="lb-row">
          <input
            type="text"
            className="lb-typebox"
            placeholder="Type into the page… (click a field in the screenshot first)"
            value={typeText}
            onChange={(e) => setTypeText(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && typeIntoPage()}
          />
          <button className="btn ghost small" onClick={typeIntoPage} disabled={busy || !typeText}>
            Type
          </button>
          <button className="btn ghost small" onClick={pressEnter} disabled={busy} title="Press Enter in the page">
            ⏎
          </button>
          <button className="btn ghost small" onClick={extractText} disabled={busy}>
            Text
          </button>
        </div>

        {error && <div className="notice err lb-err">{error}</div>}
      </div>

      <div className="card lb-view">
        <div className="lb-viewhead">
          <span className="lb-title">{pageTitle || "Live view"}</span>
          {pageUrl && <span className="mono lb-pageurl">{pageUrl}</span>}
          {stale && <span className="pill warn">reconnecting…</span>}
          {lastClick && (
            <span className="mono lb-click" title="Last click sent to the page">
              click ({lastClick.x}, {lastClick.y})
            </span>
          )}
        </div>
        {shot ? (
          <img
            src={shot}
            alt="Agent browser — click to click the page"
            className="lb-shot"
            draggable={false}
            onDragStart={(e) => e.preventDefault()}
            onClick={onShotClick}
            title="Click anywhere to click the page at that point"
          />
        ) : (
          <div className="empty">
            No frame yet. Enter a URL and press Go.
            <br />
            <span style={{ fontSize: 11 }}>
              Requires the Playwright browser daemon on the agent host.
            </span>
          </div>
        )}
        <div className="lb-status">
          {shotAt && (
            <span>
              frame {shotAt} · {shots} this session
            </span>
          )}
          <span className="lb-hint">click the page to click · coordinate space 1280×720</span>
        </div>
      </div>

      {showText && (
        <div className="card">
          <h3>
            Page text
            <button
              className="btn ghost small"
              style={{ marginLeft: 12 }}
              onClick={() => setShowText(false)}
            >
              Hide
            </button>
          </h3>
          <pre className="code" style={{ maxHeight: 400 }}>
            {pageText || "(empty)"}
          </pre>
        </div>
      )}
    </div>
  );
}
