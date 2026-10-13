# Vision models — free options for ahsan's agent

His main model (Llama-3-8B on free Kaggle GPU) is **text-only**, so the vision
pipeline needs a *separate* vision-capable model (VLM). Constraints: **free-first,
open-source only — no paid vision APIs**.

The pipeline talks to any VLM through the pluggable adapter interface
(`adapters/base_adapter.py`). The ready-made adapter
(`adapters/adapter_openai_compat.py`) speaks the OpenAI chat-completions
protocol with an `image_url` part, so **any** of the options below work with
zero code changes — just point `VISION_MODEL_URL` / `VISION_MODEL_NAME` at it.

---

## Option A — Qwen2.5-VL on his Kaggle GPU (recommended later)

- **Model:** `Qwen/Qwen2.5-VL-7B-Instruct` (open weights, Apache 2.0).
  The 7B fits a Kaggle T4/P100 (16 GB) in 4-bit/8-bit; the 3B
  (`Qwen/Qwen2.5-VL-3B-Instruct`) fits even more comfortably.
- **How:** a serving notebook running **vLLM** (or llama.cpp server) with the
  OpenAI-compatible server enabled, e.g.
  `vllm serve Qwen/Qwen2.5-VL-7B-Instruct --port 8000`
  then set `VISION_MODEL_URL=http://<kaggle-tunnel>/v1/chat/completions`,
  `VISION_MODEL_NAME=Qwen/Qwen2.5-VL-7B-Instruct`.
- **Note:** this needs a *serving notebook* on his Kaggle — **his decision**,
  per his standing boundary (we do only what he orders on Kaggle; we don't
  touch his datasets/sessions). The `haley-serve-live` notebook pattern he
  already uses (FastAPI + tunnel) is the template to copy.
- **Cost:** free (Kaggle GPU quota).

## Option B — Ollama locally (free, simplest on a decent machine)

- Install Ollama, then:
  ```
  ollama pull qwen2.5vl:7b     # or qwen2.5vl:3b on smaller machines
  ollama serve                 # exposes OpenAI-compatible API at :11434/v1
  ```
- Config:
  ```
  VISION_ADAPTER=openai_compat
  VISION_MODEL_URL=http://localhost:11434/v1/chat/completions
  VISION_MODEL_NAME=qwen2.5vl:7b
  VISION_MODEL_KEY=ollama        # Ollama ignores it; any value works
  ```
- Alternatives: `llava:7b` / `llava:13b`, `minicpm-v`, `moondream`.
- Needs ~8 GB+ RAM for the 7B (less for 3B/moondream). Fully offline, free.

## Option C — any existing OpenAI-compatible endpoint he already has

- If he ever has a VLM behind vLLM / LocalAI / llama.cpp / text-generation-webui
  (OpenAI extension) — local or remote — just point the env vars at it:
  ```
  VISION_ADAPTER=openai_compat
  VISION_MODEL_URL=https://<host>/v1/chat/completions
  VISION_MODEL_NAME=<the served model id>
  VISION_MODEL_KEY=<key if the server needs one>
  ```
- No new code needed; the adapter sends the standard `image_url` format.

## Not used

- Paid vision APIs (OpenAI GPT-4o vision, Anthropic, Google, etc.) — excluded
  by the free-first constraint. The adapter *would* work with them, but we
  don't configure or document keys for them.

---

## If NO vision model is configured

The pipeline still works: `observe()` returns a **text-only observation**
clearly labeled `"vision unavailable — text-only"`. Elements come only from
DOM facts; **no visual details are ever fabricated**. Set up one of the
options above whenever vision is needed.
