import { memo, useEffect, useRef, useState } from "react";
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
import UploadButton from "./UploadButton";

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
// sent by the agent display inline. File attachments render as download links.
function renderRich(text: string, keyPrefix: string) {
  // Match both ![alt](data:image/...) and [📎 name](data:...;base64,...)
  const re = /(!?)\[([^\]]*)\]\((data:[^;]+;base64,[A-Za-z0-9+/=]+)\)/g;
  const parts: ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  let k = 0;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) parts.push(<span key={`${keyPrefix}-t${k++}`}>{text.slice(last, m.index)}</span>);
    const isImg = m[1] === "!" && m[3].startsWith("data:image/");
    if (isImg) {
      parts.push(
        <img
          key={`${keyPrefix}-i${k++}`}
          src={m[3]}
          alt={m[2] || "screenshot"}
          style={{ maxWidth: "100%", borderRadius: 8, margin: "8px 0", border: "1px solid #333" }}
        />
      );
    } else {
      // File download link
      const fname = m[2].replace(/^📎\s*/, "") || "file";
      parts.push(
        <a
          key={`${keyPrefix}-f${k++}`}
          href={m[3]}
          download={fname}
          className="file-chip"
          style={{ margin: "6px 0", textDecoration: "none", color: "inherit" }}
        >
          📎 {fname}
        </a>
      );
    }
    last = m.index + m[0].length;
  }
  if (last < text.length) parts.push(<span key={`${keyPrefix}-t${k++}`}>{text.slice(last)}</span>);
  return parts.length > 0 ? parts : text;
}

// Downscale an image file to a compact JPEG data URI (max 1024px) so the
// chat payload stays small. Falls back to the raw data URI on any error.
function fileToDataUri(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("read failed"));
    reader.onload = () => {
      const img = new Image();
      img.onerror = () => reject(new Error("decode failed"));
      img.onload = () => {
        try {
          const MAX = 1024;
          const scale = Math.min(1, MAX / Math.max(img.width, img.height));
          const w = Math.max(1, Math.round(img.width * scale));
          const h = Math.max(1, Math.round(img.height * scale));
          const canvas = document.createElement("canvas");
          canvas.width = w;
          canvas.height = h;
          const ctx = canvas.getContext("2d");
          if (!ctx) {
            resolve(reader.result as string);
            return;
          }
          ctx.drawImage(img, 0, 0, w, h);
          resolve(canvas.toDataURL("image/jpeg", 0.85));
        } catch {
          resolve(reader.result as string);
        }
      };
      img.src = reader.result as string;
    };
    reader.readAsDataURL(file);
  });
}

// Memoized so typing in the input (which re-renders Chat on every keystroke)
// doesn't re-run the base64 regex + re-render every message each time.
const MessageView = memo(function MessageView({ m, index }: { m: ChatMsg; index: number }) {
  return (
    <div className={`msg ${m.role}`}>
      <div className="msg-head">
        <span className={`avatar ${m.role}`}>
          {m.role === "user" ? "Y" : m.role === "agent" ? "✦" : "!"}
        </span>
        <span className="meta">
          {m.role === "user" ? "You" : m.role === "agent" ? "Haley" : "Error"} · {fmtTime(m.time)}
        </span>
      </div>
      <div className="msg-body">{renderRich(m.text, `m${index}`)}</div>
      {m.toolCalls && m.toolCalls.length > 0 && (
        <details className="toolcalls">
          <summary>{m.toolCalls.length} tool call(s)</summary>
          <pre className="code">{JSON.stringify(m.toolCalls, null, 2)}</pre>
        </details>
      )}
    </div>
  );
});

export default function Chat({ conn }: { conn: ConnState }) {
  const [sessions, setSessions] = useState(listSessions);
  const [activeId, setActiveId] = useState<string | null>(() => listSessions()[0]?.id ?? null);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const [attach, setAttach] = useState<{name: string; mime: string; dataUri: string} | null>(null); // attached file
  const bottomRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  // keep the list fresh (cross-tab / store updates)
  useEffect(() => subscribeChats(() => setSessions(listSessions())), []);

  // Read the active session from the stable `sessions` state (not a fresh
  // localStorage parse per render) so message objects keep their identity
  // while typing and memoized rows + the scroll effect don't refire.
  const active = sessions.find((s) => s.id === activeId);
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
    if ((!prompt && !attach) || busy) return;
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
    // Embed the attached file: images render inline, other files as download links.
    // This reaches the agent as part of the prompt text.
    let fullPrompt = prompt;
    let titleSeed = prompt;
    if (attach) {
      const isImg = attach.mime.startsWith("image/");
      const tag = isImg
        ? `![${attach.name}](${attach.dataUri})`
        : `[📎 ${attach.name}](${attach.dataUri})`;
      fullPrompt = prompt ? `${prompt}\n\n${tag}` : tag;
      titleSeed = prompt || attach.name;
    }
    setInput("");
    setAttach(null);
    if (fileRef.current) fileRef.current.value = "";
    const withUser: ChatMsg[] = [...(sess?.messages ?? []), { role: "user", text: fullPrompt, time: Date.now() }];
    pushMsgs(sid, withUser);
    if (isFirst) {
      autoNameSession(sid, titleSeed); // the agent names the chat itself
      setSessions(listSessions());
    }
    setBusy(true);
    try {
      const res = await api.chat(fullPrompt, sid);
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
        title: titleSeed.slice(0, 80),
        detail: `reply ${String(reply).length} chars · ${toolCalls.length} tool call(s)`,
      });
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : "Request failed";
      const cur = getSession(sid)?.messages ?? withUser;
      pushMsgs(sid, [...cur, { role: "error", text: msg, time: Date.now() }]);
      addEntry({ kind: "chat", status: "error", title: titleSeed.slice(0, 80), detail: msg });
    } finally {
      setBusy(false);
    }
  };

  const onFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (!f) return;
    try {
      let dataUri: string;
      if (f.type.startsWith("image/")) {
        dataUri = await fileToDataUri(f);
      } else {
        // Non-image: read as base64 data URI (no downscaling).
        dataUri = await new Promise<string>((resolve, reject) => {
          const reader = new FileReader();
          reader.onerror = () => reject(new Error("read failed"));
          reader.onload = () => resolve(reader.result as string);
          reader.readAsDataURL(f);
        });
      }
      setAttach({name: f.name, mime: f.type || "application/octet-stream", dataUri});
    } catch {
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const clearAttach = () => {
    setAttach(null);
    if (fileRef.current) fileRef.current.value = "";
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
          <MessageView key={i} m={m} index={i} />
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
      {attach && (
        <div className="img-preview">
          {attach.mime.startsWith("image/") ? (
            <img src={attach.dataUri} alt={attach.name} />
          ) : (
            <span className="file-chip">📎 {attach.name}</span>
          )}
          <button className="img-remove" onClick={clearAttach} title="Remove file">
            ✕
          </button>
        </div>
      )}
      <div className="chat-input">
        <input
          ref={fileRef}
          type="file"
          style={{ display: "none" }}
          onChange={onFile}
        />
        <button
          className="btn attach-btn"
          onClick={() => fileRef.current?.click()}
          disabled={busy}
          title="Attach file"
        >
          📎
        </button>
        {/* UploadButton: server-side file upload (POST /api/upload) */}
        <UploadButton
          onUploaded={(r) =>
            setInput((prev) =>
              prev ? `${prev}\n[server file: ${r.filename} (${r.size} bytes)]` : `[server file: ${r.filename} (${r.size} bytes)]`
            )
          }
        />
        <textarea
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
        <button className="btn send-btn" onClick={send} disabled={busy || (!input.trim() && !attach)} title="Bhejo">
          ➤
        </button>
      </div>
    </div>
  );
}
