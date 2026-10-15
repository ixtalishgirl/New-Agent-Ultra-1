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
    # ---- Phase-2 tool modules: git / file / network / data / system ----
    new_tools = [
        ("git_status", "Git working-tree status (parsed)",
         {"path": {"type": "string", "required": False,
                   "description": "Repo path (default: cwd)"}},
         "workspace", 30),
        ("git_log", "Git log with parsed commits",
         {"path": {"type": "string", "required": False},
          "max_count": {"type": "integer", "required": False, "default": 20},
          "branch": {"type": "string", "required": False},
          "since": {"type": "string", "required": False,
                    "description": "e.g. '2 weeks ago'"},
          "author": {"type": "string", "required": False},
          "grep": {"type": "string", "required": False},
          "pathspec": {"type": "string", "required": False}},
         "workspace", 30),
        ("git_diff", "Git diff with per-file stats",
         {"path": {"type": "string", "required": False},
          "staged": {"type": "boolean", "required": False},
          "commit_a": {"type": "string", "required": False},
          "commit_b": {"type": "string", "required": False},
          "pathspec": {"type": "string", "required": False}},
         "workspace", 30),
        ("git_branch", "List/create/delete/checkout git branches",
         {"action": {"type": "string", "required": False,
                     "description": "list|create|delete|checkout (default list)"},
          "name": {"type": "string", "required": False,
                   "description": "Branch name for create/delete/checkout"},
          "ref": {"type": "string", "required": False,
                  "description": "Alias for name on checkout"},
          "start_point": {"type": "string", "required": False, "default": "HEAD"},
          "checkout_after": {"type": "boolean", "required": False},
          "force": {"type": "boolean", "required": False},
          "create": {"type": "boolean", "required": False,
                     "description": "checkout -b for checkout action"},
          "include_remote": {"type": "boolean", "required": False},
          "path": {"type": "string", "required": False}},
         "workspace", 30),
        ("git_commit", "Stage (optional) and create a git commit",
         {"message": {"type": "string", "required": True},
          "add": {"type": "array", "required": False,
                  "description": "Pathspecs to stage before committing"},
          "amend": {"type": "boolean", "required": False},
          "allow_empty": {"type": "boolean", "required": False},
          "author": {"type": "string", "required": False},
          "path": {"type": "string", "required": False}},
         "workspace", 30),
        ("file_tree", "Recursive directory tree with filters",
         {"path": {"type": "string", "required": False, "default": "."},
          "max_depth": {"type": "integer", "required": False, "default": 5},
          "include_hidden": {"type": "boolean", "required": False},
          "extensions": {"type": "array", "required": False,
                         "description": "e.g. ['.py', '.md']"}},
         "workspace", 60),
        ("file_find", "Find files by glob, optionally by content regex",
         {"root": {"type": "string", "required": False, "default": "."},
          "name_glob": {"type": "string", "required": False, "default": "*"},
          "content_regex": {"type": "string", "required": False},
          "max_results": {"type": "integer", "required": False, "default": 200},
          "extensions": {"type": "array", "required": False}},
         "workspace", 60),
        ("file_checksum", "Hash a file (sha256/sha1/md5/blake2b)",
         {"path": {"type": "string", "required": True},
          "algorithm": {"type": "string", "required": False, "default": "sha256"}},
         "workspace", 60),
        ("http_get", "HTTP GET with headers/timeout/redirect handling",
         {"url": {"type": "string", "required": True},
          "headers": {"type": "object", "required": False},
          "params": {"type": "object", "required": False},
          "timeout": {"type": "integer", "required": False, "default": 20}},
         "network", 30),
        ("http_post", "HTTP POST with JSON or form body",
         {"url": {"type": "string", "required": True},
          "json_data": {"type": "object", "required": False},
          "data": {"type": "object", "required": False,
                   "description": "Form-encoded fields"},
          "headers": {"type": "object", "required": False},
          "timeout": {"type": "integer", "required": False, "default": 20}},
         "network", 30),
        ("dns_lookup", "Resolve hostname to A/AAAA addresses",
         {"hostname": {"type": "string", "required": True}},
         "network", 20),
        ("port_check", "TCP port reachability check",
         {"host": {"type": "string", "required": True},
          "ports": {"type": "array", "required": True,
                    "description": "List of integer ports"}},
         "network", 60),
        ("json_validate", "Parse JSON and validate against a schema",
         {"text": {"type": "string", "required": True},
          "schema": {"type": "object", "required": False,
                     "description": "Schema subset: type/properties/required/items/enum/minimum/maximum/minLength/maxLength/pattern"}},
         "workspace", 30),
        ("csv_stats", "Column stats for CSV (from text or path)",
         {"text": {"type": "string", "required": False},
          "path": {"type": "string", "required": False},
          "columns": {"type": "array", "required": False}},
         "workspace", 60),
        ("sysinfo", "Host snapshot: OS, Python, CPU, memory",
         {},
         "workspace", 30),
        ("disk_usage", "Filesystem usage for a path",
         {"path": {"type": "string", "required": False, "default": "/"}},
         "workspace", 30),
        # ---- Media tools ----
        ("image_info", "Get image metadata (dimensions, format, size)",
         {"path": {"type": "string", "required": True}},
         "workspace", 20),
        ("image_resize", "Resize an image to specified dimensions",
         {"path": {"type": "string", "required": True},
          "width": {"type": "integer", "required": True},
          "height": {"type": "integer", "required": True},
          "output": {"type": "string", "required": False}},
         "workspace", 30),
        ("image_convert", "Convert image to different format",
         {"path": {"type": "string", "required": True},
          "format": {"type": "string", "required": True},
          "output": {"type": "string", "required": False}},
         "workspace", 30),
        ("image_thumbnail", "Create a thumbnail of an image",
         {"path": {"type": "string", "required": True},
          "size": {"type": "integer", "required": False, "default": 128},
          "output": {"type": "string", "required": False}},
         "workspace", 30),
        ("audio_info", "Get audio file metadata",
         {"path": {"type": "string", "required": True}},
         "workspace", 20),
        ("video_info", "Get video file metadata",
         {"path": {"type": "string", "required": True}},
         "workspace", 20),
    ]
    for name, desc, schema, profile, timeout in new_tools:
        reg.register(ToolDef(name=name, version="1.0.0", description=desc,
                             input_schema=schema,
                             permission_profile=profile, timeout=timeout))
    return reg
