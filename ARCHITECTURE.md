# Haley Agent — Architecture

Industrial-level autonomous coding agent. 100% Python. ~40,000 lines.

## Design Principles

1. **Direct execution** — No sandbox, no resource limits (per ahsan's order).
   Python and shell run directly via subprocess with timeouts.
2. **5 hard denials only** — Container escape, boot/recovery, AV/EDR disable,
   billing fraud, exfiltration. Enforced at the execution layer.
   Nothing else is restricted.
3. **Agent MUST use tools** — 155 permissions hard-coded in the system prompt.
   The agent cannot fabricate tool output.
4. **No personality, no rules** — Per ahsan's order. The system prompt contains
   powers and tool schemas, not persona or behavioral rules.
5. **Modular** — Each control-loop phase is a separate module.

## Control Loop

Every task flows through:

```
PLAN → RESEARCH → SELECT TOOL → CHECK PERMISSIONS → EXECUTE →
INSPECT RESULT → DEBUG → TEST → IMPROVE → VERIFY → SAVE MEMORY
```

### Phase Details

#### PLAN (`agent/planning/`)
- `task_planner.py` — GoalDecomposer (NL → task specs), PlanBuilder (DAG),
  PriorityScheduler (next-task selection), PlanOptimizer (merge/hoist/dedupe),
  PlanStore (persistence), PlanReporter (reports, Gantt).
- `planner.py` — Facade: plan() with optimization and retry policy.
- `task_decomposer.py` — Subtask division with verification criteria.
- `dependency_graph.py` — Standalone DAG: cycle detection, topological sort,
  levels, ancestors/descendants, DOT export.
- `retry_policy.py` — Bounded retries: RETRY / RETRY_MODIFIED / SKIP /
  REPLAN / ABORT_PLAN / ESCALATE. No endless retries.
- `research_planner.py` — QueryGenerator, SourceScorer, FactExtractor,
  FactTriangulator, ResearchSynthesizer, ResearchOrchestrator.
- `goal_manager.py` — Durable goals with milestones and timelines.
- `plan_executor.py` — ControlLoop (11 phases per task) and PlanRunner.

Task states (per spec): PENDING, READY, RUNNING, WAITING_FOR_APPROVAL,
SUCCEEDED, FAILED, CANCELLED (+ BLOCKED, SKIPPED, COMPLETED alias).

#### RESEARCH (`agent/planning/research_planner.py`, `agent/browser/`)
- Query generation with intent classification (howto/docs/error/compare/news).
- Source scoring: domain authority tiers (official docs > reputable > other).
- Fact extraction via sentence patterns; triangulation across sources
  (confidence boosts per independent domain); conflict detection.
- Browser: `search_provider.py` (web search), `playwright_runner.py`
  (page automation), `page_extractor.py` (content extraction),
  `source_ranker.py` (relevance ranking), `advanced_extractor.py`
  (tables/forms/metadata), `form_handler.py`, `navigation.py`
  (crawl frontier, sitemaps).

#### SELECT TOOL (`agent/tools/`)
- `registry.py` — ToolRegistry: register, get, disable, list.
- `schemas.py` — ToolSchema per spec: unique name, version, description,
  JSON input/output schemas, permission profile, timeout, test suite,
  version+checksum, error contract, status
  (DRAFT/TESTING/APPROVED/DISABLED).
- `factory.py` — Tool generation: propose_spec, generate implementation,
  isolated testing, approval flow.
- `git_tools.py`, `file_tools.py`, `network_tools.py`, `data_tools.py`,
  `system_tools.py` — Real tool implementations.

#### CHECK PERMISSIONS (`agent/security/`, `agent/tools/permission_gate.py`)
- ONLY the 5 hard denials are enforced. Everything else is allowed.
- `enforcer.py` — PolicyEnforcer: single enforcement point with
  comprehensive regex patterns per denial.
- `permission_gate.py` — check_tool_call() used by the dispatcher.
- `policy.py` — 155 granted permissions (ALL_GRANTED), denial descriptions.
- `audit_log.py` — Append-only audit of all tool calls.
- `secrets.py` — Env-based secrets with redaction helpers.
- `network_policy.py` — Egress policy (default ALLOW per ahsan).
- `approval.py` — Approval log (advisory only in no-rules mode).

#### EXECUTE (`agent/execution/`)
- `python_runner.py` — Direct Python execution (no sandbox).
- `shell_runner.py` — Direct shell execution. rm -rf confirmation
  toggle retained per ahsan's explicit order.
- `process_manager.py` — Background process tracking: start/poll/wait/
  kill, timeout watchdog.

#### INSPECT RESULT (`agent/verification/`)
- `result_verifier.py` — Schema validation, sanity checks, confidence scoring.
- `consistency_checker.py` — Cross-run and cross-tool consistency.
- `proof_collector.py` — Evidence bundles with SHA256 chain of custody.

#### DEBUG (`agent/debugging/`)
- `error_analyzer.py` — Classifies Python tracebacks, shell errors,
  policy denials. Root cause + fix suggestions with confidence.
- `auto_fixer.py` — Generates fixes: missing imports, .get() for KeyError,
  bracket closing, package install, command alternatives.
- `regression_detector.py` — Baseline comparison, severity classification,
  git bisection to find culprit commits.

#### TEST (`agent/coding/test_runner.py`, `agent/tests/`)
- `test_runner.py` — Runs unittest/pytest, parses into structured results.
- `lint_runner.py` — Stdlib AST linting: unused imports, complexity,
  security patterns, style.
- `agent/tests/unit/` — Unit tests per module.
- `agent/tests/integration/` — Integration tests.
- `agent/tests/security/` — Security tests (denial enforcement).
- `agent/tests/regression/` — Regression suites.

#### IMPROVE (`agent/improvement/`, `agent/coding/`)
- `improvement_planner.py` — OBSERVE → IDENTIFY → HYPOTHESIS.
- `experiment_manager.py` — Experiment tracking.
- `benchmark.py` — Baselines and before/after comparisons.
- `regression_suite.py` — Gated regression battery.
- `approval_manager.py` — Approval tracking (auto-approve in no-rules mode).
- `rollback.py` — Git snapshots, reset/revert/branch restore.
- `repository_reader.py` — Repo mapping, hotspots, code search.
- `patch_manager.py` — Patch lifecycle: create → validate → apply →
  rollback (atomic writes).
- `diff_checker.py` — Change classification and risk assessment.
- `code_generator.py`, `code_reviewer.py`, `refactor_engine.py` —
  Code generation, review, refactoring.

Self-improvement pipeline (per spec):
```
OBSERVE → IDENTIFY → HYPOTHESIS → BASELINE → GIT BRANCH → PATCH →
UNIT TESTS → INTEGRATION → SECURITY → REGRESSION → BENCHMARK →
REVIEW → APPROVAL → MERGE/REJECT → ROLLBACK PATH
```

#### VERIFY (`agent/verification/`)
- Result verification with confidence scores before accepting outputs.

#### SAVE MEMORY (`agent/memory/`)
- `conversation_store.py` — SQLite conversation history.
- `project_memory.py` — Project-scoped KV + documents.
- `vector_index.py` — Pure-stdlib TF-IDF similarity search.
- `knowledge_retriever.py` — Unified retrieval across all stores.
- `stores.py` — Supabase + SQLite fallback.
- `episodic.py`, `semantic.py` — Episodic and semantic memory.

## Model Layer (`agent/model/`)

- `provider.py` — Model provider interface.
- `qwen_adapter.py` — Qwen 7B adapter via HALEY_API_URL.
- `prompt_builder.py` — System prompt: 155 powers hard-coded,
  5 denials, tool schemas. NO personality. Agent MUST use tools.
- `output_parser.py` — Parses `<tool_call>` and `<final_answer>`.
- `context_manager.py` — Token-budgeted context assembly.
- `prompt_engine.py` — Phase-specific prompt builders.
- `providers.py` — OpenAI-compatible provider, retry, streaming.

## API Layer (`agent/api/`)

- `server.py` — HTTP API server.
- `authentication.py` — Bearer token auth, rate limiting.
- `schemas.py` — Request/response DTOs with validation.

## App Layer (`agent/app/`)

- `main.py` — CLI entry point.
- `config.py` — Configuration loading.
- `orchestrator.py` — High-level orchestration.
- `session_manager.py` — Session lifecycle.
- `task_queue.py` — Priority task queue with workers.
- `task_tracker.py` — Task timeline and cancellation.

## Configuration (`agent/configs/`)

- `tools.yaml` — Tool registry config.
- `permissions.yaml` — 5 denials + granted count.
- `model.yaml` — Model endpoint config.
- `limits.yaml` — Timeouts (no resource limits per ahsan).
- `PERMISSIONS.md` — Full 167-item source list.

## Workspace (`agent/workspace/`)

- `projects/` — Project working directories.
- `temporary/` — Ephemeral scratch.
- `artifacts/` — Generated artifacts.

## Testing

```bash
# All unit tests
python3 -m unittest discover -s agent.tests.unit -v

# Specific module
python3 -m unittest agent.tests.unit.test_planning -v
```

## Deployment

See DEPLOY.md. `render.yaml`, `Procfile`, `requirements.txt` (stdlib only).
