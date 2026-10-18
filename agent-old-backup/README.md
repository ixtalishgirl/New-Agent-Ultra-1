# Halye Autonomous Coding Agent

Python-based modular coding agent powered by self-hosted Qwen 7B.
Built per ahsan's orders + ChatGPT 15-phase architecture spec.

## Control loop
PLAN → RESEARCH → SELECT TOOL → CHECK PERMISSIONS → EXECUTE → INSPECT → DEBUG → TEST → IMPROVE → VERIFY → SAVE MEMORY

The model emits `<tool_call>` blocks; the dispatcher executes them for REAL
and feeds `<tool_result>` back. The model NEVER fabricates tool output.

## Layout
- `app/` — config, main entry, orchestrator (ReAct loop), session, task queue
- `model/` — provider abstraction, Qwen 7B adapter, system-prompt builder (all 167 permissions), output parser, context manager
- `tools/` — registry, JSON schemas, dispatcher, **permission gate** (5 denials hard-coded at execution layer)
- `execution/` — sandboxed Python runner, controlled shell runner
- `security/` — `policy.py`: all 167 PERMISSIONS.md items + 5 locked denials
- `configs/` — tools.yaml, permissions.yaml, model.yaml, limits.yaml
- Later phases: planning, browser (Playwright), coding, improvement (Git pipeline), memory, api

## Run
```
python3 -m agent.app.main "your task here"
```
Needs `HALEY_API_URL` pointing at the Qwen 7B /generate endpoint.

## Permissions
All 167 items from PERMISSIONS.md are in `security/policy.py` and the system prompt.
5 denials enforced at execution (never toggleable): container_escape, boot_recovery,
av_edr_disable, billing_fraud, exfiltration. Beyond these 5, no other restrictions.
