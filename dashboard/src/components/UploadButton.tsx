import { useRef, useState } from "react";
import { uploadFile, ApiError, type UploadResult } from "../api/client";

type Phase = "idle" | "uploading" | "done" | "error";

/**
 * Visible Upload button: opens a file picker and POSTs the file to
 * POST /api/upload as multipart/form-data. Shows progress / success /
 * error inline next to the button.
 *
 * Props:
 *   onUploaded — optional callback receiving the server response
 *                 ({ ok, filename, size, path }) so the parent (e.g. Chat)
 *                 can reference the uploaded file.
 */
export default function UploadButton({ onUploaded }: { onUploaded?: (r: UploadResult) => void }) {
  const input = useRef<HTMLInputElement | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [note, setNote] = useState("");

  const pick = async (f: File) => {
    setPhase("uploading");
    setNote(f.name);
    try {
      const r = await uploadFile(f);
      setPhase("done");
      setNote(`${r.filename} (${fmtSize(r.size)}) uploaded`);
      onUploaded?.(r);
      window.setTimeout(() => {
        setPhase("idle");
        setNote("");
      }, 5000);
    } catch (e) {
      setPhase("error");
      setNote(e instanceof ApiError ? e.message : "Upload failed");
    }
  };

  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
      <button
        className="btn ghost small"
        onClick={() => input.current?.click()}
        disabled={phase === "uploading"}
        title="Upload a file to the agent server (25MB max)"
      >
        {phase === "uploading" ? "Uploading…" : "⬆ Upload"}
      </button>
      <input
        ref={input}
        type="file"
        style={{ display: "none" }}
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) void pick(f);
          e.target.value = "";
        }}
      />
      {note && (
        <span
          style={{
            fontSize: 12,
            color: phase === "error" ? "#ff6b6b" : phase === "done" ? "#3ddc84" : "var(--muted)",
          }}
        >
          {phase === "done" ? "✓ " : phase === "error" ? "⚠ " : ""}
          {note}
        </span>
      )}
    </span>
  );
}

function fmtSize(n: number): string {
  if (n >= 1048576) return `${(n / 1048576).toFixed(1)} MB`;
  if (n >= 1024) return `${Math.round(n / 1024)} KB`;
  return `${n} B`;
}
