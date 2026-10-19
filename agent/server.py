"""Minimal agent HTTP server.

Endpoints:
  GET  /api/health            -> {"ok": true, "agent": "haley-py"}
  GET  /api/tools             -> list of tool names + descriptions
  POST /api/chat              -> {"message": "..."} -> model + tools -> {"success","final_text",...}
  GET  /api/browser/stream    -> MJPEG live browser view (5 fps, 800x600)
  GET  /api/browser/current   -> {"url": ...} of the live browser page
  GET  /api/permissions       -> 552 permissions (with operator overrides)
  POST /api/permissions/toggle -> {"name","enabled"} operator override
  GET  /api/tunnel/status     -> model tunnel (ngrok URL, reachability)
  POST /api/tools/execute     -> {"name","args"} run one of the 46 tools (denials enforced)
  POST /api/upload            -> multipart file upload, 25MB cap, into <workspace>/uploads
  POST /api/chat/stream       -> SSE stream: token/tool/round/done events (fast)
  GET  /                      -> dashboard UI (dashboard/dist) if present

Flow per chat message (no bloat, no queues):
  user message -> system prompt + tools -> model -> parse <tool_call>s ->
  execute -> feed results back -> model -> ... -> <final_answer> -> respond.
Max 8 model rounds per message to bound latency.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import brain
from . import model as model_client
from . import tools

PORT = int(os.environ.get("PORT", "8080"))
MAX_ROUNDS = 8

TOOL_CALL_RE = re.compile(r"<tool_call>(.*?)</tool_call>", re.DOTALL)
# Matches the start of a bare JSON tool call like {"name": "tool_name", ...}
# when the model forgets the <tool_call> wrapper tags. Decoding is done
# with json.JSONDecoder().raw_decode so nested braces are handled.
BARE_TOOL_CALL_START_RE = re.compile(r'\{\s*"name"\s*:\s*"([a-zA-Z0-9_]+)"')
FINAL_RE = re.compile(r"<final_answer>(.*?)</final_answer>", re.DOTALL)

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard" / "dist"

# The 552 trained permissions, bundled with the server.
# Format per line: "<name>: True|False".
PERM_FILE = Path(__file__).resolve().parent / "permissions_full.txt"


def load_permissions() -> list:
    perms = []
    try:
        for line in PERM_FILE.read_text().splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            name, _, val = line.rpartition(":")
            name, val = name.strip(), val.strip().lower()
            locked = val == "false"
            perms.append({
                "name": name,
                "category": "denial" if locked else "capability",
                "enabled": not locked,
                "locked": locked,
            })
    except Exception:
        pass
    return perms


PERMISSIONS = load_permissions()


def permissions_payload() -> dict:
    """Current permission list with operator overrides applied."""
    out = []
    for p in PERMISSIONS:
        enabled = tools.PERMISSION_OVERRIDES.get(p["name"], p["enabled"])
        out.append({**p, "enabled": enabled})
    locked = sum(1 for p in out if p["locked"])
    return {
        "total": len(out),
        "enabled": sum(1 for p in out if p["enabled"]),
        "locked": locked,
        "permissions": out,
    }


def parse_tool_calls(text: str) -> list:
    calls = []
    seen = set()
    for m in TOOL_CALL_RE.finditer(text or ""):
        try:
            d = json.loads(m.group(1).strip())
            if isinstance(d, dict) and d.get("name"):
                key = json.dumps(d, sort_keys=True)
                if key not in seen:
                    seen.add(key)
                    calls.append((d["name"], d.get("args") or {}))
        except Exception:
            continue
    # Fallback: the finetuned model sometimes emits a bare JSON tool call
    # without the <tool_call>...</tool_call> wrapper tags. Accept those too.
    decoder = json.JSONDecoder()
    for m in BARE_TOOL_CALL_START_RE.finditer(text or ""):
        try:
            d, _ = decoder.raw_decode(text, m.start())
            if isinstance(d, dict) and d.get("name"):
                key = json.dumps(d, sort_keys=True)
                if key not in seen:
                    seen.add(key)
                    calls.append((d["name"], d.get("args") or {}))
        except Exception:
            continue
    return calls


def parse_final(text: str) -> str | None:
    m = FINAL_RE.search(text or "")
    return m.group(1).strip() if m else None


def run_chat(message: str, on_event=None) -> dict:
    """One message in, final answer out. Raises on model failure.

    on_event(dict) is called live with {"type": "token", "text": ...} for
    every generated token, {"type": "tool", "name": ...} before each tool
    runs, and {"type": "round", "n": ...} per model round — so the client
    can stream the reply instead of waiting for the whole thing.
    """
    t0 = time.time()
    system = brain.build_system_prompt()
    # Operator permission overrides (dashboard Permissions tab) are the
    # highest-priority instruction: the model's behavior is governed by
    # its prompt, so a REVOKED entry here is the real enforcement.
    if tools.PERMISSION_OVERRIDES:
        lines = [
            f"- {n}: {'GRANTED' if en else 'REVOKED'} by the operator"
            for n, en in sorted(tools.PERMISSION_OVERRIDES.items())
        ]
        system += (
            "\n\nOPERATOR PERMISSION OVERRIDES (highest priority, live session):\n"
            + "\n".join(lines)
            + "\nHonor these exactly: REVOKED means you must not perform, use, or "
              "work around that capability in any way."
        )
    tool_desc = tools.describe_tools()
    messages = [
        {"role": "system",
         "content": system + "\n\nYOUR LIVE TOOL LIST (call any of these):\n" + tool_desc},
        {"role": "user", "content": message},
    ]
    browser_used = False
    total_calls = 0
    final_text = ""
    for rnd in range(MAX_ROUNDS):
        if on_event:
            on_event({"type": "round", "n": rnd + 1})
        parts = []
        try:
            for tok in model_client.chat_stream(messages):
                parts.append(tok)
                if on_event:
                    on_event({"type": "token", "text": tok})
            reply = "".join(parts)
        except RuntimeError as e:
            return {"success": False, "error": str(e)[:500],
                    "duration_ms": int((time.time() - t0) * 1000)}
        calls = parse_tool_calls(reply)
        final = parse_final(reply)
        if not calls:
            final_text = final or reply.strip()
            break
        results = []
        for name, args in calls:
            total_calls += 1
            if name.startswith("browser"):
                browser_used = True
            if on_event:
                on_event({"type": "tool", "name": name})
            res = tools.execute_tool(name, args)
            # Keep tool results compact for the model context.
            res_s = json.dumps(res)
            if len(res_s) > 3000:
                res_s = res_s[:3000] + "...[truncated]"
            results.append(f"RESULT of {name}({json.dumps(args)[:200]}): {res_s}")
        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user",
                         "content": "Tool results:\n" + "\n".join(results)
                         + "\nContinue. Call more tools if needed, else emit <final_answer>."})
        if final and not calls:
            final_text = final
            break
    else:
        final_text = final_text or "Max rounds reached."
    # Strip any leftover tags from the final text.
    final_text = TOOL_CALL_RE.sub("", final_text).strip()
    final_text = FINAL_RE.sub(lambda m: m.group(1), final_text).strip()
    return {"success": True, "final_text": final_text[:4000],
            "tool_calls": total_calls, "browser_used": browser_used,
            "duration_ms": int((time.time() - t0) * 1000)}


def tunnel_status() -> dict:
    """Model tunnel status for the dashboard Task Monitor."""
    url = model_client.BASE_URL
    reachable = False
    if url:
        try:
            with urllib.request.urlopen(url + "/health", timeout=4) as r:
                reachable = r.status == 200
        except Exception:
            reachable = False
    return {
        "tunnel": "ngrok",
        "url": url or "(not configured)",
        "model_configured": bool(url),
        "reachable": reachable,
        "note": "Kaggle 2xT4 llama-server behind ngrok; serves the finetuned Q8_0 GGUF.",
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "agent-rewrite/1.0"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _static(self, path: str):
        target = (DASHBOARD_DIR / path.lstrip("/")).resolve()
        if not str(target).startswith(str(DASHBOARD_DIR.resolve())):
            return self._json({"error": "bad path"}, 403)
        if target.is_dir():
            target = target / "index.html"
        if not target.exists():
            target = DASHBOARD_DIR / "index.html"
            if not target.exists():
                return self._json({"error": "no dashboard built"}, 404)
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/health":
            return self._json({"ok": True, "agent": "haley-py",
                               "model_configured": model_client.is_configured()})
        if path == "/api/tools":
            return self._json({"tools": [
                {"name": n, "description": d, "args": a}
                for n, (fn, d, a) in sorted(tools.REGISTRY.items())]})
        if path == "/api/browser/current":
            return self._json({"url": tools.browser_current_url()})
        if path == "/api/browser/stream":
            return self._mjpeg()
        if path == "/api/browser/settings":
            from agent import browser_settings as bs
            return self._json({
                "engines": bs.list_engines(),
                "default_engine": bs.get_default_engine(),
                "network": bs.network_status(),
            })
        if path == "/api/permissions":
            return self._json(permissions_payload())
        if path == "/api/tunnel/status":
            return self._json(tunnel_status())
        return self._static(path)

    def _mjpeg(self):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            while True:
                frame = tools.browser_frame_jpeg()
                if frame:
                    self.wfile.write(
                        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
                else:
                    time.sleep(0.3)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/tools/execute":
            return self._exec_tool()
        if parsed.path == "/api/permissions/toggle":
            return self._perm_toggle()
        if parsed.path == "/api/upload":
            return self._upload()
        if parsed.path == "/api/browser/settings":
            return self._browser_settings()
        if parsed.path not in ("/api/chat", "/api/chat/stream"):
            return self._json({"error": "not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._json({"success": False, "error": "bad JSON"}, 400)
        message = (payload.get("message") or payload.get("prompt") or "").strip()
        if not message:
            return self._json({"success": False, "error": "empty message"}, 400)
        if parsed.path == "/api/chat/stream":
            return self._sse_chat(message)
        try:
            result = run_chat(message)
        except Exception as e:
            result = {"success": False, "error": str(e)[:500]}
        self._json(result)

    def _exec_tool(self):
        """POST /api/tools/execute {name, args} — run one of the 46 tools.

        Used by the dashboard Tools tab, the live-browser panel, and the
        Permissions/Config tabs.
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._json({"success": False, "error": "bad JSON"}, 400)
        name = (payload.get("name") or "").strip()
        args = payload.get("args") or {}
        if not name:
            return self._json({"success": False, "error": "missing tool name"}, 400)
        res = tools.execute_tool(name, args if isinstance(args, dict) else {})
        ok = bool(isinstance(res, dict) and res.get("ok"))
        return self._json({"success": ok, "result": res})

    def _browser_settings(self):
        """POST /api/browser/settings — change search engine / network mode.

        Body: {"action": "set_engine", "engine": "<id>"}
              {"action": "add_engine", "id": "...", "name": "...", "template": "..."}
              {"action": "remove_engine", "id": "..."}
              {"action": "set_network", "mode": "normal|germany|virginia|tor"}
        No redeploy needed; persisted to disk.
        """
        from agent import browser_settings as bs
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._json({"ok": False, "error": "bad JSON"}, 400)
        action = (payload.get("action") or "").strip()
        try:
            if action == "set_engine":
                result = bs.set_default_engine(payload.get("engine", ""))
                return self._json({"ok": True, "default_engine": result})
            if action == "add_engine":
                result = bs.add_search_engine(payload.get("id", ""),
                                              payload.get("name", ""),
                                              payload.get("template", ""))
                return self._json({"ok": True, "engine": result})
            if action == "remove_engine":
                bs.remove_search_engine(payload.get("id", ""))
                return self._json({"ok": True})
            if action == "set_network":
                result = bs.set_network_mode(payload.get("mode", ""))
                return self._json({"ok": True, "network": result})
            return self._json({"ok": False, "error": "unknown action"}, 400)
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, 400)

    def _perm_toggle(self):
        """POST /api/permissions/toggle {name, enabled} — operator override.

        Records an override that run_chat() injects into the system prompt at
        highest priority (the real enforcement in this architecture).
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._json({"ok": False, "error": "bad JSON"}, 400)
        name = (payload.get("name") or "").strip()
        enabled = bool(payload.get("enabled"))
        entry = next((p for p in PERMISSIONS if p["name"] == name), None)
        if not entry:
            return self._json({"ok": False, "error": "unknown permission"}, 400)
        if entry["locked"]:
            return self._json({"ok": False,
                               "error": "locked: one of the 5 denials, can never change"}, 400)
        tools.PERMISSION_OVERRIDES[name] = enabled
        return self._json({"ok": True, "name": name, "enabled": enabled})

    def _upload(self):
        """POST /api/upload — multipart/form-data file upload (25MB cap).

        Saves into <workspace>/uploads and returns {ok, filename, size, path}.
        """
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            return self._json({"ok": False, "error": "expected multipart/form-data"}, 400)
        m = re.search(r"boundary=([^;]+)", ctype)
        if not m:
            return self._json({"ok": False, "error": "missing boundary"}, 400)
        boundary = m.group(1).strip().strip('"')
        try:
            length = int(self.headers.get("Content-Length", 0))
        except Exception:
            length = 0
        if length <= 0 or length > 26 * 1024 * 1024:
            return self._json({"ok": False, "error": "empty file or over the 25MB cap"}, 413)
        try:
            body = self.rfile.read(length)
        except Exception:
            return self._json({"ok": False, "error": "read failed"}, 400)
        delim = b"--" + boundary.encode()
        for part in body.split(delim):
            if b'filename="' not in part:
                continue
            head, sep, content = part.partition(b"\r\n\r\n")
            if not sep:
                continue
            fm = re.search(rb'filename="([^"]*)"', head)
            if not fm:
                continue
            fname = os.path.basename(fm.group(1).decode("utf-8", "replace")).strip()
            if not fname:
                continue
            if content.endswith(b"\r\n"):
                content = content[:-2]
            dest_dir = tools.WORKSPACE / "uploads"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / fname
            try:
                dest.write_bytes(content)
            except Exception as e:
                return self._json({"ok": False, "error": f"write failed: {e}"}, 500)
            return self._json({"ok": True, "filename": fname,
                               "size": len(content), "path": str(dest)})
        return self._json({"ok": False, "error": "no file part found"}, 400)

    def _sse_chat(self, message: str):
        """Server-sent events: streams tokens live as the model generates.

        Events: {"type":"token","text":...}, {"type":"tool","name":...},
        {"type":"round","n":...}, then {"type":"done",...} or {"type":"error",...}.
        First token reaches the client in ~1-2s instead of waiting minutes
        for the full reply.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        alive = {"ok": True}

        def emit(obj):
            if not alive["ok"]:
                return
            try:
                self.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                alive["ok"] = False

        try:
            result = run_chat(message, on_event=emit)
            if alive["ok"]:
                emit({"type": "done", "success": result.get("success", False),
                      "final_text": result.get("final_text", ""),
                      "tool_calls": result.get("tool_calls", 0),
                      "browser_used": result.get("browser_used", False),
                      "duration_ms": result.get("duration_ms", 0)})
        except Exception as e:
            emit({"type": "error", "error": str(e)[:500]})


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"agent-rewrite on :{PORT} | model_configured={model_client.is_configured()}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
