"""Tool registry — every tool has name, version, schema, permission profile, status."""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class ToolDef:
    name: str
    version: str
    description: str
    input_schema: dict
    permission_profile: str = "workspace"
    timeout: int = 60
    status: str = "APPROVED"  # DRAFT, TESTING, APPROVED, DISABLED


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolDef] = {}

    def register(self, tool: ToolDef) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDef | None:
        t = self._tools.get(name)
        return t if t and t.status == "APPROVED" else None

    def list(self) -> list[ToolDef]:
        return [t for t in self._tools.values() if t.status == "APPROVED"]

    def disable(self, name: str) -> None:
        if name in self._tools:
            self._tools[name].status = "DISABLED"


def default_registry() -> ToolRegistry:
    reg = ToolRegistry()
    for name, desc, schema in [
        ("shell", "Execute bash command", {"command": "string"}),
        ("python", "Execute Python code", {"code": "string"}),
        ("pip", "Install pip package", {"package": "string"}),
        ("file_read", "Read file", {"path": "string"}),
        ("file_write", "Write file", {"path": "string", "content": "string"}),
        ("browser_navigate", "Navigate browser", {"url": "string"}),
        ("browser_extract", "Extract page content", {}),
        ("browser_screenshot", "Screenshot page", {}),
        ("web_search", "Public web search", {"query": "string"}),
        ("germany_fetch", "HTTP via Germany", {"url": "string"}),
        ("supabase_query", "Supabase SQL", {"sql": "string"}),
        ("self_modify", "Propose code patch", {"file": "string", "new": "string", "reason": "string"}),
        ("tool_create", "Create new tool", {"name": "string", "code": "string"}),
    ]:
        reg.register(ToolDef(name=name, version="1.0.0",
                             description=desc, input_schema=schema))
    return reg
