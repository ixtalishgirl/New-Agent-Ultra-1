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
FINAL_RE = re.compile(
    r"<final_answer>(.*?)</final_answer>", re.DOTALL | re.IGNORECASE)


def parse_output(text: str) -> ParsedOutput:
    """Parse <tool_call>{"name": ..., "args": {...}}</tool_call> blocks.

    Validates: JSON well-formed, name present, args is a dict.
    Malformed calls are skipped (never executed).
    """
    tool_calls: list[ToolCall] = []
    for m in TOOL_CALL_RE.finditer(text):
        raw = m.group(1).strip()
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        name = obj.get("name")
        args = obj.get("args", {})
        if not name or not isinstance(args, dict):
            continue
        tool_calls.append(ToolCall(name=str(name), args=args))
    final_m = FINAL_RE.search(text)
    final_text = final_m.group(1).strip() if final_m else ""
    if not final_text and not tool_calls:
        final_text = text.strip()
    return ParsedOutput(tool_calls=tool_calls, final_text=final_text)
