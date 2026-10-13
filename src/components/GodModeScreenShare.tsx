import React, { useState, useRef, useEffect } from 'react';
import { Eye, EyeOff, Loader2 } from 'lucide-react';

/**
 * GodModeScreenShare — Let the agent SEE your screen. REAL eyes, not fake.
 *
 * One tap → browser asks for screen permission → frames stream to the agent.
 * The agent sees what you type/do in real time.
 *
 * Backend: POST /tools/userscreen_frame { data_base64, meta }
 * Vision is ALWAYS-ON at the backend — no toggle can disable it there.
 * Here, YOU control sharing from your device (browser security requires it).
 */
interface Props {
  taskId?: string;
  apiBase?: string;
}

export const GodModeScreenShare: React.FC<Props> = ({
  taskId = 'system',
  apiBase = '',
}) => {
  const [sharing, setSharing] = useState(false);
  const [frameCount, setFrameCount] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const intervalRef = useRef<number | null>(null);

  const captureFrame = async () => {
    const video = videoRef.current;
    const canvas = canvasRef.current;
    if (!video || !canvas || video.readyState < 2) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    // Downscale for bandwidth: max 960px wide
    const scale = Math.min(1, 960 / video.videoWidth);
    canvas.width = Math.floor(video.videoWidth * scale);
    canvas.height = Math.floor(video.videoHeight * scale);
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
    const dataUrl = canvas.toDataURL('image/jpeg', 0.6);
    const base64 = dataUrl.split(',')[1];
    try {
      const res = await fetch(`${apiBase}/tools/userscreen_frame`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          task_id: taskId,
          data_base64: base64,
          meta: {
            width: canvas.width,
            height: canvas.height,
            ts: Date.now(),
            source: 'user-screenshare',
          },
        }),
      });
      const data = await res.json();
      if (data.ok || data.data?.ok) {
        setFrameCount((c) => c + 1);
      }
    } catch (e) {
      // Silent — don't spam on transient network blips
    }
  };

  const startSharing = async () => {
    setError(null);
    try {
      const stream = await navigator.mediaDevices.getDisplayMedia({
        video: { frameRate: { ideal: 2, max: 5 } },
        audio: false,
      });
      streamRef.current = stream;
      if (videoRef.current) {
        videoRef.current.srcObject = stream;
        await videoRef.current.play().catch(() => {});
      }
      // Stop sharing if user clicks browser's "Stop sharing"
      stream.getVideoTracks()[0].onended = () => stopSharing();
      // Capture a frame every 2 seconds
      intervalRef.current = window.setInterval(captureFrame, 2000);
      setSharing(true);
    } catch (e: any) {
      if (e?.name !== 'NotAllowedError') {
        setError(e?.message || 'Screen share failed');
      }
    }
  };

  const stopSharing = () => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    setSharing(false);
  };

  useEffect(() => {
    return () => stopSharing();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="godmode-screenshare">
      {/* Hidden video + canvas for frame capture */}
      <video ref={videoRef} style={{ display: 'none' }} muted playsInline />
      <canvas ref={canvasRef} style={{ display: 'none' }} />

      <button
        onClick={sharing ? stopSharing : startSharing}
        className={`flex items-center gap-2 px-4 py-2 rounded-lg font-medium transition-colors ${
          sharing
            ? 'bg-red-600 hover:bg-red-700 text-white'
            : 'bg-purple-600 hover:bg-purple-700 text-white'
        }`}
        title={sharing ? 'Stop letting the agent see your screen' : 'Let the agent SEE your screen (real eyes)'}
      >
        {sharing ? <EyeOff size={18} /> : <Eye size={18} />}
        {sharing ? 'Ankhein Band Karo' : 'Ankhein Khol Do 👀'}
      </button>

      {sharing && (
        <div className="mt-2 text-sm text-green-400 flex items-center gap-2">
          <span className="relative flex h-3 w-3">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-green-400 opacity-75" />
            <span className="relative inline-flex rounded-full h-3 w-3 bg-green-500" />
          </span>
          Agent dekh raha hai! ({frameCount} frames)
        </div>
      )}
      {error && <div className="mt-2 text-sm text-red-400">{error}</div>}
      <div className="mt-1 text-xs text-gray-400">
        🔒 Backend vision HAMESHA ON hai — koi button isko band nahi kar sakta.
        Tum sirf apni screen share karna start/stop karte ho.
      </div>
    </div>
  );
};

export default GodModeScreenShare;
