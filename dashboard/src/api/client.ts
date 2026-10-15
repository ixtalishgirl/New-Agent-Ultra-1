/**
 * API client for the New-Agent-Ultra-1 Python backend.
 *
 * Backend (agent/api/server.py, stdlib http.server):
 *   GET  /api/health         — no auth            → { ok, agent }
 *   POST /api/chat          — x-app-password      → { success, ... }
 *   GET  /api/tools         — x-app-password      → { tools: [{name, description, status}] }
 *   POST /api/tools/execute — x-app-password      → { success, result }
 *   GET  /api/tunnel/status — x-app-password      → { tunnel, note }
 *
 * Auth: `x-app-password` header (preferred) or `?password=` query.
 * Offline is handled gracefully — every call throws ApiError with a
 * machine-readable kind; the UI shows connection status from /api/health.
 */

export type ApiErrorKind = "offline" | "unauthorized" | "server" | "bad-request";

export class ApiError extends Error {
  kind: ApiErrorKind;
  status: number;
  constructor(kind: ApiErrorKind, status: number, message: string) {
    super(message);
    this.kind = kind;
    this.status = status;
  }
}

export interface ToolInfo {
  name: string;
  description: string;
  status: string;
}

export interface ChatResult {
  success: boolean;
  reply?: string;
  tool_calls?: Array<Record<string, unknown>>;
  error?: string;
  [k: string]: unknown;
}

const LS_URL = "nau1.api_url";
const LS_PW = "nau1.api_password";

export function getStoredUrl(): string {
  return localStorage.getItem(LS_URL) || "";
}

export function getStoredPassword(): string {
  return localStorage.getItem(LS_PW) || "";
}

export function storeSettings(url: string, password: string) {
  localStorage.setItem(LS_URL, url.trim());
  localStorage.setItem(LS_PW, password);
}

/** Base URL: explicit setting, else same-origin (served by backend). */
export function resolveBaseUrl(): string {
  const stored = getStoredUrl();
  if (stored) return stored.replace(/\/+$/, "");
  return window.location.origin;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const base = resolveBaseUrl();
  const password = getStoredPassword();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(init.headers as Record<string, string> | undefined),
  };
  if (password) headers["x-app-password"] = password;

  let res: Response;
  try {
    res = await fetch(base + path, { ...init, headers });
  } catch {
    throw new ApiError("offline", 0, "Backend unreachable — is the agent server running?");
  }

  let data: unknown = null;
  try {
    data = await res.json();
  } catch {
    /* non-JSON body */
  }

  if (res.status === 401) {
    throw new ApiError("unauthorized", 401, "Unauthorized — check the app password in Settings.");
  }
  if (res.status >= 500) {
    const msg = (data as { error?: string } | null)?.error || `Server error ${res.status}`;
    throw new ApiError("server", res.status, msg);
  }
  if (res.status >= 400) {
    const msg = (data as { error?: string } | null)?.error || `Request failed (${res.status})`;
    throw new ApiError("bad-request", res.status, msg);
  }
  return data as T;
}

export const api = {
  async health(): Promise<{ ok: boolean; agent?: string }> {
    return request("/api/health");
  },
  async chat(prompt: string): Promise<ChatResult> {
    return request<ChatResult>("/api/chat", {
      method: "POST",
      body: JSON.stringify({ prompt }),
    });
  },
  async listTools(): Promise<ToolInfo[]> {
    const data = await request<{ tools: ToolInfo[] }>("/api/tools");
    return data.tools || [];
  },
  async executeTool(name: string, args: Record<string, unknown>): Promise<unknown> {
    const data = await request<{ success: boolean; result?: unknown; error?: string }>(
      "/api/tools/execute",
      { method: "POST", body: JSON.stringify({ name, args }) }
    );
    if (!data.success) throw new ApiError("server", 500, data.error || "Tool execution failed");
    return data.result;
  },
  async tunnelStatus(): Promise<Record<string, unknown>> {
    return request("/api/tunnel/status");
  },
};

/** Tools that change state — routed through the Approvals tab first. */
export const DESTRUCTIVE_TOOLS = new Set([
  "shell",
  "python",
  "pip",
  "self_modify",
  "tool_create",
  "file_write",
  "text_replace",
  "archive_extract",
]);

/** Rough category for the registry view, derived from the tool name. */
export function toolCategory(name: string): string {
  if (name.startsWith("browser_") || name === "web_search" || name === "germany_fetch") return "Web & Browser";
  if (name.startsWith("file_") || name.startsWith("text_")) return "Files & Text";
  if (name.startsWith("git_")) return "Git";
  if (name.startsWith("image_") || name.startsWith("audio_") || name.startsWith("video_")) return "Media";
  if (name.startsWith("archive_") || name.startsWith("gzip_")) return "Archives";
  if (name === "shell" || name === "python" || name === "pip" || name === "sysinfo" || name === "disk_usage") return "System";
  if (name === "http_get" || name === "http_post" || name === "dns_lookup" || name === "port_check") return "Network";
  if (name === "supabase_query") return "Database";
  if (name === "self_modify" || name === "tool_create") return "Meta";
  if (name === "json_validate" || name === "csv_stats") return "Data";
  return "Other";
}
