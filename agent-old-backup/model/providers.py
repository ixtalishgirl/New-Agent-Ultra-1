"""Model providers — OpenAI-compatible HTTP client, retries, streaming.

Works with any OpenAI-compatible endpoint: vLLM, llama.cpp server,
text-generation-webui, Ollama (/v1), or a remote Qwen server.
Stdlib only (urllib + json). No API keys in code — read from env.
"""
from __future__ import annotations

import json
import math
import os
import random
import re
import time
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Core types
# ---------------------------------------------------------------------------

@dataclass
class ChatMessage:
    role: str
    content: str
    name: str = ""

    def to_dict(self) -> dict:
        d = {"role": self.role, "content": self.content}
        if self.name:
            d["name"] = self.name
        return d


@dataclass
class ModelResponse:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    finish_reason: str = ""
    latency_s: float = 0.0
    raw: dict | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class ProviderConfig:
    base_url: str = "http://localhost:8000/v1"
    api_key: str = ""
    model: str = "qwen2.5-coder-7b-instruct"
    timeout: int = 120
    max_retries: int = 3
    extra_headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, prefix: str = "HALEY_MODEL_") -> "ProviderConfig":
        """Read config from environment variables."""
        return cls(
            base_url=os.environ.get(prefix + "BASE_URL",
                                    "http://localhost:8000/v1"),
            api_key=os.environ.get(prefix + "API_KEY", ""),
            model=os.environ.get(prefix + "MODEL",
                                 "qwen2.5-coder-7b-instruct"),
            timeout=int(os.environ.get(prefix + "TIMEOUT", "120")),
            max_retries=int(os.environ.get(prefix + "MAX_RETRIES", "3")),
        )


class ModelProvider(ABC):
    """Provider interface — swap backends without rewriting the agent."""

    @abstractmethod
    def generate(self, messages: list[ChatMessage | dict],
                 max_tokens: int = 2048,
                 temperature: float = 0.7,
                 stop: list[str] | None = None) -> ModelResponse:
        ...

    @abstractmethod
    def health_check(self) -> bool:
        ...


# ---------------------------------------------------------------------------
# HTTP plumbing (urllib)
# ---------------------------------------------------------------------------

class ProviderError(Exception):
    """Non-retryable provider failure (bad request, auth, etc.)."""


class RetryableError(Exception):
    """Failure worth retrying (timeouts, 429, 5xx, connection drops)."""


def _post_json(url: str, payload: dict, headers: dict,
               timeout: int) -> tuple[int, dict | str]:
    """POST JSON; return (status, parsed_body_or_text)."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="ignore")
            try:
                return resp.status, json.loads(text)
            except (ValueError, json.JSONDecodeError):
                return resp.status, text
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        try:
            text = e.read().decode("utf-8", errors="ignore")
        except Exception:
            text = ""
        try:
            return e.code, json.loads(text) if text else {}
        except (ValueError, json.JSONDecodeError):
            return e.code, text
    except (urllib.error.URLError, TimeoutError,  # type: ignore[attr-defined]
            ConnectionError, OSError) as e:
        raise RetryableError(f"connection failed: {e}") from e


def _get_json(url: str, headers: dict, timeout: int) -> tuple[int, object]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="ignore")
            try:
                return resp.status, json.loads(text)
            except (ValueError, json.JSONDecodeError):
                return resp.status, text
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        return e.code, {}
    except (urllib.error.URLError, TimeoutError,  # type: ignore[attr-defined]
            ConnectionError, OSError) as e:
        raise RetryableError(f"connection failed: {e}") from e


def _classify_status(status: int) -> str:
    """'ok' | 'retry' | 'fatal'."""
    if 200 <= status < 300:
        return "ok"
    if status in (408, 409, 425, 429) or 500 <= status < 600:
        return "retry"
    return "fatal"


# ---------------------------------------------------------------------------
# OpenAI-compatible provider
# ---------------------------------------------------------------------------

class OpenAICompatibleProvider(ModelProvider):
    """Chat completions client for any OpenAI-compatible server.

    Endpoints used: POST {base}/chat/completions, GET {base}/models.
    """

    def __init__(self, config: ProviderConfig | None = None, **kwargs):
        self.config = config or ProviderConfig(**kwargs)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json",
                   "User-Agent": "haley-agent/1.0"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        headers.update(self.config.extra_headers)
        return headers

    def _chat_url(self) -> str:
        return self.config.base_url.rstrip("/") + "/chat/completions"

    def generate(self, messages: list[ChatMessage | dict],
                 max_tokens: int = 2048,
                 temperature: float = 0.7,
                 stop: list[str] | None = None) -> ModelResponse:
        payload: dict = {
            "model": self.config.model,
            "messages": [m.to_dict() if isinstance(m, ChatMessage) else m
                         for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if stop:
            payload["stop"] = stop
        started = time.time()
        status, body = _post_json(self._chat_url(), payload,
                                  self._headers(), self.config.timeout)
        kind = _classify_status(status)
        if kind == "retry":
            raise RetryableError(f"server returned {status}: {body!r}"[:300])
        if kind == "fatal":
            raise ProviderError(f"request failed ({status}): {body!r}"[:300])
        if not isinstance(body, dict):
            raise ProviderError(f"non-JSON response: {str(body)[:200]}")
        return self._parse_chat_response(body, time.time() - started)

    def _parse_chat_response(self, body: dict,
                             latency: float) -> ModelResponse:
        choices = body.get("choices", []) or []
        if not choices:
            raise ProviderError(f"no choices in response: {body!r}"[:300])
        choice = choices[0]
        message = choice.get("message", {}) or {}
        text = message.get("content", "") or ""
        usage = body.get("usage", {}) or {}
        return ModelResponse(
            text=text,
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            model=str(body.get("model", self.config.model)),
            finish_reason=str(choice.get("finish_reason", "")),
            latency_s=round(latency, 2),
            raw=body)

    def list_models(self) -> list[str]:
        """GET /models — what the server offers."""
        url = self.config.base_url.rstrip("/") + "/models"
        status, body = _get_json(url, self._headers(), self.config.timeout)
        if _classify_status(status) != "ok" or not isinstance(body, dict):
            return []
        return [m.get("id", "") for m in body.get("data", [])
                if isinstance(m, dict)]

    def health_check(self) -> bool:
        """True when the server answers /models (or chat) successfully."""
        try:
            models = self.list_models()
            if models:
                return True
            # Fall back: tiny generation probe.
            resp = self.generate(
                [ChatMessage(role="user", content="ping")],
                max_tokens=4, temperature=0.0)
            return isinstance(resp.text, str)
        except Exception:
            return False


class CompletionsProvider(OpenAICompatibleProvider):
    """Legacy /completions endpoint (for base models without chat)."""

    def generate(self, messages: list[ChatMessage | dict],
                 max_tokens: int = 2048,
                 temperature: float = 0.7,
                 stop: list[str] | None = None) -> ModelResponse:
        prompt = "\n".join(
            f"{m.role if isinstance(m, ChatMessage) else m.get('role')}: "
            f"{m.content if isinstance(m, ChatMessage) else m.get('content')}"
            for m in messages)
        payload = {"model": self.config.model, "prompt": prompt,
                   "max_tokens": max_tokens, "temperature": temperature,
                   "stream": False}
        if stop:
            payload["stop"] = stop
        url = self.config.base_url.rstrip("/") + "/completions"
        started = time.time()
        status, body = _post_json(url, payload, self._headers(),
                                  self.config.timeout)
        kind = _classify_status(status)
        if kind == "retry":
            raise RetryableError(f"server returned {status}")
        if kind == "fatal" or not isinstance(body, dict):
            raise ProviderError(f"request failed ({status})")
        choices = body.get("choices", []) or []
        text = choices[0].get("text", "") if choices else ""
        usage = body.get("usage", {}) or {}
        return ModelResponse(
            text=text,
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            model=str(body.get("model", self.config.model)),
            finish_reason=str(choices[0].get("finish_reason", ""))
            if choices else "",
            latency_s=round(time.time() - started, 2),
            raw=body)


# ---------------------------------------------------------------------------
# Retry wrapper — exponential backoff with jitter
# ---------------------------------------------------------------------------

class RetryWrapper(ModelProvider):
    """Wrap any provider with retry + backoff on RetryableError."""

    def __init__(self, inner: ModelProvider, max_retries: int = 3,
                 base_delay: float = 1.0, max_delay: float = 30.0,
                 jitter: float = 0.25):
        self.inner = inner
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.jitter = jitter
        self.attempts_made = 0
        self.last_error: Exception | None = None

    def _delay(self, attempt: int) -> float:
        delay = min(self.max_delay, self.base_delay * (2 ** attempt))
        jitter_amount = delay * self.jitter
        return delay + random.uniform(-jitter_amount, jitter_amount)

    def generate(self, messages: list[ChatMessage | dict],
                 max_tokens: int = 2048,
                 temperature: float = 0.7,
                 stop: list[str] | None = None) -> ModelResponse:
        self.attempts_made = 0
        self.last_error = None
        for attempt in range(self.max_retries + 1):
            self.attempts_made += 1
            try:
                return self.inner.generate(messages, max_tokens,
                                           temperature, stop)
            except RetryableError as e:
                self.last_error = e
                if attempt >= self.max_retries:
                    raise
                delay = max(0.0, self._delay(attempt))
                time.sleep(delay)
            except ProviderError:
                raise
        raise RetryableError("retries exhausted")  # pragma: no cover

    def health_check(self) -> bool:
        try:
            return self.inner.health_check()
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Streaming — SSE parsing for chat/completions streams
# ---------------------------------------------------------------------------

def parse_sse_stream(text: str) -> list[dict]:
    """Parse Server-Sent Events text into a list of event dicts.

    Handles 'data:' lines (JSON or [DONE]), ignores comments/retries,
    joins multi-line data fields.
    """
    events: list[dict] = []
    data_lines: list[str] = []
    event_type = "message"

    def flush() -> None:
        nonlocal data_lines, event_type
        if not data_lines:
            return
        data = "\n".join(data_lines)
        data_lines = []
        if data.strip() == "[DONE]":
            events.append({"type": event_type, "done": True})
        else:
            try:
                payload = json.loads(data)
            except (ValueError, json.JSONDecodeError):
                payload = {"raw": data}
            events.append({"type": event_type, "data": payload})
        event_type = "message"

    for line in (text or "").splitlines():
        if not line.strip():
            flush()
            continue
        if line.startswith(":"):
            continue  # comment / heartbeat
        if line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif line.startswith("retry:"):
            continue
    flush()
    return events


def extract_streamed_text(events: list[dict]) -> str:
    """Concatenate delta.content / delta.text from chat SSE events."""
    parts = []
    for ev in events:
        data = ev.get("data")
        if not isinstance(data, dict):
            continue
        for choice in data.get("choices", []) or []:
            delta = choice.get("delta", {}) or {}
            chunk = delta.get("content") or delta.get("text") or ""
            if chunk:
                parts.append(chunk)
    return "".join(parts)


class StreamingHandler:
    """Incremental SSE reader for streaming chat completions."""

    def __init__(self, provider: OpenAICompatibleProvider):
        self.provider = provider

    def stream(self, messages: list[ChatMessage | dict],
               max_tokens: int = 2048,
               temperature: float = 0.7,
               stop: list[str] | None = None,
               on_token=None) -> ModelResponse:
        """Stream tokens; on_token (optional) receives each text chunk.

        Returns a final ModelResponse with the full text assembled.
        """
        payload: dict = {
            "model": self.provider.config.model,
            "messages": [m.to_dict() if isinstance(m, ChatMessage) else m
                         for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        if stop:
            payload["stop"] = stop
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.provider._chat_url(), data=body,
            headers=self.provider._headers(), method="POST")
        started = time.time()
        chunks: list[str] = []
        try:
            with urllib.request.urlopen(
                    req, timeout=self.provider.config.timeout) as resp:
                buffer = ""
                while True:
                    raw = resp.read(4096)
                    if not raw:
                        break
                    buffer += raw.decode("utf-8", errors="ignore")
                    # Process complete SSE events (blank-line separated).
                    while "\n\n" in buffer:
                        event_text, buffer = buffer.split("\n\n", 1)
                        for ev in parse_sse_stream(event_text + "\n\n"):
                            if ev.get("done"):
                                continue
                            text = extract_streamed_text([ev])
                            if text:
                                chunks.append(text)
                                if on_token:
                                    on_token(text)
                # Trailing partial event.
                for ev in parse_sse_stream(buffer):
                    if not ev.get("done"):
                        text = extract_streamed_text([ev])
                        if text:
                            chunks.append(text)
                            if on_token:
                                on_token(text)
        except (urllib.error.URLError, TimeoutError,  # type: ignore[attr-defined]
                ConnectionError, OSError) as e:
            raise RetryableError(f"stream failed: {e}") from e
        full = "".join(chunks)
        return ModelResponse(
            text=full,
            completion_tokens=TokenCounter.count(full),
            model=self.provider.config.model,
            finish_reason="stop" if full else "",
            latency_s=round(time.time() - started, 2))


# ---------------------------------------------------------------------------
# Token counter (approximation, no tokenizer dependency)
# ---------------------------------------------------------------------------

class TokenCounter:
    """Approximate token counting for budgeting (char/4 heuristic)."""

    # Extra weight for code-ish content.
    _CODE_RE = re.compile(
        r"```|def |class |import |\{|\}|\(\)|;|//|https?://")

    @classmethod
    def count(cls, text: str) -> int:
        if not text:
            return 0
        base = len(text) / 4.0
        code_hits = len(cls._CODE_RE.findall(text))
        return int(base + code_hits * 2)

    @classmethod
    def count_messages(cls, messages: list[ChatMessage | dict]) -> int:
        total = 0
        for m in messages:
            content = m.content if isinstance(m, ChatMessage) else m.get(
                "content", "")
            total += cls.count(content) + 4  # per-message overhead
        return total

    @classmethod
    def truncate(cls, text: str, max_tokens: int) -> str:
        if cls.count(text) <= max_tokens:
            return text
        return text[: max_tokens * 4 - 20].rstrip() + "…[truncated]"

    @classmethod
    def fits(cls, text: str, limit: int) -> bool:
        return cls.count(text) <= limit


# ---------------------------------------------------------------------------
# Usage tracking
# ---------------------------------------------------------------------------

@dataclass
class UsageRecord:
    timestamp: float
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_s: float
    ok: bool = True


class UsageTracker:
    """In-memory log of generations for cost/latency monitoring."""

    def __init__(self, price_per_1k: dict[str, tuple[float, float]] | None = None):
        # model -> (input $/1k, output $/1k)
        self.price_per_1k = price_per_1k or {}
        self.records: list[UsageRecord] = []

    def record(self, response: ModelResponse, ok: bool = True) -> None:
        self.records.append(UsageRecord(
            timestamp=time.time(), model=response.model,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            latency_s=response.latency_s, ok=ok))

    def totals(self) -> dict:
        pt = sum(r.prompt_tokens for r in self.records)
        ct = sum(r.completion_tokens for r in self.records)
        lat = [r.latency_s for r in self.records if r.ok]
        cost = 0.0
        for r in self.records:
            pin, pout = self.price_per_1k.get(r.model, (0.0, 0.0))
            cost += r.prompt_tokens / 1000 * pin + \
                r.completion_tokens / 1000 * pout
        return {
            "calls": len(self.records),
            "ok": sum(1 for r in self.records if r.ok),
            "prompt_tokens": pt, "completion_tokens": ct,
            "total_tokens": pt + ct,
            "avg_latency_s": round(sum(lat) / len(lat), 2) if lat else 0.0,
            "est_cost_usd": round(cost, 4),
        }

    def reset(self) -> None:
        self.records.clear()


class TrackedProvider(ModelProvider):
    """Wrap a provider and log every generation to a UsageTracker."""

    def __init__(self, inner: ModelProvider,
                 tracker: UsageTracker | None = None):
        self.inner = inner
        self.tracker = tracker or UsageTracker()

    def generate(self, messages: list[ChatMessage | dict],
                 max_tokens: int = 2048,
                 temperature: float = 0.7,
                 stop: list[str] | None = None) -> ModelResponse:
        try:
            resp = self.inner.generate(messages, max_tokens,
                                       temperature, stop)
            self.tracker.record(resp, ok=True)
            return resp
        except Exception as e:
            self.tracker.record(
                ModelResponse(text="", model=getattr(
                    getattr(self.inner, "config", None), "model", "")),
                ok=False)
            raise

    def health_check(self) -> bool:
        return self.inner.health_check()


# ---------------------------------------------------------------------------
# Fallback chain — try providers in order
# ---------------------------------------------------------------------------

class FallbackProvider(ModelProvider):
    """Try each provider in order until one succeeds."""

    def __init__(self, providers: list[ModelProvider]):
        if not providers:
            raise ValueError("need at least one provider")
        self.providers = providers
        self.last_used = 0

    def generate(self, messages: list[ChatMessage | dict],
                 max_tokens: int = 2048,
                 temperature: float = 0.7,
                 stop: list[str] | None = None) -> ModelResponse:
        errors = []
        for i, provider in enumerate(self.providers):
            try:
                resp = provider.generate(messages, max_tokens,
                                         temperature, stop)
                self.last_used = i
                return resp
            except Exception as e:  # noqa: BLE001 - try next
                errors.append(f"{type(provider).__name__}: {e}")
        raise ProviderError("all providers failed: " + " | ".join(errors))

    def health_check(self) -> bool:
        return any(p.health_check() for p in self.providers)


# ---------------------------------------------------------------------------
# Mock provider (tests / offline development)
# ---------------------------------------------------------------------------

class MockProvider(ModelProvider):
    """Deterministic fake provider for tests."""

    def __init__(self, responses: list[str] | None = None,
                 fail_times: int = 0):
        self.responses = list(responses or ["mock response"])
        self.fail_times = fail_times
        self.calls = 0

    def generate(self, messages: list[ChatMessage | dict],
                 max_tokens: int = 2048,
                 temperature: float = 0.7,
                 stop: list[str] | None = None) -> ModelResponse:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RetryableError("mock transient failure")
        text = self.responses[(self.calls - 1) % len(self.responses)]
        return ModelResponse(text=text, prompt_tokens=10,
                             completion_tokens=TokenCounter.count(text),
                             model="mock", finish_reason="stop")

    def health_check(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Endpoint probing — find a live OpenAI-compatible server
# ---------------------------------------------------------------------------

def probe_endpoint(base_url: str, timeout: int = 5) -> dict:
    """Check whether base_url serves an OpenAI-compatible API.

    Returns {"ok": bool, "models": [...], "error": str}.
    """
    cfg = ProviderConfig(base_url=base_url, timeout=timeout)
    provider = OpenAICompatibleProvider(cfg)
    try:
        models = provider.list_models()
        return {"ok": True, "models": models, "error": ""}
    except RetryableError as e:
        return {"ok": False, "models": [], "error": f"unreachable: {e}"}
    except ProviderError as e:
        return {"ok": False, "models": [], "error": str(e)}


def autodetect_endpoint(candidates: list[str] | None = None,
                        timeout: int = 5) -> str | None:
    """Return the first candidate base_url that answers, else None."""
    candidates = candidates or [
        "http://localhost:8000/v1",
        "http://localhost:8080/v1",
        "http://localhost:11434/v1",
        "http://localhost:1234/v1",
    ]
    for url in candidates:
        if probe_endpoint(url, timeout)["ok"]:
            return url
    return None


# ---------------------------------------------------------------------------
# Request logging / redaction helpers
# ---------------------------------------------------------------------------

_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|authorization)")


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Mask sensitive headers for logging."""
    out = {}
    for k, v in headers.items():
        if _SENSITIVE_KEY_RE.search(k):
            out[k] = "***redacted***"
        else:
            out[k] = v
    return out


def summarize_request(payload: dict) -> dict:
    """Log-safe summary of a chat payload (no message contents)."""
    messages = payload.get("messages", []) or []
    return {
        "model": payload.get("model"),
        "num_messages": len(messages),
        "roles": [m.get("role") for m in messages
                  if isinstance(m, dict)],
        "prompt_chars": sum(len(str(m.get("content", "")))
                            for m in messages if isinstance(m, dict)),
        "max_tokens": payload.get("max_tokens"),
        "temperature": payload.get("temperature"),
        "stream": payload.get("stream", False),
    }


# ---------------------------------------------------------------------------
# Qwen server provider — ahsan's GPU model endpoint convenience
# ---------------------------------------------------------------------------

class QwenServerProvider(OpenAICompatibleProvider):
    """Pre-configured provider for ahsan's Qwen serving endpoint.

    Reads HALEY_MODEL_BASE_URL (the ngrok URL after the GPU serve step),
    HALEY_MODEL_API_KEY, HALEY_MODEL_NAME from the environment.
    """

    def __init__(self, base_url: str = "", api_key: str = "",
                 model: str = "", timeout: int = 180):
        cfg = ProviderConfig.from_env(prefix="HALEY_MODEL_")
        if base_url:
            cfg.base_url = base_url
        if api_key:
            cfg.api_key = api_key
        if model:
            cfg.model = model
        cfg.timeout = timeout or cfg.timeout
        super().__init__(cfg)

    def wait_until_ready(self, timeout_s: float = 300.0,
                         poll_s: float = 5.0) -> bool:
        """Poll health_check until the server answers or timeout."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.health_check():
                return True
            time.sleep(poll_s)
        return False

    def generate_text(self, system: str, user: str,
                      max_tokens: int = 2048,
                      temperature: float = 0.7) -> str:
        """Convenience: system+user strings -> response text."""
        resp = self.generate(
            [ChatMessage(role="system", content=system),
             ChatMessage(role="user", content=user)],
            max_tokens=max_tokens, temperature=temperature)
        return resp.text


# ---------------------------------------------------------------------------
# Provider pool — round-robin across equivalent endpoints
# ---------------------------------------------------------------------------

class ProviderPool(ModelProvider):
    """Round-robin load balancing across equivalent providers."""

    def __init__(self, providers: list[ModelProvider]):
        if not providers:
            raise ValueError("need at least one provider")
        self.providers = providers
        self._cursor = 0
        self.errors: dict[int, int] = {}

    def _next(self) -> tuple[int, ModelProvider]:
        idx = self._cursor % len(self.providers)
        self._cursor += 1
        return idx, self.providers[idx]

    def generate(self, messages: list[ChatMessage | dict],
                 max_tokens: int = 2048,
                 temperature: float = 0.7,
                 stop: list[str] | None = None) -> ModelResponse:
        tried = 0
        while tried < len(self.providers):
            idx, provider = self._next()
            tried += 1
            try:
                return provider.generate(messages, max_tokens,
                                         temperature, stop)
            except Exception:  # noqa: BLE001 - try next in pool
                self.errors[idx] = self.errors.get(idx, 0) + 1
        raise ProviderError("all pooled providers failed")

    def health_check(self) -> bool:
        return any(p.health_check() for p in self.providers)

    def error_counts(self) -> dict[int, int]:
        return dict(self.errors)


# ---------------------------------------------------------------------------
# Caching provider — exact-prompt memoization
# ---------------------------------------------------------------------------

class CachingProvider(ModelProvider):
    """Cache generate() results keyed by the exact request.

    Useful for deterministic phases (plan/verify prompts) and tests.
    TTL-bounded, size-bounded, in-memory.
    """

    def __init__(self, inner: ModelProvider, ttl_s: float = 3600.0,
                 max_entries: int = 500):
        self.inner = inner
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._cache: dict[str, tuple[float, ModelResponse]] = {}
        self.hits = 0
        self.misses = 0

    def _key(self, messages: list[ChatMessage | dict],
             max_tokens: int, temperature: float,
             stop: list[str] | None) -> str:
        import hashlib
        parts = []
        for m in messages:
            if isinstance(m, ChatMessage):
                parts.append(f"{m.role}:{m.content}")
            else:
                parts.append(f"{m.get('role')}:{m.get('content')}")
        blob = "|".join(parts) + f"#{max_tokens}#{temperature}#{stop}"
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def generate(self, messages: list[ChatMessage | dict],
                 max_tokens: int = 2048,
                 temperature: float = 0.7,
                 stop: list[str] | None = None) -> ModelResponse:
        key = self._key(messages, max_tokens, temperature, stop)
        entry = self._cache.get(key)
        now = time.time()
        if entry and now - entry[0] <= self.ttl_s:
            self.hits += 1
            return entry[1]
        self.misses += 1
        resp = self.inner.generate(messages, max_tokens, temperature,
                                   stop)
        if len(self._cache) >= self.max_entries:
            oldest = min(self._cache, key=lambda k: self._cache[k][0])
            del self._cache[oldest]
        self._cache[key] = (now, resp)
        return resp

    def health_check(self) -> bool:
        return self.inner.health_check()

    def clear(self) -> None:
        self._cache.clear()
        self.hits = self.misses = 0

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {"hits": self.hits, "misses": self.misses,
                "hit_rate": round(self.hits / total, 3) if total else 0.0,
                "entries": len(self._cache)}


# ---------------------------------------------------------------------------
# Convenience: build the standard provider stack from env
# ---------------------------------------------------------------------------

def build_default_stack() -> ModelProvider:
    """Standard stack: cache -> retry -> tracked -> OpenAI-compatible.

    Configure via HALEY_MODEL_* env vars (see ProviderConfig.from_env).
    """
    base = OpenAICompatibleProvider(ProviderConfig.from_env())
    tracked = TrackedProvider(base)
    retried = RetryWrapper(tracked,
                           max_retries=base.config.max_retries)
    cached = CachingProvider(retried)
    return cached


def generate_simple(provider: ModelProvider, prompt: str,
                    system: str = "", max_tokens: int = 1024,
                    temperature: float = 0.7) -> str:
    """One-shot text generation from a plain string prompt."""
    messages: list[ChatMessage] = []
    if system:
        messages.append(ChatMessage(role="system", content=system))
    messages.append(ChatMessage(role="user", content=prompt))
    return provider.generate(messages, max_tokens=max_tokens,
                             temperature=temperature).text


# ---------------------------------------------------------------------------
# Rate limiting — token-bucket wrapper
# ---------------------------------------------------------------------------

class RateLimitedProvider(ModelProvider):
    """Token-bucket rate limiter in front of any provider.

    Allows `calls_per_minute` generations; excess calls block until a
    token is available (with an optional max wait).
    """

    def __init__(self, inner: ModelProvider, calls_per_minute: float = 30.0,
                 burst: int = 5, max_wait_s: float = 120.0):
        self.inner = inner
        self.rate = calls_per_minute / 60.0  # tokens per second
        self.capacity = float(max(1, burst))
        self.tokens = float(max(1, burst))
        self.max_wait_s = max_wait_s
        self._last = time.time()
        self.waited_s = 0.0

    def _take_token(self) -> None:
        now = time.time()
        elapsed = now - self._last
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self._last = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return
        needed = (1.0 - self.tokens) / self.rate if self.rate > 0 else 0
        if needed > self.max_wait_s:
            raise ProviderError(
                f"rate limit wait {needed:.1f}s exceeds max_wait_s")
        time.sleep(needed)
        self.waited_s += needed
        self.tokens = 0.0
        self._last = time.time()

    def generate(self, messages: list[ChatMessage | dict],
                 max_tokens: int = 2048,
                 temperature: float = 0.7,
                 stop: list[str] | None = None) -> ModelResponse:
        self._take_token()
        return self.inner.generate(messages, max_tokens, temperature,
                                   stop)

    def health_check(self) -> bool:
        return self.inner.health_check()


# ---------------------------------------------------------------------------
# Response guard — validate / clean model output before use
# ---------------------------------------------------------------------------

class ResponseGuard:
    """Post-generation checks: non-empty, no prompt echo, length caps."""

    def __init__(self, min_chars: int = 1, max_chars: int = 20000,
                 strip_echo: bool = True):
        self.min_chars = min_chars
        self.max_chars = max_chars
        self.strip_echo = strip_echo

    def check(self, response: ModelResponse,
              prompt_tail: str = "") -> list[str]:
        """Return a list of problems (empty = clean)."""
        problems = []
        text = response.text or ""
        if len(text.strip()) < self.min_chars:
            problems.append("response is empty")
        if len(text) > self.max_chars:
            problems.append(
                f"response too long ({len(text)} > {self.max_chars})")
        if (self.strip_echo and prompt_tail
                and text.strip().startswith(prompt_tail.strip()[-200:])):
            problems.append("response echoes the prompt")
        if response.finish_reason == "length":
            problems.append("response was cut off (finish_reason=length)")
        return problems

    def clean(self, response: ModelResponse,
              prompt_tail: str = "") -> ModelResponse:
        """Return a cleaned copy of the response."""
        text = response.text or ""
        if self.strip_echo and prompt_tail:
            tail = prompt_tail.strip()[-200:]
            if text.strip().startswith(tail):
                text = text.strip()[len(tail):].lstrip()
        if len(text) > self.max_chars:
            text = text[:self.max_chars] + "…[truncated]"
        response.text = text.strip()
        return response


class GuardedProvider(ModelProvider):
    """Wrap a provider; raise ProviderError when the guard fails."""

    def __init__(self, inner: ModelProvider,
                 guard: ResponseGuard | None = None):
        self.inner = inner
        self.guard = guard or ResponseGuard()

    def generate(self, messages: list[ChatMessage | dict],
                 max_tokens: int = 2048,
                 temperature: float = 0.7,
                 stop: list[str] | None = None) -> ModelResponse:
        resp = self.inner.generate(messages, max_tokens, temperature,
                                   stop)
        tail = ""
        if messages:
            last = messages[-1]
            tail = last.content if isinstance(last, ChatMessage) \
                else str(last.get("content", ""))
        problems = self.guard.check(resp, tail)
        if problems:
            raise ProviderError("guard rejected response: "
                                + "; ".join(problems))
        return self.guard.clean(resp, tail)

    def health_check(self) -> bool:
        return self.inner.health_check()
