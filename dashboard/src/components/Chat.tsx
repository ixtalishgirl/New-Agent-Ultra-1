import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { api, ApiError } from "../api/client";
import { addEntry } from "../store/history";
import type { ConnState } from "./Connection";

interface Msg {
  role: "user" | "agent" | "error";
  text: string;
  time: number;
  toolCalls?: Array<Record<string, unknown>>;
}

function fmtTime(t: number) {
  return new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
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
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [msgs, busy]);

  const send = async () => {
    const prompt = input.trim();
    if (!prompt || busy) return;
    if (conn === "offline") {
      setMsgs((m) => [...m, { role: "error", text: "Backend offline — cannot send.", time: Date.now() }]);
      return;
    }
    setInput("");
    setMsgs((m) => [...m, { role: "user", text: prompt, time: Date.now() }]);
    setBusy(true);
    try {
      const res = await api.chat(prompt);
      const reply =
        res.reply ||
        (res as { response?: string }).response ||
        JSON.stringify(res, null, 2).slice(0, 4000);
      const toolCalls = (res.tool_calls as Array<Record<string, unknown>>) || [];
      setMsgs((m) => [...m, { role: "agent", text: String(reply), time: Date.now(), toolCalls }]);
      addEntry({
        kind: "chat",
        status: res.success === false ? "error" : "ok",
        title: prompt.slice(0, 80),
        detail: `reply ${String(reply).length} chars · ${toolCalls.length} tool call(s)`,
      });
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : "Request failed";
      setMsgs((m) => [...m, { role: "error", text: msg, time: Date.now() }]);
      addEntry({ kind: "chat", status: "error", title: prompt.slice(0, 80), detail: msg });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="chat-wrap">
      <div className="msgs">
        {msgs.length === 0 && (
          <div className="empty">
            <p>No messages yet.</p>
            <p style={{ fontSize: 12 }}>Send a task to the agent — e.g. "list files in the workspace".</p>
          </div>
        )}
        {msgs.map((m, i) => (
          <div key={i} className={`msg ${m.role}`}>
            <div className="msg-head">
              <span className={`avatar ${m.role}`}>
                {m.role === "user" ? "Y" : m.role === "agent" ? "A" : "!"}
              </span>
              <span className="meta">
                {m.role === "user" ? "You" : m.role === "agent" ? "Agent" : "Error"} · {fmtTime(m.time)}
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
            <span style={{ marginLeft: 8 }}>Agent working</span>
          </div>
        )}
        <div ref={bottomRef} />
      </div>
      <div className="chat-input">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
          placeholder={conn === "offline" ? "Backend offline…" : "Type a task… (Enter to send)"}
          disabled={busy}
        />
        <button className="btn" onClick={send} disabled={busy || !input.trim()}>
          Send
        </button>
      </div>
    </div>
  );
}
