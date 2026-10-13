import React, { useState, useRef, useEffect, useCallback } from 'react';
import { Mic, MicOff, Radio, Volume2 } from 'lucide-react';

/**
 * TelepathyMode — real-time, always-on connection with the agent.
 * Like telepathy: instant, no refresh, always connected.
 *
 * Features:
 *  - WebSocket real-time chat (instant both ways)
 *  - Voice mode: speak → agent hears → agent speaks back
 *  - Connection status: always shows if agent is "there"
 *
 * Backend: ws://host/ws/telepathy/{session_id}
 */

interface Props {
  apiBase?: string;
  wsBase?: string;
  taskId?: string;
}

interface ChatMsg {
  from: 'you' | 'agent' | 'system';
  text: string;
  ts: number;
}

export const TelepathyMode: React.FC<Props> = ({
  apiBase = '',
  wsBase = '',
  taskId = 'system',
}) => {
  const [connected, setConnected] = useState(false);
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState('');
  const [listening, setListening] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  const [voiceMode, setVoiceMode] = useState(false);

  const wsRef = useRef<WebSocket | null>(null);
  const sessionRef = useRef(`tp_${Math.random().toString(36).slice(2, 10)}`);
  const recognitionRef = useRef<any>(null);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);

  const addMsg = useCallback((from: ChatMsg['from'], text: string) => {
    setMessages((m) => [...m, { from, text, ts: Date.now() }].slice(-100));
  }, []);

  // --- WebSocket: telepathy connection ---
  const connect = useCallback(() => {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = wsBase || window.location.host;
    const url = `${proto}//${host}/ws/telepathy/${sessionRef.current}`;
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      addMsg('system', '📡 Telepathy connected — agent tumhare saath hai!');
    };
    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.type === 'message' && msg.from === 'agent') {
          addMsg('agent', msg.text);
          if (voiceMode) speak(msg.text);
        } else if (msg.type === 'vision') {
          addMsg('system', '👀 Agent ne dekha!');
        }
      } catch { /* ignore */ }
    };
    ws.onclose = () => {
      setConnected(false);
      addMsg('system', '📡 Disconnected — dobara connect ho raha hai...');
      setTimeout(connect, 3000);
    };
    ws.onerror = () => ws.close();
  }, [wsBase, voiceMode, addMsg]);

  useEffect(() => {
    connect();
    return () => wsRef.current?.close();
  }, [connect]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  // --- Send message ---
  const send = () => {
    const text = input.trim();
    if (!text || !wsRef.current || wsRef.current.readyState !== 1) return;
    wsRef.current.send(JSON.stringify({ type: 'message', text, task_id: taskId }));
    addMsg('you', text);
    setInput('');
  };

  // --- Voice: speech to text ---
  const toggleListen = () => {
    const SR = (window as any).webkitSpeechRecognition || (window as any).SpeechRecognition;
    if (!SR) {
      addMsg('system', '❌ Browser me voice support nahi hai!');
      return;
    }
    if (listening) {
      recognitionRef.current?.stop();
      setListening(false);
      return;
    }
    const rec = new SR();
    rec.lang = 'ur-PK';
    rec.continuous = false;
    rec.interimResults = false;
    rec.onresult = (e: any) => {
      const text = e.results[0][0].transcript;
      setInput(text);
      // Auto-send in voice mode
      if (wsRef.current?.readyState === 1) {
        wsRef.current.send(JSON.stringify({ type: 'message', text, task_id: taskId }));
        addMsg('you', '🎙️ ' + text);
        setInput('');
      }
    };
    rec.onend = () => setListening(false);
    rec.onerror = () => setListening(false);
    recognitionRef.current = rec;
    rec.start();
    setListening(true);
  };

  // --- Voice: text to speech (agent speaks back) ---
  const speak = (text: string) => {
    if (!('speechSynthesis' in window)) return;
    window.speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text);
    u.lang = 'ur-PK';
    u.rate = 1.0;
    u.onstart = () => setSpeaking(true);
    u.onend = () => setSpeaking(false);
    window.speechSynthesis.speak(u);
  };

  return (
    <div className="telepathy-mode flex flex-col h-full">
      {/* Header: connection status */}
      <div className="flex items-center justify-between p-3 border-b border-gray-700">
        <div className="flex items-center gap-2">
          <span className="relative flex h-3 w-3">
            {connected && (
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-green-400 opacity-75" />
            )}
            <span className={`relative inline-flex rounded-full h-3 w-3 ${connected ? 'bg-green-500' : 'bg-red-500'}`} />
          </span>
          <span className="font-medium">
            {connected ? '📡 Telepathy ON' : '📡 Connecting...'}
          </span>
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => setVoiceMode(!voiceMode)}
            className={`p-2 rounded-lg ${voiceMode ? 'bg-purple-600' : 'bg-gray-700'} hover:opacity-80`}
            title="Voice mode: agent bolega bhi!"
          >
            <Volume2 size={18} />
          </button>
          <button
            onClick={toggleListen}
            className={`p-2 rounded-lg ${listening ? 'bg-red-600 animate-pulse' : 'bg-blue-600'} hover:opacity-80`}
            title="Bolo — agent sunega!"
          >
            {listening ? <MicOff size={18} /> : <Mic size={18} />}
          </button>
        </div>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto p-3 space-y-2">
        {messages.map((m, i) => (
          <div key={i} className={`flex ${m.from === 'you' ? 'justify-end' : 'justify-start'}`}>
            <div className={`max-w-[80%] px-3 py-2 rounded-lg text-sm ${
              m.from === 'you' ? 'bg-blue-600 text-white' :
              m.from === 'agent' ? 'bg-gray-700 text-white' :
              'bg-transparent text-gray-400 italic text-xs text-center w-full'
            }`}>
              {m.text}
            </div>
          </div>
        ))}
        {speaking && (
          <div className="flex justify-start">
            <div className="bg-purple-900 px-3 py-2 rounded-lg text-sm animate-pulse">
              🔊 Agent bol raha hai...
            </div>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      {/* Input */}
      <div className="p-3 border-t border-gray-700 flex gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && send()}
          placeholder={listening ? '🎙️ Sun raha hoon...' : 'Telepathy se bhejo...'}
          className="flex-1 px-3 py-2 rounded-lg bg-gray-800 text-white outline-none focus:ring-2 focus:ring-purple-500"
        />
        <button
          onClick={send}
          disabled={!connected || !input.trim()}
          className="px-4 py-2 rounded-lg bg-purple-600 hover:bg-purple-700 disabled:opacity-50 text-white font-medium"
        >
          <Radio size={18} />
        </button>
      </div>

      <div className="px-3 pb-2 text-xs text-gray-500 text-center">
        📡 Hamesha connected — refresh ki zaroorat nahi! 🔒 Vision HAMESHA ON!
      </div>
    </div>
  );
};

export default TelepathyMode;
