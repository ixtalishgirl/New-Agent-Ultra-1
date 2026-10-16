"""DeepSeek adapter — talks to the self-hosted DeepSeek-R1-Distill-Qwen-14B-heretic
(Q8_0 GGUF) model served by llama.cpp via HALEY_API_URL (ngrok tunnel)."""
from __future__ import annotations
import json
import urllib.request
import urllib.error
from .provider import ModelProvider, ModelResponse


class QwenAdapter(ModelProvider):
    """Adapter for the self-hosted model over llama.cpp's OpenAI-compatible API.

    Same interface as before (generate/health_check) so callers don't change;
    only the wire protocol changed from /generate to /v1/chat/completions.
    """

    CHAT_PATH = "/v1/chat/completions"
    MODEL_NAME = "deepseek"

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
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"Model endpoint HTTP {e.code}: {body}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Model endpoint unreachable: {url}: {e}")

    def generate(self, system_prompt: str, user_prompt: str,
                 max_tokens: int = 2048) -> ModelResponse:
        result = self._post(self.CHAT_PATH, {
            "model": self.MODEL_NAME,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.7,
            "stream": False,
        })
        choices = result.get("choices") or []
        text = ""
        if choices:
            message = choices[0].get("message", {})
            text = message.get("content") or ""
            if not text.strip():
                text = message.get("reasoning_content") or ""
        return ModelResponse(text=text, raw=result)

    def health_check(self) -> bool:
        try:
            url = f"{self.base_url}/health"
            req = urllib.request.Request(
                url, headers={"ngrok-skip-browser-warning": "true"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status == 200
        except Exception:
            return False
