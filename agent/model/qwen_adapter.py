"""Qwen 7B adapter — talks to the self-hosted Qwen2.5-Coder-7B model via HALEY_API_URL."""
from __future__ import annotations
import json
import urllib.request
import urllib.error
from .provider import ModelProvider, ModelResponse


class QwenAdapter(ModelProvider):
    """Adapter for the self-hosted Qwen 7B /generate endpoint."""

    def __init__(self, base_url: str, timeout: int = 120):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _post(self, path: str, payload: dict) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json",
                     "ngrok-skip-browser-warning": "true"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise RuntimeError(f"Model endpoint unreachable: {url}: {e}")

    def generate(self, system_prompt: str, user_prompt: str,
                 max_tokens: int = 2048) -> ModelResponse:
        full_prompt = f"{system_prompt}\n\n### TASK\n{user_prompt}"
        result = self._post("/generate", {
            "prompt": full_prompt,
            "max_tokens": max_tokens,
        })
        text = result.get("text")
        if text is None:
            choices = result.get("choices") or []
            if choices:
                text = choices[0].get("message", {}).get("content", "")
        return ModelResponse(text=text or "", raw=result)

    def health_check(self) -> bool:
        try:
            url = f"{self.base_url}/health"
            req = urllib.request.Request(
                url, headers={"ngrok-skip-browser-warning": "true"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode("utf-8")
                return '"ok":true' in body.replace(" ", "") or resp.status == 200
        except Exception:
            return False
