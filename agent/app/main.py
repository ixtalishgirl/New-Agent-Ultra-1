"""Halye autonomous coding agent — main entry point.

Run as:  python3 -m agent.app.main [task...]   (from the repo root)
"""
from __future__ import annotations
import sys

from .config import load_config
from ..model.qwen_adapter import QwenAdapter
from .orchestrator import Orchestrator
from ..tools.registry import default_registry
from ..tools.dispatcher import Dispatcher


def main() -> None:
    cfg = load_config()
    provider = QwenAdapter(base_url=cfg.model.base_url,
                           timeout=cfg.model.timeout)
    print(f"[model] health check: {cfg.model.base_url} ...")
    healthy = provider.health_check()
    print(f"[model] healthy: {healthy}")
    if not healthy:
        print("[model] WARNING: model endpoint not reachable. "
              "Start the Qwen 7B server / ngrok tunnel first.")
    registry = default_registry()
    dispatcher = Dispatcher(registry, workspace=cfg.execution.workspace)
    orch = Orchestrator(provider, dispatcher,
                        max_iterations=cfg.max_iterations)

    if len(sys.argv) > 1:
        task = " ".join(sys.argv[1:])
    else:
        task = input("Task: ")
    result = orch.run(task)
    print("\n" + "=" * 60)
    print(f"SUCCESS: {result.success}  iterations: {result.iterations}")
    print("=" * 60)
    print(result.final_text)
    print("\n--- tool history ---")
    for h in result.tool_history:
        print(f"[{h['tool']}] ok={h['ok']}")


if __name__ == "__main__":
    main()
