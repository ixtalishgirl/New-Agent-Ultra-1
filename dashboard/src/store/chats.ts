/**
 * Multi-chat sessions for the Agent Chat tab.
 * Each session has its own message list, an auto-generated title,
 * and can be created (+) or deleted. Persisted in localStorage.
 */

export interface ChatMsg {
  role: "user" | "agent" | "error";
  text: string;
  time: number;
  toolCalls?: Array<Record<string, unknown>>;
}

export interface ChatSession {
  id: string;
  title: string;
  autoTitle: boolean; // true until the agent/user renames it
  messages: ChatMsg[];
  createdAt: number;
  updatedAt: number;
}

const LS_KEY = "nau1.chat_sessions";
const MAX_SESSIONS = 50;

type Listener = () => void;
const listeners = new Set<Listener>();

function uid(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function read(): ChatSession[] {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return [];
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
}

function write(sessions: ChatSession[]) {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(sessions.slice(0, MAX_SESSIONS)));
  } catch {
    /* storage full — keep in-memory only */
  }
  listeners.forEach((l) => l());
}

export function subscribe(fn: Listener): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function listSessions(): ChatSession[] {
  return read().sort((a, b) => b.updatedAt - a.updatedAt);
}

export function getSession(id: string): ChatSession | undefined {
  return read().find((s) => s.id === id);
}

/** Create a new empty chat and return it. */
export function createSession(): ChatSession {
  const s: ChatSession = {
    id: uid(),
    title: "New chat",
    autoTitle: true,
    messages: [],
    createdAt: Date.now(),
    updatedAt: Date.now(),
  };
  write([s, ...read()]);
  return s;
}

/** Delete a chat session. Returns the remaining sessions. */
export function deleteSession(id: string): ChatSession[] {
  const rest = read().filter((s) => s.id !== id);
  write(rest);
  return rest.sort((a, b) => b.updatedAt - a.updatedAt);
}

/** Replace the message list of a session (bumps updatedAt). */
export function saveMessages(id: string, messages: ChatMsg[]) {
  write(
    read().map((s) =>
      s.id === id ? { ...s, messages, updatedAt: Date.now() } : s
    )
  );
}

/**
 * Auto-name a chat from its first user message — the agent names the chat
 * itself so the user never has to. Takes the first ~6 meaningful words.
 */
export function autoNameSession(id: string, firstUserText: string) {
  const words = firstUserText
    .replace(/[\r\n]+/g, " ")
    .trim()
    .split(/\s+/)
    .slice(0, 6)
    .join(" ");
  const title = words.length > 42 ? words.slice(0, 42).trimEnd() + "…" : words;
  write(
    read().map((s) =>
      s.id === id && s.autoTitle
        ? { ...s, title: title || "New chat", updatedAt: Date.now() }
        : s
    )
  );
}

/** Manual rename (marks autoTitle false). */
export function renameSession(id: string, title: string) {
  const t = title.trim().slice(0, 60) || "Untitled";
  write(read().map((s) => (s.id === id ? { ...s, title: t, autoTitle: false } : s)));
}

if (typeof window !== "undefined") {
  window.addEventListener("storage", (ev) => {
    if (ev.key === LS_KEY) listeners.forEach((l) => l());
  });
}
