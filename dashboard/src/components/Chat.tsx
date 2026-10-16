import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { api, ApiError } from "../api/client";
import { addEntry } from "../store/history";
import {
  listSessions,
  getSession,
  createSession,
  deleteSession,
  saveMessages,
  autoNameSession,
  subscribe as subscribeChats,
  type ChatMsg,
} from "../store/chats";
import type { ConnState } from "./Connection";

function fmtTime(t: number) {
  return new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function fmtDate(t: number) {
  const d = new Date(t);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return fmtTime(t);
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

// Render markdown images with data: URIs as real <img> tags so screenshots
// sent by the agent display inline. Other text renders as-is.
function renderRich(text: string, keyPrefix: string) {
  const re = /!\[([^\]]*)\]\((data:image\/[a-zA-Z+]+;base64,[A-Za-z0-9+/=]+)\)/g;
  const parts: ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  let k = 0;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) parts.push(<span key={`${keyPrefix}-t${k++}`}>{text.slice(last, m.index)}</span>);
    parts.push(
      <img
        key={`${keyPrefix}-i${k++}`}
        src={m[2]}
        alt={m[1] || "screenshot"}
        style={{ maxWidth: "100%", borderRadius: 8, margin: "8px 0", border: "1px solid #333" }}
      />
    );
    last = m.index + m[0].length;
  }
  if (last < text.length) parts.push(<span key={`${keyPrefix}-t${k++}`}>{text.slice(last)}</span>);
  return parts.length > 0 ? parts : text;
}

export default function Chat({ conn }: { conn: ConnState }) {
  const [sessions, setSessions] = useState(listSessions);
  const [activeId, setActiveId] = useState<string | null>(() => listSessions()[0]?.id ?? null);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // auto-grow the message box as the user types
  useEffect(() => {
    const el = inputRef.current;
    if (el) {
      el.style.height = "auto";
      el.style.height = Math.min(el.scrollHeight, 160) + "px";
    }
  }, [input]);

  // auto-grow the message box as the user types
  useEffect(() => {
    const el = inputRef.current;
    if (el) {
      el.style.height = "auto";
      el.style.height = Math.min(el.scrollHeight, 160) + "px";
    }
  }, [input]);

  // keep the list fresh (cross-tab / store updates)
  useEffect(() => subscribeChats(() => setSessions(listSessions())), []);

  const active = activeId ? getSession(activeId) : undefined;
  const msgs: ChatMsg[] = active?.messages ?? [];

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [msgs, busy, activeId]);

  const newChat = () => {
    const s = createSession();
    setSessions(listSessions());
    setActiveId(s.id);
    setDrawer(false);
  };

  const switchChat = (id: string) => {
    setActiveId(id);
    setDrawer(false);
  };

  const removeChat = (id: string) => {
    if (!window.confirm("Delete this chat?")) return;
    const rest = deleteSession(id);
    setSessions(rest);
    if (activeId === id) setActiveId(rest[0]?.id ?? null);
  };

  const pushMsgs = (id: string, next: ChatMsg[]) => {
    saveMessages(id, next);
    setSessions(listSessions());
  };

  const send = async () => {
    const prompt = input.trim();
    if (!prompt || busy) return;
    let sid = activeId;
    if (!sid) {
      sid = createSession().id;
      setSessions(listSessions());
      setActiveId(sid);
    }
    if (conn === "offline") {
      pushMsgs(sid, [...(getSession(sid)?.messages ?? []), { role: "error", text: "Backend offline — cannot send.", time: Date.now() }]);
      return;
    }
    const sess = getSession(sid);
    const isFirst = (sess?.messages.length ?? 0) === 0;
    setInput("");
    const withUser: ChatMsg[] = [...(sess?.messages ?? []), { role: "user", text: prompt, time: Date.now() }];
    pushMsgs(sid, withUser);
    if (isFirst) {
      autoNameSession(sid, prompt); // the agent names the chat itself
      setSessions(listSessions());
    }
    setBusy(true);
    try {
      const res = await api.chat(prompt);
      // The backend returns {success, final_text, iterations, ...} —
      // show ONLY her words, never the raw technical wrapper.
      const reply =
        (typeof res.final_text === "string" && res.final_text.trim()) ||
        res.reply ||
        (res as { response?: string }).response ||
        "…";
      const toolCalls = (res.tool_calls as Array<Record<string, unknown>>) || [];
      const cur = getSession(sid)?.messages ?? withUser;
      pushMsgs(sid, [...cur, { role: "agent", text: String(reply), time: Date.now(), toolCalls }]);
      addEntry({
        kind: "chat",
        status: res.success === false ? "error" : "ok",
        title: prompt.slice(0, 80),
        detail: `reply ${String(reply).length} chars · ${toolCalls.length} tool call(s)`,
      });
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : "Request failed";
      const cur = getSession(sid)?.messages ?? withUser;
      pushMsgs(sid, [...cur, { role: "error", text: msg, time: Date.now() }]);
      addEntry({ kind: "chat", status: "error", title: prompt.slice(0, 80), detail: msg });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="chat-wrap">
      {/* chat header: sessions drawer toggle, auto title, new chat (+) */}
      <div className="chat-topbar">
        <button className="icon-btn" onClick={() => setDrawer((d) => !d)} title="Chats">
          ☰
        </button>
        <div className="chat-title" title={active?.title ?? "New chat"}>
          {active?.title ?? "New chat"}
        </div>
        <button className="icon-btn new-chat" onClick={newChat} title="New chat">
          ＋
        </button>
      </div>

      {/* sessions drawer */}
      {drawer && (
        <div className="chat-drawer">
          <button className="btn small new-chat-btn" onClick={newChat}>
            ＋ New chat
          </button>
          <div className="chat-list">
            {sessions.length === 0 && <div className="empty small">No chats yet.</div>}
            {sessions.map((s) => (
              <div key={s.id} className={`chat-item ${s.id === activeId ? "active" : ""}`}>
                <button className="chat-item-main" onClick={() => switchChat(s.id)} title={s.title}>
                  <span className="chat-item-title">{s.title}</span>
                  <span className="chat-item-meta">
                    {s.messages.length} msg · {fmtDate(s.updatedAt)}
                  </span>
                </button>
                <button
                  className="icon-btn danger-ghost"
                  title="Delete chat"
                  onClick={() => removeChat(s.id)}
                >
                  🗑
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="msgs">
        {msgs.length === 0 && (
          <div className="empty">
            <p>👋 Start a new conversation.</p>
            <p style={{ fontSize: 12 }}>Send a task to the agent — e.g. "list files in the workspace".</p>
          </div>
        )}
        {msgs.map((m, i) => (
          <div key={i} className={`msg ${m.role}`}>
            <div className="msg-head">
              <span className={`avatar ${m.role}`}>
                {m.role === "user" ? "Y" : m.role === "agent" ? "✦" : "!"}
              </span>
              <span className="meta">
                {m.role === "user" ? "You" : m.role === "agent" ? "Haley" : "Error"} · {fmtTime(m.time)}
              </span>
            </div>
            <div className="msg-body">{renderRich(m.text, `m${i}`)}</div>
            {m.toolCalls && m.toolCalls.length > 0 && (
              <details className="toolcalls">
                <summary>{m.toolCalls.length} tool call(s)</summary>
                <pre className="code">{JSON.stringify(m.toolCalls, null, 2)}</pre>
              </details>
            )}
          </div>
        ))}
        {busy && (
          <div className="typing">
            <span className="tdot" />
            <span className="tdot" />
            <span className="tdot" />
            <span style={{ marginLeft: 8 }}>Haley working</span>
          </div>
        )}
        <div ref={bottomRef} />
      </div>
      <div className="chat-input">
        <textarea
          ref={inputRef}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
          placeholder={conn === "offline" ? "Backend offline…" : "Haley se kuch kaho… (Enter dabao)"}
          disabled={busy}
          rows={1}
        />
        <button className="btn send-btn" onClick={send} disabled={busy || !input.trim()} title="Bhejo">
          ➤
        </button>
      </div>
    </div>
  );
}
