"""Model client — talks to the self-hosted model via HALEY_API_URL.

Thin wrapper over llama.cpp's OpenAI-compatible /v1/chat/completions.
Timeout 300s, max_tokens 512 (fast responses).
"""
from __future__ import annotations

import json
import os
import urllib.request
import urllib.error

BASE_URL = os.environ.get("HALEY_API_URL", "").rstrip("/")
TIMEOUT = 300
MAX_TOKENS = 512
CHAT_PATH = "/v1/chat/completions"


def is_configured() -> bool:
    return bool(BASE_URL)


def chat(messages: list, max_tokens: int = MAX_TOKENS,
         timeout: int = TIMEOUT) -> str:
    """Send chat messages, return the assistant's raw text response.

    Raises RuntimeError on failure — callers decide how to surface it.
    """
    if not BASE_URL:
        raise RuntimeError("HALEY_API_URL is not set — model unreachable.")
    payload = {
        "model": "agent",
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BASE_URL + CHAT_PATH, data=data,
        headers={"Content-Type": "application/json",
                 "ngrok-skip-browser-warning": "true"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"model HTTP {e.code}: {body}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"model unreachable at {BASE_URL}: {e}")
    try:
        msg = out["choices"][0]["message"]
        # DeepSeek-R1 distills put their reply in reasoning_content and may
        # leave content empty — fall back so chat never returns blank.
        return msg.get("content") or msg.get("reasoning_content") or ""
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"unexpected model response: {str(out)[:300]}")


def chat_stream(messages: list, max_tokens: int = MAX_TOKENS,
               timeout: int = TIMEOUT):
    """Yield content deltas as the model generates them (SSE).

    Raises RuntimeError on failure — callers decide how to surface it.
    First token typically arrives in ~1-2s; the caller streams it straight
    to the user instead of waiting for the full reply.
    """
    if not BASE_URL:
        raise RuntimeError("HALEY_API_URL is not set — model unreachable.")
    payload = {
        "model": "agent",
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "stream": True,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BASE_URL + CHAT_PATH, data=data,
        headers={"Content-Type": "application/json",
                 "Accept": "text/event-stream",
                 "ngrok-skip-browser-warning": "true"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"model HTTP {e.code}: {body}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"model unreachable at {BASE_URL}: {e}")
    try:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data_s = line[5:].strip()
            if data_s == "[DONE]":
                break
            try:
                delta = json.loads(data_s)["choices"][0]["delta"]
                # DeepSeek-R1 distills stream their reply as
                # reasoning_content; fall back so streaming never goes blank.
                tok = delta.get("content") or delta.get("reasoning_content")
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            if tok:
                yield tok
    finally:
        resp.close()


def health() -> bool:
    """Quick check that the model endpoint answers."""
    if not BASE_URL:
        return False
    try:
        req = urllib.request.Request(
            BASE_URL + "/health",
            headers={"ngrok-skip-browser-warning": "true"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False
