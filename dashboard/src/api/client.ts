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
  final_text?: string;
  tool_calls?: Array<Record<string, unknown>>;
  error?: string;
  [k: string]: unknown;
}

export interface PermissionEntry {
  name: string;
  category: string;
  enabled: boolean;
  locked: boolean;
  description?: string;
}

export interface PermissionsData {
  total: number;
  enabled: number;
  locked: number;
  permissions: PermissionEntry[];
}

/**
 * Normalize the /api/permissions payload. The backend was rewritten to return
 * {total, enabled, locked, permissions:[{name, category, enabled, locked}]}.
 * Older builds returned {granted_count, denied_count, granted:[{id,category,text}], denials:[{id,description}]}.
 * This handles both so the dashboard works against either backend version.
 */
export function normalizePermissions(raw: unknown): PermissionsData {
  const d = raw as Record<string, unknown>;
  if (Array.isArray(d.permissions)) {
    const perms = (d.permissions as Array<Record<string, unknown>>).map((p) => ({
      name: String(p.name ?? ""),
      category: String(p.category ?? "other"),
      enabled: Boolean(p.enabled),
      locked: Boolean(p.locked),
      description: typeof p.description === "string" ? p.description : undefined,
    }));
    const locked = perms.filter((p) => p.locked).length;
    return {
      total: perms.length,
      enabled: perms.filter((p) => p.enabled).length,
      locked,
      permissions: perms,
    };
  }
  // legacy shape
  const granted = (d.granted as Array<{ id: number; category: string; text: string }> | undefined) || [];
  const denials = (d.denials as Array<{ id: string; description: string }> | undefined) || [];
  const perms: PermissionEntry[] = granted.map((g) => ({
    name: String(g.text ?? g.id),
    category: g.category || "other",
    enabled: true,
    locked: false,
  }));
  for (const x of denials) {
    perms.push({
      name: x.id,
      category: "denial",
      enabled: false,
      locked: true,
      description: x.description,
    });
  }
  return {
    total: perms.length,
    enabled: perms.filter((p) => p.enabled).length,
    locked: denials.length,
    permissions: perms,
  };
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

async function request<T>(path: string, init: RequestInit = {}, baseOverride?: string): Promise<T> {
  const base = (baseOverride || resolveBaseUrl()).replace(/\/+$/, "");
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

/** Live egress IP / geolocation of the backend (GET /api/ip, no auth). */
export interface IpInfo {
  ip?: string;
  country?: string;
  countryCode?: string;
  city?: string;
  region?: string;
}

export interface ChatStreamEvent {
  type: "token" | "tool" | "round" | "done" | "error";
  text?: string;
  name?: string;
  n?: number;
  success?: boolean;
  final_text?: string;
  reply?: string;
  tool_calls?: number;
  browser_used?: boolean;
  duration_ms?: number;
  error?: string;
}

/**
 * Streaming chat via POST /api/chat/stream (SSE). Tokens arrive live as the
 * model generates them — the first token typically shows in ~1-2s instead of
 * waiting minutes for the full reply. Resolves with the final done/error event.
 */
export function chatStream(
  prompt: string,
  onEvent: (e: ChatStreamEvent) => void,
  chatId?: string
): Promise<ChatStreamEvent> {
  return new Promise((resolve, reject) => {
    const base = resolveBaseUrl().replace(/\/+$/, "");
    const password = getStoredPassword();
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (password) headers["x-app-password"] = password;
    (async () => {
      let res: Response;
      try {
        res = await fetch(base + "/api/chat/stream", {
          method: "POST",
          headers,
          body: JSON.stringify(chatId ? { prompt, chat_id: chatId } : { prompt }),
        });
      } catch {
        reject(new ApiError("offline", 0, "Backend unreachable — is the agent server running?"));
        return;
      }
      if (!res.ok || !res.body) {
        reject(new ApiError("server", res.status, `Stream failed (${res.status})`));
        return;
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      let last: ChatStreamEvent = { type: "done", success: false };
      try {
        for (;;) {
          const { done: rd, value } = await reader.read();
          if (rd) break;
          buf += decoder.decode(value, { stream: true });
          let idx: number;
          while ((idx = buf.indexOf("\n\n")) >= 0) {
            const chunk = buf.slice(0, idx);
            buf = buf.slice(idx + 2);
            for (const line of chunk.split("\n")) {
              const t = line.trim();
              if (!t.startsWith("data:")) continue;
              const raw = t.slice(5).trim();
              if (!raw) continue;
              try {
                const ev = JSON.parse(raw) as ChatStreamEvent;
                onEvent(ev);
                if (ev.type === "done" || ev.type === "error") last = ev;
              } catch {
                /* partial chunk — wait for more */
              }
            }
          }
        }
      } catch (e) {
        reject(e instanceof Error ? e : new Error("Stream read failed"));
        return;
      }
      resolve(last);
    })();
  });
}

export const api = {
  async health(): Promise<{ ok: boolean; agent?: string }> {
    return request("/api/health");
  },
  /** Live egress location of the backend (or a selected backend via baseUrl). */
  async ipInfo(baseUrl?: string): Promise<IpInfo> {
    return request<IpInfo>("/api/ip", {}, baseUrl);
  },
  async chat(prompt: string, chatId?: string): Promise<ChatResult> {
    return request<ChatResult>("/api/chat", {
      method: "POST",
      // chatId namespaces server-side memory: each dashboard side-chat
      // gets its own conversation history ("main" when omitted).
      body: JSON.stringify(chatId ? { prompt, chat_id: chatId } : { prompt }),
    });
  },
  async listTools(): Promise<ToolInfo[]> {
    const data = await request<{ tools: ToolInfo[] }>("/api/tools");
    return data.tools || [];
  },
  async executeTool(name: string, args: Record<string, unknown>, baseUrl?: string): Promise<unknown> {
    const data = await request<{ success: boolean; result?: unknown; error?: string }>(
      "/api/tools/execute",
      { method: "POST", body: JSON.stringify({ name, args }) },
      baseUrl
    );
    if (!data.success) throw new ApiError("server", 500, data.error || "Tool execution failed");
    return data.result;
  },
  async tunnelStatus(): Promise<Record<string, unknown>> {
    return request("/api/tunnel/status");
  },
  async permissions(): Promise<PermissionsData> {
    const raw = await request<unknown>("/api/permissions");
    return normalizePermissions(raw);
  },
  /** Flip one permission via the operator-override endpoint.
   *  The 5 locked denials can never be flipped (the server refuses).
   *  Overrides are injected into the system prompt at highest priority. */
  async togglePermission(name: string, enable: boolean): Promise<string> {
    const out = await request<{ ok: boolean; error?: string }>("/api/permissions/toggle", {
      method: "POST",
      body: JSON.stringify({ name, enabled: enable }),
    });
    if (!out.ok) throw new ApiError("server", 500, out.error || "toggle failed");
    return "ok";
  },
  /** Master safe-mode switch. enabled=true re-asserts safe defaults (475 ON, 5 locked). */
  async setSafeMode(enabled: boolean): Promise<string> {
    const out = await this.executeTool("safe_mode", { enabled });
    return typeof out === "string" ? out : JSON.stringify(out);
  },
  /** Agent session info (pid, uptime, python, cwd). */
  async sessionInfo(): Promise<string> {
    const out = await this.executeTool("session_info", {});
    return typeof out === "string" ? out : JSON.stringify(out, null, 2);
  },
  /** Live browser screenshot as a data URI (PNG). Requires ?url=. */
  async browserShot(url: string, baseUrl?: string): Promise<{ data_uri: string; url: string }> {
    const base = (baseUrl || resolveBaseUrl()).replace(/\/+$/, "");
    const password = getStoredPassword();
    const headers: Record<string, string> = {};
    if (password) headers["x-app-password"] = password;
    let res: Response;
    try {
      res = await fetch(base + "/api/browser/shot?url=" + encodeURIComponent(url), { headers });
    } catch {
      throw new ApiError("offline", 0, "Backend unreachable — is the agent server running?");
    }
    let data: unknown = null;
    try {
      data = await res.json();
    } catch {
      /* non-JSON */
    }
    if (res.status === 401) throw new ApiError("unauthorized", 401, "Unauthorized — check the app password in Settings.");
    if (!res.ok) {
      const msg = (data as { error?: string } | null)?.error || `Screenshot failed (${res.status})`;
      throw new ApiError("server", res.status, msg);
    }
    return data as { data_uri: string; url: string };
  },
};

export interface UploadResult {
  ok: boolean;
  filename: string;
  size: number;
  path?: string;
}

/** Upload a file via POST /api/upload (multipart/form-data, 25MB server cap). */
export async function uploadFile(file: File, fieldName = "file"): Promise<UploadResult> {
  const base = resolveBaseUrl().replace(/\/+$/, "");
  const password = getStoredPassword();
  const form = new FormData();
  form.append(fieldName, file, file.name);
  const headers: Record<string, string> = {};
  if (password) headers["x-app-password"] = password;
  let res: Response;
  try {
    res = await fetch(base + "/api/upload", { method: "POST", headers, body: form });
  } catch {
    throw new ApiError("offline", 0, "Backend unreachable — is the agent server running?");
  }
  let data: unknown = null;
  try {
    data = await res.json();
  } catch {
    /* non-JSON */
  }
  if (res.status === 401) {
    throw new ApiError("unauthorized", 401, "Unauthorized — check the app password in Settings.");
  }
  if (res.status === 413) {
    throw new ApiError("bad-request", 413, "File too large — the server caps uploads at 25MB.");
  }
  if (!res.ok) {
    const msg = (data as { error?: string } | null)?.error || `Upload failed (${res.status})`;
    throw new ApiError(res.status >= 500 ? "server" : "bad-request", res.status, msg);
  }
  return data as UploadResult;
}

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
  if (name.startsWith("screen_") || name.startsWith("ui_") || name.startsWith("visual_") || name === "ocr_read" || name === "screenshot_analyze" || name === "live_screen_stream") return "Screen & Vision";
  if (name.startsWith("mouse_") || name.startsWith("touch_")) return "Mouse & Touch";
  if (name.startsWith("key_") || name.startsWith("clipboard_") || name === "unicode_type" || name === "text_select_all" || name === "layout_detect" || name === "input_method_switch") return "Keyboard & Clipboard";
  if (name.startsWith("file_") || name.startsWith("text_") || name.startsWith("dir_") || name === "trash_restore" || name === "filemgmt" || name === "archive_create" || name === "archive_extract") return "Files & Text";
  if (name.startsWith("git_")) return "Git";
  if (name.startsWith("image_") || name.startsWith("audio_") || name.startsWith("video_")) return "Media";
  if (name.startsWith("archive_") || name.startsWith("gzip_")) return "Archives";
  if (name === "shell" || name === "python" || name === "pip" || name === "sysinfo" || name === "disk_usage" || name.startsWith("process_") || name.startsWith("service_") || name === "health_check") return "System";
  if (name.startsWith("env_") || name === "dep_check") return "Environment";
  if (name === "http_get" || name === "http_post" || name === "dns_lookup" || name === "port_check" || name === "net_latency" || name === "api_request") return "Network";
  if (name === "supabase_query" || name.startsWith("db_") || name === "dataset_process" || name === "dataset_validate") return "Database";
  if (name === "self_modify" || name === "tool_create" || name.startsWith("tool_") || name === "code_generate" || name === "tool_discover") return "Meta & Tool Creation";
  if (name.startsWith("code_") || name === "api_scaffold" || name === "sdk_scaffold" || name === "repo_scaffold" || name === "docs_generate" || name === "code_complete" || name === "code_refactor" || name === "code_review" || name === "arch_analyze" || name === "repo_understand" || name === "multi_file_edit") return "Code Development";
  if (name.startsWith("bug_") || name === "stacktrace_analyze" || name === "log_analyze" || name === "root_cause" || name === "patch_generate" || name === "multi_file_fix" || name === "regression_check" || name === "fix_verify" || name === "test_failure_diagnose" || name === "memleak_detect" || name === "deadlock_detect" || name === "race_note" || name === "dep_conflicts" || name === "runtime_check" || name === "type_check" || name === "debugger_run") return "Debugging";
  if (name.startsWith("test_") || name === "coverage_run" || name === "load_test" || name === "stress_test" || name === "fuzz_run" || name === "api_test" || name === "e2e_note" || name === "ui_test" || name === "property_test" || name === "a11y_check" || name === "fail_repro" || name === "benchmark_run" || name === "benchmark_compare") return "Testing & QA";
  if (name.startsWith("game_")) return "Game Dev";
  if (name.startsWith("profile_") || name === "alloc_track" || name === "thread_analyze" || name === "complexity_estimate" || name === "perf_regression" || name === "optimize_suggest" || name === "optimize_verify" || name === "cache_note" || name === "gpu_note" || name === "render_note" || name === "concurrency_note") return "Performance";
  if (name.startsWith("ml_") || name === "model_route" || name === "rag_index" || name === "rag_query" || name === "embed_generate" || name === "vector_search" || name === "eval_dataset_gen" || name === "prompt_eval" || name === "quality_score" || name === "gpu_monitor" || name === "quantize_note" || name === "finetune_note" || name === "lora_note" || name === "infer_opt_note" || name === "model_version" || name === "ai_chat" || name === "ai_assistant" || name === "agent_message") return "AI & ML";
  if (name.startsWith("cicd") || name === "deploy_preview" || name === "uptime_check" || name === "iac_validate" || name === "k8s_validate" || name === "db_migrate_test" || name === "backup_verify" || name === "alert_emit" || name === "cloud_note" || name === "staging_note" || name === "container_note") return "Cloud & Infra";
  if (name.startsWith("sast") || name === "vuln_scan_deps" || name === "license_check" || name === "config_audit" || name === "perm_audit" || name === "auth_test" || name === "input_fuzz" || name === "dast_note" || name === "sec_regression" || name === "dlp_check" || name === "supply_chain_check" || name === "cred_protect" || name === "secret_detect") return "Security";
  if (name.startsWith("perm_") || name === "audit_log" || name === "audit_logging" || name === "approval_request" || name === "confirm_prompt" || name === "safe_mode" || name === "session_info" || name === "rate_limit_status" || name === "op_timeout" || name === "website_allowlist" || name === "file_access_rules" || name === "backup_create" || name === "restore_point" || name === "checkpoint_create" || name === "checkpoint_restore") return "Safety & Access";
  if (name.startsWith("memory_") || name.startsWith("macro_") || name.startsWith("workflow_") || name.startsWith("sched_") || name === "cron_add" || name === "task_plan" || name === "task_decompose" || name === "task_monitor" || name === "work_queue" || name === "parallel_run" || name === "loop_repeat" || name === "retry_backoff" || name === "branch_if" || name === "event_emit" || name === "progress_report" || name === "error_detect" || name === "failure_memory" || name === "gap_analyze" || name === "improve_hypothesis" || name === "branch_create" || name === "release_deploy" || name === "release_notes" || name === "kb_update" || name === "config_version" || name === "repro_build") return "Memory & Automation";
  if (name.startsWith("app_") || name === "window_resize_move" || name === "menu_navigate" || name === "dialog_handle" || name === "editor_control") return "Applications";
  if (name === "spawn_agent" || name === "agent_status" || name === "agent_result" || name === "agent_stop" || name === "fan_out" || name === "ssh_exec" || name === "tunnel_create" || name === "root_exec" || name === "prod_secret_read" || name === "prod_deploy" || name === "iam_note" || name === "firewall_note" || name === "self_perm_audit") return "High-Risk";
  if (name === "json_validate" || name === "csv_stats") return "Data";
  if (name === "webhook_register") return "Integrations";
  return "Other";
}

/* ---------------- theme (accent color) ---------------- */

const LS_ACCENT = "nau1.accent";
const LS_ACCENT2 = "nau1.accent2";

export const ACCENT_PRESETS: Array<{ name: string; a: string; b: string }> = [
  { name: "Blue", a: "#6e9bff", b: "#9d7bff" },
  { name: "Green", a: "#3ddc84", b: "#2bb673" },
  { name: "Cyan", a: "#38e1ff", b: "#2b9df4" },
  { name: "Orange", a: "#ffab4a", b: "#ff7a45" },
  { name: "Pink", a: "#ff6ec7", b: "#b967ff" },
  { name: "Red", a: "#ff6b6b", b: "#e04848" },
];

export function getAccent(): { a: string; b: string } {
  const a = localStorage.getItem(LS_ACCENT) || "#6e9bff";
  const b = localStorage.getItem(LS_ACCENT2) || "#9d7bff";
  return { a, b };
}

export function applyTheme(a?: string, b?: string) {
  const cur = getAccent();
  const accent = a || cur.a;
  const accent2 = b || cur.b;
  const root = document.documentElement.style;
  root.setProperty("--accent", accent);
  root.setProperty("--accent-2", accent2);
}

export function setAccent(a: string, b: string) {
  localStorage.setItem(LS_ACCENT, a);
  localStorage.setItem(LS_ACCENT2, b);
  applyTheme(a, b);
}
