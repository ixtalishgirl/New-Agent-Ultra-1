import { useEffect, useMemo, useState } from "react";
import { api, ApiError, type PermissionEntry, type PermissionsData } from "../api/client";
import type { ConnState } from "./Connection";

function Toggle({ on, locked, busy, onFlip }: { on: boolean; locked: boolean; busy: boolean; onFlip: () => void }) {
  if (locked) {
    return (
      <span className="pill err" title="Locked denial — can never be granted">
        🔒 locked
      </span>
    );
  }
  return (
    <button
      className={`switch ${on ? "on" : ""} ${busy ? "busy" : ""}`}
      onClick={onFlip}
      disabled={busy}
      title={on ? "Click to revoke (asks for confirmation)" : "Click to grant (asks for confirmation)"}
      aria-label={on ? "Revoke permission" : "Grant permission"}
    >
      <span className="knob" />
    </button>
  );
}

/**
 * Permission toggles — every agent permission with an on/off switch.
 * Flips go through the agent's own perm_grant / perm_revoke tools.
 * The 5 locked denials render as LOCKED with no toggle.
 */
export default function PermissionToggles({ conn, compact }: { conn: ConnState; compact?: boolean }) {
  const [data, setData] = useState<PermissionsData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [catFilter, setCatFilter] = useState("all");
  const [stateFilter, setStateFilter] = useState<"all" | "on" | "off" | "locked">("all");
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [notice, setNotice] = useState("");

  const load = () => {
    if (conn !== "online") {
      setLoading(false);
      return;
    }
    setLoading(true);
    api
      .permissions()
      .then((d) => {
        setData(d);
        setError("");
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : "Failed to load permissions"))
      .finally(() => setLoading(false));
  };

  useEffect(load, [conn]);

  const categories = useMemo(() => {
    if (!data) return [];
    const m = new Map<string, number>();
    for (const p of data.permissions) m.set(p.category, (m.get(p.category) || 0) + 1);
    return [...m.entries()].sort((a, b) => b[1] - a[1]);
  }, [data]);

  const filtered = useMemo(() => {
    if (!data) return [];
    const q = query.trim().toLowerCase();
    return data.permissions.filter((p) => {
      if (catFilter !== "all" && p.category !== catFilter) return false;
      if (stateFilter === "on" && !(p.enabled && !p.locked)) return false;
      if (stateFilter === "off" && !(!p.enabled && !p.locked)) return false;
      if (stateFilter === "locked" && !p.locked) return false;
      if (q && !p.name.toLowerCase().includes(q) && !p.category.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [data, query, catFilter, stateFilter]);

  const flip = async (p: PermissionEntry) => {
    if (p.locked || busy[p.name]) return;
    // HARD RULE: a permission may only change when the user deliberately taps
    // THIS exact toggle and confirms. Never on load, never in bulk, never by default.
    const action = p.enabled ? "REVOKE (turn OFF)" : "GRANT (turn ON)";
    if (
      !confirm(
        `${action} the permission "${p.name}"?\n\nAll permissions are ON by default. Only change this if you are sure.`
      )
    )
      return;
    setBusy((b) => ({ ...b, [p.name]: true }));
    setNotice("");
    try {
      await api.togglePermission(p.name, !p.enabled);
      setData((d) =>
        d
          ? {
              ...d,
              permissions: d.permissions.map((x) =>
                x.name === p.name ? { ...x, enabled: !x.enabled } : x
              ),
              enabled: d.enabled + (!p.enabled ? 1 : -1),
            }
          : d
      );
    } catch (e) {
      setNotice(`Failed to flip ${p.name}: ${e instanceof ApiError ? e.message : "error"}`);
    } finally {
      setBusy((b) => ({ ...b, [p.name]: false }));
    }
  };

  if (conn !== "online") return <div className="empty">Backend offline — permissions unavailable.</div>;
  if (loading) return <div className="empty">Loading permissions…</div>;
  if (error)
    return (
      <div className="empty" style={{ color: "var(--red)" }}>
        {error}
      </div>
    );
  if (!data) return <div className="empty">No permission data.</div>;

  return (
    <div>
      {!compact && (
        <div className="grid3" style={{ marginBottom: 16 }}>
          <div className="card stat-card" style={{ marginBottom: 0 }}>
            <div className="stat">
              <span className="v">{data.total}</span>
              <span className="k">Total permissions</span>
            </div>
          </div>
          <div className="card stat-card" style={{ marginBottom: 0 }}>
            <div className="stat">
              <span className="v" style={{ color: "var(--green)" }}>
                {data.enabled}
              </span>
              <span className="k">Enabled</span>
            </div>
          </div>
          <div className="card stat-card" style={{ marginBottom: 0 }}>
            <div className="stat">
              <span className="v" style={{ color: "var(--red)" }}>
                {data.locked}
              </span>
              <span className="k">Locked denials</span>
            </div>
          </div>
        </div>
      )}

      <div className="toolbar">
        <div className="search" style={{ marginBottom: 0, flex: 1, minWidth: 200 }}>
          <input
            type="text"
            placeholder={`Search ${data.total} permissions…`}
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
        <select value={stateFilter} onChange={(e) => setStateFilter(e.target.value as typeof stateFilter)} style={{ width: "auto" }}>
          <option value="all">All states</option>
          <option value="on">On</option>
          <option value="off">Off</option>
          <option value="locked">Locked</option>
        </select>
        <button className="btn ghost small" onClick={load}>
          ⟳ Refresh
        </button>
      </div>

      {notice && (
        <div className="notice err" style={{ marginBottom: 12 }}>
          {notice}
        </div>
      )}
      <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 10 }}>
        Showing {filtered.length} of {data.total} · flips go through the agent's perm_grant / perm_revoke tools
      </div>

      <div className="perm-list">
        {filtered.map((p) => (
          <div key={p.name} className={`perm-row ${p.locked ? "is-locked" : ""} ${p.enabled && !p.locked ? "is-on" : ""}`}>
            <div className="perm-info">
              <div className="mono perm-name">{p.name}</div>
              <div className="perm-cat">{p.category}{p.description ? ` · ${p.description}` : ""}</div>
            </div>
            <Toggle on={p.enabled} locked={p.locked} busy={!!busy[p.name]} onFlip={() => flip(p)} />
          </div>
        ))}
      </div>
      {filtered.length === 0 && <div className="empty">No permissions match the filters.</div>}
    </div>
  );
}
