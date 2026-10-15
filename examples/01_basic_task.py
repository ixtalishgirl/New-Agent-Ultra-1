#!/usr/bin/env python3
"""Example 01: Run a basic task through the agent.

This demonstrates the simplest usage of the orchestrator:
create a provider, dispatcher, and run a task.

Requires: QWEN_BASE_URL set (or uses MockProvider for demo).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.tools.registry import default_registry
from agent.tools.dispatcher import Dispatcher


def main():
    # Use mock provider for demo (no model needed)
    from agent.model.providers import MockProvider
    from agent.app.orchestrator import Orchestrator

    provider = MockProvider(
        responses=[
            # First: model decides to list files
            '<tool_call name="shell">{"command": "ls -la"}</tool_call>',
            # Second: model gives final answer
            "<final_answer>Listed files successfully.</final_answer>",
        ])
    registry = default_registry()
    dispatcher = Dispatcher(registry, workspace=".")
    orch = Orchestrator(provider, dispatcher, max_iterations=5,
                        use_planning=False, use_memory=False)

    print("Running task: List files in current directory")
    print("-" * 50)
    result = orch.run("List files in the current directory")

    print(f"\nSuccess: {result.success}")
    print(f"Iterations: {result.iterations}")
    print(f"Tools used: {len(result.tool_history)}")
    print(f"\nFinal answer:\n{result.final_text}")


if __name__ == "__main__":
    main()
