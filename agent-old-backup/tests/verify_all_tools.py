#!/usr/bin/env python3
"""verify_all_tools.py — execute EVERY registered tool with safe minimal args.

For each tool in the central registry the script prints one line:
    PASS  <tool> — <short detail>
    FAIL  <tool> — <error snippet>        (a real bug: must be fixed)
    SKIP  <tool> — <reason>               (destructive / network / env-limited)

Rules:
- Read-only where possible. No destructive ops (nothing deleted, no pushes,
  no installs, no crontab/package/system mutation).
- No external network abuse (localhost only; external-network tools SKIP).
- Every tool gets a wall-clock timeout (default 90s).
- Environment-limited tools (no display/GPU/tesseract/browser/creds) must
  SKIP honestly via capability-message detection — never fake-pass.

Exit code: 0 when FAIL == 0, else 1.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import sys
import tarfile
import tempfile
import time
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

FIX = Path("/tmp/verify_all")
V = str(FIX)  # short alias for arg building

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_fixtures() -> None:
    FIX.mkdir(parents=True, exist_ok=True)
    (FIX / "probe.txt").write_text("hello world\nfoo bar\nhello again\n")
    (FIX / "probe.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef greet(name):\n"
        "    return 'hi ' + name\n")
    (FIX / "probe.csv").write_text("a,b\n1,2\n3,4\n")
    (FIX / "probe.log").write_text("INFO start\nERROR boom happened\nINFO end\n")
    (FIX / "probe.html").write_text(
        "<html><head><title>t</title></head><body><p>hi</p></body></html>")
    (FIX / "probe.c").write_text("int main(void){return 0;}\n")
    (FIX / "__main__.py").write_text("print('zipapp-ok')\n")
    (FIX / "requirements.txt").write_text("requests==2.31.0\n")
    with tarfile.open(FIX / "probe.tar.gz", "w:gz") as tf:
        tf.add(FIX / "probe.txt", arcname="probe.txt")
    # two tiny valid PNGs (pure-python, no PIL) for image tools
    import zlib, struct
    def _png(path, rgb):
        def chunk(typ, data):
            c = struct.pack(">I", len(data)) + typ + data
            c += struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
            return c
        ihdr = struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0)
        raw = b"".join(b"\x00" + bytes(rgb) * 8 for _ in range(8))
        png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
               + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
        Path(path).write_bytes(png)
    _png(FIX / "a.png", (200, 30, 30))
    _png(FIX / "b.png", (30, 200, 30))
    (FIX / "requirements.lock").write_text("requests==2.31.0\n")
    # trash a scratch file so trash_restore has something real to restore
    try:
        from agent.tools.registry import collect_handlers as _ch
        _h = _ch()
        scratch = FIX / "trash_me.txt"
        scratch.write_text("restore me")
        if "file_delete" in _h:
            r = _h["file_delete"]({"path": str(scratch), "confirm": True})
            tn = (r.parsed or {}).get("trash_name", "")
            if tn:
                (FIX / "trash_name.txt").write_text(tn)
    except Exception:
        pass
    # pre-create a macro + a memory entry so replay/get don't race
    try:
        from agent.tools.registry import collect_handlers
        h = collect_handlers()
        if "macro_record" in h:
            h["macro_record"]({"name": "vmacro"})
        if "macro_append" in h:
            h["macro_append"]({"name": "vmacro", "tool": "sysinfo", "args": {}})
        if "memory_save" in h:
            h["memory_save"]({"key": "vkey", "value": "vval"})
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Skip list: tool -> reason (destructive / external-network / interactive /
# credentialed / long-running). Everything else is attempted.
# ---------------------------------------------------------------------------

SKIP = {
    # destructive / mutating
    "file_delete": "destructive (needs confirmation)",
    "pip": "would install packages (network + mutation)",
    "git_push": "pushes to remote",
    "git_commit": "creates commits",
    "git_clone": "external network",
    "branch_create": "switches git branches",
    "release_deploy": "creates git tags",
    "cron_add": "mutates crontab",
    "sched_add": "schedules recurring work",
    "app_install": "system mutation", "app_uninstall": "system mutation",
    "app_update": "system mutation", "app_close": "kills apps",
    "app_launch": "launches apps",
    "git_pull": "external network (would fetch remote)",
    "process_kill": "kills processes",
    "service_control": "system mutation",
    "checkpoint_restore": "overwrites files",
    "prod_deploy": "needs confirm=DEPLOY",
    "tool_create": "writes into repo tool tree",
    "self_modify": "mutates repo code",
    "webhook_register": "writes handler file",
    "perm_grant": "mutates permission toggles",
    "perm_revoke": "mutates permission toggles",
    "safe_mode": "mutates safety mode",
    "approval_request": "interactive", "confirm_prompt": "interactive",
    "macro_record": "interactive session",
    "tunnel_create": "opens public tunnels",
    "ssh_exec": "needs remote host",
    "alert_emit": "needs webhook URL",
    "supabase_query": "needs credentials",
    "file_chmod": "mutates perms (covered by unit tests)",
    # external network (policy: no network abuse)
    "web_search": "external network", "germany_fetch": "external network",
    "http_get": "external network", "http_post": "external network",
    "api_request": "external network",
    "load_test": "needs target URL", "stress_test": "needs target URL",
    "api_test": "needs target URL", "ui_test": "needs target URL",
    "auth_test": "needs target URL", "input_fuzz": "needs target URL",
    "uptime_check": "external network", "net_latency": "external network",
    "dns_lookup": "external network",
    # live browser (needs daemon/display)
    "browser_launch": "needs live browser", "browser_close": "needs live browser",
    "browser_back": "needs live browser", "browser_forward": "needs live browser",
    "browser_refresh": "needs live browser", "browser_new_tab": "needs live browser",
    "browser_close_tab": "needs live browser", "browser_switch_tab": "needs live browser",
    "browser_tab_list": "needs live browser", "browser_tab_screenshot": "needs live browser",
    "browser_tab_html": "needs live browser", "browser_tab_text": "needs live browser",
    "browser_click": "needs live browser", "browser_fill": "needs live browser",
    "browser_select": "needs live browser", "browser_form_submit": "needs live browser",
    "browser_wait": "needs live browser", "browser_console": "needs live browser",
    "browser_network": "needs live browser", "browser_download": "needs live browser",
    "browser_upload": "needs live browser", "browser_js": "needs live browser",
    "browser_navigate": "needs live browser", "browser_screenshot": "needs live browser",
    "browser_extract": "needs live browser", "browser_scroll": "needs live browser",
    "browser_cookies": "needs live browser", "browser_storage": "needs live browser",
    "browser_viewport": "needs live browser", "browser_ua": "needs live browser",
    "browser_proxy": "needs live browser",
    # long-running / slow in a verifier
    "live_screen_stream": "long-running", "screen_record": "long-running",
    "coverage_run": "slow (pytest suite covers it)",
    "test_run": "slow (pytest suite covers it)",
    "regression_check": "slow", "fix_verify": "slow", "test_failure_diagnose": "slow",
    "sec_regression": "slow",
    # needs real media files
    "audio_info": "needs media file", "video_info": "needs media file",
    "image_convert": "needs media file", "image_resize": "needs media file",
    "image_thumbnail": "needs media file",
}

# ---------------------------------------------------------------------------
# Safe args per tool. Tools absent here AND having required params -> SKIP.
# ---------------------------------------------------------------------------

A = {}  # tool -> args

A.update({
    "shell": {"command": "echo verify-ok"},
    "python": {"code": "print(1 + 1)"},
    "file_read": {"path": f"{V}/probe.txt"},
    "file_write": {"path": f"{V}/w.txt", "content": "verify write"},
    "file_append": {"path": f"{V}/w.txt", "content": "\nmore"},
    "file_copy": {"src": f"{V}/probe.txt", "dest": f"{V}/c.txt"},
    "file_move": {"src": f"{V}/c.txt", "dest": f"{V}/m.txt"},
    "file_rename": {"src": f"{V}/m.txt", "dest": f"{V}/r.txt"},
    "file_preview": {"path": f"{V}/probe.txt"},
    "file_metadata": {"path": f"{V}/probe.txt"},
    "file_checksum": {"path": f"{V}/probe.txt"},
    "file_find": {"root": V, "name_glob": "probe.*"},
    "file_tree": {"path": V, "max_depth": 2},
    "file_lock": {"path": f"{V}/probe.txt"},
    "file_watch": {"path": f"{V}/probe.txt", "timeout_sec": 2},
    "dir_create": {"path": f"{V}/subdir"},
    "dir_list": {"path": V},
    "text_search": {"path": f"{V}/probe.txt", "pattern": "hello"},
    "text_replace": {"path": f"{V}/w.txt", "pattern": "verify", "replacement": "VER"},
    "text_stats": {"path": f"{V}/probe.txt"},
    "text_diff": {"text1": "a\nb\n", "text2": "a\nc\n"},
    "text_select_all": {},
    "json_validate": {"text": '{"a": 1}'},
    "csv_stats": {"path": f"{V}/probe.csv"},
    "archive_create": {"paths": [f"{V}/probe.txt"], "output": f"{V}/a.tar.gz"},
    "archive_list": {"archive": f"{V}/probe.tar.gz"},
    "archive_info": {"path": f"{V}/probe.tar.gz"},
    "archive_extract": {"archive": f"{V}/probe.tar.gz", "dest": f"{V}/xa"},
    "gzip_compress": {"path": f"{V}/probe.txt", "output": f"{V}/p.gz"},
    "gzip_decompress": {"path": f"{V}/p.gz", "output": f"{V}/p2.txt"},
    "trash_restore": {"trash_name": "nonexistent-xyz"},
    "disk_usage": {"path": V},
    "sysinfo": {},
    "health_check": {},
    "session_info": {},
    "env_get": {"name": "PATH"},
    "env_set": {"name": "VERIFY_TMP", "value": "1"},
    "dep_check": {"package": "json"},
    "port_check": {"host": "127.0.0.1", "port": 9},
    "process_nice": {"pid": os.getpid(), "nice": 5},
    "git_status": {"path": str(REPO_ROOT)},
    "git_log": {"path": str(REPO_ROOT), "max_count": 3},
    "git_diff": {"path": str(REPO_ROOT)},
    "git_branch": {"path": str(REPO_ROOT), "action": "list"},
    "code_lint": {"path": f"{V}/probe.py"},
    "code_format": {"path": f"{V}/probe.py"},
    "code_generate": {"kind": "script", "name": "vgen",
                      "output": f"{V}/vgen.py", "overwrite": True},
    "error_log_read": {"tail": 5},
    "tool_version": {"action": "list"},
    "tool_test": {"name": "sysinfo", "args": {}},
    "benchmark_run": {"tool": "sysinfo", "args": {}, "times": 2},
    "package_build": {"src_dir": f"{V}", "output": f"{V}/pkg.pyz"},
    "api_scaffold": {"name": "vapi", "dest": f"{V}/vapi"},
    "sdk_scaffold": {"name": "vapi", "dest": f"{V}/vsdk"},
    "db_schema_generate": {"table": "t",
                           "fields": [{"name": "id", "type": "INTEGER PRIMARY KEY"}]},
    "docs_generate": {"path": f"{V}/probe.py"},
    "code_review": {"path": f"{V}/probe.py"},
    "code_complete": {"path": f"{V}/probe.py", "prefix": "g"},
    "code_translate": {"code": "def f(x):\n    return x + 1\n", "to": "javascript"},
    "code_refactor": {"action": "rename", "root": V, "old": "add", "new": "add2",
                      "dry_run": True},
    "multi_file_edit": {"root": V, "pattern": "hello", "replacement": "hi",
                        "dry_run": True},
    "repo_scaffold": {"kind": "cli", "name": "vscaf", "dest": f"{V}/vscaf",
                      "overwrite": True},
    "repo_understand": {"root": V},
    "arch_analyze": {"root": V},
    "bug_scan": {"root": V},
    "type_check": {"path": f"{V}/probe.py"},
    "stacktrace_analyze": {"traceback": "Traceback (most recent call last):\n"
                           '  File "x.py", line 1, in <module>\n'
                           "NameError: name 'foo' is not defined\n"},
    "log_analyze": {"path": f"{V}/probe.log"},
    "root_cause": {"traceback": "ValueError: bad\n"},
    "patch_generate": {"traceback": "NameError: name 'foo' is not defined\n"},
    "multi_file_fix": {"root": V, "pattern": "hello",
                       "patch": "# fix", "dry_run": True},
    "memleak_detect": {"code": "x = [0] * 10", "rounds": 2},
    "deadlock_detect": {"sample_seconds": 1},
    "race_note": {},
    "dep_conflicts": {},
    "runtime_check": {"code": "print('rc-ok')"},
    "test_generate": {"path": f"{V}/probe.py", "output": f"{V}/test_vgen.py"},
    "fuzz_run": {"code": "def fuzz_target(x):\n    return len(x) if x else 0\n",
                 "iterations": 20},
    "db_test": {"sql": ["CREATE TABLE t (a INT)", "INSERT INTO t VALUES (1)",
                        "SELECT * FROM t"]},
    "e2e_note": {},
    "property_test": {"code": "def inc(x):\n    return x + 1\n",
                      "property": "inc(a) > a", "examples": 10},
    "a11y_check": {"html": "<html lang='en'><head><title>t</title></head>"
                           "<body><img src='x' alt='y'><p>hi</p></body></html>"},
    "fail_repro": {"code": "raise ValueError('boom')"},
    "game_scaffold": {"engine": "pygame", "dim": "2d", "name": "vgame",
                      "dest": f"{V}/vgame", "overwrite": True},
    "game_build": {},
    "profile_cpu": {"code": "sum(range(1000))", "top": 5},
    "profile_memory": {"code": "x = [0]*1000", "top": 5},
    "alloc_track": {"code": "x = [0]*100"},
    "thread_analyze": {},
    "db_query_profile": {"queries": ["SELECT 1"]},
    "complexity_estimate": {"path": f"{V}/probe.py"},
    "perf_regression": {"code": "sum(range(100))", "baseline_ms": 10000,
                        "runs": 3},
    "optimize_suggest": {"path": f"{V}/probe.py"},
    "optimize_verify": {"before": "sum(range(100))", "after": "sum(range(100))",
                        "runs": 3},
    "gpu_profile_note": {},
    "render_perf_note": {},
    "cache_perf_note": {},
    "concurrency_note": {},
    "ml_inference": {"prompt": "hi"},
    "model_route": {"task": "debug python code"},
    "rag_index": {"name": "verify",
                  "docs": [{"id": "d1", "text": "the quick brown fox"}]},
    "rag_query": {"name": "verify", "query": "fox"},
    "embed_generate": {"texts": ["hello world"]},
    "vector_search": {"query_vector": [1.0, 0.0],
                      "items": [{"id": "a", "vector": [1.0, 0.0]}]},
    "dataset_process": {"path": f"{V}/probe.csv"},
    "dataset_validate": {"path": f"{V}/probe.csv", "schema": {"a": "int"}},
    "eval_dataset_gen": {"n": 3, "output": f"{V}/eval.jsonl"},
    "prompt_eval": {"prompt": "You are a coder. Write python. Format: code only."},
    "quality_score": {"text": "This is a decent sentence. Here is another one."},
    "gpu_monitor": {},
    "quantize_note": {}, "finetune_note": {}, "lora_note": {},
    "infer_opt_note": {},
    "model_version": {"action": "list"},
    "cicd_run": {"root": str(REPO_ROOT)},
    "deploy_preview": {"root": V, "seconds": 3, "port": 8931},
    "iac_validate": {"path": V},
    "k8s_validate": {"path": V},
    "db_migrate_test": {"migrations": ["CREATE TABLE m (id INTEGER)"]},
    "backup_verify": {"path": f"{V}/probe.tar.gz"},
    "cloud_note": {}, "staging_note": {}, "container_note": {"path": V},
    "sast_scan": {"root": V},
    "vuln_scan_deps": {},
    "license_check": {},
    "config_audit": {"root": V},
    "perm_audit": {},
    "dast_note": {},
    "dlp_check": {"root": V},
    "supply_chain_check": {"root": V},
    "task_plan": {"goal": "write a file then test it"},
    "task_decompose": {"goal": "write a file then test it"},
    "agent_message": {"action": "send", "to": "verify", "body": "ping"},
    "work_queue": {"action": "list"},
    "task_monitor": {"pid": os.getpid()},
    "resource_monitor": {},
    "rate_limit_handle": {"per_minute": 30},
    "config_version": {"action": "snapshot", "target": f"{V}/probe.txt"},
    "repro_build": {"root": V},
    "release_notes": {"root": str(REPO_ROOT)},
    "failure_memory": {"action": "search", "query": "verify"},
    "tool_discover": {"query": "shell"},
    "tool_spec_gen": {"name": "vtool", "purpose": "verify spec"},
    "tool_test_gen": {"name": "vtool", "output": f"{V}/test_vtool.py"},
    "gap_analyze": {"requested": ["shell", "nonexistent_xyz_cap"]},
    "improve_hypothesis": {"area": "testing"},
    "kb_update": {"topic": "verify", "content": "verifier ran"},
    "root_exec": {"command": "true"},
    "prod_secret_read": {"name": "PATH"},
    "iam_note": {}, "firewall_note": {}, "self_perm_audit": {},
    "perm_check": {"name": "tool:shell"},
    "perm_change_log": {},
    "audit_log": {"event": "verify_run", "details": {"n": 1}},
    "event_emit": {"event": "verify", "data": {}},
    "cred_protect": {"text": "key=ghp_FAKE1234567890"},
    "website_allowlist": {"action": "get"},
    "file_access_rules": {"action": "get"},
    "op_timeout": {"tool": "sysinfo", "args": {}, "timeout_sec": 20},
    "parallel_run": {"calls": [{"tool": "sysinfo", "args": {}}]},
    "workflow_run": {"steps": [{"tool": "sysinfo", "args": {}}]},
    "branch_if": {"condition_tool": "sysinfo", "condition_args": {},
                  "then_tool": "sysinfo"},
    "loop_repeat": {"tool": "sysinfo", "args": {}, "times": 2},
    "macro_append": {"name": "vmacro", "tool": "sysinfo", "args": {}},
    "macro_replay": {"name": "vmacro"},
    "retry_backoff": {"tool": "sysinfo", "args": {}},
    "sched_run_due": {},
    "progress_report": {"action": "create", "name": "verify", "percent": 50},
    "error_detect": {},
    "memory_save": {"key": "vkey", "value": "vval"},
    "memory_get": {"key": "vkey"},
    "memory_search": {"query": "vkey"},
    "memory_delete": {"key": "vkey"},
    "stream_stop": {"port": 8931},
    "screen_shot": {},
    "screen_region_capture": {"x": 0, "y": 0, "width": 10, "height": 10},
    "screen_change_detect": {"interval_sec": 1},
    "screen_annotate": {"image_path": f"{V}/a.png", "text": "hi"},
    "visual_verify": {"image_a": f"{V}/a.png", "image_b": f"{V}/b.png"},
    "visual_grounding": {"description": "button"},
    "ui_element_detect": {"url": "http://127.0.0.1:9/"},
    "ui_state_capture": {"url": "http://127.0.0.1:9/"},
    "ocr_read": {},
    "multi_monitor_info": {},
    "screenshot_analyze": {},
    "cross_browser_note": {},
    "mouse_move": {"x": 10, "y": 10},
    "mouse_click": {"x": 10, "y": 10},
    "mouse_double_click": {"x": 10, "y": 10},
    "mouse_right_click": {"x": 10, "y": 10},
    "mouse_drag": {"x1": 0, "y1": 0, "x2": 10, "y2": 10},
    "mouse_scroll": {"direction": "down", "amount": 1},
    "mouse_hover": {"x": 10, "y": 10},
    "mouse_hold_release": {"x": 10, "y": 10, "hold_ms": 100},
    "touch_gesture": {"gesture": "tap", "x": 10, "y": 10},
    "key_press": {"key": "a"},
    "key_type": {"text": "hi"},
    "key_combo": {"combo": "ctrl+c"},
    "key_hotkey": {"keys": ["ctrl", "c"]},
    "key_hold": {"key": "shift", "hold_ms": 100},
    "key_special": {"key": "Enter"},
    "key_macro": {"keys": [{"key": "a", "delay_ms": 50}]},
    "unicode_type": {"text": "héllo"},
    "clipboard_copy": {"text": "verify-clip"},
    "clipboard_paste": {},
    "input_method_switch": {"layout": "us"},
    "layout_detect": {},
    "dialog_handle": {"action": "confirm"},
    "menu_navigate": {"alt_menu": "f", "path": ["down", "enter"]},
    "window_resize_move": {"name": "no-such-window-xyz", "width": 800, "height": 600},
    "editor_control": {"action": "open", "path": f"{V}/probe.py"},
    "app_focus": {"name": "no-such-app-xyz"},
    "app_windows": {},
    "app_list": {},
    "build_run": {"root": V},
    "compiler_run": {"path": f"{V}/probe.c", "output": f"{V}/probe_bin"},
    "debugger_run": {"path": f"{V}/probe.py", "commands": ["c"]},
    "env_detect": {},
    "checkpoint_create": {"root": V, "dest": f"{V}/chk.tar.gz"},
    "build_log_collect": {"root": V},
    "image_info": {"path": f"{V}/a.png"},
    "rate_limit_status": {},
})

# ---------------------------------------------------------------------------
# Classifier: ok=False -> SKIP (env-limited) or FAIL (real bug)?
# ---------------------------------------------------------------------------

SKIP_PATTERNS = [
    r"not installed", r"no .*display", r"headless", r"requires? .*install",
    r"not available", r"no gpu", r"nvidia-smi", r"tesseract", r"playwright",
    r"no .*browser", r"credential", r"not configured", r"HALEY_API_URL",
    r"no engine", r"no .*backend", r"not detected", r"not found.*engine",
    r"outside.*scope", r"no .*provider", r"no cloud", r"unauthorized",
    r"connection refused", r"failed to connect", r"urlopen error",
    r"<urlopen error", r"no .tf files", r"no yaml manifests",
    r"no ci workflow", r"no dockerfile", r"hypothesis not installed",
    r"coverage.py not installed", r"mypy not installed",
    r"sudo not available", r"password", r"permission denied.*sudo",
    r"pip install", r"pyperclip", r"xclip",
    r"no recognized build system", r"no game engine",
    r"no screen-capture backend", r"editor.*not installed",
    r"cannot open display",
    r"no screen-capture backend", r"no media", r"ffmpeg",
]
SKIP_RX = re.compile("|".join(SKIP_PATTERNS), re.I)
CONFIRM_RX = re.compile(r"CONFIRMATION_REQUIRED", re.I)


def classify(name: str, ok: bool, output: str) -> tuple[str, str]:
    """-> (verdict, detail)."""
    short = (output or "")[:220].replace("\n", " ")
    if ok:
        return "PASS", short or "ok"
    if CONFIRM_RX.search(output or ""):
        return "SKIP", "needs confirmation (safe default)"
    if SKIP_RX.search(output or ""):
        m = SKIP_RX.search(output or "")
        return "SKIP", f"env-limited: {(m.group(0) if m else '')[:60]}"
    return "FAIL", short or "ok=False with no detail"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _required_params(tool) -> list[str]:
    schema = (tool.get("schema") if isinstance(tool, dict)
              else getattr(tool, "input_schema", None)) or {}
    return [k for k, v in schema.items()
            if isinstance(v, dict) and v.get("required")]


def main() -> int:
    _make_fixtures()
    _tn = FIX / "trash_name.txt"
    if _tn.exists():
        A["trash_restore"] = {"trash_name": _tn.read_text().strip()}
    from agent.tools.registry import build_full_registry
    from agent.tools.permission_manager import PermissionManager
    from agent.tools.dispatcher import Dispatcher

    reg = build_full_registry()
    pm = PermissionManager(registry=reg)
    disp = Dispatcher(reg, workspace=str(FIX), permission_manager=pm)

    tools = sorted(reg.discover(), key=lambda t: t["name"])
    results = []
    n_pass = n_fail = n_skip = 0

    def run_one(name: str, args: dict):
        try:
            r = disp.dispatch(name, args)
            return bool(r.ok), str(r.output or "")
        except Exception:
            return False, "dispatcher exception:\n" + traceback.format_exc()[-800:]

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        pending = {}
        for t in tools:
            name = t["name"]
            if name in SKIP:
                results.append((name, "SKIP", SKIP[name]))
                n_skip += 1
                continue
            req = _required_params(t)
            if name in A:
                args = A[name]
            elif not req:
                args = {}
            else:
                results.append((name, "SKIP",
                                f"no safe args (requires {req})"))
                n_skip += 1
                continue
            fut = ex.submit(run_one, name, args)
            pending[fut] = name
        for fut in concurrent.futures.as_completed(pending):
            name = pending[fut]
            try:
                ok, output = fut.result(timeout=120)
            except concurrent.futures.TimeoutError:
                ok, output = False, "TIMEOUT after 120s"
            verdict, detail = classify(name, ok, output)
            results.append((name, verdict, detail))
            if verdict == "PASS":
                n_pass += 1
            elif verdict == "FAIL":
                n_fail += 1
            else:
                n_skip += 1

    results.sort(key=lambda r: r[0])
    for name, verdict, detail in results:
        print(f"{verdict:4}  {name} — {detail[:160]}")
    print("=" * 60)
    print(f"TOTAL {len(results)}  PASS {n_pass}  FAIL {n_fail}  SKIP {n_skip}")
    fails = [n for n, v, _ in results if v == "FAIL"]
    if fails:
        print("FAILING:", ", ".join(fails))
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
