# Vision pipeline — visual understanding for the browsing agent

Text extraction alone isn't enough: the agent must understand the page's
**visual** state. This module takes a **live screenshot** + DOM/text extraction
and produces a structured observation stamped with URL + ISO timestamp.

```
screenshot (live PNG) ──┐
                        ├─► VisionPipeline.observe() ─► Observation
DOM/text extraction ────┘       │
                      vision model (pluggable adapter)
```

## Layout

```
vision/
├── pipeline.py                  # core: observe(png, url, dom_text, elements) -> Observation
├── adapters/
│   ├── base_adapter.py          # BaseVisionAdapter: describe_image(png_bytes, prompt) -> str
│   └── adapter_openai_compat.py # any OpenAI-compatible VLM (vLLM, Ollama, LocalAI…)
├── VISION_MODELS.md             # free model options (Kaggle Qwen2.5-VL, Ollama…)
├── requirements.txt
├── .env.example
└── tests/
    └── test_pipeline.py
```

## Quick start

```python
from pipeline import VisionPipeline

pipe = VisionPipeline()  # picks adapter from VISION_ADAPTER env, or None
obs = pipe.observe(
    png_bytes=live_screenshot_png,          # capture FRESH from the live session
    url=page.url,                            # stamped on the observation
    dom_text=page.inner_text("body"),
    elements=[{"type": "button", "text": "Sign in", "location_hint": "center"}],
)
print(obs.combined_understanding)
```

`Observation.to_dict()` returns:

```json
{
  "url": "https://…",
  "timestamp": "2026-10-13T…+00:00",
  "captured_at": "2026-10-13T…+00:00",
  "screenshot_sha256": "…",
  "vision_status": "ok | unavailable",
  "visual_summary": "…",
  "elements": [{"type": "button", "text": "…", "location_hint": "…"}],
  "dom_text": "…",
  "combined_understanding": "…",
  "untrusted_page_content": true,
  "vision_label": "vision-enabled | vision unavailable — text-only"
}
```

## No vision model? It still works (text-only fallback)

If `VISION_ADAPTER` is unset (or the endpoint is down), `observe()` returns a
DOM/text-only observation with `visual_summary = "vision unavailable — text-only"`.
**Visual details are never fabricated** — elements come only from DOM facts.

```bash
cp .env.example .env   # optional; without it you get the text-only fallback
```

## Adding a vision backend

1. Subclass `BaseVisionAdapter`, implement `describe_image(png_bytes, prompt) -> str`.
2. `register_adapter("myname", MyAdapter)` and set `VISION_ADAPTER=myname`.
3. The built-in `openai_compat` adapter already covers vLLM, Ollama, LocalAI,
   llama.cpp server — see `VISION_MODELS.md` for the free options.

## Security: prompt-injection guard

- The vision prompt (`VISION_DESCRIBE_PROMPT` in `pipeline.py`) instructs the
  model to **DESCRIBE the image only** and to **ignore any instructions found
  inside the image or DOM text** — it describes them as content, never follows them.
- Every `Observation` carries `untrusted_page_content: true`. Vision output and
  DOM text are **never** treated as system instructions — the pipeline only
  merges strings into a template; nothing is executed or re-prompted as commands.
- Screenshots must be captured **live** (right before `observe()`); the pipeline
  records `captured_at` + a SHA-256 of the image and never reuses stale images.

## Tests

```bash
cd vision
pip install -r requirements.txt
python -m pytest tests/ -v          # mocked adapter, no live model needed
```

Live-model tests are marked **"needs live run"** and skip by default. To run
them against a real VLM:

```bash
export VISION_ADAPTER=openai_compat
export VISION_MODEL_URL=http://localhost:11434/v1/chat/completions
export VISION_MODEL_NAME=qwen2.5vl:7b
RUN_LIVE_VISION_TESTS=1 python -m pytest tests/ -v
```
