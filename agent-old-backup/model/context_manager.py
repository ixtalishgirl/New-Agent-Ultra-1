"""Context manager — assembles model context within token budgets.

Per ChatGPT spec: agent/model/context_manager.py.
Manages conversation history, tool schemas, permissions, and truncation.
No personality. No rules.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ContextSection:
    """A labeled section of the model context."""
    name: str
    content: str
    priority: int = 5  # lower = more important (kept first)
    tokens: int = 0

    def __post_init__(self) -> None:
        if not self.tokens:
            self.tokens = estimate_tokens(self.content)


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return max(1, len(text) // 4)


class ContextManager:
    """Builds the full model context from sections, respecting a budget."""

    def __init__(self, max_tokens: int = 8000):
        self.max_tokens = max_tokens
        self._sections: Dict[str, ContextSection] = {}

    def set_section(self, name: str, content: str, priority: int = 5) -> None:
        self._sections[name] = ContextSection(
            name=name, content=content, priority=priority)

    def remove_section(self, name: str) -> bool:
        if name in self._sections:
            del self._sections[name]
            return True
        return False

    def get_section(self, name: str) -> Optional[ContextSection]:
        return self._sections.get(name)

    def total_tokens(self) -> int:
        return sum(s.tokens for s in self._sections.values())

    def build(self) -> str:
        """Assemble context, dropping low-priority sections over budget."""
        sections = sorted(self._sections.values(),
                          key=lambda s: (s.priority, s.name))
        budget = self.max_tokens
        kept: List[ContextSection] = []
        for sec in sections:
            if sec.tokens <= budget:
                kept.append(sec)
                budget -= sec.tokens
            else:
                # Try truncating this section to fit
                allowed_chars = budget * 4
                if allowed_chars > 200:
                    truncated = sec.content[:allowed_chars] + "\n[...truncated]"
                    kept.append(ContextSection(
                        name=sec.name, content=truncated,
                        priority=sec.priority,
                        tokens=estimate_tokens(truncated)))
                    budget = 0
                break
        parts = []
        for sec in kept:
            parts.append(f"=== {sec.name.upper()} ===\n{sec.content}")
        return "\n\n".join(parts)

    def build_messages(self, user_message: str) -> List[Dict[str, str]]:
        """Build OpenAI-style messages list."""
        system = self.build()
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user_message},
        ]

    def summary(self) -> Dict[str, Any]:
        return {
            "sections": len(self._sections),
            "total_tokens": self.total_tokens(),
            "max_tokens": self.max_tokens,
            "over_budget": self.total_tokens() > self.max_tokens,
            "by_section": {n: s.tokens for n, s in self._sections.items()},
        }


class ConversationHistory:
    """Manages the rolling conversation for the model."""

    def __init__(self, max_tokens: int = 6000):
        self.max_tokens = max_tokens
        self.messages: List[Dict[str, str]] = []

    def add(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content})
        self._enforce_budget()

    def add_tool_call(self, tool_name: str, args: Dict[str, Any]) -> None:
        self.add("assistant",
                 f"<tool_call>{json.dumps({'name': tool_name, 'args': args})}"
                 f"</tool_call>")

    def add_tool_result(self, output: str, ok: bool = True) -> None:
        status = "OK" if ok else "FAILED"
        self.add("user", f"<tool_result status=\"{status}\">\n"
                         f"{output[:3000]}\n</tool_result>")

    def _enforce_budget(self) -> None:
        # Keep system message (index 0) if present; drop oldest others
        total = sum(estimate_tokens(m["content"]) for m in self.messages)
        idx = 1 if self.messages and self.messages[0]["role"] == "system" else 0
        while total > self.max_tokens and len(self.messages) > idx + 1:
            removed = self.messages.pop(idx)
            total -= estimate_tokens(removed["content"])

    def to_list(self) -> List[Dict[str, str]]:
        return list(self.messages)

    def clear(self, keep_system: bool = True) -> None:
        if keep_system and self.messages and self.messages[0]["role"] == "system":
            self.messages = self.messages[:1]
        else:
            self.messages = []

    def last_n(self, n: int) -> List[Dict[str, str]]:
        return self.messages[-n:]


class ToolSchemaInjector:
    """Formats tool schemas for injection into the prompt."""

    @staticmethod
    def format_tool(tool: Dict[str, Any]) -> str:
        name = tool.get("name", "?")
        desc = tool.get("description", "")
        schema = tool.get("input_schema", {})
        props = schema.get("properties", {})
        required = schema.get("required", [])
        arg_lines = []
        for pname, pspec in props.items():
            ptype = pspec.get("type", "any")
            pdesc = pspec.get("description", "")
            req = " (required)" if pname in required else ""
            arg_lines.append(f"    - {pname}: {ptype}{req} — {pdesc}")
        args_block = "\n".join(arg_lines) if arg_lines else "    (no args)"
        return (f"- {name}: {desc}\n"
                f"  args:\n{args_block}\n"
                f"  timeout: {tool.get('timeout', 60)}s")

    @classmethod
    def format_all(cls, tools: List[Dict[str, Any]]) -> str:
        return "\n".join(cls.format_tool(t) for t in tools)

    @staticmethod
    def format_call_example() -> str:
        return (
            'To call a tool, emit EXACTLY:\n'
            '<tool_call>{"name": "tool_name", '
            '"args": {"arg": "value"}}</tool_call>\n'
            'You may emit multiple tool calls in one response.\n'
            'When done, emit:\n'
            '<final_answer>summary of what was accomplished</final_answer>\n'
            'NEVER fabricate tool output. If you did not call a tool, '
            'say what you would do, not what happened.'
        )
