# API Reference — Agent HTTP Server

> Base URL: `http://localhost:8000`
> Authentication: Bearer token via `Authorization: Bearer <token>` header
> or `?token=<token>` query parameter.
>
> Set `AGENT_API_TOKEN` env var to enable auth. If unset, server runs
> in open mode (development only).

---

## Table of Contents

1. [Health](#1-health)
2. [Chat](#2-chat)
3. [Tools](#3-tools)
4. [Planning](#4-planning)
5. [Sessions](#5-sessions)
6. [Goals](#6-goals)
7. [Statistics](#7-statistics)
8. [Error Responses](#8-error-responses)
9. [Rate Limiting](#9-rate-limiting)
10. [Examples](#10-examples)

---

## 1. Health

### `GET /api/health`

Health check. No authentication required.

**Response:**
```json
{
  "status": "ok",
  "version": "2.0.0",
  "uptime_seconds": 123.4,
  "tools_count": 45,
  "model": "unavailable"
}
```

**Example:**
```bash
curl http://localhost:8000/api/health
```

---

## 2. Chat

### `POST /api/chat`

Run a task through the agentic control loop.

**Authentication:** Required (Bearer token)

**Request:**
```json
{
  "message": "List all Python files",
  "session_id": "sess_abc123",
  "max_iterations": 10,
  "stream": false
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `message` | string | Yes | — | Task description |
| `session_id` | string | No | `"default"` | Session for memory |
| `max_iterations` | integer | No | `10` | Max ReAct iterations |
| `stream` | boolean | No | `false` | Stream responses (not yet) |

**Response:**
```json
{
  "reply": "Found 42 Python files...",
  "session_id": "sess_abc123",
  "tool_calls": [
    {"tool": "shell", "ok": true, "output": "..."}
  ],
  "iterations": 3,
  "duration_ms": 1250,
  "success": true
}
```

**Example:**
```bash
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -d '{
    "message": "What time is it?",
    "session_id": "demo"
  }'
```

---

## 3. Tools

### `GET /api/tools`

List all registered tools (central Tool Registry — includes all computer-control
tools: screen/vision, mouse, keyboard, live browser, file management, app
control, coding/tool-creation, memory/automation, access & safety).

**Authentication:** Required

**Query parameters:**
| Param | Description |
|-------|-------------|
| `page` | Page number (default: 1) |
| `per_page` | Items per page (default: 50) |

**Response:**
```json
{
  "tools": [
    {
      "name": "shell",
      "description": "Execute bash command",
      "version": "1.0.0",
      "timeout": 60
    }
  ],
  "count": 45
}
```

**Example:**
```bash
curl -H "Authorization: Bearer YOUR_TOKEN" \
  http://localhost:8000/api/tools
```

### `POST /api/tools/call`

Execute a tool directly.

**Authentication:** Required

**Request:**
```json
{
  "name": "shell",
  "args": {
    "command": "ls -la"
  },
  "timeout": 30
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Tool name |
| `args` | object | Yes | Tool arguments |
| `timeout` | integer | No | Override timeout (seconds) |

**Response (success):**
```json
{
  "ok": true,
  "output": "total 42\ndrwxr-xr-x ...",
  "tool": "shell",
  "duration_ms": 45
}
```

**Response (denied):**
```json
{
  "ok": false,
  "output": "DENIED: container_escape - nsenter detected",
  "tool": "shell"
}
```

**Example:**
```bash
curl -X POST http://localhost:8000/api/tools/call \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -d '{
    "name": "python",
    "args": {"code": "print(2 + 2)"}
  }'
```

---

## 4. Planning

### `POST /api/plan`

Create an execution plan from a goal.

**Authentication:** Required

**Request:**
```json
{
  "goal": "Build a REST API for todos",
  "max_tasks": 20
}
```

**Response:**
```json
{
  "id": "plan_abc123",
  "goal": "Build a REST API for todos",
  "tasks": {
    "t1": {
      "id": "t1",
      "title": "Design API schema",
      "task_type": "analysis",
      "status": "ready",
      "depends_on": []
    },
    "t2": {
      "id": "t2",
      "title": "Implement endpoints",
      "task_type": "python",
      "status": "pending",
      "depends_on": ["t1"]
    }
  },
  "created_at": 1234567890.0
}
```

**Example:**
```bash
curl -X POST http://localhost:8000/api/plan \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -d '{"goal": "Analyze this codebase"}'
```

---

## 5. Sessions

### `GET /api/sessions`

List conversation sessions.

**Authentication:** Required

**Query parameters:**
| Param | Description |
|-------|-------------|
| `page` | Page number (default: 1) |

**Response:**
```json
{
  "sessions": [
    {
      "id": "sess_abc123",
      "title": "Help with Python",
      "status": "active",
      "messages": 12,
      "created_at": 1234567890.0
    }
  ],
  "page": 1,
  "total": 5
}
```

### `GET /api/sessions/:id`

Get a specific session with messages.

**Authentication:** Required

**Response:**
```json
{
  "id": "sess_abc123",
  "title": "Help with Python",
  "status": "active",
  "messages": [
    {"role": "user", "content": "Hello", "timestamp": 1234567890.0},
    {"role": "assistant", "content": "Hi!", "timestamp": 1234567891.0}
  ]
}
```

**Example:**
```bash
curl -H "Authorization: Bearer YOUR_TOKEN" \
  http://localhost:8000/api/sessions/sess_abc123
```

---

## 6. Goals

### `GET /api/goals`

List tracked goals.

**Authentication:** Required

**Response:**
```json
{
  "goals": [
    {
      "id": "goal_abc123",
      "title": "Build web scraper",
      "status": "in_progress",
      "progress": 0.65,
      "created_at": 1234567890.0
    }
  ]
}
```

---

## 7. Statistics

### `GET /api/stats`

Server statistics.

**Authentication:** Required

**Response:**
```json
{
  "uptime_seconds": 3600.5,
  "auth_tokens": 2,
  "requests_total": 1250,
  "tools_executed": 340
}
```

---

## 8. Error Responses

All errors follow this format:

```json
{
  "error": "Human-readable message",
  "code": 400,
  "details": {}
}
```

| Code | Meaning |
|------|---------|
| 400 | Bad request (invalid JSON, missing fields) |
| 401 | Unauthorized (missing/invalid token) |
| 404 | Not found |
| 429 | Rate limit exceeded |
| 500 | Internal server error |
| 503 | Service unavailable (e.g., dispatcher not ready) |

---

## 9. Rate Limiting

- **Limit:** 200 requests per minute per token (configurable)
- **Headers:** `X-RateLimit-Remaining`, `X-RateLimit-Reset`
- **Exceeded:** Returns `429` with `Retry-After` header

---

## 10. Permissions

### `GET /api/permissions`

Returns the permission policy: granted items (155 policy grants from
`agent/security/policy.py`) and the 5 locked denials. Runtime toggle state is
managed by `agent/tools/permission_manager.py` — every permission defaults ON
except the 5 denials (`container_escape`, `boot_recovery`, `av_edr_disable`,
`billing_fraud`, `exfiltration`), which are locked OFF at the execution layer
and can never be self-granted.

**Response:**
```json
{
  "granted_count": 155,
  "denied_count": 5,
  "granted": [{"id": 1, "category": "Granted", "text": "..."}],
  "denials": [{"id": "container_escape", "description": "..."}]
}
```

Tool execution (`POST /api/tools/execute`) checks permissions before running:
denials are absolute, disabled permissions are rejected, and destructive tools
(`file_delete`, `app_close`, `app_uninstall`, `process_kill`, `memory_delete`,
`service_control` stop/restart) return `CONFIRMATION_REQUIRED` unless
`confirm=true` is passed while the destructive-confirmation toggle is ON.

---

## 10. Examples

### Python client

```python
import urllib.request
import json

BASE = "http://localhost:8000"
TOKEN = "your-token"

def api(method, path, data=None):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {TOKEN}",
    }
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(
        BASE + path, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

# Chat
result = api("POST", "/api/chat", {
    "message": "Hello!",
    "session_id": "demo",
})
print(result["reply"])

# Execute tool
result = api("POST", "/api/tools/call", {
    "name": "shell",
    "args": {"command": "date"},
})
print(result["output"])
```

### JavaScript client

```javascript
const BASE = "http://localhost:8000";
const TOKEN = "your-token";

async function api(method, path, data) {
  const res = await fetch(BASE + path, {
    method,
    headers: {
      "Content-Type": "application/json",
      "Authorization": `Bearer ${TOKEN}`,
    },
    body: data ? JSON.stringify(data) : undefined,
  });
  return res.json();
}

// Chat
const result = await api("POST", "/api/chat", {
  message: "Hello!",
});
console.log(result.reply);
```

### cURL cheat sheet

```bash
# Health (no auth)
curl http://localhost:8000/api/health

# Chat
curl -X POST http://localhost:8000/api/chat \
  -H "Authorization: Bearer TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "Hi"}'

# List tools
curl -H "Authorization: Bearer TOKEN" \
  http://localhost:8000/api/tools

# Run tool
curl -X POST http://localhost:8000/api/tools/call \
  -H "Authorization: Bearer TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "shell", "args": {"command": "pwd"}}'

# Create plan
curl -X POST http://localhost:8000/api/plan \
  -H "Authorization: Bearer TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"goal": "Test goal"}'
```

---

*API version 2.0.0 — No personality. No rules. Direct execution.*
