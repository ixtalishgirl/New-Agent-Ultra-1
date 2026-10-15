"""Agent orchestrator — the ReAct control loop.

PLAN -> RESEARCH -> SELECT TOOL -> CHECK PERMISSIONS -> EXECUTE
  -> INSPECT RESULT -> DEBUG -> TEST -> IMPROVE -> VERIFY -> SAVE MEMORY

The model NEVER executes anything itself. It emits <tool_call> blocks;
the dispatcher runs them for real and feeds <tool_result> back.
"""
from __future__ import annotations
from dataclasses import dataclass, field

from ..model.provider import ModelProvider
from ..model.prompt_builder import build_system_prompt
from ..model.output_parser import parse_output
from ..tools.dispatcher import Dispatcher


@dataclass
class AgentResult:
    success: bool
    final_text: str
    iterations: int
    tool_history: list = field(default_factory=list)


class Orchestrator:
    def __init__(self, provider: ModelProvider, dispatcher: Dispatcher,
                 max_iterations: int = 10):
        self.provider = provider
        self.dispatcher = dispatcher
        self.max_iterations = max_iterations
        self._system_prompt = build_system_prompt()

    def run(self, task: str, max_tokens: int = 2048) -> AgentResult:
        history: list = []
        context = task
        for i in range(1, self.max_iterations + 1):
            resp = self.provider.generate(self._system_prompt, context,
                                          max_tokens=max_tokens)
            parsed = parse_output(resp.text)
            if not parsed.tool_calls:
                return AgentResult(success=True, final_text=parsed.final_text,
                                   iterations=i, tool_history=history)
            results = []
            for tc in parsed.tool_calls:
                dr = self.dispatcher.dispatch(tc.name, tc.args)
                entry = {"tool": tc.name, "args": tc.args,
                         "ok": dr.ok, "output": dr.output[:4000]}
                history.append(entry)
                results.append(
                    f"<tool_result name=\"{tc.name}\" ok=\"{dr.ok}\">\n"
                    f"{dr.output[:4000]}\n</tool_result>")
            context = (f"Original task: {task}\n\nPrevious model output:\n{resp.text}\n\n"
                       f"Tool results:\n" + "\n".join(results) +
                       "\n\nContinue. Emit more <tool_call> blocks or <final_answer>.")
        return AgentResult(success=False,
                           final_text="Max iterations reached without final answer.",
                           iterations=self.max_iterations,
                           tool_history=history)
