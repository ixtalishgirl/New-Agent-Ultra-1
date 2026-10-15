/**
 * Local activity log — every chat run and tool execution is recorded here.
 * TaskMonitor, History and Approvals all read from this store.
 * Persisted in localStorage; live updates via the 'storage' event + a
 * lightweight subscription so all mounted components refresh.
 */

export type EntryKind = "chat" | "tool" | "approval";
export type EntryStatus = "ok" | "error" | "pending";

export interface LogEntry {
  id: string;
  kind: EntryKind;
  status: EntryStatus;
  title: string;
  detail?: string;
  time: number;
}

const LS_KEY = "nau1.activity_log";
const MAX = 500;

type Listener = () => void;
const listeners = new Set<Listener>();

function read(): LogEntry[] {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return [];
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
}

function write(entries: LogEntry[]) {
  localStorage.setItem(LS_KEY, JSON.stringify(entries.slice(0, MAX)));
  listeners.forEach((l) => l());
}

export function subscribe(fn: Listener): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function getLog(): LogEntry[] {
  return read();
}

export function addEntry(e: Omit<LogEntry, "id" | "time">): LogEntry {
  const entry: LogEntry = {
    ...e,
    id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    time: Date.now(),
  };
  write([entry, ...read()]);
  return entry;
}

export function updateEntry(id: string, patch: Partial<LogEntry>) {
  write(read().map((e) => (e.id === id ? { ...e, ...patch } : e)));
}

export function clearLog() {
  write([]);
}

// Cross-tab refresh
if (typeof window !== "undefined") {
  window.addEventListener("storage", (ev) => {
    if (ev.key === LS_KEY) listeners.forEach((l) => l());
  });
}

/* ---------------- approvals ---------------- */

export interface Approval {
  id: string;
  tool: string;
  args: Record<string, unknown>;
  reason: string;
  time: number;
  state: "pending" | "approved" | "rejected";
}

const LS_APPR = "nau1.approvals";

function readAppr(): Approval[] {
  try {
    const raw = localStorage.getItem(LS_APPR);
    const arr = raw ? JSON.parse(raw) : [];
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
}

function writeAppr(a: Approval[]) {
  localStorage.setItem(LS_APPR, JSON.stringify(a.slice(0, 200)));
  listeners.forEach((l) => l());
}

export function getApprovals(): Approval[] {
  return readAppr();
}

export function addApproval(tool: string, args: Record<string, unknown>, reason: string): Approval {
  const a: Approval = {
    id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    tool,
    args,
    reason,
    time: Date.now(),
    state: "pending",
  };
  writeAppr([a, ...readAppr()]);
  addEntry({ kind: "approval", status: "pending", title: `Approval requested: ${tool}`, detail: reason });
  return a;
}

export function setApprovalState(id: string, state: "approved" | "rejected") {
  writeAppr(readAppr().map((a) => (a.id === id ? { ...a, state } : a)));
}

export function clearApprovals() {
  writeAppr([]);
}

if (typeof window !== "undefined") {
  window.addEventListener("storage", (ev) => {
    if (ev.key === LS_APPR) listeners.forEach((l) => l());
  });
}
