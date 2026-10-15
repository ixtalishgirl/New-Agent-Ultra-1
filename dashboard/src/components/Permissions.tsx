import PermissionToggles from "./PermissionToggles";
import type { ConnState } from "./Connection";

/**
 * Permissions tab — the agent's full capability manifest with live toggles.
 * The 5 hard denials are locked and can never be granted.
 */
export default function Permissions({ conn }: { conn: ConnState }) {
  return (
    <div>
      <div className="card" style={{ borderColor: "rgba(255,107,107,0.35)", marginBottom: 16 }}>
        <h3>🔒 5 hard denials — locked forever, no toggle, no override</h3>
        <p style={{ color: "var(--muted)", fontSize: 13, margin: 0 }}>
          container_escape · boot_recovery · av_edr_disable · billing_fraud · exfiltration.
          Everything else is ON by default and can be flipped below.
        </p>
      </div>
      <PermissionToggles conn={conn} />
    </div>
  );
}
