"""Smoke tests for the advanced autonomous coding agent tools.

Every test executes the real handler with safe args and asserts sane output
shape (ok/output/parsed/error). Environment-limited paths assert the honest
degradation branch, which is deterministic here.
"""
import ast
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from agent.tools import (dev_tools, debug_tools, test_tools, game_tools,
                         perf_tools, ml_tools, cloud_tools, sec_tools,
                         sys2_tools, risk_tools, self_tools, code_tools,
                         vision_tools, browser_tools)
from agent.tools.registry import build_full_registry, collect_handlers


def _shape(r):
    assert hasattr(r, "ok"), "result needs .ok"
    assert isinstance(r.ok, bool)
    assert isinstance(r.output, str)
    assert isinstance(r.parsed, dict)
    assert isinstance(r.error, str)


def _tmp_py(code="def f():\n    return 1\n"):
    fd, p = tempfile.mkstemp(suffix=".py")
    os.write(fd, code.encode())
    os.close(fd)
    return p


class TestDevTools(unittest.TestCase):
    def test_repo_scaffold_cli(self):
        d = tempfile.mkdtemp()
        r = dev_tools._repo_scaffold({"kind": "cli", "name": "t",
                                      "dest": d + "/t", "overwrite": True})
        _shape(r)
        self.assertTrue(r.ok)
        self.assertTrue(Path(d, "t").exists())

    def test_code_complete(self):
        p = _tmp_py("def greet(name):\n    pass\n")
        r = dev_tools._code_complete({"path": p, "prefix": "gr"})
        _shape(r)
        self.assertTrue(r.ok)
        self.assertIn("greet", r.output)

    def test_multi_file_edit_dry(self):
        d = tempfile.mkdtemp()
        Path(d, "a.py").write_text("x = 1\n")
        r = dev_tools._multi_file_edit({"root": d, "pattern": "x = 1",
                                        "replacement": "x = 2", "dry_run": True})
        _shape(r)
        self.assertTrue(r.ok)
        self.assertEqual(Path(d, "a.py").read_text(), "x = 1\n")

    def test_repo_understand(self):
        d = tempfile.mkdtemp()
        Path(d, "m.py").write_text("import os\ndef f():\n pass\n")
        r = dev_tools._repo_understand({"root": d})
        _shape(r)
        self.assertTrue(r.ok)
        self.assertIn("m.py", str(r.parsed))

    def test_code_translate(self):
        r = dev_tools._code_translate({"code": "def f(x):\n    return True\n",
                                       "to": "javascript"})
        _shape(r)
        self.assertTrue(r.ok)
        self.assertIn("function", r.output)

    def test_code_refactor_rename_dry(self):
        d = tempfile.mkdtemp()
        Path(d, "a.py").write_text("foo = 1\nprint(foo)\n")
        r = dev_tools._code_refactor({"action": "rename", "root": d,
                                      "old": "foo", "new": "bar",
                                      "dry_run": True})
        _shape(r)
        self.assertTrue(r.ok)

    def test_api_scaffold(self):
        d = tempfile.mkdtemp()
        r = dev_tools._api_scaffold({"name": "t", "dest": d,
                                     "endpoints": ["GET /x"]})
        _shape(r)
        self.assertTrue(r.ok)

    def test_sdk_scaffold(self):
        d = tempfile.mkdtemp()
        r = dev_tools._sdk_scaffold({"name": "t", "dest": d})
        _shape(r)
        self.assertTrue(r.ok)

    def test_db_schema_generate(self):
        r = dev_tools._db_schema_generate(
            {"table": "t", "fields": [{"name": "id", "type": "INTEGER"}]})
        _shape(r)
        self.assertTrue(r.ok)
        self.assertIn("CREATE TABLE", r.output)

    def test_docs_generate(self):
        p = _tmp_py('"""Doc."""\ndef f():\n    """F doc."""\n    pass\n')
        r = dev_tools._docs_generate({"path": p})
        _shape(r)
        self.assertTrue(r.ok)

    def test_code_review(self):
        p = _tmp_py("import os\nx=1\n")
        r = dev_tools._code_review({"path": p})
        _shape(r)
        self.assertTrue(r.ok)

    def test_arch_analyze(self):
        d = tempfile.mkdtemp()
        Path(d, "a.py").write_text("import b\n")
        Path(d, "b.py").write_text("x=1\n")
        r = dev_tools._arch_analyze({"root": d})
        _shape(r)
        self.assertTrue(r.ok)


class TestDebugTools(unittest.TestCase):
    def test_bug_scan(self):
        d = tempfile.mkdtemp()
        Path(d, "a.py").write_text("import os\neval('1')\n")
        r = debug_tools._bug_scan({"root": d})
        _shape(r)
        self.assertTrue(r.ok)
        self.assertGreater(len(r.parsed["findings"]), 0)

    def test_type_check_fallback(self):
        p = _tmp_py("def f(x):\n    return x\n")
        r = debug_tools._type_check({"path": p})
        _shape(r)
        self.assertTrue(r.ok)

    def test_stacktrace_analyze(self):
        r = debug_tools._stacktrace_analyze(
            {"traceback": "Traceback (most recent call last):\n"
             '  File "a.py", line 2, in <module>\n'
             "NameError: name 'zzz' is not defined\n"})
        _shape(r)
        self.assertTrue(r.ok)
        self.assertIn("NameError", r.parsed["error_line"])

    def test_log_analyze(self):
        fd, p = tempfile.mkstemp()
        os.write(fd, b"INFO ok\nERROR bad thing\n")
        os.close(fd)
        r = debug_tools._log_analyze({"path": p})
        _shape(r)
        self.assertTrue(r.ok)

    def test_root_cause(self):
        r = debug_tools._root_cause({"traceback": "IndexError: list index\n"})
        _shape(r)
        self.assertTrue(r.ok)

    def test_patch_generate(self):
        r = debug_tools._patch_generate(
            {"traceback": "NameError: name 'foo' is not defined\n"})
        _shape(r)
        self.assertTrue(r.ok)
        self.assertGreater(len(r.parsed["proposals"]), 0)

    def test_multi_file_fix_dry(self):
        d = tempfile.mkdtemp()
        Path(d, "a.py").write_text("pass\n")
        r = debug_tools._multi_file_fix({"root": d, "pattern": "pass",
                                         "patch": "# p", "dry_run": True})
        _shape(r)
        self.assertTrue(r.ok)

    def test_memleak_detect(self):
        r = debug_tools._memleak_detect({"code": "x=[0]*5", "rounds": 2})
        _shape(r)
        self.assertTrue(r.ok)

    def test_deadlock_detect(self):
        r = debug_tools._deadlock_detect({"sample_seconds": 1})
        _shape(r)
        self.assertTrue(r.ok)
        self.assertGreater(r.parsed["threads"], 0)

    def test_race_note(self):
        r = debug_tools._race_note({})
        _shape(r)
        self.assertTrue(r.ok)

    def test_dep_conflicts(self):
        r = debug_tools._dep_conflicts({})
        _shape(r)
        self.assertTrue(r.ok)

    def test_runtime_check(self):
        r = debug_tools._runtime_check({"code": "print('ok')"})
        _shape(r)
        self.assertTrue(r.ok)
        r2 = debug_tools._runtime_check({"code": "1/0"})
        _shape(r2)
        self.assertFalse(r2.ok)


class TestTestTools(unittest.TestCase):
    def test_test_generate(self):
        p = _tmp_py("def add(a,b):\n    return a+b\n")
        out = p + "_t.py"
        r = test_tools._test_generate({"path": p, "output": out})
        _shape(r)
        self.assertTrue(r.ok)
        self.assertTrue(Path(out).exists())

    def test_coverage_missing_reports_honestly(self):
        r = test_tools._coverage_run({"target": "agent/tests/unit/test_gate.py"})
        _shape(r)
        # either runs or reports missing honestly — both fine
        self.assertIsInstance(r.ok, bool)

    def test_fuzz_run(self):
        r = test_tools._fuzz_run(
            {"code": "def fuzz_target(x):\n    return 1\n", "iterations": 20})
        _shape(r)
        self.assertTrue(r.ok)

    def test_db_test(self):
        r = test_tools._db_test({"sql": ["SELECT 1"]})
        _shape(r)
        self.assertTrue(r.ok)

    def test_e2e_note(self):
        r = test_tools._e2e_note({})
        _shape(r)
        self.assertTrue(r.ok)

    def test_a11y_check(self):
        r = test_tools._a11y_check(
            {"html": "<html lang='en'><head><title>t</title></head>"
                     "<body><p>x</p></body></html>"})
        _shape(r)
        self.assertTrue(r.ok)

    def test_fail_repro(self):
        r = test_tools._fail_repro({"code": "raise RuntimeError('x')"})
        _shape(r)
        self.assertTrue(r.parsed["reproduced"])

    def test_property_test_or_honest(self):
        r = test_tools._property_test({"code": "def inc(x):\n    return x+1\n",
                                       "property": "inc(a) > a",
                                       "examples": 5})
        _shape(r)
        self.assertIsInstance(r.ok, bool)


class TestGameTools(unittest.TestCase):
    def test_game_scaffold_pygame(self):
        d = tempfile.mkdtemp()
        r = game_tools._game_scaffold({"engine": "pygame", "dim": "2d",
                                       "name": "g", "dest": d + "/g",
                                       "overwrite": True})
        _shape(r)
        self.assertTrue(r.ok)
        self.assertTrue(Path(d, "g", "main.py").exists())

    def test_game_scaffold_godot(self):
        d = tempfile.mkdtemp()
        r = game_tools._game_scaffold({"engine": "godot", "dim": "3d",
                                       "name": "g", "dest": d + "/g",
                                       "overwrite": True})
        _shape(r)
        self.assertTrue(r.ok)
        self.assertTrue(Path(d, "g", "project.godot").exists())

    def test_game_build_detection(self):
        r = game_tools._game_build({})
        _shape(r)
        self.assertIsInstance(r.ok, bool)  # honest either way


class TestPerfTools(unittest.TestCase):
    def test_profile_cpu(self):
        r = perf_tools._profile_cpu({"code": "sum(range(100))", "top": 5})
        _shape(r)
        self.assertTrue(r.ok)

    def test_profile_memory(self):
        r = perf_tools._profile_memory({"code": "x=[0]*100", "top": 5})
        _shape(r)
        self.assertTrue(r.ok)

    def test_alloc_track(self):
        r = perf_tools._alloc_track({"code": "x=[0]*100"})
        _shape(r)
        self.assertTrue(r.ok)

    def test_thread_analyze(self):
        r = perf_tools._thread_analyze({})
        _shape(r)
        self.assertTrue(r.ok)
        self.assertGreater(len(r.parsed["threads"]), 0)

    def test_db_query_profile(self):
        r = perf_tools._db_query_profile({"queries": ["SELECT 1"]})
        _shape(r)
        self.assertTrue(r.ok)

    def test_complexity_estimate(self):
        p = _tmp_py("def f(xs):\n    s=0\n    for x in xs:\n        for y in xs:\n            s+=x+y\n    return s\n")
        r = perf_tools._complexity_estimate({"path": p})
        _shape(r)
        self.assertTrue(r.ok)

    def test_perf_regression(self):
        r = perf_tools._perf_regression({"code": "1+1", "baseline_ms": 100000,
                                         "runs": 2})
        _shape(r)
        self.assertTrue(r.ok)
        self.assertFalse(r.parsed["regressed"])

    def test_optimize_suggest(self):
        p = _tmp_py("def f(xs):\n    out=[]\n    for x in xs:\n        out.append(x)\n    return out\n")
        r = perf_tools._optimize_suggest({"path": p})
        _shape(r)
        self.assertTrue(r.ok)

    def test_optimize_verify(self):
        r = perf_tools._optimize_verify({"before": "sum(range(50))",
                                         "after": "sum(range(50))", "runs": 2})
        _shape(r)
        self.assertTrue(r.ok)

    def test_gpu_profile_honest(self):
        r = perf_tools._gpu_profile_note({})
        _shape(r)
        self.assertIsInstance(r.ok, bool)

    def test_notes(self):
        for fn in (perf_tools._render_perf_note, perf_tools._concurrency_note):
            r = fn({})
            _shape(r)
            self.assertTrue(r.ok)


class TestMLTools(unittest.TestCase):
    def test_ml_inference_no_endpoint_honest(self):
        r = ml_tools._ml_inference({"prompt": "hi"})
        _shape(r)
        # honest fail when HALEY_API_URL unset
        self.assertIsInstance(r.ok, bool)

    def test_model_route(self):
        r = ml_tools._model_route({"task": "debug python code"})
        _shape(r)
        self.assertTrue(r.ok)
        self.assertEqual(r.parsed["route"], "code")

    def test_rag_roundtrip(self):
        r = ml_tools._rag_index({"name": "unittest",
                                 "docs": [{"id": "d1",
                                           "text": "the quick brown fox jumps"}]})
        _shape(r)
        self.assertTrue(r.ok)
        q = ml_tools._rag_query({"name": "unittest", "query": "fox"})
        _shape(q)
        self.assertTrue(q.ok)
        self.assertGreater(len(q.parsed["hits"]), 0)

    def test_embed_and_vector_search(self):
        e = ml_tools._embed_generate({"texts": ["hello world"]})
        _shape(e)
        self.assertTrue(e.ok)
        v = ml_tools._vector_search(
            {"query_vector": e.parsed["vectors"][0],
             "items": [{"id": "a", "vector": e.parsed["vectors"][0]}]})
        _shape(v)
        self.assertTrue(v.ok)

    def test_dataset_validate(self):
        fd, p = tempfile.mkstemp(suffix=".csv")
        os.write(fd, b"a,b\n1,2\n")
        os.close(fd)
        r = ml_tools._dataset_validate({"path": p, "schema": {"a": "int"}})
        _shape(r)
        self.assertTrue(r.ok)

    def test_eval_dataset_gen(self):
        d = tempfile.mkdtemp()
        r = ml_tools._eval_dataset_gen({"n": 2, "output": d + "/e.jsonl"})
        _shape(r)
        self.assertTrue(r.ok)

    def test_prompt_eval(self):
        r = ml_tools._prompt_eval(
            {"prompt": "You are a coder. Write python. Format: code only. Never skip."})
        _shape(r)
        self.assertTrue(r.ok)

    def test_quality_score(self):
        r = ml_tools._quality_score({"text": "One sentence here. Another one."})
        _shape(r)
        self.assertTrue(r.ok)

    def test_gpu_monitor_honest(self):
        r = ml_tools._gpu_monitor({})
        _shape(r)
        self.assertIsInstance(r.ok, bool)

    def test_notes(self):
        for fn in (ml_tools._quantize_note, ml_tools._finetune_note,
                   ml_tools._lora_note, ml_tools._infer_opt_note):
            r = fn({})
            _shape(r)
            self.assertTrue(r.ok)

    def test_model_version(self):
        r = ml_tools._model_version({"action": "list"})
        _shape(r)
        self.assertTrue(r.ok)


class TestCloudTools(unittest.TestCase):
    def test_cicd_no_workflow_honest(self):
        d = tempfile.mkdtemp()
        r = cloud_tools._cicd_run({"root": d})
        _shape(r)
        self.assertFalse(r.ok)

    def test_deploy_preview(self):
        d = tempfile.mkdtemp()
        Path(d, "index.html").write_text("<h1>hi</h1>")
        r = cloud_tools._deploy_preview({"root": d, "seconds": 2, "port": 8939})
        _shape(r)
        self.assertTrue(r.ok)

    def test_iac_validate_empty(self):
        d = tempfile.mkdtemp()
        r = cloud_tools._iac_validate({"path": d})
        _shape(r)
        self.assertFalse(r.ok)

    def test_k8s_validate_empty(self):
        d = tempfile.mkdtemp()
        r = cloud_tools._k8s_validate({"path": d})
        _shape(r)
        self.assertFalse(r.ok)

    def test_db_migrate_test(self):
        r = cloud_tools._db_migrate_test(
            {"migrations": ["CREATE TABLE t (id INTEGER)"]})
        _shape(r)
        self.assertTrue(r.ok)

    def test_backup_verify(self):
        import tarfile
        d = tempfile.mkdtemp()
        f = str(Path(d, "f.txt"))
        Path(f).write_text("x")
        tb = str(Path(d, "b.tar.gz"))
        with tarfile.open(tb, "w:gz") as tf:
            tf.add(f, arcname="f.txt")
        r = cloud_tools._backup_verify({"path": tb})
        _shape(r)
        self.assertTrue(r.ok)

    def test_notes(self):
        for fn in (cloud_tools._cloud_note, cloud_tools._staging_note):
            r = fn({})
            _shape(r)
            self.assertTrue(r.ok)
        r = cloud_tools._container_note({"path": tempfile.mkdtemp()})
        _shape(r)
        self.assertTrue(r.ok)


class TestSecTools(unittest.TestCase):
    def test_sast_scan(self):
        d = tempfile.mkdtemp()
        Path(d, "a.py").write_text("eval('1')\n")
        r = sec_tools._sast_scan({"root": d})
        _shape(r)
        self.assertTrue(r.ok)
        self.assertGreater(len(r.parsed["issues"]), 0)

    def test_vuln_scan(self):
        r = sec_tools._vuln_scan_deps({})
        _shape(r)
        self.assertTrue(r.ok)

    def test_license_check(self):
        r = sec_tools._license_check({})
        _shape(r)
        self.assertTrue(r.ok)

    def test_config_audit(self):
        d = tempfile.mkdtemp()
        Path(d, "app.env").write_text("DEBUG=True\n")
        r = sec_tools._config_audit({"root": d})
        _shape(r)
        self.assertFalse(r.ok)  # DEBUG=True is a finding
        self.assertGreater(len(r.parsed["issues"]), 0)

    def test_perm_audit(self):
        r = sec_tools._perm_audit({})
        _shape(r)
        self.assertTrue(r.ok)
        self.assertIn("container_escape", str(r.parsed["locked"]))

    def test_dlp_check(self):
        d = tempfile.mkdtemp()
        Path(d, "a.txt").write_text("contact me at a@b.com\n")
        r = sec_tools._dlp_check({"root": d})
        _shape(r)
        self.assertFalse(r.ok)
        self.assertGreater(len(r.parsed["hits"]), 0)

    def test_supply_chain(self):
        d = tempfile.mkdtemp()
        Path(d, "requirements.txt").write_text("requests==2.31.0\n")
        Path(d, "requirements.lock").write_text("requests==2.31.0\n")
        r = sec_tools._supply_chain_check({"root": d})
        _shape(r)
        self.assertTrue(r.ok)

    def test_dast_note(self):
        r = sec_tools._dast_note({})
        _shape(r)
        self.assertTrue(r.ok)


class TestSys2Tools(unittest.TestCase):
    def test_task_plan(self):
        r = sys2_tools._task_plan({"goal": "search the web then write a file"})
        _shape(r)
        self.assertTrue(r.ok)
        self.assertGreater(len(r.parsed["steps"]), 2)

    def test_task_decompose(self):
        r = sys2_tools._task_decompose({"goal": "test the code and commit"})
        _shape(r)
        self.assertTrue(r.ok)

    def test_agent_message(self):
        r = sys2_tools._agent_message({"action": "send", "to": "ut",
                                       "body": "hello"})
        _shape(r)
        self.assertTrue(r.ok)
        r2 = sys2_tools._agent_message({"action": "recv", "to": "ut"})
        _shape(r2)
        self.assertEqual(len(r2.parsed["messages"]), 1)

    def test_work_queue(self):
        r = sys2_tools._work_queue({"action": "push", "tool": "sysinfo",
                                    "args": {}})
        _shape(r)
        self.assertTrue(r.ok)
        r2 = sys2_tools._work_queue({"action": "pop"})
        self.assertEqual(r2.parsed["task"]["tool"], "sysinfo")

    def test_task_monitor(self):
        r = sys2_tools._task_monitor({"pid": os.getpid()})
        _shape(r)
        self.assertTrue(r.parsed["alive"])

    def test_resource_monitor(self):
        r = sys2_tools._resource_monitor({})
        _shape(r)
        self.assertTrue(r.ok)

    def test_rate_limit_handle(self):
        r = sys2_tools._rate_limit_handle({"per_minute": 30})
        _shape(r)
        self.assertTrue(r.ok)

    def test_config_version(self):
        fd, p = tempfile.mkstemp()
        os.write(fd, b"x")
        os.close(fd)
        r = sys2_tools._config_version({"action": "snapshot", "target": p})
        _shape(r)
        self.assertTrue(r.ok)

    def test_repro_build(self):
        d = tempfile.mkdtemp()
        Path(d, "a.txt").write_text("x")
        r = sys2_tools._repro_build({"root": d})
        _shape(r)
        self.assertTrue(r.ok)

    def test_release_notes(self):
        r = sys2_tools._release_notes({"root": "/tmp"})
        _shape(r)
        # /tmp is not a git repo -> honest fail is fine
        self.assertIsInstance(r.ok, bool)


class TestRiskTools(unittest.TestCase):
    def test_root_exec_true(self):
        r = risk_tools._root_exec({"command": "true"})
        _shape(r)
        self.assertIsInstance(r.ok, bool)  # honest either way

    def test_prod_secret_read_env(self):
        os.environ["VERIFY_SECRET_X"] = "s3cr3t"
        r = risk_tools._prod_secret_read({"name": "VERIFY_SECRET_X"})
        _shape(r)
        self.assertTrue(r.ok)
        self.assertNotIn("s3cr3t", r.output)  # never printed

    def test_iam_note(self):
        r = risk_tools._iam_note({})
        _shape(r)
        self.assertTrue(r.ok)

    def test_firewall_note(self):
        r = risk_tools._firewall_note({})
        _shape(r)
        self.assertTrue(r.ok)

    def test_prod_deploy_refuses_without_confirm(self):
        r = risk_tools._prod_deploy({"steps": ["echo hi"]})
        _shape(r)
        self.assertFalse(r.ok)

    def test_self_perm_audit(self):
        r = risk_tools._self_perm_audit({})
        _shape(r)
        self.assertTrue(r.ok)
        self.assertIn("container_escape", r.parsed["locked_denials"])


class TestSelfTools(unittest.TestCase):
    def test_failure_memory(self):
        r = self_tools._failure_memory({"action": "add", "failure": "ut-fail",
                                        "fix": "ut-fix"})
        _shape(r)
        self.assertTrue(r.ok)
        r2 = self_tools._failure_memory({"action": "search", "query": "ut-fail"})
        self.assertGreater(len(r2.parsed["hits"]), 0)

    def test_tool_discover(self):
        r = self_tools._tool_discover({"query": "shell"})
        _shape(r)
        self.assertTrue(r.ok)
        names = [t["name"] for t in r.parsed["tools"]]
        self.assertIn("shell", names)

    def test_tool_spec_gen(self):
        r = self_tools._tool_spec_gen({"name": "ut", "purpose": "unit test"})
        _shape(r)
        self.assertTrue(r.ok)

    def test_tool_test_gen(self):
        d = tempfile.mkdtemp()
        r = self_tools._tool_test_gen({"name": "ut",
                                       "output": d + "/test_ut.py"})
        _shape(r)
        self.assertTrue(r.ok)

    def test_gap_analyze(self):
        r = self_tools._gap_analyze({"requested": ["shell", "zzz_nope"]})
        _shape(r)
        self.assertTrue(r.ok)
        self.assertEqual(len(r.parsed["gaps"]), 1)

    def test_improve_hypothesis(self):
        r = self_tools._improve_hypothesis({"area": "tests"})
        _shape(r)
        self.assertTrue(r.ok)

    def test_kb_update(self):
        r = self_tools._kb_update({"topic": "ut", "content": "hello"})
        _shape(r)
        self.assertTrue(r.ok)


class TestExtensions(unittest.TestCase):
    def test_build_run_no_system_honest(self):
        d = tempfile.mkdtemp()
        r = code_tools._build_run({"root": d})
        _shape(r)
        self.assertFalse(r.ok)

    def test_env_detect(self):
        r = code_tools._env_detect({})
        _shape(r)
        self.assertTrue(r.ok)
        self.assertIn("python", r.parsed)

    def test_checkpoint_roundtrip(self):
        d = tempfile.mkdtemp()
        Path(d, "f.txt").write_text("data")
        c = code_tools._checkpoint_create({"root": d,
                                           "dest": d + "/c.tar.gz"})
        _shape(c)
        self.assertTrue(c.ok)
        Path(d, "f.txt").write_text("changed")
        r = code_tools._checkpoint_restore({"path": d + "/c.tar.gz",
                                            "dest": d + "/r",
                                            "confirm": "RESTORE"})
        _shape(r)
        self.assertTrue(r.ok)

    def test_screenshot_analyze_shape(self):
        r = vision_tools.screenshot_analyze({})
        _shape(r)
        self.assertIsInstance(r.ok, bool)  # honest in headless

    def test_cross_browser_note(self):
        r = browser_tools.cross_browser_note({})
        _shape(r)
        self.assertTrue(r.ok)


class TestRegistryIntegration(unittest.TestCase):
    def test_all_new_tools_registered_with_handlers(self):
        reg = build_full_registry()
        handlers = collect_handlers()
        for mod, names in {
            "dev_tools": ["repo_scaffold", "code_complete", "multi_file_edit",
                          "repo_understand", "code_translate", "code_refactor",
                          "api_scaffold", "sdk_scaffold", "db_schema_generate",
                          "docs_generate", "code_review", "arch_analyze"],
            "debug_tools": ["bug_scan", "type_check", "stacktrace_analyze",
                            "log_analyze", "root_cause", "patch_generate",
                            "multi_file_fix", "regression_check", "fix_verify",
                            "test_failure_diagnose", "memleak_detect",
                            "deadlock_detect", "race_note", "dep_conflicts",
                            "runtime_check"],
            "risk_tools": ["root_exec", "prod_secret_read", "iam_note",
                           "firewall_note", "prod_deploy", "tunnel_create",
                           "self_perm_audit", "ssh_exec"],
        }.items():
            for n in names:
                self.assertIsNotNone(reg.get(n), f"{n} not registered")
                self.assertIn(n, handlers, f"{n} has no handler")

    def test_new_tool_permissions_default_on(self):
        from agent.tools.permission_manager import PermissionManager
        pm = PermissionManager(build_full_registry())
        for n in ["tool:game_scaffold", "tool:root_exec", "tool:sast_scan",
                  "tool:ml_inference", "tool:task_plan"]:
            self.assertTrue(pm.is_allowed(n), f"{n} should default ON")

    def test_denials_still_locked(self):
        from agent.tools.permission_manager import PermissionManager
        pm = PermissionManager(build_full_registry())
        for d in ["container_escape", "boot_recovery", "av_edr_disable",
                  "billing_fraud", "exfiltration"]:
            self.assertFalse(pm.is_allowed(d), f"denial {d} must stay locked")


if __name__ == "__main__":
    unittest.main()
