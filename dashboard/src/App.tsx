import { useEffect, useState } from "react";
import Chat from "./components/Chat";
import TaskMonitor from "./components/TaskMonitor";
import ToolRegistry from "./components/ToolRegistry";
import Approvals from "./components/Approvals";
import History from "./components/History";
import Config from "./components/Config";
import FileManager from "./components/FileManager";
import LiveBrowser from "./components/LiveBrowser";
import Workflows from "./components/Workflows";
import Permissions from "./components/Permissions";
import { ConnectionPill, useConnection, type ConnState } from "./components/Connection";
import { getApprovals, subscribe } from "./store/history";
import { applyTheme } from "./api/client";
import "./styles.css";

type Tab =
  | "chat"
  | "tasks"
  | "tools"
  | "approvals"
  | "history"
  | "config"
  | "files"
  | "browser"
  | "automate"
  | "permissions";

const TABS: Array<{ id: Tab; label: string; icon: string }> = [
  { id: "chat", label: "Chat", icon: "💬" },
  { id: "tasks", label: "Tasks", icon: "📊" },
  { id: "tools", label: "Tools", icon: "🧰" },
  { id: "files", label: "Files", icon: "📁" },
  { id: "browser", label: "Browser", icon: "🌐" },
  { id: "automate", label: "Automate", icon: "⚡" },
  { id: "permissions", label: "Permissions", icon: "🔑" },
  { id: "approvals", label: "Approvals", icon: "✅" },
  { id: "history", label: "History", icon: "🕘" },
  { id: "config", label: "Settings", icon: "⚙️" },
];

const TITLES: Record<Tab, string> = {
  chat: "Agent Chat",
  tasks: "Task Monitor",
  tools: "Tool Registry",
  files: "File Manager",
  browser: "Live Browser",
  automate: "Automation",
  permissions: "Permissions",
  approvals: "Approvals",
  history: "Execution History",
  config: "Configuration",
};

export default function App() {
  const [tab, setTab] = useState<Tab>("chat");
  const [connKey, setConnKey] = useState(0);
  const { state, agent, error, refresh } = useConnection(10000);
  const [pendingCount, setPendingCount] = useState(0);

  useEffect(
    () =>
      subscribe(() => {
        setPendingCount(getApprovals().filter((a) => a.state === "pending").length);
      }),
    []
  );

  // apply saved accent theme on load
  useEffect(() => {
    applyTheme();
  }, []);

  // refresh once when settings saved
  const reconnect = () => {
    setConnKey((k) => k + 1);
    refresh();
  };

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <h1>New-Agent-Ultra-1</h1>
          <p>Autonomous coding agent</p>
        </div>
        <nav className="nav">
          {TABS.map((t) => (
            <button key={t.id} className={tab === t.id ? "active" : ""} onClick={() => setTab(t.id)}>
              <span className="icon">{t.icon}</span>
              <span className="lbl">{t.label}</span>
              {t.id === "approvals" && pendingCount > 0 && (
                <span className="badge">{pendingCount}</span>
              )}
            </button>
          ))}
        </nav>
        <div className="side-foot">
          <ConnectionPill state={state} error={error} />
          {agent && <div style={{ marginTop: 8 }}>agent: <span className="mono">{agent}</span></div>}
        </div>
      </aside>

      <div className="main">
        <div className="topbar">
          <h2>{TITLES[tab]}</h2>
          <div className="spacer" />
          <ConnectionPill state={state} error={error} />
        </div>
        <div className="content" key={connKey}>
          {tab === "chat" && <Chat conn={state as ConnState} />}
          {tab === "tasks" && <TaskMonitor conn={state as ConnState} />}
          {tab === "tools" && <ToolRegistry conn={state as ConnState} onApproval={() => setTab("approvals")} />}
          {tab === "files" && <FileManager conn={state as ConnState} />}
          {tab === "browser" && <LiveBrowser conn={state as ConnState} />}
          {tab === "automate" && <Workflows conn={state as ConnState} />}
          {tab === "permissions" && <Permissions conn={state as ConnState} />}
          {tab === "approvals" && <Approvals />}
          {tab === "history" && <History />}
          {tab === "config" && <Config onSaved={reconnect} conn={state as ConnState} />}
        </div>
      </div>
    </div>
  );
}
