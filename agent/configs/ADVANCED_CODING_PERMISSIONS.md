# Advanced Autonomous Coding Agent — Permissions

Issued by ahsan (2026-10-15). Every item below is implemented as a real tool
in `agent/tools/` and registered in the central Tool Registry
(`agent/tools/registry.py`). Default state: **ON** for everything, including
the high-risk category (audit-logged), except the 5 hard denials
(container_escape, boot_recovery, av_edr_disable, billing_fraud,
exfiltration), which stay locked OFF at the execution layer
(`agent/tools/permission_gate.py`) with no toggle, per his standing order.

Runtime authority: `agent/tools/permission_manager.py` (PermissionManager) —
`is_allowed`, `enable`/`disable`, `list_all`, `check(tool, args)`.
Verification: `agent/tests/verify_all_tools.py` executes every registered
tool with safe args (PASS/FAIL/SKIP per tool); `agent/tests/unit/` holds
smoke tests. High-risk invocations append to `agent/logs/risk_audit.jsonl`.

## 1. Code Development — `agent/tools/dev_tools.py`

| Permission | Tool | What it does |
|---|---|---|
| code_generation | `repo_scaffold` | Multi-file project scaffolds (web/cli/api/saas/lib/plugin/desktop) |
| code_completion | `code_complete` | Heuristic AST-based completions (honest scope note) |
| multi_file_editing | `multi_file_edit` | Regex replace across files (dry-run default) |
| repo_code_understanding | `repo_understand` | Symbol index + import graph |
| cross_language_translation | `code_translate` | Rule-based python→javascript (honest scope) |
| refactoring | `code_refactor` | Rename symbol / extract function (syntax-checked) |
| api_development | `api_scaffold` | REST endpoint handler scaffolds |
| sdk_development | `sdk_scaffold` | urllib client SDK from endpoints |
| library_development | `repo_scaffold(kind=lib)` | pyproject/src-layout/test scaffold |
| plugin_development | `repo_scaffold(kind=plugin)` | Host-contract plugin scaffold |
| cli_development | `repo_scaffold(kind=cli)` | argparse CLI scaffold |
| desktop_development | `repo_scaffold(kind=desktop)` | tkinter starter (needs display) |
| web_development | `repo_scaffold(kind=web)` | Static HTML/CSS/JS app |
| mobile_development | `repo_scaffold(kind=desktop)` | Kivy note — honest: build needs Buildozer |
| saas_development | `repo_scaffold(kind=saas)` | API + deploy notes |
| backend_frontend | `api_scaffold` + `repo_scaffold(kind=web)` | Both ends |
| db_schema_generation | `db_schema_generate` | CREATE TABLE DDL from field specs |
| documentation_generation | `docs_generate` | Markdown docs from docstrings via ast |
| code_review | `code_review` | Static findings (syntax, bare except, TODOs, complexity) |
| architecture_analysis | `arch_analyze` | Module dependency graph |

## 2. Bug Fixing & Debugging — `agent/tools/debug_tools.py`

| Permission | Tool | What it does |
|---|---|---|
| auto_bug_detection | `bug_scan` | Static scan: syntax, risky patterns, unused imports (+pyflakes) |
| runtime_error_detection | `runtime_check` | Run snippet, capture + analyze traceback |
| syntax_error_detection | `code_lint` | py_compile + ast |
| type_error_detection | `type_check` | mypy if installed, else annotation coverage |
| logic_error_detection | `bug_scan` + `code_review` | Heuristic patterns (honest scope) |
| crash_analysis | `stacktrace_analyze` | Frames + likely causes |
| stack_trace_analysis | `stacktrace_analyze` | Same |
| log_analysis | `log_analyze` / `error_log_read` | Bucket error lines |
| root_cause_analysis | `root_cause` | Suspect frame heuristic |
| auto_patch_generation | `patch_generate` | Template fixes (apply only with target) |
| multi_file_bug_fixing | `multi_file_fix` | Patch comment across matching files |
| regression_detection | `regression_check` | pytest baseline for diffing |
| memory_leak_detection | `memleak_detect` | tracemalloc growth check |
| deadlock_detection | `deadlock_detect` | faulthandler thread sample |
| race_condition_analysis | `race_note` | Honest heuristic guidance |
| perf_bottleneck_detection | `profile_cpu` | cProfile hotspots |
| dependency_conflict_resolution | `dep_conflicts` | pip check |
| build_failure_diagnosis | `build_run` | Build + captured log |
| test_failure_diagnosis | `test_failure_diagnose` | Failures + traceback hints |
| auto_fix_verification | `fix_verify` | pytest after fix |
| fix_regression_testing | `regression_check` | Same |
| git_diff_review | `git_diff` | Diff inspection |
| auto_rollback | `restore_point` / `checkpoint_restore` | Snapshots |

## 3. Testing & QA — `agent/tools/test_tools.py`

| Permission | Tool | What it does |
|---|---|---|
| unit_testing | `test_run` | pytest runner |
| integration_testing | `test_run(markers=...)` | Marker-selected runs |
| e2e_testing | `e2e_note` | Guidance (needs live system) |
| ui_testing | `ui_test` | Fetch page, basic checks |
| api_testing | `api_test` | HTTP cases vs base URL |
| database_testing | `db_test` | SQL on throwaway sqlite |
| load_testing | `load_test` | Concurrent HTTP with latency stats |
| stress_testing | `stress_test` | Heavier load |
| fuzz_testing | `fuzz_run` | Random-input function fuzzing |
| property_testing | `property_test` | hypothesis if installed (honest) |
| regression_testing | `regression_check` | Same |
| cross_browser_testing | `cross_browser_note` | Honest guidance |
| cross_platform_testing | `env_detect` | Honest: matrix needs CI |
| accessibility_testing | `a11y_check` | Heuristic HTML checks |
| compatibility_testing | `env_detect` | Honest scope |
| test_case_generation | `test_generate` | pytest scaffolds from functions |
| coverage_analysis | `coverage_run` | coverage.py if installed (honest) |
| auto_test_execution | `test_run` | Same |
| failure_reproduction | `fail_repro` | Reproduce from snippet |
| benchmark_comparison | `benchmark_run` / `perf_regression` | Timings vs baseline |

## 4. Game Development — `agent/tools/game_tools.py`

| Permission | Tool | What it does |
|---|---|---|
| game_project_creation | `game_scaffold` | Runnable starter per engine |
| 2d_game_development | `game_scaffold(dim=2d)` | pygame/godot/unity/unreal |
| 3d_game_development | `game_scaffold(dim=3d)` | Same |
| game_logic_generation | `game_scaffold` | Player controller included |
| npc_ai_programming | `game_scaffold` | Documented extension points |
| pathfinding | `game_scaffold` | Honest: scaffold-level |
| physics_integration | `game_scaffold` | Engine physics (CharacterBody/Rigidbody) |
| animation_integration | `game_scaffold` | Honest: scaffold-level |
| character_controller | `game_scaffold` | Included in templates |
| gameplay_mechanics | `game_scaffold` | Starter loop |
| inventory_quest_systems | `game_scaffold` | Honest: extension points |
| multiplayer_architecture | `game_scaffold` | Honest: notes |
| network_sync | `game_scaffold` | Honest: notes |
| game_ui | `game_scaffold` | HUD text in templates |
| shader_development | `game_scaffold` | Honest: engine-side |
| procedural_content | `game_scaffold` | Honest: extension points |
| level_generation | `game_scaffold` | Honest: extension points |
| asset_pipeline | `game_scaffold` | Honest: engine-side |
| audio_integration | `game_scaffold` | Honest: engine-side |
| save_load_systems | `game_scaffold` | Honest: extension points |
| performance_profiling | `profile_cpu` | Same |
| frame_time_analysis | `profile_cpu` | Same |
| game_build_automation | `game_build` | Engine CLI detect; headless Godot export |
| unreal_integration | `game_scaffold(engine=unreal)` | Build.cs template |
| unity_integration | `game_scaffold(engine=unity)` | C# controller |
| godot_integration | `game_scaffold(engine=godot)` | project.godot + GDScript |
| blender_pipeline | `game_scaffold` | Honest: asset-side |

## 5. Advanced Debugging & Performance — `agent/tools/perf_tools.py`

| Permission | Tool |
|---|---|
| cpu_profiling | `profile_cpu` |
| gpu_profiling | `gpu_profile_note` (honest: needs CUDA) |
| memory_profiling | `profile_memory` |
| allocation_tracking | `alloc_track` |
| thread_analysis | `thread_analyze` |
| concurrency_analysis | `concurrency_note` (honest guidance) |
| db_query_profiling | `db_query_profile` |
| network_latency_analysis | `net_latency` |
| rendering_perf_analysis | `render_perf_note` (honest guidance) |
| cache_perf_analysis | `cache_perf_note` |
| hotspot_detection | `profile_cpu` |
| complexity_analysis | `complexity_estimate` |
| benchmark_automation | `benchmark_run` |
| perf_regression_detection | `perf_regression` |
| optimization_suggestions | `optimize_suggest` |
| optimization_verification | `optimize_verify` |

## 6. Web & Live Browser — `agent/tools/browser_tools.py` + `vision_tools.py`

Covered by existing tools (`browser_*`, `web_search`, `ocr_read`,
`ui_element_detect`, mouse/keyboard, clipboard) plus new
`screenshot_analyze` (screenshot + OCR + elements combined) and
`cross_browser_note` (honest guidance).

## 7. Files, Terminal & Environment — `agent/tools/code_tools.py` (+ existing)

New: `build_run` (make/npm/gradle/cargo/python-build detect + run),
`compiler_run` (gcc/g++/javac/rustc), `debugger_run` (scripted pdb),
`env_detect` (OS/python/tool inventory), `checkpoint_create` /
`checkpoint_restore` (tar snapshots), `build_log_collect`.
Existing: file_*, `shell`, `python`, `pip`, git_*, `backup_create`,
`restore_point`, `op_timeout`.

## 8. AI & Machine Learning — `agent/tools/ml_tools.py`

| Permission | Tool |
|---|---|
| local_llm_integration | `ml_inference` (HALEY_API_URL; honest when unset) |
| model_inference | `ml_inference` |
| model_routing | `model_route` (heuristic) |
| multi_agent_orchestration | `parallel_run` |
| tool_calling | dispatcher |
| rag | `rag_index` / `rag_query` (TF-IDF, honestly labeled) |
| embedding_generation | `embed_generate` (hashed BOW, labeled non-semantic) |
| vector_search | `vector_search` |
| dataset_processing | `dataset_process` |
| dataset_validation | `dataset_validate` |
| finetune_pipeline | `finetune_note` (honest: needs GPU + data) |
| lora_training | `lora_note` (peft availability) |
| eval_dataset_generation | `eval_dataset_gen` |
| model_benchmarking | `benchmark_run` |
| prompt_evaluation | `prompt_eval` (heuristic rubric) |
| output_quality_scoring | `quality_score` (surface heuristics) |
| gpu_vram_monitoring | `gpu_monitor` (nvidia-smi or honest) |
| inference_optimization | `infer_opt_note` (guidance) |
| quantization | `quantize_note` (bitsandbytes availability) |
| model_version_management | `model_version` |

## 9. Self-Improvement & Tool Creation — `agent/tools/self_tools.py`

| Permission | Tool |
|---|---|
| failure_memory | `failure_memory` |
| persistent_project_memory | `memory_save` |
| tool_discovery | `tool_discover` |
| tool_spec_generation | `tool_spec_gen` |
| tool_code_generation | `tool_create` |
| tool_test_generation | `tool_test_gen` |
| tool_registration | registry (automatic) |
| tool_versioning | `tool_version` |
| capability_gap_analysis | `gap_analyze` |
| improvement_hypotheses | `improve_hypothesis` |
| auto_benchmarking | `benchmark_run` |
| patch_proposals | `patch_generate` |
| separate_git_branch | `branch_create` |
| regression_test_execution | `regression_check` |
| change_comparison | `text_diff` |
| approved_release_deploy | `release_deploy` (confirm-gated) |
| performance_tracking | `progress_report` |
| auto_failure_rollback | `restore_point` |
| knowledge_base_updates | `kb_update` |

## 10. Cloud & Infrastructure — `agent/tools/cloud_tools.py`

| Permission | Tool |
|---|---|
| cloud_docs_research | `web_search` |
| cloud_sdk_integration | `cloud_note` (honest: needs credentials) |
| cloud_resource_monitoring | `cloud_note` |
| cloud_cost_monitoring | `cloud_note` |
| cloud_log_inspection | `cloud_note` |
| cicd_integration | `cicd_run` (validate; `act` when installed) |
| build_pipeline_automation | `build_run` |
| deployment_preview | `deploy_preview` (local serve + smoke) |
| staging_deployment | `staging_note` (honest guidance) |
| health_check_monitoring | `health_check` |
| availability_monitoring | `uptime_check` |
| infra_config_validation | `iac_validate` |
| iac_validation | `iac_validate` |
| container_build | `container_note` (honest: no Docker layer by design) |
| k8s_manifest_validation | `k8s_validate` |
| db_migration_testing | `db_migrate_test` |
| backup_verification | `backup_verify` |
| deployment_rollback | `restore_point` |
| alert_integration | `alert_emit` |
| resource_usage_reporting | `resource_monitor` / `sysinfo` |

## 11. Security & Reliability — `agent/tools/sec_tools.py`

| Permission | Tool |
|---|---|
| sast | `sast_scan` (bandit or patterns) |
| dependency_vuln_scanning | `vuln_scan_deps` (pip-audit or inventory) |
| secret_detection | `cred_protect` |
| license_compliance | `license_check` |
| secure_code_review | `code_review` |
| configuration_auditing | `config_audit` |
| permission_auditing | `perm_audit` |
| api_auth_testing | `auth_test` |
| input_validation_testing | `input_fuzz` (authorized targets only) |
| authorized_dast | `dast_note` (guidance) |
| security_regression | `sec_regression` |
| audit_log_generation | `audit_log` |
| dlp_checks | `dlp_check` |
| backup_recovery_verification | `backup_verify` |
| supply_chain_checks | `supply_chain_check` |

## 12. Advanced System Capabilities — `agent/tools/sys2_tools.py`

| Permission | Tool |
|---|---|
| task_planning | `task_plan` |
| task_decomposition | `task_decompose` |
| parallel_execution | `parallel_run` |
| agent_to_agent_messaging | `agent_message` |
| work_queue_management | `work_queue` |
| long_task_monitoring | `task_monitor` |
| auto_retry_policies | `retry_backoff` |
| failure_recovery | `restore_point` |
| resource_scheduling | `task_plan` (honest: advisory) |
| cpu_ram_gpu_monitoring | `resource_monitor` |
| rate_limit_handling | `rate_limit_handle` |
| structured_tool_schemas | registry |
| tool_permission_enforcement | `perm_check` |
| human_approval_workflow | `approval_request` |
| audit_trail | `audit_log` |
| emergency_stop | `safe_mode` / `stream_stop` |
| versioned_configuration | `config_version` |
| reproducible_builds | `repro_build` |
| automated_release_notes | `release_notes` |

## 13. High-Risk — `agent/tools/risk_tools.py` (default ON, audit-logged)

| Permission | Tool | Safeguard |
|---|---|---|
| os_admin_root_access | `root_exec` | `sudo -n` only; every call → `risk_audit.jsonl` |
| unrestricted_filesystem | file_* | existing |
| unrestricted_shell | `shell` | existing |
| browser_cookie_credential_access | `browser_cookies` | existing |
| production_secret_access | `prod_secret_read` | audit-logged; value never printed |
| production_db_write_delete | `supabase_query` | existing |
| cloud_iam_administration | `iam_note` | read-only; refuses to invent credentials |
| firewall_security_modification | `firewall_note` | note only; never disables protections |
| production_deployment | `prod_deploy` | requires confirm='DEPLOY'; stops on failure |
| public_tunnel_creation | `tunnel_create` | audit-logged; needs provider binary |
| self_permission_modification | `self_perm_audit` | read-only report; 5 denials untouchable |
| unrestricted_remote_exec | `ssh_exec` | BatchMode, existing keys only |

The 5 denials (container_escape, boot_recovery, av_edr_disable,
billing_fraud, exfiltration) remain hard-locked in
`agent/tools/permission_gate.py` — no toggle, no self-grant, ever.
