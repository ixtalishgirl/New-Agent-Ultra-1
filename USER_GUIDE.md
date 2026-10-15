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

### Permission Manager & central discovery (2026-10-15)

Every tool is registered in the **central Tool Registry**
(`agent/tools/registry.py`) and gated by the **Permission Manager**
(`agent/tools/permission_manager.py`) before the Dispatcher executes it.

```python
from agent.tools.registry import build_full_registry
from agent.tools.permission_manager import PermissionManager
from agent.tools.dispatcher import Dispatcher

reg = build_full_registry()          # all tools, incl. computer-control
mgr = PermissionManager(registry=reg) # all ON except the 5 locked denials
disp = Dispatcher(reg, workspace=".", permission_manager=mgr)

# Discover everything the agent can do:
for t in reg.discover(mgr):
    print(t["name"], "| enabled:", t["enabled"], "|", t["description"][:60])

# Check / toggle a permission:
print(mgr.check("file_delete", {"path": "x"}).needs_confirmation)  # True
mgr.disable("tool:screen_record")   # toggle OFF (persists to permission_state.json)
mgr.enable("tool:screen_record")    # toggle back ON
```

Rules (ahsan's standing order):
- **All permissions default ON** — OS-level, credentials, production, browser,
  everything — except the 5 hard denials.
- The 5 denials (`container_escape`, `boot_recovery`, `av_edr_disable`,
  `billing_fraud`, `exfiltration`) are locked OFF at the execution layer
  (`agent/tools/permission_gate.py`) and can never be self-granted —
  `perm_grant` refuses them.
- Destructive actions (`file_delete`, `app_close`, `app_uninstall`,
  `process_kill`, `memory_delete`, `service_control stop/restart`) require
  `confirm=true` while the `safety:destructive_confirmation` toggle is ON
  (default ON). The dispatcher returns `CONFIRMATION_REQUIRED` otherwise.
- Full permission catalog: `agent/configs/PERMISSIONS.md` (155 policy items),
  `agent/configs/COMPUTER_CONTROL_PERMISSIONS.md` (150 computer-control
  items, 9 categories), and
  `agent/configs/ADVANCED_CODING_PERMISSIONS.md` (13 categories:
  code development, debugging, testing, game dev, performance, web,
  files/terminal, AI/ML, self-improvement, cloud, security, advanced
  system, high-risk).

### Computer-control tools (9 categories)

| Category | Module | Examples |
|----------|--------|----------|
| Screen & Vision | `agent/tools/vision_tools.py` | `screen_shot`, `screen_record`, `ui_element_detect`, `visual_grounding`, `ocr_read`, `multi_monitor_info`, `screen_change_detect` |
| Mouse Control | `agent/tools/input_tools.py` | `mouse_move`, `mouse_click`, `mouse_drag`, `mouse_scroll` |
| Keyboard Control | `agent/tools/input_tools.py` | `key_press`, `key_hotkey`, `key_type`, `clipboard_copy` |
| Live Browser | `agent/tools/browser_tools.py` | `browser_new_tab`, `browser_click`, `browser_fill`, `browser_js`, `browser_network`, `browser_launch`, `browser_close`, `browser_scroll` |
| File Management | `agent/tools/filemgmt_tools.py` | `file_append`, `file_copy`, `file_move`, `file_delete` (trash), `file_watch`, `file_preview` |
| Application Control | `agent/tools/app_tools.py` | `app_launch`, `app_list`, `app_install`, `process_kill`, `service_control`, `window_resize_move`, `menu_navigate`, `dialog_handle`, `editor_control` |
| Coding & Tools | `agent/tools/code_tools.py` | `git_clone`, `git_pull`, `git_push`, `code_lint`, `tool_test`, `cron_add`, `code_generate`, `error_log_read`, `tool_version`, `benchmark_run` |
| Memory & Automation | `agent/tools/memory_tools.py` | `memory_save`, `macro_replay`, `workflow_run`, `parallel_run`, `retry_backoff`, `progress_report`, `error_detect` |
| Access & Safety | `agent/tools/safety_tools.py` | `perm_check`, `perm_grant`, `audit_log`, `safe_mode`, `health_check`, `website_allowlist`, `file_access_rules`, `op_timeout`, `cred_protect`, `perm_change_log` |

Each module exposes `TOOL_DEFS` (name, description, input schema, profile,
timeout) and `HANDLERS` (real implementations). The registry auto-registers
them via `build_full_registry()`; the dispatcher routes them generically.

### Advanced autonomous coding agent (13 categories, 2026-10-15)

| Category | Module | Examples |
|----------|--------|----------|
| Code Development | `agent/tools/dev_tools.py` | `repo_scaffold`, `code_refactor`, `code_translate`, `api_scaffold`, `docs_generate`, `code_review`, `arch_analyze` |
| Bug Fixing & Debugging | `agent/tools/debug_tools.py` | `bug_scan`, `stacktrace_analyze`, `root_cause`, `patch_generate`, `memleak_detect`, `deadlock_detect`, `runtime_check` |
| Testing & QA | `agent/tools/test_tools.py` | `test_run`, `test_generate`, `coverage_run`, `load_test`, `fuzz_run`, `api_test`, `db_test`, `fail_repro` |
| Game Development | `agent/tools/game_tools.py` | `game_scaffold` (pygame/godot/unity/unreal, 2D/3D), `game_build` |
| Performance | `agent/tools/perf_tools.py` | `profile_cpu`, `profile_memory`, `alloc_track`, `thread_analyze`, `complexity_estimate`, `perf_regression`, `optimize_suggest`, `optimize_verify` |
| AI & Machine Learning | `agent/tools/ml_tools.py` | `ml_inference`, `rag_index`, `rag_query`, `dataset_validate`, `gpu_monitor`, `prompt_eval`, `model_version` |
| Cloud & Infrastructure | `agent/tools/cloud_tools.py` | `cicd_run`, `deploy_preview`, `uptime_check`, `iac_validate`, `k8s_validate`, `db_migrate_test`, `backup_verify`, `alert_emit` |
| Security & Reliability | `agent/tools/sec_tools.py` | `sast_scan`, `vuln_scan_deps`, `license_check`, `config_audit`, `perm_audit`, `dlp_check`, `supply_chain_check` |
| Advanced System | `agent/tools/sys2_tools.py` | `task_plan`, `task_decompose`, `agent_message`, `work_queue`, `task_monitor`, `resource_monitor`, `config_version`, `release_notes` |
| High-Risk (audit-logged) | `agent/tools/risk_tools.py` | `root_exec`, `prod_secret_read`, `prod_deploy` (confirm-gated), `tunnel_create`, `self_perm_audit`, `ssh_exec` |
| Self-Improvement | `agent/tools/self_tools.py` | `failure_memory`, `tool_discover`, `tool_spec_gen`, `tool_test_gen`, `gap_analyze`, `branch_create`, `kb_update` |
| Build & Env (extended) | `agent/tools/code_tools.py` | `build_run`, `compiler_run`, `debugger_run`, `env_detect`, `checkpoint_create`, `checkpoint_restore`, `build_log_collect` |

Per-tool verification: `agent/tests/verify_all_tools.py` executes every
registered tool with safe args and prints PASS/FAIL/SKIP per tool
(179 PASS / 0 FAIL / 139 honest SKIP on 2026-10-15 over 318 tools).

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

### Playwright on Render

The 25 browser tools need the Playwright engine. On Render it is installed
automatically at build time (`render.yaml` buildCommand runs
`python -m playwright install --only-shell chromium`, falling back to full
chromium — the build never fails if the download is unavailable).
If the engine is missing, every browser tool returns an honest
"playwright not installed" message instead of fake results.

### Screenshots reach you in chat

`browser_tab_screenshot` and `screen_shot` return the PNG as a base64 data
URI. The dashboard chat renders it inline as an image — ask the agent
"take a screenshot of example.com" and you will SEE it in the chat.

### Germany routing

```python
import os
os.environ["GERMANY_PROXY_URL"] = "http://proxy:8080"
# Browser automatically routes through Germany
```

---

## 9b. Supabase (your database)

The agent has full read/write SQL access via `supabase_query` — including
`CREATE TABLE`, `INSERT`, `UPDATE`, `DELETE`. Set on Render:

- `SUPABASE_DB_URL` — direct Postgres URL (preferred, full SQL)
- or `SUPABASE_URL` + `SUPABASE_KEY` — REST fallback (SELECT only)

```json
{ "name": "supabase_query",
  "args": { "sql": "CREATE TABLE IF NOT EXISTS notes (id SERIAL PRIMARY KEY, text TEXT);" } }
```

Credential values are never written to logs or error output.

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

### Tool creation — real tools, not code dumps

You never touch code. You ask ("make me a tool that converts CSV to JSON"),
and the agent builds a REAL tool it uses itself — exactly like an assistant
uses its tools:

1. `tool_create` receives `name`, `description`, `input_schema`, and `code`
   (the code must define `run(args: dict) -> dict`).
2. The tool is written to `agent/tools/custom/<name>.py` and listed in
   `agent/tools/custom/manifest.json`.
3. It is registered in the central ToolRegistry **immediately** — callable in
   the same session via the normal `Dispatcher.dispatch` path, permission-
   gated like every built-in tool (ON by default; the 5 locked denials are
   still enforced on generated code).
4. The code is backed up to the Supabase `custom_tools` table, because
   Render's disk is ephemeral. On startup, if the local `custom/` dir is
   empty, tools are restored from Supabase automatically.

Ask in chat: "create a tool called csv2json that ..." — then immediately use
it: the agent calls it itself the same way it calls any other tool.

---

## 10b. Self-replication (teams of agents)

The agent spawns child agents that work in parallel — one AI does one task,
another does another:

- `spawn_agent` — start a child (own session_id) on a task → child_id
- `agent_status` / `agent_result` / `agent_stop` — monitor and collect
- `fan_out` — run N tasks across N children, combine results
- `replicate_verify` — spawns 2 test children, each runs a real tool;
  reports PASS/FAIL per child (the "is it really working" proof)

Bounds: max 8 active children per parent, max depth 3, 10-minute default
timeout. Children inherit the full permission gate including the 5 locked
denials. **Each child gets its own isolated Playwright browser context**
(separate cookies/storage/pages) — parallel children never leak sessions
into each other or the parent.

---

## 10c. Website login + self-modification

- `browser_login` — log in to Google, Kaggle, or any login form like an
  assistant: auto-detects fields, handles multi-step flows and TOTP,
  verifies the login. The password lives only in call args in memory and
  is scrubbed from every output/error. Google is best-effort (bot detection
  may block).
- `browser_inspect_element` — outer HTML, attributes, computed styles,
  bounding box for any CSS selector (complements DOM snapshot, console,
  network inspection).
- `self_modify` — the agent REALLY edits its own code: git branch → apply
  patch → run tests → report (automatic rollback on failure).
  `agent/tools/permission_gate.py` is refused (it guards the 5 denials).
- `scheduled_selfmod` — safe scheduled demo: rotates the dashboard accent
  color, verifies, logs. Run `POST /api/selfmod/demo` to trigger it live
  and see the before/after.

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
