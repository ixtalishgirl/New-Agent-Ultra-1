"""Output parser — extracts structured tool calls from model text."""
from __future__ import annotations
import json
import re
from dataclasses import dataclass


@dataclass
class ToolCall:
    name: str
    args: dict


@dataclass
class ParsedOutput:
    tool_calls: list[ToolCall]
    final_text: str


TOOL_CALL_RE = re.compile(
    r"<tool_call>(.*?)</tool_call>", re.DOTALL | re.IGNORECASE)
# Also accept ```json blocks containing {"name": ..., "args": {...}}
JSON_BLOCK_RE = re.compile(
    r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
FINAL_RE = re.compile(
    r"<final_answer>(.*?)</final_answer>", re.DOTALL | re.IGNORECASE)


def _try_add_tool_call(raw: str, tool_calls: list) -> None:
    """Parse one raw JSON string into a ToolCall if valid."""
    try:
        obj = json.loads(raw.strip())
    except json.JSONDecodeError:
        return
    name = obj.get("name")
    args = obj.get("args", {})
    if not name or not isinstance(args, dict):
        return
    tool_calls.append(ToolCall(name=str(name), args=args))


def parse_output(text: str) -> ParsedOutput:
    """Parse <tool_call>{"name": ..., "args": {...}}</tool_call> blocks.

    Validates: JSON well-formed, name present, args is a dict.
    Malformed calls are skipped (never executed).
    """
    tool_calls: list[ToolCall] = []
    for m in TOOL_CALL_RE.finditer(text):
        _try_add_tool_call(m.group(1), tool_calls)
    # Fallback: ```json blocks with {"name": ..., "args": {...}}
    if not tool_calls:
        for m in JSON_BLOCK_RE.finditer(text):
            _try_add_tool_call(m.group(1), tool_calls)
    final_m = FINAL_RE.search(text)
    final_text = final_m.group(1).strip() if final_m else ""
    if not final_text and not tool_calls:
        final_text = text.strip()
    return ParsedOutput(tool_calls=tool_calls, final_text=final_text)
