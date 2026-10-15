"""Unit tests for the verification modules.

Covers:
    result_verifier:   SchemaValidator, format validators, strategies,
                       ResultVerifier orchestration, confidence scoring,
                       gate decisions, contracts, RetryAdvisor
    consistency_checker: similarity primitives, TemporalFactStore,
                       ConsistencyChecker analyses, DriftDetector,
                       ClaimExtractor, SnapshotTracker
    proof_collector:   Evidence hashing, chain of custody, ProofBundle
                       export/import/verify, tamper detection, Merkle proofs,
                       ChainOfCustodyLog, redaction
"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from agent.verification.result_verifier import (
    ResultVerifier, VerificationReport, CheckResult, Severity,
    SchemaValidator, validate_format, detect_error_patterns,
    detect_fabrication_markers, is_empty_output, check_json_parseable,
    ShellOutputVerifier, PythonOutputVerifier, WebContentVerifier,
    FileOperationVerifier, CrossReference, OutputContract,
    DEFAULT_CONTRACTS, COMMON_SCHEMAS, RetryAdvisor, verify_result,
    values_agree, normalize_for_comparison,
)
from agent.verification.consistency_checker import (
    ConsistencyChecker, Contradiction, ContradictionSeverity,
    ConflictResolutionStrategy, TemporalFactStore, VerifiedFact,
    ConsistencyReport, fuzzy_ratio, token_jaccard, numeric_close,
    extract_numbers, semantic_similarity, DriftDetector, ClaimExtractor,
    Snapshot, SnapshotTracker, diff_snapshots,
)
from agent.verification.proof_collector import (
    ProofCollector, Evidence, ProofBundle, EvidenceType,
    sha256_text, merkle_root, merkle_proof, verify_merkle_proof,
    EvidenceQuery, redact_evidence, ChainOfCustodyLog,
    cosign_bundle, verify_cosignatures, compare_bundles,
)


# ===========================================================================
# result_verifier
# ===========================================================================

class TestFormatValidators(unittest.TestCase):
    def test_url_valid(self):
        ok, _ = validate_format("https://example.com/path?q=1", "url")
        self.assertTrue(ok)

    def test_url_bad_scheme(self):
        ok, why = validate_format("gopher://x", "url")
        self.assertFalse(ok)
        self.assertIn("scheme", why)

    def test_email(self):
        self.assertTrue(validate_format("a@b.com", "email")[0])
        self.assertFalse(validate_format("not-an-email", "email")[0])

    def test_ip(self):
        self.assertTrue(validate_format("192.168.1.1", "ipv4")[0])
        self.assertFalse(validate_format("192.168.1.1", "ipv6")[0])
        self.assertTrue(validate_format("::1", "ip")[0])
        self.assertFalse(validate_format("999.1.1.1", "ip")[0])

    def test_date(self):
        self.assertTrue(validate_format("2026-10-15", "date")[0])
        self.assertFalse(validate_format("15/10/2026", "date")[0])

    def test_datetime(self):
        self.assertTrue(validate_format("2026-10-15T13:00:00Z", "datetime")[0])
        self.assertFalse(validate_format("yesterday", "datetime")[0])

    def test_uuid(self):
        self.assertTrue(validate_format("123e4567-e89b-12d3-a456-426614174000", "uuid")[0])
        self.assertFalse(validate_format("xyz", "uuid")[0])

    def test_sha256(self):
        self.assertTrue(validate_format("a" * 64, "sha256")[0])
        self.assertFalse(validate_format("a" * 63, "sha256")[0])

    def test_semver(self):
        self.assertTrue(validate_format("1.2.3", "semver")[0])
        self.assertFalse(validate_format("1.2", "semver")[0])

    def test_unknown_format(self):
        ok, why = validate_format("x", "frobnicator")
        self.assertFalse(ok)
        self.assertIn("unknown format", why)


class TestSchemaValidator(unittest.TestCase):
    def test_simple_types(self):
        v = SchemaValidator({"type": "object", "required": ["a"],
                             "properties": {"a": {"type": "integer"}}})
        self.assertTrue(v.validate({"a": 1})[0])
        ok, errors = v.validate({"a": "x"})
        self.assertFalse(ok)
        self.assertTrue(any("expected integer" in e for e in errors))

    def test_bool_is_not_integer(self):
        v = SchemaValidator({"type": "integer"})
        self.assertFalse(v.validate(True)[0])

    def test_nested_and_required(self):
        v = SchemaValidator({"type": "object", "required": ["user"],
                             "properties": {"user": {
                                 "type": "object", "required": ["name"],
                                 "properties": {"name": {"type": "string"}}}}})
        ok, errors = v.validate({"user": {}})
        self.assertFalse(ok)
        self.assertTrue(any("missing required property 'name'" in e for e in errors))

    def test_enum_and_range(self):
        v = SchemaValidator({"type": "integer", "enum": [1, 2, 3],
                             "minimum": 1, "maximum": 3})
        self.assertTrue(v.validate(2)[0])
        self.assertFalse(v.validate(4)[0])

    def test_array_items_unique(self):
        v = SchemaValidator({"type": "array", "items": {"type": "string"},
                             "minItems": 1, "uniqueItems": True})
        self.assertTrue(v.validate(["a", "b"])[0])
        self.assertFalse(v.validate(["a", "a"])[0])
        self.assertFalse(v.validate([])[0])

    def test_pattern(self):
        v = SchemaValidator({"type": "string", "pattern": r"^\d+$"})
        self.assertTrue(v.validate("123")[0])
        self.assertFalse(v.validate("abc")[0])

    def test_format_in_schema(self):
        v = SchemaValidator({"type": "string", "format": "email"})
        self.assertTrue(v.validate("a@b.com")[0])
        self.assertFalse(v.validate("nope")[0])

    def test_coerce(self):
        v = SchemaValidator({"type": "integer", "coerce": True})
        ok, _ = v.validate("42")
        self.assertTrue(ok)
        self.assertFalse(v.validate("abc")[0])

    def test_additional_properties_false(self):
        v = SchemaValidator({"type": "object", "properties": {"a": {"type": "any"}},
                             "additionalProperties": False})
        self.assertFalse(v.validate({"a": 1, "b": 2})[0])

    def test_validate_or_raise(self):
        v = SchemaValidator({"type": "string"})
        with self.assertRaises(ValueError):
            v.validate_or_raise(123)


class TestSanityHelpers(unittest.TestCase):
    def test_detect_error_patterns(self):
        hits = detect_error_patterns("Traceback (most recent call last):\n  ValueError: x")
        self.assertTrue(any(h["pattern"] == "python_traceback" for h in hits))

    def test_detect_fabrication(self):
        hits = detect_fabrication_markers("result: [TODO: fill in]")
        self.assertTrue(any(h["marker"] == "placeholder_brackets" for h in hits))
        self.assertEqual(detect_fabrication_markers("clean output"), [])

    def test_is_empty_output(self):
        self.assertTrue(is_empty_output(None)[0])
        self.assertTrue(is_empty_output("   ")[0])
        self.assertTrue(is_empty_output("null")[0])
        self.assertTrue(is_empty_output([])[0])
        self.assertFalse(is_empty_output("data")[0])
        self.assertFalse(is_empty_output({"a": 1})[0])

    def test_json_parseable(self):
        ok, val, _ = check_json_parseable('{"a": 1}')
        self.assertTrue(ok)
        self.assertEqual(val, {"a": 1})
        ok, _, err = check_json_parseable("{bad")
        self.assertFalse(ok)
        self.assertIn("line", err)

    def test_values_agree_numbers(self):
        ok, _ = values_agree(100.0, 100.5, numeric_tolerance=0.01)
        self.assertTrue(ok)
        ok, _ = values_agree(100.0, 200.0)
        self.assertFalse(ok)

    def test_values_agree_strings(self):
        ok, _ = values_agree("Hello World", "hello   world")
        self.assertTrue(ok)
        ok, _ = values_agree("apple", "orange")
        self.assertFalse(ok)

    def test_normalize(self):
        self.assertEqual(normalize_for_comparison("  Hello\tWorld\n"), "hello world")


class TestStrategies(unittest.TestCase):
    def test_shell_success(self):
        s = ShellOutputVerifier()
        checks = s.verify({"command": "echo hi", "stdout": "hi", "stderr": "", "exit_code": 0})
        by_name = {c.name: c for c in checks}
        self.assertTrue(by_name["exit_code_zero"].passed)
        self.assertTrue(by_name["no_error_patterns"].passed)

    def test_shell_failure(self):
        s = ShellOutputVerifier()
        checks = s.verify({"command": "bad", "stdout": "", "stderr": "command not found", "exit_code": 127})
        by_name = {c.name: c for c in checks}
        self.assertFalse(by_name["exit_code_zero"].passed)
        self.assertFalse(by_name["stderr_clean"].passed)

    def test_python_traceback(self):
        s = PythonOutputVerifier()
        checks = s.verify({"stdout": "", "stderr": "Traceback (most recent call last):\nValueError: x", "exit_code": 1})
        by_name = {c.name: c for c in checks}
        self.assertFalse(by_name["no_traceback"].passed)
        self.assertEqual(by_name["no_traceback"].severity, Severity.CRITICAL)

    def test_python_json(self):
        s = PythonOutputVerifier()
        checks = s.verify({"stdout": '{"ok": true}', "stderr": "", "exit_code": 0, "expect_json": True})
        by_name = {c.name: c for c in checks}
        self.assertTrue(by_name["json_parseable"].passed)

    def test_web_ok(self):
        s = WebContentVerifier()
        checks = s.verify({"content": "<html><body>hello world, this is content</body></html>",
                           "status_code": 200, "url": "https://example.com"})
        by_name = {c.name: c for c in checks}
        self.assertTrue(by_name["http_status_ok"].passed)
        self.assertTrue(by_name["url_valid"].passed)

    def test_web_404_and_bot_wall(self):
        s = WebContentVerifier()
        checks = s.verify({"content": "Please complete the captcha to verify you are human " * 10,
                           "status_code": 404, "url": "https://example.com"})
        by_name = {c.name: c for c in checks}
        self.assertFalse(by_name["http_status_ok"].passed)
        self.assertFalse(by_name["no_soft_error_page"].passed)

    def test_file_write_python_syntax(self):
        s = FileOperationVerifier()
        checks = s.verify({"operation": "write", "path": "x.py",
                           "content": "def f(:\n  bad"})
        by_name = {c.name: c for c in checks}
        self.assertFalse(by_name["written_python_syntax"].passed)

    def test_file_hash_match(self):
        import hashlib
        content = "hello"
        s = FileOperationVerifier()
        checks = s.verify({"operation": "write", "path": "x.txt", "content": content,
                           "expected_sha256": hashlib.sha256(b"hello").hexdigest()})
        by_name = {c.name: c for c in checks}
        self.assertTrue(by_name["content_hash_match"].passed)


class TestResultVerifier(unittest.TestCase):
    def setUp(self):
        self.v = ResultVerifier(schemas=dict(COMMON_SCHEMAS))

    def test_passing_shell(self):
        r = self.v.verify("shell", {"command": "echo hi", "stdout": "hi",
                                    "stderr": "", "exit_code": 0})
        self.assertIsInstance(r, VerificationReport)
        self.assertTrue(r.passed)
        self.assertGreaterEqual(r.confidence, 0.6)
        allowed, reason = self.v.gate(r)
        self.assertTrue(allowed)
        self.assertIn("ALLOWED", reason)

    def test_failing_python_blocks(self):
        r = self.v.verify("python", {"stdout": "",
                                     "stderr": "Traceback (most recent call last):\nValueError",
                                     "exit_code": 1})
        self.assertFalse(r.passed)
        allowed, reason = self.v.gate(r)
        self.assertFalse(allowed)
        self.assertIn("BLOCKED", reason)

    def test_schema_violation_fails(self):
        r = self.v.verify("shell", {"stdout": "x"})  # missing exit_code
        self.assertFalse(r.passed)
        self.assertTrue(any(c.name == "schema_valid" and not c.passed
                            for c in r.checks))

    def test_cross_reference_agree(self):
        xref = CrossReference("second_run", lambda: "42")
        r = self.v.verify("shell", {"stdout": "42", "stderr": "", "exit_code": 0},
                          cross_references=[xref], expected="42")
        self.assertTrue(all(c.passed for c in r.checks if c.name.startswith("xref")))

    def test_cross_reference_disagree(self):
        xref = CrossReference("second_run", lambda: "43")
        r = self.v.verify("shell", {"stdout": "42", "stderr": "", "exit_code": 0},
                          cross_references=[xref])
        self.assertTrue(any(c.name == "xref_second_run" and not c.passed
                            for c in r.checks))

    def test_expected_mismatch(self):
        r = self.v.verify("shell", {"stdout": "apple", "stderr": "", "exit_code": 0},
                          expected="orange")
        self.assertTrue(any(c.name == "matches_expected" and not c.passed
                            for c in r.checks))

    def test_unknown_tool_generic_checks(self):
        r = self.v.verify("mystery_tool", {"data": "hello"})
        self.assertTrue(any(c.name == "non_empty_result" for c in r.checks))

    def test_report_serialization(self):
        r = self.v.verify("shell", {"stdout": "hi", "stderr": "", "exit_code": 0})
        d = r.to_dict()
        self.assertIn("checks", d)
        j = r.to_json()
        self.assertEqual(json.loads(j)["tool_name"], "shell")
        self.assertIn("PASSED", r.summary())

    def test_confidence_breakdown_present(self):
        r = self.v.verify("shell", {"stdout": "hi", "stderr": "", "exit_code": 0})
        self.assertIn("confidence_breakdown", r.evidence)
        self.assertIn("checks", r.evidence["confidence_breakdown"])

    def test_batch_verify(self):
        reports = self.v.batch_verify([
            ("shell", {"stdout": "a", "stderr": "", "exit_code": 0}),
            ("shell", {"stdout": "", "stderr": "boom", "exit_code": 1}),
        ])
        self.assertEqual(len(reports), 2)
        self.assertTrue(reports[0].passed)
        self.assertFalse(reports[1].passed)

    def test_convenience_wrappers(self):
        r = self.v.verify_shell("ls", "a\nb", "", 0)
        self.assertTrue(r.passed)
        r = self.v.verify_python("ok", "", 0)
        self.assertTrue(r.passed)


class TestOutputContract(unittest.TestCase):
    def test_contract_pass(self):
        c = DEFAULT_CONTRACTS["web_fetch"]
        checks = c.check({"content": "x" * 50, "status_code": 200,
                          "url": "https://example.com"})
        self.assertTrue(all(x.passed for x in checks))

    def test_contract_missing_key(self):
        c = DEFAULT_CONTRACTS["web_fetch"]
        checks = c.check({"content": "x"})
        failed = [x for x in checks if not x.passed]
        self.assertTrue(any("status_code" in x.name for x in failed))

    def test_contract_forbidden_key(self):
        c = OutputContract(tool_name="t", forbidden_keys=["secret"])
        checks = c.check({"secret": "abc"})
        self.assertFalse(all(x.passed for x in checks))

    def test_verify_result_applies_contract(self):
        r = verify_result("shell", {"stdout": "hi", "stderr": "", "exit_code": 0})
        self.assertTrue(r.passed)


class TestRetryAdvisor(unittest.TestCase):
    def test_traceback_advises_abort(self):
        v = ResultVerifier()
        r = v.verify("python", {"stdout": "", "stderr": "Traceback (most recent call last):",
                                "exit_code": 1})
        advice = RetryAdvisor().advise(r)
        self.assertEqual(advice["action"], "abort")
        self.assertTrue(advice["suggestions"])

    def test_empty_output_suggests_fix(self):
        v = ResultVerifier()
        r = v.verify("shell", {"stdout": "", "stderr": "", "exit_code": 0})
        advice = RetryAdvisor().advise(r)
        self.assertIn("retry", advice["action"])
        self.assertTrue(any("empty" in s for s in advice["suggestions"]))


# ===========================================================================
# consistency_checker
# ===========================================================================

class TestSimilarity(unittest.TestCase):
    def test_fuzzy_ratio(self):
        self.assertEqual(fuzzy_ratio("", ""), 1.0)
        self.assertEqual(fuzzy_ratio("abc", ""), 0.0)
        self.assertGreater(fuzzy_ratio("hello world", "hello world!"), 0.9)

    def test_token_jaccard(self):
        self.assertAlmostEqual(token_jaccard("a b c", "a b c"), 1.0)
        self.assertLess(token_jaccard("a b c", "x y z"), 0.5)

    def test_numeric_close(self):
        ok, rel = numeric_close(100.0, 101.0, rel_tol=0.02)
        self.assertTrue(ok)
        ok, _ = numeric_close(100.0, 150.0, rel_tol=0.02)
        self.assertFalse(ok)

    def test_extract_numbers(self):
        self.assertEqual(extract_numbers("costs 1,234.50 and 7"), [1234.5, 7.0])

    def test_semantic_similarity(self):
        s = semantic_similarity("disk free: 81.5 GB", "disk free: 81.5 GB")
        self.assertGreater(s, 0.9)
        s2 = semantic_similarity("disk free: 81.5 GB", "the cat sat on the mat")
        self.assertLess(s2, 0.45)


class TestTemporalFactStore(unittest.TestCase):
    def test_record_and_latest(self):
        store = TemporalFactStore()
        store.record("k", "v1", source="s1", confidence=0.9)
        store.record("k", "v2", source="s2", confidence=0.8)
        latest = store.latest("k")
        self.assertEqual(latest.value, "v2")
        self.assertEqual(len(store.history("k")), 2)

    def test_ttl_expiry(self):
        store = TemporalFactStore()
        store.record("k", "v", source="s", ttl_seconds=0.05)
        self.assertIsNotNone(store.latest("k"))
        time.sleep(0.08)
        self.assertIsNone(store.latest("k"))
        self.assertEqual(store.prune_expired(), 1)

    def test_normalize_key(self):
        store = TemporalFactStore()
        store.record("  My Key ", "v", source="s")
        self.assertIsNotNone(store.latest("my key"))

    def test_serialization_roundtrip(self):
        store = TemporalFactStore()
        store.record("k", {"n": 1}, source="s", confidence=0.5)
        store2 = TemporalFactStore.from_dict(store.to_dict())
        self.assertEqual(store2.latest("k").value, {"n": 1})


class TestConsistencyChecker(unittest.TestCase):
    def setUp(self):
        self.ck = ConsistencyChecker()

    def test_temporal_agree(self):
        self.ck.fact_store.record("temp", 20.0, source="s", confidence=0.9,
                                  ttl_seconds=60)
        r = self.ck.check_temporal("temp", 20.1, "s2")
        self.assertTrue(r.consistent)

    def test_temporal_contradiction(self):
        self.ck.fact_store.record("disk", 81.5, source="shell", confidence=0.95,
                                  ttl_seconds=3600)
        r = self.ck.check_temporal("disk", 12.0, "shell2")
        self.assertFalse(r.consistent)
        self.assertEqual(r.contradictions[0].severity, ContradictionSeverity.CRITICAL)

    def test_temporal_bool_flip(self):
        self.ck.fact_store.record("up", True, source="s", confidence=0.9,
                                  ttl_seconds=60)
        r = self.ck.check_temporal("up", False, "s2")
        self.assertFalse(r.consistent)

    def test_temporal_no_prior(self):
        r = self.ck.check_temporal("nope", 1, "s")
        self.assertTrue(r.consistent)

    def test_cross_run_consistent(self):
        self.ck.record_run("shell", {"cmd": "x"}, "output text here", 0.9)
        r = self.ck.check_cross_run("shell", {"cmd": "x"}, "output text here!")
        self.assertTrue(r.consistent)

    def test_cross_run_contradiction(self):
        self.ck.record_run("shell", {"cmd": "x"}, "the service is running fine", 0.9)
        r = self.ck.check_cross_run("shell", {"cmd": "x"}, "zebra quantum banana pancake")
        self.assertFalse(r.consistent)

    def test_cross_tool_agree(self):
        r = self.ck.check_cross_tool([
            {"tool": "shell", "value": "42", "confidence": 0.9},
            {"tool": "web", "value": "42", "confidence": 0.8},
        ])
        self.assertTrue(r.consistent)

    def test_cross_tool_disagree(self):
        r = self.ck.check_cross_tool([
            {"tool": "shell", "value": "the server is up and healthy", "confidence": 0.9},
            {"tool": "web", "value": "quantum banana zebra xylophone", "confidence": 0.8},
        ])
        self.assertFalse(r.consistent)

    def test_internal_kv_conflict(self):
        r = self.ck.check_internal("status: ok\nstatus: failed")
        self.assertFalse(r.consistent)
        self.assertTrue(any("status" in c.description for c in r.contradictions))

    def test_internal_negation(self):
        r = self.ck.check_internal("The service is running.\nThe service is not running.")
        self.assertFalse(r.consistent)

    def test_internal_clean(self):
        r = self.ck.check_internal("status: ok\ncount: 5")
        self.assertTrue(r.consistent)

    def test_analyze_merges(self):
        self.ck.fact_store.record("k", "v", source="s", confidence=0.9,
                                  ttl_seconds=60)
        r = self.ck.analyze("shell", "status: ok", inputs={"cmd": "x"},
                            fact_key="k", fact_value="v")
        self.assertTrue(r.consistent)
        self.assertGreater(r.score, 0.8)

    def test_resolution_strategies(self):
        cands = [
            {"value": "a", "source": "s1", "confidence": 0.9, "timestamp": 1},
            {"value": "b", "source": "s2", "confidence": 0.5, "timestamp": 2},
        ]
        contra = Contradiction("d", ["s1", "s2"], ContradictionSeverity.MEDIUM, "r")
        w = ConflictResolutionStrategy("highest_confidence_wins").resolve(contra, cands)
        self.assertEqual(w["winner"]["value"], "a")
        w = ConflictResolutionStrategy("newest_wins").resolve(contra, cands)
        self.assertEqual(w["winner"]["value"], "b")
        cands2 = cands + [{"value": "a", "source": "s3", "confidence": 0.4, "timestamp": 3}]
        w = ConflictResolutionStrategy("majority_wins").resolve(contra, cands2)
        self.assertEqual(w["winner"]["value"], "a")
        w = ConflictResolutionStrategy("escalate").resolve(contra, cands)
        self.assertIsNone(w["winner"])
        with self.assertRaises(ValueError):
            ConflictResolutionStrategy("bogus")


class TestDriftDetector(unittest.TestCase):
    def test_no_drift(self):
        dd = DriftDetector()
        for v in [10, 10.1, 9.9, 10.2, 10.0]:
            dd.add("m", v)
        r = dd.check_drift("m", 10.1)
        self.assertTrue(r.consistent)

    def test_drift_detected(self):
        dd = DriftDetector()
        for v in [10, 10.1, 9.9, 10.2, 10.0]:
            dd.add("m", v)
        r = dd.check_drift("m", 50.0)
        self.assertFalse(r.consistent)
        self.assertTrue(r.contradictions)

    def test_stats_none_when_cold(self):
        dd = DriftDetector()
        self.assertIsNone(dd.stats("m"))
        dd.add("m", 1.0)
        self.assertIsNone(dd.stats("m"))


class TestClaimExtractor(unittest.TestCase):
    def test_extract_basic(self):
        ex = ClaimExtractor()
        claims = ex.extract("The service is running. disk_free: 81.5 GB. status: ok.")
        kinds = {c.kind for c in claims}
        self.assertTrue({"equality", "quantity", "status"} & kinds)

    def test_empty(self):
        self.assertEqual(ClaimExtractor().extract(""), [])

    def test_claims_about(self):
        ex = ClaimExtractor()
        claims = ex.extract("The database is healthy. The cache is empty.")
        found = ex.claims_about(claims, "database")
        self.assertTrue(found)


class TestSnapshots(unittest.TestCase):
    def test_identical(self):
        a = Snapshot("f", "hello")
        b = Snapshot("f", "hello")
        d = diff_snapshots(a, b)
        self.assertTrue(d["identical"])

    def test_diff(self):
        a = Snapshot("f", "a\nb\nc")
        b = Snapshot("f", "a\nB\nc")
        d = diff_snapshots(a, b)
        self.assertFalse(d["identical"])
        self.assertEqual(d["added"], 1)
        self.assertEqual(d["removed"], 1)

    def test_tracker_baseline_then_change(self):
        t = SnapshotTracker()
        r1 = t.check("f", "v1")
        self.assertTrue(r1.consistent)
        r2 = t.check("f", "v2")
        self.assertFalse(r2.consistent)
        r3 = t.check("f", "v3", expect_change=True)
        self.assertTrue(r3.consistent)


# ===========================================================================
# proof_collector
# ===========================================================================

class TestProofCollector(unittest.TestCase):
    def setUp(self):
        self.pc = ProofCollector()

    def test_chain_links(self):
        e1 = self.pc.collect_note("first")
        e2 = self.pc.collect_note("second")
        self.assertEqual(e1.chain_hash, sha256_text("" + e1.hash))
        self.assertEqual(e2.chain_hash, sha256_text(e1.chain_hash + e2.hash))

    def test_content_hash(self):
        e = self.pc.collect_note("hello")
        self.assertTrue(e.verify_hash())
        self.assertEqual(e.hash, sha256_text("hello"))

    def test_command_output(self):
        e = self.pc.collect_command_output("shell", "echo hi", "hi", "", 0)
        self.assertEqual(e.type, EvidenceType.COMMAND_OUTPUT)
        self.assertEqual(e.metadata["exit_code"], 0)

    def test_file_snapshot(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("data")
            path = fh.name
        try:
            e = self.pc.collect_file_snapshot(path)
            self.assertEqual(e.type, EvidenceType.FILE_SNAPSHOT)
            self.assertIn("data", e.content)
        finally:
            os.unlink(path)

    def test_file_hash(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("data")
            path = fh.name
        try:
            e = self.pc.collect_file_hash(path)
            self.assertEqual(e.type, EvidenceType.FILE_HASH)
            self.assertIn("sha256", e.content)
        finally:
            os.unlink(path)

    def test_bundle_verify_ok(self):
        self.pc.collect_note("a")
        self.pc.collect_note("b")
        b = self.pc.bundle("claim: notes exist")
        self.assertTrue(b.verify()["ok"])

    def test_tamper_detected(self):
        self.pc.collect_note("a")
        b = self.pc.bundle("claim")
        b2 = ProofBundle.from_dict(b.to_dict())
        b2.evidence[0].content = "TAMPERED"
        v = b2.verify()
        self.assertFalse(v["ok"])
        self.assertTrue(any("content_hash" in f for f in v["failed"]))

    def test_chain_break_detected(self):
        self.pc.collect_note("a")
        self.pc.collect_note("b")
        b = self.pc.bundle("claim")
        b2 = ProofBundle.from_dict(b.to_dict())
        b2.evidence.pop(0)  # deletion breaks the chain
        self.assertFalse(b2.verify()["ok"])

    def test_export_import_roundtrip(self):
        self.pc.collect_note("a")
        b = self.pc.bundle("claim")
        path = tempfile.mktemp(suffix=".json")
        try:
            self.pc.export_bundle(b, path)
            b2 = self.pc.import_bundle(path)
            self.assertTrue(b2.verify()["ok"])
            self.assertEqual(b2.bundle_hash, b.bundle_hash)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_verify_chain(self):
        self.pc.collect_note("a")
        self.assertTrue(self.pc.verify_chain()["ok"])

    def test_screenshot_base64(self):
        e = self.pc.collect_screenshot(b"\x89PNG fake", source="browser")
        self.assertEqual(e.type, EvidenceType.SCREENSHOT)
        self.assertTrue(e.verify_hash())

    def test_large_content_truncated(self):
        import agent.verification.proof_collector as pcm
        old = pcm.MAX_INLINE_BYTES
        pcm.MAX_INLINE_BYTES = 10
        try:
            e = self.pc.add(EvidenceType.TEXT_NOTE, "x" * 100, source="t")
            self.assertTrue(e.content_truncated)
            self.assertFalse(e.verify_hash())  # honest: can't verify
        finally:
            pcm.MAX_INLINE_BYTES = old

    def test_evidence_serialization(self):
        e = self.pc.collect_note("x", label="lbl")
        d = e.to_dict()
        e2 = Evidence.from_dict(d)
        self.assertEqual(e2.id, e.id)
        self.assertEqual(e2.hash, e.hash)

    def test_bundle_since(self):
        e1 = self.pc.collect_note("first")
        self.pc.collect_note("second")
        b = self.pc.bundle_since(e1.id, "claim: after first")
        self.assertEqual(len(b.evidence), 1)


class TestMerkle(unittest.TestCase):
    def test_root_and_proof(self):
        hs = [sha256_text(str(i)) for i in range(8)]
        root = merkle_root(hs)
        for i in range(8):
            pr = merkle_proof(hs, i)
            self.assertEqual(pr["root"], root)
            self.assertTrue(verify_merkle_proof(pr["leaf"], pr["proof"], root))
        self.assertFalse(verify_merkle_proof(hs[0], merkle_proof(hs, 1)["proof"], root))

    def test_empty(self):
        self.assertTrue(merkle_root([]))

    def test_bad_index(self):
        with self.assertRaises(ValueError):
            merkle_proof(["a"], 5)


class TestEvidenceQuery(unittest.TestCase):
    def setUp(self):
        pc = ProofCollector()
        pc.collect_note("alpha note", label="first")
        pc.collect_command_output("shell", "ls", "out", "", 0, label="listing")
        self.q = EvidenceQuery(pc.evidence)

    def test_by_type(self):
        self.assertEqual(len(self.q.by_type(EvidenceType.TEXT_NOTE)), 1)

    def test_by_label(self):
        self.assertEqual(len(self.q.by_label("list")), 1)

    def test_search_content(self):
        self.assertEqual(len(self.q.search_content("alpha")), 1)

    def test_stats(self):
        s = self.q.stats()
        self.assertEqual(s["count"], 2)
        self.assertIn("text_note", s["by_type"])

    def test_timeline(self):
        self.assertEqual(len(self.q.timeline()), 2)


class TestRedaction(unittest.TestCase):
    def test_redacts_secret(self):
        pc = ProofCollector()
        e = pc.add(EvidenceType.TEXT_NOTE, "api_key: abc123", source="t")
        r = redact_evidence(e)
        self.assertIn("[REDACTED]", r.content)
        self.assertNotIn("abc123", r.content)
        self.assertEqual(r.hash, e.hash)  # commitment preserved
        self.assertTrue(r.metadata["redacted"])


class TestChainOfCustodyLog(unittest.TestCase):
    def test_append_and_verify(self):
        pc = ProofCollector()
        path = tempfile.mktemp(suffix=".jsonl")
        try:
            log = ChainOfCustodyLog(path)
            log.append(pc.collect_note("a"))
            log.append(pc.collect_note("b"))
            v = log.verify()
            self.assertTrue(v["ok"])
            self.assertEqual(v["checked"], 2)
            self.assertEqual(len(log.read_all()), 2)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_tamper_detected(self):
        pc = ProofCollector()
        path = tempfile.mktemp(suffix=".jsonl")
        try:
            log = ChainOfCustodyLog(path)
            log.append(pc.collect_note("a"))
            # tamper with the file directly
            with open(path, "a", encoding="utf-8") as fh:
                fh.write('{"seq": 99, "bogus": true}\n')
            v = log.verify()
            self.assertFalse(v["ok"])
        finally:
            if os.path.exists(path):
                os.unlink(path)


class TestCosign(unittest.TestCase):
    def test_cosign_valid(self):
        pc = ProofCollector()
        pc.collect_note("a")
        b = pc.bundle("claim")
        sig = cosign_bundle(b, "reviewer-1", "looks good")
        r = verify_cosignatures(b, [sig])
        self.assertTrue(r["all_valid"])
        self.assertEqual(r["valid_count"], 1)

    def test_cosign_wrong_bundle(self):
        pc = ProofCollector()
        pc.collect_note("a")
        b = pc.bundle("claim")
        sig = cosign_bundle(b, "reviewer-1")
        b2 = pc.bundle("other claim")
        r = verify_cosignatures(b2, [sig])
        self.assertFalse(r["all_valid"])


class TestCompareBundles(unittest.TestCase):
    def test_compare(self):
        pc = ProofCollector()
        pc.collect_note("a")
        b1 = pc.bundle("claim")
        pc.collect_note("b")
        b2 = pc.bundle("claim")
        c = compare_bundles(b1, b2)
        self.assertTrue(c["same_claim"])
        self.assertEqual(len(c["added"]), 1)
        self.assertEqual(c["new_count"], 2)


if __name__ == "__main__":
    unittest.main()
