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
        return out["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"unexpected model response: {str(out)[:300]}")


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
