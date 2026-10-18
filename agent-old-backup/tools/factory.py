"""
Tool Factory — autonomous tool generation with isolated testing.
The agent detects missing capabilities, generates new tools, tests them
in isolation, and registers passing tools as APPROVED.
"""
from __future__ import annotations
import hashlib
import json
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GeneratedTool:
    name: str
    version: str
    description: str
    input_schema: dict
    output_schema: dict
    code: str
    language: str = "python"  # python | bash
    timeout: int = 60
    checksum: str = ""
    status: str = "DRAFT"  # DRAFT, TESTING, APPROVED, DISABLED
    test_results: list = field(default_factory=list)

    def __post_init__(self):
        if not self.checksum:
            self.checksum = hashlib.sha256(self.code.encode()).hexdigest()[:16]


class ToolFactory:
    """
    Generates new tools when existing tools cannot complete a task.
    Workflow: propose → generate → test in isolation → register.
    """

    def __init__(self, generated_dir: Path, registry=None):
        self.generated_dir = Path(generated_dir)
        self.generated_dir.mkdir(parents=True, exist_ok=True)
        self.registry = registry
        self._history: list[GeneratedTool] = []

    def propose_spec(
        self,
        name: str,
        description: str,
        input_schema: dict,
        output_schema: dict | None = None,
    ) -> dict:
        """Create a tool specification for model/parent review."""
        return {
            "name": name,
            "description": description,
            "input_schema": input_schema,
            "output_schema": output_schema or {"type": "object"},
            "status": "PROPOSED",
        }

    def generate(
        self,
        name: str,
        description: str,
        input_schema: dict,
        code: str,
        language: str = "python",
        output_schema: dict | None = None,
        timeout: int = 60,
    ) -> GeneratedTool:
        """Generate a new tool and save to generated/ directory."""
        # Sanitize name
        safe_name = "".join(c if c.isalnum() or c == "_" else "_" for c in name)
        version = "1.0.0"

        tool = GeneratedTool(
            name=safe_name,
            version=version,
            description=description,
            input_schema=input_schema,
            output_schema=output_schema or {"type": "object"},
            code=code,
            language=language,
            timeout=timeout,
            status="DRAFT",
        )

        # Save implementation
        ext = "py" if language == "python" else "sh"
        impl_path = self.generated_dir / f"{safe_name}_v{version}.{ext}"
        impl_path.write_text(code)

        # Save metadata
        meta_path = self.generated_dir / f"{safe_name}_v{version}.json"
        meta_path.write_text(json.dumps({
            "name": tool.name,
            "version": tool.version,
            "description": tool.description,
            "input_schema": tool.input_schema,
            "output_schema": tool.output_schema,
            "language": tool.language,
            "timeout": tool.timeout,
            "checksum": tool.checksum,
            "status": tool.status,
        }, indent=2))

        self._history.append(tool)
        return tool

    def test_isolated(self, tool: GeneratedTool, test_cases: list[dict]) -> dict:
        """
        Test a generated tool in isolation.
        Each test_case: {"input": {...}, "expect_contains": "string" or None}
        Returns: {"passed": int, "failed": int, "results": [...]}
        """
        tool.status = "TESTING"
        results = []
        passed = 0

        for i, tc in enumerate(test_cases):
            test_input = tc.get("input", {})
            expect = tc.get("expect_contains")

            try:
                output = self._run_isolated(tool, test_input)
                ok = True
                if expect and expect not in str(output):
                    ok = False
                results.append({
                    "test": i,
                    "input": test_input,
                    "output": str(output)[:500],
                    "passed": ok,
                })
                if ok:
                    passed += 1
            except Exception as e:
                results.append({
                    "test": i,
                    "input": test_input,
                    "output": f"ERROR: {e}",
                    "passed": False,
                })

        tool.test_results = results
        summary = {
            "passed": passed,
            "failed": len(test_cases) - passed,
            "total": len(test_cases),
            "results": results,
        }

        # Auto-promote if all tests pass
        if summary["failed"] == 0 and summary["total"] > 0:
            self.approve(tool)

        return summary

    def _run_isolated(self, tool: GeneratedTool, test_input: dict) -> str:
        """Run tool code in a disposable subprocess with timeout."""
        with tempfile.TemporaryDirectory() as tmpdir:
            if tool.language == "python":
                script_path = Path(tmpdir) / "tool_test.py"
                # Wrap: tool code defines main(input_dict) or runs directly
                wrapper = textwrap.dedent(f"""
                    import json, sys
                    test_input = json.loads(sys.argv[1])
                    {tool.code}
                    # If code defines main(), call it
                    if 'main' in dir():
                        result = main(test_input)
                        print(json.dumps(result) if not isinstance(result, str) else result)
                """)
                script_path.write_text(wrapper)
                proc = subprocess.run(
                    [sys.executable, str(script_path), json.dumps(test_input)],
                    capture_output=True,
                    text=True,
                    timeout=tool.timeout,
                    cwd=tmpdir,
                )
            else:  # bash
                script_path = Path(tmpdir) / "tool_test.sh"
                script_path.write_text(tool.code)
                script_path.chmod(0o755)
                proc = subprocess.run(
                    ["bash", str(script_path)],
                    capture_output=True,
                    text=True,
                    timeout=tool.timeout,
                    cwd=tmpdir,
                    input=json.dumps(test_input),
                )

            if proc.returncode != 0:
                raise RuntimeError(f"Tool failed: {proc.stderr[:300]}")
            return proc.stdout.strip()

    def approve(self, tool: GeneratedTool) -> None:
        """Promote a tested tool to APPROVED and register it."""
        tool.status = "APPROVED"
        # Update metadata file
        meta_path = self.generated_dir / f"{tool.name}_v{tool.version}.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            meta["status"] = "APPROVED"
            meta_path.write_text(json.dumps(meta, indent=2))

        # Register in tool registry if available
        if self.registry:
            from .registry import ToolDef
            self.registry.register(ToolDef(
                name=tool.name,
                version=tool.version,
                description=tool.description,
                input_schema=tool.input_schema,
                status="APPROVED",
            ))

    def disable(self, name: str) -> bool:
        """Disable a generated tool."""
        for tool in self._history:
            if tool.name == name:
                tool.status = "DISABLED"
                return True
        return False

    def list_history(self) -> list[dict]:
        """List all generated tools with their status."""
        return [
            {
                "name": t.name,
                "version": t.version,
                "status": t.status,
                "checksum": t.checksum,
                "tests_passed": sum(1 for r in t.test_results if r.get("passed")),
            }
            for t in self._history
        ]
