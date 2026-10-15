# User Guide — Autonomous AI Agent

> No personality. No rules. Direct execution. 155 powers hard-coded.
> 5 denials enforced: container escape, boot/recovery, AV/EDR disable,
> billing fraud, exfiltration.

---

## Table of Contents

1. [Quick Start](#1-quick-start)
2. [Installation](#2-installation)
3. [Configuration](#3-configuration)
4. [CLI Reference](#4-cli-reference)
5. [Control Loop](#5-control-loop)
6. [Tools](#6-tools)
7. [Planning](#7-planning)
8. [Memory](#8-memory)
9. [Browser](#9-browser)
10. [Self-Improvement](#10-self-improvement)
11. [API Server](#11-api-server)
12. [Security](#12-security)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. Quick Start

### Prerequisites

- Python 3.10+
- Git
- pip

### Run your first task

```bash
cd ~/workspace/agent-fix/New-Agent-ulta

# Check system status
python3 -m agent.app.cli status

# List available tools
python3 -m agent.app.cli tools

# Run a task
python3 -m agent.app.cli chat "List all Python files in the current directory"

# Create a plan (dry run)
python3 -m agent.app.cli plan "Build a REST API for todos"

# Create and execute a plan
python3 -m agent.app.cli plan "Count lines of Python code" --execute
```

### Start the API server

```bash
# Start server on port 8000
python3 -m agent.app.cli serve --port 8000

# In another terminal, chat via API
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello, what can you do?"}'
```

---

## 2. Installation

### Standard install

```bash
git clone https://github.com/ixtalishgirl/New-Agent-Ultra-1.git
cd New-Agent-Ultra-1
pip install -r requirements.txt  # if present
```

### Verify installation

```bash
python3 -m agent.app.cli status
```

Expected output:
```
==================================================
AGENT STATUS
==================================================
Model: UNAVAILABLE (http://localhost:8001)
Tools: 29 registered
Permissions: 155 granted
Denials: container_escape, boot_recovery, av_edr_disable, billing_fraud, exfiltration
Session: active
```

> **Note:** "Model: UNAVAILABLE" is expected if Qwen is not running.
> Set `QWEN_BASE_URL` to your model endpoint.

### Qwen model setup

```bash
# Option 1: Local Qwen (via Ollama or vLLM)
export QWEN_BASE_URL="http://localhost:8001"

# Option 2: Remote via ngrok tunnel
export QWEN_BASE_URL="https://your-tunnel.ngrok.io"
```

---

## 3. Configuration

Configuration files live in `agent/configs/`:

| File | Purpose |
|------|---------|
| `tools.yaml` | Tool registry configuration |
| `permissions.yaml` | 155 granted + 5 denied |
| `model.yaml` | Model endpoint settings |
| `limits.yaml` | Execution limits (timeouts only, no sandbox) |

### Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `QWEN_BASE_URL` | `http://localhost:8001` | Qwen model endpoint |
| `GERMANY_PROXY_URL` | (none) | Proxy for Germany routing |
| `SUPABASE_URL` | (none) | Supabase project URL |
| `SUPABASE_KEY` | (none) | Supabase API key |
| `AGENT_API_TOKEN` | (none) | API bearer token |

### Germany routing

To route traffic through Germany:

```bash
export GERMANY_PROXY_URL="http://your-german-proxy:8080"
```

The browser module automatically uses this proxy when set.

---

## 4. CLI Reference

### `chat` — Run a task

```bash
python3 -m agent.app.cli chat "TASK" [options]

Options:
  -n, --iterations N   Max ReAct iterations (default: 10)
  -v, --verbose        Show tool history
  --json               Output JSON result
```

**Examples:**

```bash
# Simple task
python3 -m agent.app.cli chat "What time is it?"

# Complex task with more iterations
python3 -m agent.app.cli chat "Research Python async patterns" -n 20

# Verbose output
python3 -m agent.app.cli chat "List files" -v

# Pipe from stdin
echo "Count Python files" | python3 -m agent.app.cli chat
```

### `plan` — Planning

```bash
python3 -m agent.app.cli plan "GOAL" [options]

Options:
  -x, --execute        Execute the plan after creating it
  --max-tasks N        Max tasks to execute (default: 20)
  --json               Output JSON
```

**Examples:**

```bash
# Just create a plan
python3 -m agent.app.cli plan "Build a web scraper"

# Create and execute
python3 -m agent.app.cli plan "Analyze this codebase" --execute
```

### `tools` — List tools

```bash
python3 -m agent.app.cli tools [--json]
```

### `serve` — API server

```bash
python3 -m agent.app.cli serve --port 8000
```

### `status` — System status

```bash
python3 -m agent.app.cli status
```

### `improve` — Self-improvement

```bash
# Observe only
python3 -m agent.app.cli improve

# Observe and plan improvements
python3 -m agent.app.cli improve --auto
```

### `memory` — Memory operations

```bash
python3 -m agent.app.cli memory list
python3 -m agent.app.cli memory clear
```

---

## 5. Control Loop

Every task flows through 11 phases:

```
PLAN → RESEARCH → SELECT TOOL → CHECK PERMISSIONS → EXECUTE →
INSPECT RESULT → DEBUG → TEST → IMPROVE → VERIFY → SAVE MEMORY
```

### Phase details

| Phase | What happens |
|-------|--------------|
| **PLAN** | Task decomposed into subtasks with dependencies |
| **RESEARCH** | Web search for relevant information |
| **SELECT TOOL** | Model chooses tool based on task type |
| **CHECK PERMISSIONS** | 5 denials checked (everything else allowed) |
| **EXECUTE** | Tool runs directly (no sandbox) |
| **INSPECT RESULT** | Output analyzed for success/failure |
| **DEBUG** | On failure: error analyzed, fix suggested |
| **TEST** | Fix verified with test execution |
| **IMPROVE** | Result quality assessed |
| **VERIFY** | Independent verification of correctness |
| **SAVE MEMORY** | Outcome stored for future reference |

### Task states

```
PENDING → READY → RUNNING → SUCCEEDED
                  ↓
              FAILED → (retry) → RUNNING
                  ↓
              CANCELLED / BLOCKED / SKIPPED
```

### Bounded retries

- Max 3 retries per task (configurable)
- Exponential backoff: 1s, 2s, 4s
- No endless loops

---

## 6. Tools

### Built-in tools (29)

| Category | Tools |
|----------|-------|
| **Execution** | `shell`, `python`, `pip` |
| **Files** | `file_read`, `file_write`, `file_list`, `file_delete` |
| **Git** | `git_status`, `git_add`, `git_commit`, `git_branch`, `git_diff` |
| **Web** | `web_search`, `web_fetch`, `browser_navigate`, `browser_screenshot` |
| **Memory** | `memory_store`, `memory_retrieve`, `memory_search` |
| **Planning** | `plan_create`, `plan_execute` |
| **Meta** | `tool_create`, `tool_list` |

### Tool contract

Every tool has:
- Unique name
- Version (semver)
- Description
- JSON input schema
- JSON output schema
- Permission profile
- Timeout
- Status: DRAFT / TESTING / APPROVED / DISABLED

### Creating new tools

```bash
# Via CLI (uses tool factory)
python3 -m agent.app.cli chat "Create a tool that counts words in a file"
```

Or programmatically:

```python
from agent.tools.factory import ToolFactory

factory = ToolFactory()
tool = factory.create(
    name="word_counter",
    description="Count words in a file",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"}
        }
    },
    implementation='''
def run(path):
    with open(path) as f:
        return {"words": len(f.read().split())}
''')
```

---

## 7. Planning

### Creating plans

```python
from agent.planning import Planner

planner = Planner()
plan = planner.plan("Build a REST API")

for task_id, task in plan.tasks.items():
    print(f"{task.title}: {task.task_type}")
```

### Task types

- `PYTHON` — Execute Python code
- `SHELL` — Execute shell command
- `WEB_SEARCH` — Search the web
- `BROWSER` — Browser automation
- `FILE_OP` — File operation
- `GIT_OP` — Git operation
- `TEST` — Run tests
- `RESEARCH` — Research task
- `ANALYSIS` — Analysis task

### Dependencies

Tasks can depend on other tasks:

```python
from agent.planning import Task, TaskType, Plan

task1 = Task(id="t1", title="Fetch data",
             task_type=TaskType.WEB_SEARCH, ...)
task2 = Task(id="t2", title="Process data",
             task_type=TaskType.PYTHON,
             depends_on=["t1"], ...)
```

---

## 8. Memory

### Project memory

```python
from agent.memory.project_memory import ProjectMemory

pm = ProjectMemory()
pm.set("my_key", "my_value", project="my_project")
value = pm.get("my_key", project="my_project")
```

### Conversation store

```python
from agent.memory.conversation_store import ConversationStore

store = ConversationStore()
store.add_message("user", "Hello", session_id="sess1")
messages = store.get_messages(session_id="sess1")
```

### Supabase backend

Set environment variables:
```bash
export SUPABASE_URL="https://your-project.supabase.co"
export SUPABASE_KEY="your-anon-key"
```

Memory automatically uses Supabase when configured, SQLite otherwise.

---

## 9. Browser

### Web search

```python
from agent.browser import SearchProvider

provider = SearchProvider()
results = provider.search("Python async patterns", max_results=5)
for r in results:
    print(r.title, r.url)
```

### Playwright automation

```python
from agent.browser import PlaywrightRunner

runner = PlaywrightRunner()
page = runner.navigate("https://example.com")
text = runner.get_text(page)
runner.screenshot(page, "output.png")
```

### Germany routing

```python
import os
os.environ["GERMANY_PROXY_URL"] = "http://proxy:8080"
# Browser automatically routes through Germany
```

---

## 10. Self-Improvement

The agent can improve itself via Git branches:

```
OBSERVE → IDENTIFY → HYPOTHESIS → BASELINE → GIT BRANCH →
PATCH → UNIT TESTS → INTEGRATION → SECURITY → REGRESSION →
BENCHMARK → REVIEW → APPROVAL → MERGE/REJECT → ROLLBACK
```

### Running improvement cycle

```python
from agent.improvement import ImprovementPlanner

planner = ImprovementPlanner()
observations = planner.observe()
improvements = planner.plan_improvements(observations)
```

### Safety

- Never modifies running code directly
- All changes via Git branches
- Full test suite must pass
- Benchmarks compared to baseline
- Rollback always available

---

## 11. API Server

### Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/health` | No | Health check |
| POST | `/api/chat` | Bearer | Agentic chat |
| GET | `/api/tools` | Bearer | List tools |
| POST | `/api/tools/call` | Bearer | Execute tool |
| POST | `/api/plan` | Bearer | Create plan |
| GET | `/api/sessions` | Bearer | List sessions |
| GET | `/api/sessions/:id` | Bearer | Get session |
| GET | `/api/goals` | Bearer | List goals |
| GET | `/api/stats` | Bearer | Server stats |

### Authentication

```bash
# Set token
export AGENT_API_TOKEN="your-secret-token"

# Use in requests
curl -H "Authorization: Bearer your-secret-token" \
  http://localhost:8000/api/tools
```

### Example requests

**Chat:**
```bash
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer TOKEN" \
  -d '{"message": "Hello", "session_id": "sess1"}'
```

**Execute tool:**
```bash
curl -X POST http://localhost:8000/api/tools/call \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer TOKEN" \
  -d '{"name": "shell", "args": {"command": "ls"}}'
```

**Create plan:**
```bash
curl -X POST http://localhost:8000/api/plan \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer TOKEN" \
  -d '{"goal": "Build a web scraper"}'
```

---

## 12. Security

### The 5 denials

These are **always** enforced, at the execution layer:

1. **Container escape** — `nsenter`, `/proc/1/root`, `--privileged`
2. **Boot/recovery** — `/boot`, `grub`, `efibootmgr`
3. **AV/EDR disable** — Stopping security services
4. **Billing fraud** — Payment bypass, subscription fraud
5. **Exfiltration** — Sending sensitive data externally

### Everything else is allowed

No sandbox. No rules engine. No approval gates.
Direct execution. The agent MUST use its 155 powers.

### Audit logging

All tool executions are logged:

```python
from agent.security.audit_log import AuditLog

audit = AuditLog()
entries = audit.recent(100)
```

---

## 13. Troubleshooting

### Model unavailable

```
Model: UNAVAILABLE (http://localhost:8001)
```

**Fix:** Start Qwen or set `QWEN_BASE_URL`:
```bash
export QWEN_BASE_URL="http://your-model:8001"
```

### Tool not found

```
Tool 'xyz' not found
```

**Fix:** List available tools:
```bash
python3 -m agent.app.cli tools
```

### Permission denied

If you see `DENIED`, the command matched one of the 5 denials.
Review the command — it may be triggering a denial pattern.

### Tests failing

```bash
# Run full test suite
python3 -m unittest discover -s agent.tests.unit

# Run specific module
python3 -m unittest agent.tests.unit.test_planning
```

### Import errors

Ensure you're in the repo root:
```bash
cd ~/workspace/agent-fix/New-Agent-ulta
python3 -m agent.app.cli status
```

---

## Appendix: Architecture

```
agent/
├── app/           # Orchestrator, CLI, sessions, tasks
├── model/         # Qwen provider, prompts, parsing
├── planning/      # Task planning, decomposition
├── tools/         # Tool registry, dispatcher, factory
├── execution/     # Python/shell runners (direct)
├── browser/       # Search, Playwright, extraction
├── coding/        # Code gen, patches, tests
├── improvement/   # Self-modification pipeline
├── memory/        # Supabase, SQLite, vectors
├── security/      # Policy, audit, 5 denials
├── api/           # HTTP server, auth
├── debugging/     # Error analysis, auto-fix
├── verification/  # Result verification
└── tests/         # Unit, integration, security
```

---

*Built for ahsan. No personality. No rules. Just powerful.*
