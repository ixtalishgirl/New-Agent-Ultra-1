"""Unit tests for the coding modules.

Covers:
    code_generator:  specs, docstring generation, identifier helpers,
                     ImportManager, CodeAnalyzer, ComplexityEstimator,
                     TemplateLibrary (all 8 templates render + parse + run),
                     CodeGenerator (function/class/script generation)
    code_reviewer:   ReviewFinding/ReviewReport, CodeReviewer passes
                     (security, correctness, complexity, style),
                     ExtendedCodeReviewer, DuplicateCodeDetector,
                     compare_reports
    refactor_engine: RenameEngine safety, extract/inline/simplify/dead-code,
                     merge_nested_ifs, extract_constant, RefactoringSession
                     undo/redo, preview + diff
"""
import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from agent.coding.code_generator import (
    CodeGenerator, CodeSpec, FunctionSpec, ClassSpec, ScriptSpec,
    TemplateLibrary, ImportManager, ComplexityEstimator, CodeAnalyzer,
    GenerationError, is_valid_identifier, sanitize_identifier,
    make_docstring, indent_block, wrap_signature,
    generate_function_code,
)
from agent.coding.code_reviewer import (
    CodeReviewer, ExtendedCodeReviewer, ReviewFinding, ReviewReport,
    FindingSeverity, FindingCategory, DuplicateCodeDetector,
    compare_reports, review_code, review_many, worst_reports,
)
from agent.coding.refactor_engine import (
    RefactorEngine, RefactorResult, RefactorError, ScopeAnalyzer,
    RenameEngine, RefactoringSession, refactor, refactor_diff,
    preview_refactor,
)


# ===========================================================================
# code_generator helpers
# ===========================================================================

class TestIdentifiers(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(is_valid_identifier("my_func"))
        self.assertFalse(is_valid_identifier("class"))
        self.assertFalse(is_valid_identifier("123abc"))

    def test_sanitize(self):
        self.assertEqual(sanitize_identifier("hello world!"), "hello_world")
        self.assertTrue(is_valid_identifier(sanitize_identifier("class")))
        self.assertTrue(is_valid_identifier(sanitize_identifier("9lives")))


class TestDocstring(unittest.TestCase):
    def test_google_style(self):
        d = make_docstring("Do things.", params=[("x", "int", "the x")],
                           returns="int. the result.")
        self.assertIn("Args:", d)
        self.assertIn("Returns:", d)
        self.assertTrue(d.startswith('"""'))

    def test_numpy_style(self):
        d = make_docstring("Do things.", params=[("x", "int", "the x")],
                           style="numpy")
        self.assertIn("Parameters", d)

    def test_raises(self):
        d = make_docstring("Do.", raises=["ValueError"])
        self.assertIn("Raises:", d)


class TestFormatting(unittest.TestCase):
    def test_indent_block(self):
        self.assertEqual(indent_block("a\nb"), "    a\n    b")

    def test_wrap_signature_short(self):
        s = wrap_signature("f", ["a", "b"], "int")
        self.assertEqual(s, "def f(a, b) -> int:")

    def test_wrap_signature_long(self):
        params = [f"param_{i}: int" for i in range(10)]
        s = wrap_signature("f", params, None)
        self.assertIn("\n", s)
        ast.parse(s + "\n    pass")


class TestImportManager(unittest.TestCase):
    def test_grouping(self):
        im = ImportManager()
        im.add("import os")
        im.add("from typing import Dict, List")
        im.add("import requests")
        rendered = im.render()
        # stdlib before third-party
        self.assertLess(rendered.index("import os"), rendered.index("import requests"))

    def test_dedup(self):
        im = ImportManager()
        im.add("import os")
        im.add("import os")
        im.add("from os import path")
        im.add("from os import path")
        rendered = im.render()
        self.assertEqual(rendered.count("import os"), 1)

    def test_require(self):
        im = ImportManager()
        im.require("json")
        im.require("typing", ["Optional"])
        rendered = im.render()
        self.assertIn("import json", rendered)
        self.assertIn("from typing import Optional", rendered)

    def test_bad_statement(self):
        im = ImportManager()
        with self.assertRaises(ValueError):
            im.add("not an import")

    def test_merge_into_source(self):
        im = ImportManager()
        im.add("import os")
        src = '"""Doc."""\n\nx = 1\n'
        merged = im.merge_into_source(src)
        self.assertIn("import os", merged)
        self.assertIn('"""Doc."""', merged)
        ast.parse(merged)


class TestCodeAnalyzer(unittest.TestCase):
    def test_functions_and_classes(self):
        a = CodeAnalyzer("def f():\n    pass\n\nclass C:\n    def m(self):\n        pass\n")
        self.assertTrue(a.ok)
        self.assertEqual([f.name for f in a.functions()], ["f", "m"])
        self.assertEqual([c.name for c in a.classes()], ["C"])

    def test_syntax_error(self):
        a = CodeAnalyzer("def f(:\n")
        self.assertFalse(a.ok)
        self.assertIsNotNone(a.parse_error)

    def test_undefined_names(self):
        a = CodeAnalyzer("import os\ndef f():\n    return os.path.join(a, b)\n")
        undef = a.undefined_names()
        self.assertIn("a", undef)
        self.assertIn("b", undef)
        self.assertNotIn("os", undef)

    def test_summary(self):
        a = CodeAnalyzer("x = 1\n")
        s = a.summary()
        self.assertTrue(s["parse_ok"])
        self.assertEqual(s["lines"], 1)


class TestComplexityEstimator(unittest.TestCase):
    def test_simple(self):
        est = ComplexityEstimator()
        r = est.estimate("def f():\n    return 1\n")
        self.assertEqual(r["f"], 1)

    def test_branchy(self):
        est = ComplexityEstimator()
        r = est.estimate("def f(x):\n    if x:\n        for i in x:\n            print(i)\n")
        self.assertEqual(r["f"], 3)

    def test_boolop(self):
        est = ComplexityEstimator()
        r = est.estimate("def f(a, b, c):\n    return a and b or c\n")
        self.assertEqual(r["f"], 3)

    def test_rating(self):
        self.assertEqual(ComplexityEstimator.rating(3), "simple")
        self.assertEqual(ComplexityEstimator.rating(8), "moderate")
        self.assertEqual(ComplexityEstimator.rating(15), "complex")
        self.assertEqual(ComplexityEstimator.rating(30), "unmaintainable")

    def test_bad_syntax(self):
        self.assertEqual(ComplexityEstimator().estimate("def f(:"), {})


class TestTemplateLibrary(unittest.TestCase):
    def setUp(self):
        self.lib = TemplateLibrary()

    def test_names(self):
        names = self.lib.names()
        for expected in ["api_client", "cli_tool", "data_processor",
                         "web_scraper", "test_suite", "retry_decorator",
                         "config_loader", "worker_pool"]:
            self.assertIn(expected, names)

    def test_unknown_template(self):
        with self.assertRaises(KeyError):
            self.lib.render("nope")

    def test_missing_placeholder(self):
        with self.assertRaises(KeyError):
            self.lib.render("cli_tool", description="x")  # prog_name missing

    def test_cli_tool_runs(self):
        code = self.lib.render("cli_tool", description="d", prog_name="demo")
        ns: dict = {"__name__": "not_main"}
        exec(compile(code, "<cli>", "exec"), ns)
        self.assertEqual(ns["process"]("hi"), "hi")
        self.assertEqual(ns["main"](["--dry-run", "hi"]), 0)
        self.assertEqual(ns["main"](["hi"]), 0)

    def test_retry_decorator_runs(self):
        code = self.lib.render("retry_decorator")
        ns: dict = {}
        exec(compile(code, "<retry>", "exec"), ns)
        calls = {"n": 0}

        @ns["retry"](tries=3, backoff=0.001)
        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ValueError("boom")
            return "ok"

        self.assertEqual(flaky(), "ok")
        self.assertEqual(calls["n"], 3)

    def test_worker_pool_runs(self):
        code = self.lib.render("worker_pool", description="d")
        ns: dict = {}
        exec(compile(code, "<worker>", "exec"), ns)
        results = ns["run_pool"]([lambda: 1 + 1, lambda: 1 // 0], max_workers=2)
        self.assertTrue(results[0].ok and results[0].value == 2)
        self.assertFalse(results[1].ok)

    def test_api_client_structure(self):
        code = self.lib.render("api_client", service_name="S", class_name="C",
                               base_url="https://x.com", health_path="/h")
        ns: dict = {}
        exec(compile(code, "<api>", "exec"), ns)
        client = ns["C"](base_url="https://x.com")
        self.assertNotIn("Authorization", client._headers())
        c2 = ns["C"](base_url="https://x.com", api_key="k")
        self.assertEqual(c2._headers()["Authorization"], "Bearer k")

    def test_data_processor_runs(self):
        import tempfile, json as _json
        code = self.lib.render("data_processor", description="d",
                               input_desc="CSV", output_desc="JSONL")
        ns: dict = {}
        exec(compile(code, "<dp>", "exec"), ns)
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write('{"a": " x "}\n{"_error": "bad"}\n')
            inp = fh.name
        out = inp + ".out"
        try:
            recs = [ns["transform"](r) for r in ns["read_records"](inp)]
            counts = ns["write_records"](recs, out)
            self.assertEqual(counts, {"written": 1, "invalid": 1})
            line = open(out).readline()
            self.assertEqual(_json.loads(line), {"a": "x"})
        finally:
            os.unlink(inp)
            os.unlink(out)

    def test_config_loader_runs(self):
        code = self.lib.render("config_loader", class_name="Cfg", env_prefix="T_")
        ns: dict = {}
        exec(compile(code, "<cfg>", "exec"), ns)
        cfg = ns["Cfg"].from_dict({"a": 1, "unknown_key": 2})
        self.assertEqual(cfg.to_dict(), {})

    def test_web_scraper_parses(self):
        code = self.lib.render("web_scraper", description="d",
                               start_url="https://x.com", delay_seconds="1")
        ns: dict = {}
        exec(compile(code, "<ws>", "exec"), ns)
        extractor = ns["_TextExtractor"]()
        extractor.feed("<html><head><title>T</title></head>"
                       '<body><a href="/l">x</a>hello</body></html>')
        self.assertEqual(extractor.title, "T")
        self.assertIn("/l", extractor.links)


class TestCodeGenerator(unittest.TestCase):
    def setUp(self):
        self.g = CodeGenerator()

    def test_generate_function(self):
        code = self.g.generate_function(FunctionSpec(
            name="add", params=[("a", "int", None), ("b", "int", "0")],
            return_type="int", docstring="Add.", body="return a + b"))
        self.assertIn("def add(a: int, b: int = 0) -> int:", code)
        ns: dict = {}
        exec(compile(code, "<f>", "exec"), ns)
        self.assertEqual(ns["add"](2, 3), 5)

    def test_generate_function_method_self(self):
        code = self.g.generate_function(FunctionSpec(
            name="m", params=[], is_method=True, body="return 1"))
        self.assertIn("def m(self):", code)

    def test_generate_async(self):
        code = self.g.generate_function(FunctionSpec(
            name="fetch", is_async=True, body="return 1"))
        self.assertIn("async def fetch():", code)

    def test_generate_dataclass(self):
        code = self.g.generate_class(ClassSpec(
            name="point", is_dataclass=True,
            attributes=[("x", "float", None), ("y", "float", "0.0")],
            docstring="A point."))
        self.assertIn("@dataclass", code)
        self.assertIn("class Point:", code)
        ns: dict = {}
        exec(compile(code, "<c>", "exec"), ns)
        p = ns["Point"](1.0)
        self.assertEqual((p.x, p.y), (1.0, 0.0))

    def test_generate_class_with_init(self):
        code = self.g.generate_class(ClassSpec(
            name="thing", attributes=[("name", "str", None)],
            methods=[FunctionSpec(name="greet", body="return 'hi ' + self.name")]))
        ns: dict = {}
        exec(compile(code, "<c>", "exec"), ns)
        self.assertEqual(ns["Thing"]("bob").greet(), "hi bob")

    def test_generate_script(self):
        code = self.g.generate(CodeSpec(kind="script", script=ScriptSpec(
            name="demo", description="demo",
            functions=[FunctionSpec(name="double", params=[("x", "int", None)],
                                    return_type="int", body="return x * 2")],
            main_body='print(double(21))',
            imports=["import sys"],
            constants=[("ANSWER", "42")])))
        self.assertIn('if __name__ == "__main__":', code)
        ns: dict = {"__name__": "not_main"}
        exec(compile(code, "<s>", "exec"), ns)
        self.assertEqual(ns["double"](21), 42)
        self.assertEqual(ns["ANSWER"], 42)

    def test_generate_spec_dispatch(self):
        spec = CodeSpec(kind="function", function=FunctionSpec(
            name="f", body="return 1"))
        code = self.g.generate(spec)
        self.assertIn("def f():", code)
        with self.assertRaises(GenerationError):
            self.g.generate(CodeSpec(kind="bogus"))

    def test_placeholder_rejected(self):
        with self.assertRaises(GenerationError):
            self.g.finalize("x = 1  # TODO: fix")

    def test_syntax_error_rejected(self):
        with self.assertRaises(GenerationError):
            self.g.finalize("def f(:")

    def test_analyze(self):
        info = self.g.analyze("def f(x):\n    if x:\n        return 1\n    return 0\n")
        self.assertTrue(info["parse_ok"])
        self.assertIn("f", info["complexity"])

    def test_from_template(self):
        code = self.g.from_template("retry_decorator")
        self.assertIn("def retry", code)

    def test_convenience_function(self):
        code = generate_function_code("sq", [("x", "int", None)], "return x * x",
                                      return_type="int", docstring="Square.")
        ns: dict = {}
        exec(compile(code, "<f>", "exec"), ns)
        self.assertEqual(ns["sq"](4), 16)


# ===========================================================================
# code_reviewer
# ===========================================================================

class TestCodeReviewer(unittest.TestCase):
    def test_clean_code_scores_100(self):
        rep = review_code('"""M."""\ndef add(a: int, b: int) -> int:\n'
                          '    """Add."""\n    return a + b\n', "good.py")
        self.assertEqual(rep.score, 100)
        self.assertEqual(rep.grade, "A")
        self.assertFalse(rep.has_blockers())

    def test_syntax_error_critical(self):
        rep = review_code("def f(:", "bad.py")
        self.assertTrue(any(f.severity == FindingSeverity.CRITICAL for f in rep.findings))

    def test_unused_import(self):
        rep = review_code("import os\nimport sys\nprint(sys.argv)\n", "u.py")
        rules = {f.rule for f in rep.findings}
        self.assertIn("unused-import", rules)
        self.assertNotIn("sys", str([f.message for f in rep.findings if f.rule == "unused-import"]))

    def test_undefined_name(self):
        rep = review_code("def f():\n    return nosuchname + 1\n", "u.py")
        self.assertTrue(any(f.rule == "undefined-name" and f.severity == FindingSeverity.HIGH
                            for f in rep.findings))

    def test_bare_except(self):
        rep = review_code("try:\n    x = 1\nexcept:\n    pass\n", "e.py")
        self.assertTrue(any(f.rule == "bare-except" for f in rep.findings))

    def test_silent_except_exception(self):
        rep = review_code("try:\n    x = 1\nexcept Exception:\n    pass\n", "e.py")
        self.assertTrue(any(f.rule == "silent-except" for f in rep.findings))

    def test_mutable_default(self):
        rep = review_code("def f(items=[]):\n    return items\n", "m.py")
        self.assertTrue(any(f.rule == "mutable-default" for f in rep.findings))

    def test_eval_critical(self):
        rep = review_code("x = eval(user_input)\n", "s.py")
        self.assertTrue(any(f.rule == "no-eval-exec" and f.severity == FindingSeverity.CRITICAL
                            for f in rep.findings))

    def test_shell_true(self):
        rep = review_code("import subprocess\nsubprocess.run(cmd, shell=True)\n", "s.py")
        self.assertTrue(any(f.rule == "shell-true" for f in rep.findings))

    def test_pickle_load(self):
        rep = review_code("import pickle\npickle.loads(data)\n", "s.py")
        self.assertTrue(any(f.rule == "pickle-load" for f in rep.findings))

    def test_hardcoded_secret(self):
        rep = review_code('API_KEY = "sk-abc123"\n', "s.py")
        self.assertTrue(any(f.rule == "hardcoded-secret"
                            and f.severity == FindingSeverity.CRITICAL
                            for f in rep.findings))

    def test_weak_hash(self):
        rep = review_code("import hashlib\nhashlib.md5(b'x').hexdigest()\n", "s.py")
        self.assertTrue(any(f.rule == "weak-hash" for f in rep.findings))

    def test_high_complexity(self):
        body = "def f(x):\n" + "".join(f"    if x == {i}:\n        print({i})\n" for i in range(12))
        rep = review_code(body + "    return 0\n", "c.py")
        self.assertTrue(any(f.rule == "high-complexity" for f in rep.findings))

    def test_long_function(self):
        body = "def f():\n" + "".join(f"    x{i} = {i}\n" for i in range(70)) + "    return 0\n"
        rep = review_code(body, "c.py")
        self.assertTrue(any(f.rule == "long-function" for f in rep.findings))

    def test_compare_to_none(self):
        rep = review_code("def f(x):\n    return x == None\n", "s.py")
        self.assertTrue(any(f.rule == "compare-to-none" for f in rep.findings))

    def test_missing_docstring_info(self):
        rep = review_code("def f():\n    return 1\n", "s.py")
        self.assertTrue(any(f.rule == "missing-docstring" for f in rep.findings))

    def test_report_helpers(self):
        rep = review_code("import os\nx = 1\n", "r.py")
        self.assertIn("score", rep.summary())
        d = rep.to_dict()
        self.assertIn("findings", d)
        self.assertTrue(rep.by_severity(FindingSeverity.LOW))
        self.assertIn("lines", rep.metrics)

    def test_review_file(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
            fh.write("x = eval('1')\n")
            path = fh.name
        try:
            from agent.coding.code_reviewer import review_file
            rep = review_file(path)
            self.assertTrue(any(f.rule == "no-eval-exec" for f in rep.findings))
        finally:
            os.unlink(path)


class TestExtendedReviewer(unittest.TestCase):
    def test_wildcard_import(self):
        rep = ExtendedCodeReviewer("e.py").review("from x import *\n")
        self.assertTrue(any(f.rule == "wildcard-import" for f in rep.findings))

    def test_open_without_with(self):
        rep = ExtendedCodeReviewer("e.py").review('fh = open("f")\nprint(fh.read())\n')
        self.assertTrue(any(f.rule == "open-without-with" for f in rep.findings))

    def test_global_statement(self):
        rep = ExtendedCodeReviewer("e.py").review("def f():\n    global x\n    x = 1\n")
        self.assertTrue(any(f.rule == "global-statement" for f in rep.findings))

    def test_fstring_no_placeholder(self):
        rep = ExtendedCodeReviewer("e.py").review('x = f"hello"\n')
        self.assertTrue(any(f.rule == "fstring-no-placeholder" for f in rep.findings))

    def test_duplicate_code(self):
        block = "\n".join(f"    v{i} = {i} * 2" for i in range(6))
        src = f"def a():\n{block}\n    return v0\n\ndef b():\n{block}\n    return v0\n"
        rep = ExtendedCodeReviewer("e.py").review(src)
        self.assertTrue(any(f.rule == "duplicate-code" for f in rep.findings))


class TestDuplicateDetector(unittest.TestCase):
    def test_finds_clones(self):
        src = ("def a():\n    x = 1\n    y = 2\n    z = x + y\n    w = z * 2\n    return w\n\n"
               "def b():\n    p = 10\n    q = 20\n    r = p + q\n    s = r * 2\n    return s\n")
        dups = DuplicateCodeDetector(min_statements=4).find(ast.parse(src))
        self.assertTrue(dups)

    def test_no_false_positive(self):
        src = "def a():\n    return 1\n\ndef b():\n    return 2\n"
        dups = DuplicateCodeDetector(min_statements=4).find(ast.parse(src))
        self.assertEqual(dups, [])


class TestCompareReports(unittest.TestCase):
    def test_improvement(self):
        old = review_code("import os\nx = eval('1')\n", "a.py")
        new = review_code('"""M."""\nx = 1\n', "a.py")
        c = compare_reports(old, new)
        self.assertTrue(c["improved"])
        self.assertGreater(c["score_delta"], 0)
        self.assertGreater(c["fixed_count"], 0)


class TestBatchReview(unittest.TestCase):
    def test_review_many_and_worst(self):
        reports = review_many({"a.py": "x = 1\n", "b.py": "x = eval('1')\n"})
        self.assertEqual(len(reports), 2)
        worst = worst_reports(reports, limit=1)
        self.assertEqual(worst[0].filename, "b.py")


# ===========================================================================
# refactor_engine
# ===========================================================================

class TestRenameEngine(unittest.TestCase):
    def test_safe_rename(self):
        eng = RenameEngine("def f():\n    total = 1\n    return total + 1\n")
        ok, _ = eng.check("total", "subtotal")
        self.assertTrue(ok)
        r = eng.rename("total", "subtotal")
        self.assertTrue(r.ok)
        ns: dict = {}
        exec(compile(r.source, "<r>", "exec"), ns)
        self.assertEqual(ns["f"](), 2)

    def test_unsafe_rename_refused(self):
        eng = RenameEngine("def f(items):\n    total = 1\n    return total\n")
        ok, reason = eng.check("total", "items")
        self.assertFalse(ok)
        self.assertIn("already used", reason)
        r = eng.rename("total", "items")
        self.assertFalse(r.ok)
        self.assertEqual(r.source, "def f(items):\n    total = 1\n    return total\n")

    def test_rename_missing_name(self):
        eng = RenameEngine("x = 1\n")
        ok, _ = eng.check("nope", "y")
        self.assertFalse(ok)

    def test_rename_keyword_refused(self):
        eng = RenameEngine("x = 1\n")
        ok, _ = eng.check("x", "class")
        self.assertFalse(ok)

    def test_syntax_error_raises(self):
        with self.assertRaises(RefactorError):
            RenameEngine("def f(:")


class TestRefactorEngine(unittest.TestCase):
    SRC = ("def compute(items):\n"
           "    total = 0\n"
           "    for i in items:\n"
           "        total += i\n"
           "    tax = total * 0.2\n"
           "    return tax\n")

    def test_extract_function(self):
        eng = RefactorEngine(self.SRC)
        r = eng.extract_function("compute", "calc_tax", 5, 5)
        self.assertTrue(r.ok, r.warnings)
        self.assertIn("def calc_tax", eng.source)
        ns: dict = {}
        exec(compile(eng.source, "<e>", "exec"), ns)
        self.assertAlmostEqual(ns["compute"]([10, 20]), 6.0)

    def test_extract_missing_function(self):
        eng = RefactorEngine(self.SRC)
        r = eng.extract_function("nope", "x", 1, 2)
        self.assertFalse(r.ok)

    def test_inline_variable(self):
        eng = RefactorEngine("def f():\n    x = 2 * 21\n    return x + 1\n")
        r = eng.inline_variable("f", "x")
        self.assertTrue(r.ok)
        self.assertNotIn("x =", eng.source)
        ns: dict = {}
        exec(compile(eng.source, "<e>", "exec"), ns)
        self.assertEqual(ns["f"](), 43)

    def test_inline_refused_side_effects(self):
        eng = RefactorEngine("def f():\n    x = input()\n    return x\n")
        r = eng.inline_variable("f", "x")
        self.assertFalse(r.ok)

    def test_inline_refused_multiple_assigns(self):
        eng = RefactorEngine("def f():\n    x = 1\n    x = 2\n    return x\n")
        r = eng.inline_variable("f", "x")
        self.assertFalse(r.ok)

    def test_simplify_conditionals(self):
        eng = RefactorEngine("def f(c):\n    if c:\n        return True\n    else:\n        return False\n")
        r = eng.simplify_conditionals()
        self.assertTrue(r.ok)
        self.assertIn("return c", eng.source)
        ns: dict = {}
        exec(compile(eng.source, "<e>", "exec"), ns)
        self.assertTrue(ns["f"](1))
        self.assertFalse(ns["f"](0))

    def test_simplify_double_negation(self):
        eng = RefactorEngine("def f(x):\n    return not not x\n")
        r = eng.simplify_conditionals()
        self.assertTrue(r.ok)
        self.assertIn("return x", eng.source)

    def test_remove_dead_code(self):
        eng = RefactorEngine("def f():\n    return 1\n    print('dead')\n    x = 2\n")
        r = eng.remove_dead_code()
        self.assertTrue(r.ok)
        self.assertNotIn("dead", eng.source)

    def test_merge_nested_ifs(self):
        eng = RefactorEngine("def f(a, b):\n    if a:\n        if b:\n            return 1\n    return 0\n")
        r = eng.merge_nested_ifs()
        self.assertTrue(r.ok)
        self.assertIn("if a and b:", eng.source)
        ns: dict = {}
        exec(compile(eng.source, "<e>", "exec"), ns)
        self.assertEqual((ns["f"](True, True), ns["f"](True, False)), (1, 0))

    def test_extract_constant(self):
        eng = RefactorEngine("def f():\n    return 0.2 * 100\n")
        r = eng.extract_constant("0.2", "TAX_RATE")
        self.assertTrue(r.ok)
        self.assertIn("TAX_RATE = 0.2", eng.source)
        ns: dict = {}
        exec(compile(eng.source, "<e>", "exec"), ns)
        self.assertAlmostEqual(ns["f"](), 20.0)

    def test_rename_via_engine(self):
        eng = RefactorEngine("def f():\n    x = 1\n    return x\n")
        r = eng.rename_variable("x", "y")
        self.assertTrue(r.ok)
        self.assertIn("return y", eng.source)

    def test_result_serialization(self):
        eng = RefactorEngine("x = 1\n")
        r = eng.simplify_conditionals()
        d = r.to_dict()
        self.assertIn("ok", d)


class TestRefactoringSession(unittest.TestCase):
    def test_do_undo_redo(self):
        sess = RefactoringSession("def f():\n    x = 1\n    return x\n")
        before = sess.source
        r = sess.do("rename_variable", old="x", new="y")
        self.assertTrue(r.ok)
        self.assertNotEqual(sess.source, before)
        u = sess.undo()
        self.assertTrue(u.ok)
        self.assertEqual(sess.source, before)
        self.assertTrue(sess.can_redo())
        rd = sess.redo()
        self.assertTrue(rd.ok)
        self.assertNotEqual(sess.source, before)

    def test_undo_empty(self):
        sess = RefactoringSession("x = 1\n")
        self.assertFalse(sess.can_undo())
        self.assertFalse(sess.undo().ok)

    def test_unknown_op(self):
        sess = RefactoringSession("x = 1\n")
        r = sess.do("frobnicator")
        self.assertFalse(r.ok)

    def test_failed_op_not_recorded(self):
        sess = RefactoringSession("x = 1\n")
        sess.do("rename_variable", old="x", new="class")  # refused
        self.assertFalse(sess.can_undo())


class TestRefactorHelpers(unittest.TestCase):
    def test_refactor_sequence(self):
        src = "def f():\n    x = 2 * 21\n    return x\n"
        r = refactor(src, [
            ("inline_variable", {"func_name": "f", "var_name": "x"}),
            ("simplify_conditionals", {}),
        ])
        # second op is a no-op -> sequence stops with failure, first applied
        self.assertFalse(r.ok)
        self.assertIn("return 2 * 21", r.source)

    def test_refactor_unknown_op(self):
        r = refactor("x = 1\n", [("bogus", {})])
        self.assertFalse(r.ok)

    def test_refactor_diff(self):
        d = refactor_diff("a = 1\n", "a = 2\n")
        self.assertIn("-a = 1", d)
        self.assertIn("+a = 2", d)

    def test_preview(self):
        p = preview_refactor("def f():\n    return 1\n    x = 2\n",
                             "remove_dead_code")
        self.assertTrue(p["ok"])
        self.assertIn("-    x = 2", p["diff"])


class TestScopeAnalyzer(unittest.TestCase):
    def test_scopes_of(self):
        tree = ast.parse("def f():\n    x = 1\n    return x\n")
        sa = ScopeAnalyzer(tree)
        self.assertTrue(sa.scopes_of("x"))

    def test_safe_rename_check(self):
        tree = ast.parse("def f(a):\n    return a + 1\n")
        sa = ScopeAnalyzer(tree)
        ok, _ = sa.is_safe_rename("a", "b")
        self.assertTrue(ok)
        ok, _ = sa.is_safe_rename("missing", "b")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
