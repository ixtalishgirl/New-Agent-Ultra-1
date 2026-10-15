import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";

/** Shared backend connection state, polled from /api/health. */
export type ConnState = "checking" | "online" | "offline";

export function useConnection(pollMs = 10000): { state: ConnState; agent: string; error: string; refresh: () => void } {
  const [state, setState] = useState<ConnState>("checking");
  const [agent, setAgent] = useState("");
  const [error, setError] = useState("");

  const check = async () => {
    try {
      const h = await api.health();
      setAgent(h.agent || "");
      setError("");
      setState("online");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Unreachable");
      setState("offline");
    }
  };

  useEffect(() => {
    check();
    const t = setInterval(check, pollMs);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return { state, agent, error, refresh: check };
}

export function ConnectionPill({ state, error }: { state: ConnState; error: string }) {
  const label = state === "online" ? "Connected" : state === "offline" ? "Offline" : "Checking";
  return (
    <span className={`conn ${state}`} title={state === "offline" ? error : undefined}>
      <span className="dot" />
      {label}
    </span>
  );
}
