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
    """Parse one raw JSON string into a ToolCall if valid.
    Tolerates key variants the model may emit: name/tool/function and
    args/arguments/parameters.
    """
    try:
        obj = json.loads(raw.strip())
    except json.JSONDecodeError:
        return
    if not isinstance(obj, dict):
        return
    name = next((obj.get(k) for k in ("name", "tool", "function")
                 if obj.get(k)), None)
    args = next((obj.get(k) for k in ("args", "arguments", "parameters")
                 if isinstance(obj.get(k), dict)), {})
    if not name:
        return
    tool_calls.append(ToolCall(name=str(name), args=args))

def _try_add_bare_json_call(text: str, tool_calls: list) -> None:
    """Fallback: a bare {"name": ..., "args": {...}} object with no
    <tool_call> tags and no code fence (some reasoning models emit raw JSON).
    Only accepts objects with an explicit args-like dict key, so ordinary
    JSON in prose does not misfire. Stops at the first valid call.
    """
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text, m.start())
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        name = obj.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        if not any(isinstance(obj.get(k), dict)
                   for k in ("args", "arguments", "parameters")):
            continue
        _try_add_tool_call(json.dumps(obj), tool_calls)
        if tool_calls:
            return

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
    # Fallback: bare {"name": ..., "args": {...}} object (no tags/fence)
    if not tool_calls:
        _try_add_bare_json_call(text, tool_calls)
    final_m = FINAL_RE.search(text)
    final_text = final_m.group(1).strip() if final_m else ""
    if not final_text and not tool_calls:
        final_text = text.strip()
    return ParsedOutput(tool_calls=tool_calls, final_text=final_text)

def find_mentioned_tools(text: str, known_names: list[str]) -> list[str]:
    """Return known tool names mentioned in plain text (no valid call parsed).
    Used for the one-shot format repair: the model said e.g. TOOL_DISCOVER
    instead of emitting <tool_call>. Whole-word, case-insensitive match.
    Never raises.
    """
    found: list[str] = []
    try:
        for n in known_names or []:
            if not n or not isinstance(n, str):
                continue
            if re.search(r"(?<![A-Za-z0-9_])" + re.escape(n) +
                         r"(?![A-Za-z0-9_])", text, re.IGNORECASE):
                found.append(n)
    except Exception:
        return []
    return found
