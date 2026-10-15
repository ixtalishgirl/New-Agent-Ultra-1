"""Tool schemas — JSON schema definitions for all tools (ChatGPT spec).

Per spec: agent/tools/schemas.py
Every tool has: unique name, version, description, JSON input/output schemas,
permission profile, timeout. Status: DRAFT/TESTING/APPROVED/DISABLED.
No personality. No rules.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


class ToolStatus(Enum):
    DRAFT = "draft"
    TESTING = "testing"
    APPROVED = "approved"
    DISABLED = "disabled"


@dataclass
class ToolSchema:
    """Full specification for a tool (ChatGPT spec compliant)."""
    name: str
    version: str
    description: str
    input_schema: Dict[str, Any] = field(default_factory=dict)
    output_schema: Dict[str, Any] = field(default_factory=dict)
    permission_profile: str = "standard"
    timeout: int = 60
    status: ToolStatus = ToolStatus.APPROVED
    error_contract: Dict[str, str] = field(default_factory=dict)
    checksum: str = ""
    tags: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.checksum:
            self.checksum = self.compute_checksum()
        if not self.error_contract:
            self.error_contract = {
                "ok_false": "Tool execution failed; see output for details",
                "timeout": "Tool exceeded timeout; output truncated",
                "denied": "Tool call denied by policy",
                "unknown": "Unknown tool or disabled",
            }

    def compute_checksum(self) -> str:
        h = hashlib.sha256()
        h.update(self.name.encode())
        h.update(self.version.encode())
        h.update(json.dumps(self.input_schema, sort_keys=True).encode())
        return h.hexdigest()[:16]

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ToolSchema":
        d = dict(d)
        d["status"] = ToolStatus(d.get("status", "approved"))
        return cls(**d)

    def validate_args(self, args: Dict[str, Any]) -> List[str]:
        """Validate args against input_schema. Returns error list."""
        errors = []
        props = self.input_schema.get("properties", {})
        required = self.input_schema.get("required", [])
        for r in required:
            if r not in args:
                errors.append(f"missing required arg: {r}")
        for k, v in args.items():
            if k in props:
                expected = props[k].get("type")
                if expected and not self._type_ok(v, expected):
                    errors.append(
                        f"arg '{k}' should be {expected}, "
                        f"got {type(v).__name__}")
        return errors

    @staticmethod
    def _type_ok(value: Any, expected: str) -> bool:
        mapping = {
            "string": str, "integer": int, "number": (int, float),
            "boolean": bool, "array": list, "object": dict,
        }
        py = mapping.get(expected)
        if py is None:
            return True
        if expected == "integer" and isinstance(value, bool):
            return False
        return isinstance(value, py)


def _s(props: Dict[str, Any],
        required: Optional[List[str]] = None) -> Dict[str, Any]:
    """Shorthand for object schemas."""
    return {"type": "object", "properties": props,
            "required": required or []}


def _out(desc: str = "Tool output text") -> Dict[str, Any]:
    return _s({
        "ok": {"type": "boolean", "description": "Success flag"},
        "output": {"type": "string", "description": desc},
    })


# ---------------------------------------------------------------------------
# Built-in tool schemas
# ---------------------------------------------------------------------------

BUILTIN_SCHEMAS: List[ToolSchema] = [
    ToolSchema(
        name="shell", version="2.0.0",
        description="Execute a bash command directly (no sandbox).",
        input_schema=_s({
            "command": {"type": "string",
                        "description": "Bash command to execute"},
            "timeout": {"type": "integer",
                        "description": "Timeout in seconds"},
        }, required=["command"]),
        output_schema=_out("stdout+stderr of the command"),
        permission_profile="execution", timeout=60,
        tags=["execution", "core"]),

    ToolSchema(
        name="python", version="2.0.0",
        description="Execute Python code directly (no sandbox).",
        input_schema=_s({
            "code": {"type": "string",
                     "description": "Python code to execute"},
            "timeout": {"type": "integer",
                        "description": "Timeout in seconds"},
        }, required=["code"]),
        output_schema=_out("stdout+stderr of the script"),
        permission_profile="execution", timeout=60,
        tags=["execution", "core"]),

    ToolSchema(
        name="pip", version="1.0.0",
        description="Install a Python package via pip.",
        input_schema=_s({
            "package": {"type": "string",
                        "description": "Package spec, e.g. requests==2.31"},
        }, required=["package"]),
        output_schema=_out("pip install output"),
        permission_profile="execution", timeout=300,
        tags=["execution", "setup"]),

    ToolSchema(
        name="file_read", version="1.0.0",
        description="Read a file (full or line range).",
        input_schema=_s({
            "path": {"type": "string", "description": "File path"},
            "offset": {"type": "integer", "description": "Start line (1-based)"},
            "limit": {"type": "integer", "description": "Max lines"},
        }, required=["path"]),
        output_schema=_out("File content"),
        permission_profile="filesystem", timeout=30,
        tags=["files", "core"]),

    ToolSchema(
        name="file_write", version="1.0.0",
        description="Write content to a file (atomic).",
        input_schema=_s({
            "path": {"type": "string", "description": "File path"},
            "content": {"type": "string", "description": "Content to write"},
        }, required=["path", "content"]),
        output_schema=_out("Write confirmation"),
        permission_profile="filesystem", timeout=30,
        tags=["files", "core"]),

    ToolSchema(
        name="browser_navigate", version="1.0.0",
        description="Navigate Playwright browser to a URL.",
        input_schema=_s({
            "url": {"type": "string", "description": "URL to visit"},
        }, required=["url"]),
        output_schema=_out("Navigation result"),
        permission_profile="network", timeout=60,
        tags=["browser", "core"]),

    ToolSchema(
        name="browser_extract", version="1.0.0",
        description="Extract title, text, and links from current page.",
        input_schema=_s({}),
        output_schema=_out("Extracted page data as JSON"),
        permission_profile="network", timeout=60,
        tags=["browser", "core"]),

    ToolSchema(
        name="browser_screenshot", version="1.0.0",
        description="Capture a screenshot of the current page.",
        input_schema=_s({
            "path": {"type": "string",
                     "description": "Where to save the PNG"},
        }),
        output_schema=_out("Screenshot path"),
        permission_profile="network", timeout=30,
        tags=["browser"]),

    ToolSchema(
        name="web_search", version="1.0.0",
        description="Public web search via search provider.",
        input_schema=_s({
            "query": {"type": "string", "description": "Search query"},
            "max_results": {"type": "integer",
                            "description": "Max results (default 10)"},
        }, required=["query"]),
        output_schema=_out("Search results as JSON"),
        permission_profile="network", timeout=60,
        tags=["research", "core"]),

    ToolSchema(
        name="germany_fetch", version="1.0.0",
        description="HTTP GET via Germany proxy (GERMANY_PROXY_URL) or direct.",
        input_schema=_s({
            "url": {"type": "string", "description": "URL to fetch"},
        }, required=["url"]),
        output_schema=_out("Response body"),
        permission_profile="network", timeout=60,
        tags=["network", "research"]),

    ToolSchema(
        name="supabase_query", version="1.0.0",
        description="Run SQL against Supabase Postgres (or SQLite fallback).",
        input_schema=_s({
            "sql": {"type": "string", "description": "SQL to execute"},
        }, required=["sql"]),
        output_schema=_out("Query results as JSON"),
        permission_profile="data", timeout=30,
        tags=["memory", "data"]),

    ToolSchema(
        name="self_modify", version="1.0.0",
        description="Propose a code patch (via Git branch + tests).",
        input_schema=_s({
            "file": {"type": "string", "description": "File to patch"},
            "old": {"type": "string", "description": "Old text"},
            "new": {"type": "string", "description": "New text"},
            "reason": {"type": "string",
                       "description": "Why this change"},
        }, required=["file", "new", "reason"]),
        output_schema=_out("Patch result"),
        permission_profile="self_modify", timeout=120,
        tags=["improvement"]),

    ToolSchema(
        name="tool_create", version="1.0.0",
        description="Create a new tool (tested in isolation first).",
        input_schema=_s({
            "name": {"type": "string", "description": "Tool name"},
            "code": {"type": "string",
                     "description": "Python implementation"},
            "description": {"type": "string", "description": "What it does"},
        }, required=["name", "code"]),
        output_schema=_out("Tool creation result"),
        permission_profile="tool_create", timeout=120,
        tags=["improvement", "meta"]),
]


class SchemaRegistry:
    """Registry of tool schemas with lookup and validation."""

    def __init__(self, schemas: Optional[List[ToolSchema]] = None):
        self._schemas: Dict[str, ToolSchema] = {}
        for s in schemas or BUILTIN_SCHEMAS:
            self._schemas[s.name] = s

    def get(self, name: str) -> Optional[ToolSchema]:
        return self._schemas.get(name)

    def register(self, schema: ToolSchema) -> None:
        self._schemas[schema.name] = schema

    def list(self, status: Optional[ToolStatus] = None,
             tag: Optional[str] = None) -> List[ToolSchema]:
        out = list(self._schemas.values())
        if status:
            out = [s for s in out if s.status == status]
        if tag:
            out = [s for s in out if tag in s.tags]
        return sorted(out, key=lambda s: s.name)

    def set_status(self, name: str, status: ToolStatus) -> bool:
        s = self._schemas.get(name)
        if not s:
            return False
        s.status = status
        return True

    def validate_call(self, name: str,
                      args: Dict[str, Any]) -> List[str]:
        s = self._schemas.get(name)
        if not s:
            return [f"unknown tool: {name}"]
        if s.status == ToolStatus.DISABLED:
            return [f"tool disabled: {name}"]
        return s.validate_args(args)

    def to_prompt_block(self) -> str:
        """Format all APPROVED schemas for the system prompt."""
        lines = []
        for s in self.list(status=ToolStatus.APPROVED):
            lines.append(f"- {s.name} (v{s.version}): {s.description}")
            props = s.input_schema.get("properties", {})
            req = s.input_schema.get("required", [])
            for pname, pspec in props.items():
                r = " [required]" if pname in req else ""
                lines.append(f"    {pname}: {pspec.get('type', '?')}{r}")
        return "\n".join(lines)

    def export_json(self) -> str:
        return json.dumps([s.to_dict() for s in self.list()],
                          indent=2)

    def checksums(self) -> Dict[str, str]:
        return {s.name: s.checksum for s in self.list()}
