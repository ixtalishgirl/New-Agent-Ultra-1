"""OpenAI-compatible vision adapter.

Talks to ANY OpenAI-compatible chat-completions endpoint that accepts
images (vLLM, Ollama, LocalAI, llama.cpp server, text-generation-webui
with the OpenAI extension, a remote OpenAI-compatible VLM, etc.).

Configuration (env vars):
    VISION_ADAPTER=openai_compat   # selects this adapter
    VISION_MODEL_URL=...           # e.g. http://localhost:8000/v1/chat/completions
                                   #        http://localhost:11434/v1/chat/completions
    VISION_MODEL_NAME=...          # model id the server exposes, e.g. qwen2.5vl:7b
    VISION_MODEL_KEY=...           # optional; sent as Bearer token (many local
                                   # servers accept anything or nothing)
    VISION_TIMEOUT=60              # seconds for the HTTP call (default 60)
    VISION_MAX_TOKENS=512          # cap on the description length (default 512)

The image is sent as a base64 data-URL in the standard OpenAI
`image_url` content-part format.

Security note: the response is UNTRUSTED PAGE CONTENT. This adapter
returns it verbatim; the pipeline tags it and never treats it as
instructions. The prompt is passed through unchanged — the pipeline's
prompt already carries the describe-only / injection-guard wording.
"""

from __future__ import annotations

import base64
import os

import requests

from adapters.base_adapter import AdapterError, BaseVisionAdapter, register_adapter


class OpenAICompatVisionAdapter(BaseVisionAdapter):
    """Vision via any OpenAI-compatible /v1/chat/completions endpoint."""

    name = "openai_compat"

    def __init__(
        self,
        model_url: str | None = None,
        model_name: str | None = None,
        api_key: str | None = None,
        timeout: int | None = None,
        max_tokens: int | None = None,
    ):
        self.model_url = (model_url or os.environ.get("VISION_MODEL_URL", "")).rstrip("/")
        self.model_name = model_name or os.environ.get("VISION_MODEL_NAME", "")
        self.api_key = api_key if api_key is not None else os.environ.get("VISION_MODEL_KEY", "")
        self.timeout = int(timeout if timeout is not None else os.environ.get("VISION_TIMEOUT", "60"))
        self.max_tokens = int(max_tokens if max_tokens is not None else os.environ.get("VISION_MAX_TOKENS", "512"))

        if not self.model_url:
            raise ValueError("VISION_MODEL_URL is not set")
        if not self.model_name:
            raise ValueError("VISION_MODEL_NAME is not set")

    # -- BaseVisionAdapter ------------------------------------------------

    def describe_image(self, png_bytes: bytes, prompt: str) -> str:
        self.check_png(png_bytes)

        b64 = base64.b64encode(png_bytes).decode("ascii")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model_name,
            "max_tokens": self.max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}"},
                        },
                    ],
                }
            ],
        }

        try:
            resp = requests.post(self.model_url, json=payload, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise AdapterError(f"vision endpoint unreachable ({self.model_url}): {exc}") from exc

        if resp.status_code != 200:
            body = (resp.text or "")[:500]
            raise AdapterError(f"vision endpoint HTTP {resp.status_code}: {body}")

        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise AdapterError(f"unexpected vision endpoint response: {exc}") from exc

        return (text or "").strip()

    def __repr__(self) -> str:  # never leak the key
        return (
            f"OpenAICompatVisionAdapter(url={self.model_url!r}, "
            f"model={self.model_name!r}, timeout={self.timeout})"
        )


register_adapter("openai_compat", OpenAICompatVisionAdapter)
# Common aliases people type:
register_adapter("openai-compatible", OpenAICompatVisionAdapter)
register_adapter("vllm", OpenAICompatVisionAdapter)
register_adapter("ollama", OpenAICompatVisionAdapter)
