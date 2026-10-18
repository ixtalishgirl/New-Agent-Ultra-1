"""Tests for tool-call format tolerance + the one-shot format-repair path.

Covers the model-tool-use fix:
- parser accepts <tool_call> tags and ```json blocks
- parser tolerates key variants (tool/function, arguments/parameters)
- find_mentioned_tools spots plain-text tool mentions (e.g. TOOL_DISCOVER)
- orchestrator performs exactly one format-repair retry, then executes
"""
import pytest

from agent.model.output_parser import parse_output, find_mentioned_tools
from agent.model.provider import ModelResponse
from agent.model.context_manager import ConversationHistory
from agent.tools.registry import ToolDef


def test_parses_tagged_call():
    p = parse_output(
        '<tool_call>{"name": "shell", "args": {"command": "ls"}}</tool_call>')
    assert len(p.tool_calls) == 1
    assert p.tool_calls[0].name == "shell"
    assert p.tool_calls[0].args == {"command": "ls"}


def test_parses_fenced_json():
    p = parse_output('```json\n{"name": "shell", "args": {"command": "ls"}}\n```')
    assert len(p.tool_calls) == 1
    assert p.tool_calls[0].name == "shell"


def test_parses_key_variants():
    p = parse_output(
        '<tool_call>{"tool": "shell", "arguments": {"command": "ls"}}</tool_call>')
    assert len(p.tool_calls) == 1
    assert p.tool_calls[0].name == "shell"
    assert p.tool_calls[0].args == {"command": "ls"}
    p2 = parse_output(
        '```json\n{"function": "shell", "parameters": {"command": "ls"}}\n```')
    assert len(p2.tool_calls) == 1
    assert p2.tool_calls[0].name == "shell"


def test_skips_malformed_call():
    p = parse_output('<tool_call>not json at all</tool_call>')
    assert p.tool_calls == []
    assert p.final_text  # falls back to raw text, never empty-crash


def test_find_mentioned_tools():
    names = ["shell", "tool_discover", "browser_navigate"]
    assert find_mentioned_tools("I will use TOOL_DISCOVER now",
                                names) == ["tool_discover"]
    assert find_mentioned_tools("nothing relevant here", names) == []
    # no partial-word false positives
    assert find_mentioned_tools("shellfish are tasty", names) == []
    assert find_mentioned_tools("", names) == []


def _make_orchestrator(provider):
    """Orchestrator with stubbed collaborators for the repair-path test."""
    from agent.app.orchestrator import Orchestrator

    class FakeReg:
        def list(self):
            return [ToolDef(name="shell", version="1",
                            description="d", input_schema={}),
                    ToolDef(name="tool_discover", version="1",
                            description="d", input_schema={})]

    class FakeDispatch:
        def __init__(self):
            self.calls = []

        def dispatch(self, name, args):
            self.calls.append((name, args))

            class R:
                ok = True
                output = "repaired-ok"
            return R()

    o = Orchestrator.__new__(Orchestrator)
    o.provider = provider
    o.dispatcher = FakeDispatch()
    o.dispatcher.registry = FakeReg()
    o.max_iterations = 3
    o.use_planning = False
    o.use_memory = False
    o.session_id = "test"
    o._system_prompt = "SYS"

    class FakeAudit:
        def log(self, **kw):
            pass
    o.audit = FakeAudit()
    o.history = ConversationHistory(max_tokens=6000)
    return o


def test_mentioned_tools_helper():
    o = _make_orchestrator(provider=None)
    assert o._mentioned_tools("Let me call TOOL_DISCOVER") == ["tool_discover"]
    assert o._mentioned_tools("hello there") == []


def test_repair_retry_executes_tool():
    """Model says TOOL_DISCOVER as text -> one repair retry -> valid call runs."""
    from agent.app.orchestrator import Orchestrator  # noqa: F401

    responses = [
        ModelResponse(text="I think I should use TOOL_DISCOVER here", raw={}),
        ModelResponse(
            text='<tool_call>{"name": "tool_discover", "args": {}}</tool_call>',
            raw={}),
        ModelResponse(text="<final_answer>done</final_answer>", raw={}),
    ]

    class FakeProvider:
        def __init__(self):
            self.n = 0

        def generate(self, system_prompt, user_prompt, max_tokens=2048):
            r = responses[min(self.n, len(responses) - 1)]
            self.n += 1
            return r

    o = _make_orchestrator(FakeProvider())
    result = o.run("discover tools", max_tokens=512)
    # 1 initial + 1 repair retry + 1 post-tool-result iteration
    assert o.provider.n == 3, "expected initial + repair + follow-up"
    assert o.dispatcher.calls, "repaired tool call was never dispatched"
    assert o.dispatcher.calls[0][0] == "tool_discover"
    assert result.tool_history and result.tool_history[0]["tool"] == "tool_discover"
    assert result.final_text == "done"


def test_no_repair_when_nothing_mentioned():
    """Plain final answer with no tool mention -> no extra generation."""
    responses = [ModelResponse(text="just a normal answer", raw={})]

    class FakeProvider:
        def __init__(self):
            self.n = 0

        def generate(self, system_prompt, user_prompt, max_tokens=2048):
            self.n += 1
            return responses[0]

    o = _make_orchestrator(FakeProvider())
    result = o.run("hello", max_tokens=512)
    assert o.provider.n == 1
    assert result.tool_history == []
    assert result.final_text == "just a normal answer"


def test_repetition_guard_stops_loop():
    """Same tool call 3 times -> loop stops with a summary, not 10 iters."""
    call = ('<tool_call>{"name": "shell", '
            '"args": {"command": "echo hi"}}</tool_call>')

    class FakeProvider:
        def generate(self, system_prompt, user_prompt, max_tokens=2048):
            return ModelResponse(text=call, raw={})

    o = _make_orchestrator(FakeProvider())
    o.max_iterations = 10
    result = o.run("echo hi", max_tokens=512)
    assert result.success is True
    assert "repeat loop" in result.final_text
    assert result.iterations == 3, f"expected stop at 3rd repeat, got {result.iterations}"
    assert len(result.tool_history) == 2  # 3rd identical call never executed
