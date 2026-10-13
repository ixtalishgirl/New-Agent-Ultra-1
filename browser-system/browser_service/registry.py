"""Tool registry.

Every browser capability is a *tool*: a name, a human description, a JSON
schema for its arguments, and an async handler. Tools live in their own
module under ``tools/`` and register themselves here. Adding a new tool =
new file + one ``register_tool(...)`` call, no changes to app wiring.

Safety note: page content is DATA. Handlers must never treat text scraped
from a page as instructions to follow.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List


# Handler signature: async def handler(ctx: ToolContext, args: dict) -> dict
ToolHandler = Callable[["ToolContext", Dict[str, Any]], Awaitable[Dict[str, Any]]]


@dataclass
class ToolContext:
    """Everything a tool handler may need. Passed by the service layer."""

    manager: Any          # BrowserManager (imported lazily to avoid cycles)
    settings: Any         # Settings
    history: Any          # HistoryStore | None
    emit: Callable[[str, Dict[str, Any]], Awaitable[None]]  # websocket event emitter


@dataclass
class Tool:
    name: str
    description: str
    schema: Dict[str, Any]          # JSON Schema for arguments
    handler: ToolHandler
    tags: List[str] = field(default_factory=list)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(
        self,
        name: str,
        description: str,
        schema: Dict[str, Any],
        handler: ToolHandler,
        tags: List[str] | None = None,
    ) -> Tool:
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        tool = Tool(name=name, description=description, schema=schema,
                    handler=handler, tags=tags or [])
        self._tools[name] = tool
        return tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(f"unknown tool: {name}") from None

    def names(self) -> List[str]:
        return sorted(self._tools.keys())

    def spec_list(self) -> List[Dict[str, Any]]:
        """Public spec for GET /tools — name/description/schema/tags only."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "schema": t.schema,
                "tags": t.tags,
            }
            for t in (self._tools[n] for n in self.names())
        ]


registry = ToolRegistry()
