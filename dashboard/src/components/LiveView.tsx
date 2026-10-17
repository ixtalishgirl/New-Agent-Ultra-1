import { useEffect, useRef } from "react";

/**
 * LiveView — view-only live browser screen.
 * No buttons, no URL bar, no options. The AGENT controls the browser
 * via chat ("ye website kholo"); the user just watches the live page.
 */
export default function LiveView({ conn }: { conn: string }) {
  const imgRef = useRef<HTMLImageElement>(null);
  const base = conn === "offline" ? "" : conn;
  const streamUrl = `${base}/api/browser/live`;

  useEffect(() => {
    const img = imgRef.current;
    if (!img) return;
    let t: ReturnType<typeof setTimeout>;
    img.onerror = () => {
      t = setTimeout(() => {
        if (imgRef.current) imgRef.current.src = `${streamUrl}?t=${Date.now()}`;
      }, 3000);
    };
    return () => clearTimeout(t);
  }, [streamUrl]);

  return (
    <div style={{ height: "100%", background: "#000", overflow: "hidden" }}>
      <img
        ref={imgRef}
        src={streamUrl}
        alt="Agent ka live browser"
        draggable={false}
        style={{
          width: "100%",
          height: "100%",
          objectFit: "contain",
          userSelect: "none",
        }}
      />
    </div>
  );
}
