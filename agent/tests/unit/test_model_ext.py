"""Unit tests for the prompt engine and model providers.

Provider tests use MockProvider — no network access. One probe test
targets a closed localhost port (connection refused is immediate).
"""
import sys
import os
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from agent.model.prompt_engine import (
    PromptEngine, Message, ToolSchema, FewShotExample, FewShotLibrary,
    PromptRegistry, PromptVariant, BudgetPlan,
    estimate_tokens, truncate_to_tokens, truncate_history,
    summarize_tool_result, format_tool_schemas, inject_permissions,
    inject_denials, inject_capability_summary,
    compress_history, inject_recap, plan_budget, rebalance_budget,
    lint_prompt, stable_prefix_hash, check_prefix_stability,
    generation_hint, CONTROL_LOOP,
)
from agent.model.providers import (
    ModelProvider, ChatMessage, ModelResponse, ProviderConfig,
    ProviderError, RetryableError,
    OpenAICompatibleProvider, CompletionsProvider, QwenServerProvider,
    RetryWrapper, StreamingHandler, TokenCounter, UsageTracker,
    TrackedProvider, FallbackProvider, MockProvider, ProviderPool,
    CachingProvider, RateLimitedProvider, ResponseGuard, GuardedProvider,
    parse_sse_stream, extract_streamed_text,
    probe_endpoint, redact_headers, summarize_request,
    generate_simple, build_default_stack,
)


def sample_tools():
    return [
        ToolSchema(name="shell", description="Run a bash command.",
                   args={"command": "string (required)"}),
        {"name": "file_read", "description": "Read a file.",
         "args": {"path": "string (required)"}},
    ]


def sample_perms():
    return [(1, "os", "run shell commands"), (2, "net", "fetch http urls")]


def sample_denials():
    return {"container_escape": "never", "exfiltration": "never"}


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

class TestTokens(unittest.TestCase):
    def test_estimate(self):
        self.assertEqual(estimate_tokens(""), 0)
        # 400 chars -> ~100 tokens
        self.assertEqual(estimate_tokens("x" * 400), 100)

    def test_truncate(self):
        text = "line\n" * 1000
        out = truncate_to_tokens(text, 100)
        self.assertLessEqual(estimate_tokens(out), 130)
        self.assertIn("[truncated]", out)
        self.assertEqual(truncate_to_tokens("short", 100), "short")
        self.assertEqual(truncate_to_tokens("abc", 0), "")


class TestToolSchemas(unittest.TestCase):
    def test_text_style(self):
        out = format_tool_schemas(sample_tools(), style="text")
        self.assertIn("- shell:", out)
        self.assertIn("file_read", out)

    def test_json_style(self):
        import json
        out = format_tool_schemas(sample_tools(), style="json")
        parsed = json.loads(out)
        self.assertEqual(parsed[0]["name"], "shell")

    def test_xml_style(self):
        out = format_tool_schemas(sample_tools(), style="xml")
        self.assertIn("<tools>", out)
        self.assertIn('name="shell"', out)

    def test_inject_permissions(self):
        out = inject_permissions(sample_perms())
        self.assertIn("1. run shell commands", out)

    def test_inject_permissions_truncates(self):
        perms = [(i, "s", "x" * 500) for i in range(50)]
        out = inject_permissions(perms, max_tokens=100)
        self.assertLessEqual(estimate_tokens(out), 130)

    def test_inject_denials(self):
        out = inject_denials(sample_denials())
        self.assertIn("container_escape", out)

    def test_capability_summary(self):
        out = inject_capability_summary(sample_perms())
        self.assertIn("2 granted permissions", out)


class TestTruncateHistory(unittest.TestCase):
    def _msgs(self, n=10, size=500):
        return [Message(role="system", content="sys"),
                *[Message(role="user" if i % 2 == 0 else "assistant",
                          content=f"m{i} " + "x" * size)
                  for i in range(n)]]

    def test_keeps_system_and_tail(self):
        msgs = truncate_history(self._msgs(), max_tokens=2000,
                                keep_last=2)
        self.assertEqual(msgs[0].role, "system")
        self.assertEqual(msgs[0].content, "sys")
        # last two preserved verbatim
        self.assertTrue(msgs[-1].content.startswith("m9"))
        self.assertTrue(msgs[-2].content.startswith("m8"))

    def test_marks_truncation(self):
        msgs = truncate_history(self._msgs(), max_tokens=600,
                                keep_last=2)
        self.assertTrue(any("truncated" in m.content for m in msgs))
        # tail still preserved verbatim
        self.assertTrue(msgs[-1].content.startswith("m9"))

    def test_dict_messages(self):
        msgs = truncate_history([{"role": "user", "content": "hi"}],
                                max_tokens=100)
        self.assertEqual(msgs[0].role, "user")

    def test_empty(self):
        self.assertEqual(truncate_history([], 100), [])

    def test_summarize_tool_result(self):
        long_out = "x" * 10000
        out = summarize_tool_result(long_out, max_tokens=100)
        self.assertIn("omitted", out)
        self.assertLess(len(out), len(long_out))
        self.assertEqual(summarize_tool_result("short", 100), "short")


class TestPromptEngine(unittest.TestCase):
    def setUp(self):
        self.pe = PromptEngine()

    def test_system_prompt_assembly(self):
        sys_p = self.pe.system_prompt_assembly(
            sample_perms(), sample_denials(), sample_tools())
        self.assertIn("2 permissions", sys_p)
        self.assertIn("container_escape", sys_p)
        self.assertIn("shell", sys_p)
        self.assertIn("<tool_call>", sys_p)
        self.assertIn("UNTRUSTED", sys_p)
        self.assertEqual(lint_prompt(
            [Message("system", sys_p), Message("user", "hi")]), [])

    def test_react_prompt(self):
        msgs = self.pe.build_react_prompt(
            "do the thing", sample_perms(), sample_denials(),
            sample_tools(), context="ctx", memory="mem",
            history=[Message("user", "earlier")])
        self.assertEqual(msgs[0].role, "system")
        self.assertEqual(msgs[-1].role, "user")
        self.assertIn("do the thing", msgs[-1].content)
        self.assertIn("ctx", msgs[-1].content)

    def test_plan_prompt(self):
        msgs = self.pe.build_plan_prompt("task", sample_tools())
        self.assertIn("numbered list", msgs[1].content)

    def test_debug_prompt(self):
        msgs = self.pe.build_debug_prompt("t", "rm -rf /", "boom" * 5000)
        self.assertIn("Root cause", msgs[1].content)

    def test_verify_prompt(self):
        msgs = self.pe.build_verify_prompt("t", "done", "evidence")
        self.assertIn("VERIFIED", msgs[1].content)

    def test_memory_save_prompt(self):
        msgs = self.pe.build_memory_save_prompt("t", "o")
        self.assertIn("triples", msgs[1].content)

    def test_improve_prompt(self):
        msgs = self.pe.build_improve_prompt("t", "built x", "tests pass")
        self.assertIn("improvements", msgs[1].content)

    def test_research_prompt(self):
        msgs = self.pe.build_research_prompt("t")
        self.assertIn("research plan only", msgs[1].content)

    def test_tool_select_prompt(self):
        msgs = self.pe.build_tool_select_prompt("t", "step 1",
                                                sample_tools())
        self.assertIn("shell", msgs[1].content)

    def test_inspect_prompt(self):
        msgs = self.pe.build_inspect_prompt("ran cmd", "output ok")
        self.assertIn("SUCCESS", msgs[1].content)

    def test_test_prompt(self):
        msgs = self.pe.build_test_prompt("t", "a script")
        self.assertIn("happy path", msgs[1].content)

    def test_summarize_prompt(self):
        msgs = self.pe.build_summarize_prompt(
            "t", [Message("user", "hi")])
        self.assertIn("handoff summary", msgs[1].content)

    def test_permission_check_prompt(self):
        msgs = self.pe.build_permission_check_prompt(
            "shell", {"command": "ls"}, sample_perms(), sample_denials())
        self.assertIn("ALLOW or DENY", msgs[1].content)

    def test_sections(self):
        self.assertIn("POWERS", self.pe.permissions_section(sample_perms()))
        self.assertIn("DENIALS", self.pe.denials_section(sample_denials()))
        self.assertIn("TOOLS", self.pe.tools_section(sample_tools()))
        task = self.pe.task_section("T", context="C", memory="M")
        self.assertIn("CURRENT TASK", task)

    def test_openai_conversion(self):
        msgs = [Message("user", "hi", name="")]
        dicts = self.pe.messages_to_openai(msgs)
        self.assertEqual(dicts, [{"role": "user", "content": "hi"}])

    def test_fits_and_shrink(self):
        msgs = [Message("system", "s"),
                Message("user", "h1 " + "x" * 8000),
                Message("assistant", "h2 " + "y" * 8000),
                Message("user", "current task")]
        self.assertFalse(self.pe.fits_context(msgs, 3000))
        shrunk = self.pe.shrink_to_fit(msgs, 3000, reserve=500)
        self.assertTrue(self.pe.fits_context(shrunk, 3000))
        # the current task (tail) is never dropped
        self.assertEqual(shrunk[-1].content, "current task")
        # already fits -> unchanged length
        small = [Message("user", "hi")]
        self.assertEqual(len(self.pe.shrink_to_fit(small, 8192)), 1)


class TestFewShot(unittest.TestCase):
    def test_library(self):
        lib = FewShotLibrary()
        for e in lib.default_tool_use_examples():
            lib.add(e)
        found = lib.find(["file"])
        self.assertEqual(len(found), 1)
        rendered = lib.render(found)
        self.assertIn("<tool_call>", rendered)
        self.assertEqual(len(lib.find(limit=5)), 2)

    def test_example_dataclass(self):
        e = FewShotExample(task="t", reasoning="r", tags=["x"])
        self.assertEqual(e.tool_calls, [])


class TestRegistry(unittest.TestCase):
    def test_variants(self):
        reg = PromptRegistry()
        self.assertEqual(reg.get("nope").name, "default")
        reg.register(PromptVariant(name="custom", history_budget=100))
        self.assertEqual(reg.get("custom").history_budget, 100)

    def test_apply(self):
        pe = PromptEngine()
        reg = PromptRegistry()
        msgs = reg.apply(pe, "tight-context", "do X", sample_perms(),
                         sample_denials(), sample_tools())
        self.assertEqual(len(msgs), 2)
        self.assertIn("terse", msgs[0].content)
        # identity restored afterwards
        self.assertNotIn("terse", pe.identity)


class TestHistoryCompression(unittest.TestCase):
    def test_compress(self):
        msgs = [Message("system", "s")] + \
               [Message("user", f"m{i}") for i in range(10)]
        out = compress_history(msgs, keep_recent=3)
        self.assertEqual(len(out), 4)
        self.assertEqual(out[0].content, "s")

    def test_inject_recap(self):
        msgs = [Message("system", "s")] + \
               [Message("user", f"m{i}") for i in range(10)]
        out = inject_recap(msgs, "we did stuff", keep_recent=2)
        self.assertIn("Recap", out[1].content)
        self.assertEqual(len(out), 4)


class TestBudget(unittest.TestCase):
    def test_plan_and_rebalance(self):
        plan = plan_budget(8192, "sys", "task",
                           [Message("user", "x" * 40000)])
        self.assertFalse(plan.fits)
        self.assertGreater(plan.overflow, 0)
        caps = rebalance_budget(plan)
        self.assertLess(caps["history"], plan.history_tokens)

    def test_fits_no_rebalance(self):
        plan = plan_budget(8192, "sys", "task", [])
        self.assertTrue(plan.fits)
        caps = rebalance_budget(plan)
        self.assertEqual(caps["system"], plan.system_tokens)


class TestLint(unittest.TestCase):
    def test_empty(self):
        self.assertIn("prompt is empty", lint_prompt([]))

    def test_missing_system(self):
        w = lint_prompt([Message("user", "hi")])
        self.assertTrue(any("no system" in x for x in w))

    def test_missing_tool_format(self):
        w = lint_prompt([Message("system", "be nice")])
        self.assertTrue(any("tool_call" in x for x in w))

    def test_prefix_stability(self):
        a = [Message("system", "same"), Message("user", "one")]
        b = [Message("system", "same"), Message("user", "two")]
        c = [Message("system", "diff"), Message("user", "one")]
        self.assertTrue(check_prefix_stability(a, b))
        self.assertFalse(check_prefix_stability(a, c))
        self.assertEqual(len(stable_prefix_hash(a)), 16)

    def test_generation_hint(self):
        self.assertIn("stop", generation_hint("react"))
        self.assertEqual(generation_hint("unknown"), {})


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

class TestChatMessage(unittest.TestCase):
    def test_to_dict(self):
        m = ChatMessage(role="user", content="hi")
        self.assertEqual(m.to_dict(), {"role": "user", "content": "hi"})
        m2 = ChatMessage(role="tool", content="out", name="shell")
        self.assertEqual(m2.to_dict()["name"], "shell")

    def test_response_total(self):
        r = ModelResponse(text="x", prompt_tokens=3, completion_tokens=4)
        self.assertEqual(r.total_tokens, 7)


class TestProviderConfig(unittest.TestCase):
    def test_from_env(self):
        os.environ["HALEY_MODEL_BASE_URL"] = "http://x:8000/v1"
        os.environ["HALEY_MODEL_MODEL"] = "test-model"
        cfg = ProviderConfig.from_env()
        self.assertEqual(cfg.base_url, "http://x:8000/v1")
        self.assertEqual(cfg.model, "test-model")
        del os.environ["HALEY_MODEL_BASE_URL"]
        del os.environ["HALEY_MODEL_MODEL"]


class TestMockProvider(unittest.TestCase):
    def test_generate(self):
        p = MockProvider(responses=["hello"])
        r = p.generate([ChatMessage("user", "hi")])
        self.assertEqual(r.text, "hello")
        self.assertEqual(r.model, "mock")
        self.assertTrue(p.health_check())

    def test_fail_times(self):
        p = MockProvider(fail_times=2)
        for _ in range(2):
            with self.assertRaises(RetryableError):
                p.generate([ChatMessage("user", "x")])
        self.assertEqual(p.generate([ChatMessage("user", "x")]).text,
                         "mock response")

    def test_round_robin_responses(self):
        p = MockProvider(responses=["a", "b"])
        msgs = [ChatMessage("user", "x")]
        self.assertEqual(p.generate(msgs).text, "a")
        self.assertEqual(p.generate(msgs).text, "b")
        self.assertEqual(p.generate(msgs).text, "a")


class TestRetryWrapper(unittest.TestCase):
    def test_retries_then_succeeds(self):
        inner = MockProvider(responses=["ok"], fail_times=2)
        rw = RetryWrapper(inner, max_retries=3, base_delay=0.001)
        r = rw.generate([ChatMessage("user", "x")])
        self.assertEqual(r.text, "ok")
        self.assertEqual(rw.attempts_made, 3)

    def test_exhausts(self):
        inner = MockProvider(fail_times=10)
        rw = RetryWrapper(inner, max_retries=2, base_delay=0.001)
        with self.assertRaises(RetryableError):
            rw.generate([ChatMessage("user", "x")])
        self.assertEqual(rw.attempts_made, 3)
        self.assertIsNotNone(rw.last_error)

    def test_fatal_not_retried(self):
        class Fatal(MockProvider):
            def generate(self, *a, **k):
                raise ProviderError("bad request")
        rw = RetryWrapper(Fatal(), max_retries=3, base_delay=0.001)
        with self.assertRaises(ProviderError):
            rw.generate([ChatMessage("user", "x")])
        self.assertEqual(rw.attempts_made, 1)

    def test_health_check_passthrough(self):
        rw = RetryWrapper(MockProvider())
        self.assertTrue(rw.health_check())


class TestSSE(unittest.TestCase):
    SSE = ('data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
           'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
           ': heartbeat\n\n'
           'data: [DONE]\n\n')

    def test_parse(self):
        events = parse_sse_stream(self.SSE)
        self.assertEqual(len(events), 3)
        self.assertTrue(events[-1].get("done"))
        self.assertEqual(extract_streamed_text(events), "Hello")

    def test_multiline_data(self):
        events = parse_sse_stream('data: {"a":\ndata: 1}\n\n')
        self.assertEqual(events[0]["data"], {"a": 1})

    def test_empty(self):
        self.assertEqual(parse_sse_stream(""), [])


class TestTokenCounter(unittest.TestCase):
    def test_count(self):
        self.assertEqual(TokenCounter.count(""), 0)
        self.assertEqual(TokenCounter.count("x" * 400), 100)

    def test_messages(self):
        msgs = [ChatMessage("user", "x" * 400)]
        self.assertEqual(TokenCounter.count_messages(msgs), 104)

    def test_truncate_and_fits(self):
        self.assertTrue(TokenCounter.fits("hi", 100))
        self.assertFalse(TokenCounter.fits("x" * 4000, 10))
        out = TokenCounter.truncate("x" * 4000, 10)
        self.assertIn("[truncated]", out)


class TestUsageTracker(unittest.TestCase):
    def test_totals(self):
        t = UsageTracker(price_per_1k={"m": (1.0, 2.0)})
        t.record(ModelResponse(text="a", model="m", prompt_tokens=1000,
                               completion_tokens=500, latency_s=1.0))
        t.record(ModelResponse(text="b", model="m", prompt_tokens=0,
                               completion_tokens=0, latency_s=2.0))
        totals = t.totals()
        self.assertEqual(totals["calls"], 2)
        self.assertEqual(totals["total_tokens"], 1500)
        self.assertAlmostEqual(totals["est_cost_usd"], 2.0)
        t.reset()
        self.assertEqual(t.totals()["calls"], 0)


class TestTrackedProvider(unittest.TestCase):
    def test_records_ok(self):
        inner = MockProvider(responses=["ok"])
        tp = TrackedProvider(inner)
        tp.generate([ChatMessage("user", "x")])
        self.assertEqual(tp.tracker.totals()["ok"], 1)

    def test_records_failure(self):
        inner = MockProvider(fail_times=99)
        tp = TrackedProvider(inner)
        with self.assertRaises(RetryableError):
            tp.generate([ChatMessage("user", "x")])
        totals = tp.tracker.totals()
        self.assertEqual(totals["calls"], 1)
        self.assertEqual(totals["ok"], 0)


class TestFallbackProvider(unittest.TestCase):
    def test_falls_through(self):
        fb = FallbackProvider([MockProvider(fail_times=99),
                               MockProvider(responses=["second"])])
        r = fb.generate([ChatMessage("user", "x")])
        self.assertEqual(r.text, "second")
        self.assertEqual(fb.last_used, 1)

    def test_all_fail(self):
        fb = FallbackProvider([MockProvider(fail_times=99)])
        with self.assertRaises(ProviderError):
            fb.generate([ChatMessage("user", "x")])

    def test_needs_one(self):
        with self.assertRaises(ValueError):
            FallbackProvider([])

    def test_health(self):
        fb = FallbackProvider([MockProvider()])
        self.assertTrue(fb.health_check())


class TestProviderPool(unittest.TestCase):
    def test_round_robin(self):
        pool = ProviderPool([MockProvider(responses=["a"]),
                             MockProvider(responses=["b"])])
        msgs = [ChatMessage("user", "x")]
        self.assertEqual([pool.generate(msgs).text for _ in range(3)],
                         ["a", "b", "a"])

    def test_skips_failed(self):
        pool = ProviderPool([MockProvider(fail_times=99),
                             MockProvider(responses=["ok"])])
        r = pool.generate([ChatMessage("user", "x")])
        self.assertEqual(r.text, "ok")
        self.assertEqual(pool.error_counts(), {0: 1})


class TestCachingProvider(unittest.TestCase):
    def test_hit(self):
        inner = MockProvider(responses=["v"])
        c = CachingProvider(inner)
        msgs = [ChatMessage("user", "q")]
        self.assertEqual(c.generate(msgs).text, "v")
        self.assertEqual(c.generate(msgs).text, "v")
        self.assertEqual(inner.calls, 1)
        stats = c.stats()
        self.assertEqual(stats["hits"], 1)
        self.assertAlmostEqual(stats["hit_rate"], 0.5)

    def test_different_prompts_miss(self):
        c = CachingProvider(MockProvider(responses=["v"]))
        c.generate([ChatMessage("user", "a")])
        c.generate([ChatMessage("user", "b")])
        self.assertEqual(c.stats()["misses"], 2)

    def test_clear(self):
        c = CachingProvider(MockProvider())
        c.generate([ChatMessage("user", "a")])
        c.clear()
        self.assertEqual(c.stats()["entries"], 0)


class TestRateLimitedProvider(unittest.TestCase):
    def test_allows_burst(self):
        rl = RateLimitedProvider(MockProvider(responses=["x"]),
                                 calls_per_minute=6000, burst=3)
        msgs = [ChatMessage("user", "q")]
        for _ in range(3):
            self.assertEqual(rl.generate(msgs).text, "x")

    def test_max_wait_exceeded(self):
        rl = RateLimitedProvider(MockProvider(), calls_per_minute=1,
                                 burst=1, max_wait_s=0.001)
        msgs = [ChatMessage("user", "q")]
        rl.generate(msgs)  # uses the burst token
        with self.assertRaises(ProviderError):
            rl.generate(msgs)


class TestGuard(unittest.TestCase):
    def test_rejects_empty(self):
        g = GuardedProvider(MockProvider(responses=[""]),
                            guard=ResponseGuard(min_chars=1))
        with self.assertRaises(ProviderError):
            g.generate([ChatMessage("user", "x")])

    def test_rejects_too_long(self):
        g = GuardedProvider(MockProvider(responses=["y" * 100]),
                            guard=ResponseGuard(max_chars=10))
        with self.assertRaises(ProviderError):
            g.generate([ChatMessage("user", "x")])

    def test_cleans_truncation(self):
        guard = ResponseGuard(max_chars=10)
        r = ModelResponse(text="y" * 50)
        cleaned = guard.clean(r)
        self.assertIn("[truncated]", cleaned.text)

    def test_passes_clean(self):
        g = GuardedProvider(MockProvider(responses=["fine answer"]))
        r = g.generate([ChatMessage("user", "q")])
        self.assertEqual(r.text, "fine answer")


class TestRedaction(unittest.TestCase):
    def test_redact_headers(self):
        out = redact_headers({"Authorization": "Bearer sk-123",
                              "Content-Type": "application/json",
                              "X-Api-Key": "abc"})
        self.assertEqual(out["Authorization"], "***redacted***")
        self.assertEqual(out["X-Api-Key"], "***redacted***")
        self.assertEqual(out["Content-Type"], "application/json")

    def test_summarize_request(self):
        payload = {"model": "m",
                   "messages": [{"role": "user",
                                 "content": "secret prompt here"}],
                   "max_tokens": 100, "temperature": 0.5}
        s = summarize_request(payload)
        self.assertEqual(s["num_messages"], 1)
        self.assertNotIn("secret prompt here", str(s))


class TestProbe(unittest.TestCase):
    def test_closed_port(self):
        # Connection refused is immediate; must not raise.
        result = probe_endpoint("http://127.0.0.1:1/v1", timeout=3)
        self.assertFalse(result["ok"])
        self.assertIn("error", result)


class TestConvenience(unittest.TestCase):
    def test_generate_simple(self):
        p = MockProvider(responses=["simple"])
        self.assertEqual(generate_simple(p, "hi", system="sys"), "simple")

    def test_build_default_stack(self):
        # Structure only — no network calls in unit tests.
        stack = build_default_stack()
        self.assertIsInstance(stack, CachingProvider)
        self.assertIsInstance(stack.inner, RetryWrapper)
        self.assertIsInstance(stack.inner.inner, TrackedProvider)
        self.assertIsInstance(stack.inner.inner.inner,
                              OpenAICompatibleProvider)

    def test_qwen_provider_env(self):
        os.environ["HALEY_MODEL_BASE_URL"] = "http://gpu:8000/v1"
        os.environ["HALEY_MODEL_MODEL"] = "qwen-test"
        p = QwenServerProvider()
        self.assertEqual(p.config.base_url, "http://gpu:8000/v1")
        self.assertEqual(p.config.model, "qwen-test")
        del os.environ["HALEY_MODEL_BASE_URL"]
        del os.environ["HALEY_MODEL_MODEL"]

    def test_streaming_handler_parses(self):
        # Exercise the SSE helpers through StreamingHandler indirectly:
        # build a fake provider is complex; test helpers directly instead.
        events = parse_sse_stream('data: {"choices":[{"delta":{"content":"a"}}]}\n\n')
        self.assertEqual(extract_streamed_text(events), "a")
        self.assertIsInstance(StreamingHandler(MockProvider()),  # type: ignore[arg-type]
                              StreamingHandler)


if __name__ == "__main__":
    unittest.main()
