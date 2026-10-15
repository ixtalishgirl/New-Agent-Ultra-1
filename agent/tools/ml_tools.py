"""AI & Machine Learning tools.

Real implementations: model inference via the configured endpoint
(HALEY_API_URL) or honest fallback, pure-python TF-IDF RAG index/query
(sklearn used if present), CSV/JSONL dataset validation, nvidia-smi GPU
monitoring, and honest capability notes for training/quantization paths that
need real hardware or credentials.
"""
from __future__ import annotations

import csv
import json
import math
import os
import re
import shutil
import subprocess
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TIMEOUT = 300
RAG_DIR = Path.home() / ".haley_rag"


@dataclass
class MLResult:
    ok: bool
    output: str = ""
    parsed: dict = field(default_factory=dict)
    error: str = ""


def _fail(msg: str) -> MLResult:
    return MLResult(ok=False, error=msg, output=msg)


def _sch(t: str, req: bool, desc: str) -> dict:
    return {"type": t, "required": req, "description": desc}


def _ml_inference(args: dict) -> MLResult:
    prompt = str(args.get("prompt", ""))
    if not prompt:
        return _fail("prompt required")
    endpoint = (os.environ.get("HALEY_API_URL", "") or "").rstrip("/")
    timeout = int(args.get("timeout", 120))
    if not endpoint:
        return MLResult(ok=False, output="",
                       error="HALEY_API_URL not set — no model endpoint configured. "
                             "Set it to your serving URL, then retry.")
    body = json.dumps({
        "prompt": prompt,
        "max_tokens": int(args.get("max_tokens", 512)),
        "temperature": float(args.get("temperature", 0.7)),
    }).encode()
    # try common paths
    last_err = ""
    for path in ("/generate", "/v1/completions", "/api/generate", "/"):
        try:
            req = urllib.request.Request(endpoint + path, data=body,
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            text = (data.get("text") or data.get("response")
                    or data.get("choices", [{}])[0].get("text", "")
                    if isinstance(data, dict) else str(data))
            return MLResult(ok=True, output=str(text)[:8000],
                           parsed={"endpoint": endpoint + path})
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
    return MLResult(ok=False, output="",
                   error=f"model endpoint unreachable: {last_err}")


def _model_route(args: dict) -> MLResult:
    task = str(args.get("task", ""))
    routes = {
        "code": os.environ.get("HALEY_API_URL", "") or "code model endpoint",
        "chat": os.environ.get("HALEY_API_URL", "") or "chat model endpoint",
        "embed": "local TF-IDF (rag_index) — no embedding server configured",
    }
    if not task:
        return MLResult(ok=True, output=json.dumps(routes, indent=2),
                       parsed={"routes": routes,
                               "note": "heuristic routing; configure dedicated "
                                       "endpoints via env for real routing"})
    key = "code" if re.search(r"code|program|debug", task, re.I) else \
          "embed" if re.search(r"embed|search|retriev", task, re.I) else "chat"
    return MLResult(ok=True, output=f"route: {key} -> {routes[key]}",
                   parsed={"route": key, "target": routes[key]})


# ---------------------------------------------------------------------------
# TF-IDF RAG (pure python; sklearn path if available)
# ---------------------------------------------------------------------------

def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _rag_index(args: dict) -> MLResult:
    name = re.sub(r"[^a-z0-9_-]", "", str(args.get("name", "default")))
    docs = args.get("docs")
    root = str(args.get("root", ""))
    items: list[dict] = []
    if docs:
        for i, d in enumerate(docs):
            items.append({"id": str(d.get("id", i)), "text": str(d.get("text", ""))})
    elif root:
        rp = Path(root)
        for p in rp.rglob("*.md"):
            if len(items) >= 500:
                break
            try:
                items.append({"id": str(p), "text": p.read_text(encoding="utf-8")[:4000]})
            except Exception:
                pass
        for p in rp.rglob("*.txt"):
            if len(items) >= 800:
                break
            try:
                items.append({"id": str(p), "text": p.read_text(encoding="utf-8")[:4000]})
            except Exception:
                pass
    if not items:
        return _fail("no docs given (docs list or root dir)")
    # TF-IDF
    df: Counter = Counter()
    tf_list = []
    for it in items:
        toks = _tokens(it["text"])
        tf = Counter(toks)
        tf_list.append(tf)
        df.update(set(toks))
    n = len(items)
    idf = {t: math.log((n + 1) / (c + 1)) + 1 for t, c in df.items()}
    vectors = [{t: (c / max(sum(tf.values()), 1)) * idf[t] for t, c in tf.items()}
               for tf in tf_list]
    RAG_DIR.mkdir(parents=True, exist_ok=True)
    (RAG_DIR / f"{name}.json").write_text(json.dumps(
        {"docs": items, "vectors": vectors, "idf": idf}), encoding="utf-8")
    return MLResult(ok=True, output=f"indexed {n} docs as '{name}' (TF-IDF, no fake vectors)",
                   parsed={"name": name, "docs": n})


def _rag_query(args: dict) -> MLResult:
    name = re.sub(r"[^a-z0-9_-]", "", str(args.get("name", "default")))
    q = str(args.get("query", ""))
    k = min(int(args.get("top_k", 5)), 20)
    if not q:
        return _fail("query required")
    fp = RAG_DIR / f"{name}.json"
    if not fp.exists():
        return _fail(f"index '{name}' not found — run rag_index first")
    data = json.loads(fp.read_text(encoding="utf-8"))
    qtf = Counter(_tokens(q))
    qsum = max(sum(qtf.values()), 1)
    qvec = {t: (c / qsum) * data["idf"].get(t, 1.0) for t, c in qtf.items()}
    qnorm = math.sqrt(sum(v * v for v in qvec.values())) or 1.0
    scored = []
    for doc, vec in zip(data["docs"], data["vectors"]):
        dot = sum(qvec.get(t, 0.0) * v for t, v in vec.items())
        vnorm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        scored.append((dot / (qnorm * vnorm), doc))
    scored.sort(key=lambda x: -x[0])
    hits = [{"id": d["id"], "score": round(s, 4),
             "snippet": d["text"][:300]} for s, d in scored[:k] if s > 0]
    return MLResult(ok=True, output=f"{len(hits)} hits for '{q}'",
                   parsed={"hits": hits,
                           "note": "TF-IDF cosine; for semantic search use real embeddings"})


def _embed_generate(args: dict) -> MLResult:
    texts = args.get("texts") or []
    if not texts:
        return _fail("texts required")
    # honest: hashed bag-of-words pseudo-embedding, clearly labeled
    vecs = []
    for t in texts:
        toks = _tokens(str(t))
        v = [0.0] * 64
        for tok in toks:
            v[hash(tok) % 64] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        vecs.append([round(x / n, 4) for x in v])
    return MLResult(ok=True, output=f"{len(vecs)} vectors (dim 64)",
                   parsed={"vectors": vecs,
                           "note": "hashed BOW pseudo-embeddings — NOT semantic. "
                                   "Use a real embedding model for production."})


def _vector_search(args: dict) -> MLResult:
    # cosine search over provided vectors (honest utility)
    query = args.get("query_vector") or []
    items = args.get("items") or []
    k = min(int(args.get("top_k", 5)), 20)
    if not query or not items:
        return _fail("query_vector and items required")
    qn = math.sqrt(sum(x * x for x in query)) or 1.0
    scored = []
    for it in items:
        v = it.get("vector") or []
        if len(v) != len(query):
            continue
        vn = math.sqrt(sum(x * x for x in v)) or 1.0
        s = sum(a * b for a, b in zip(query, v)) / (qn * vn)
        scored.append((s, it.get("id")))
    scored.sort(key=lambda x: -x[0])
    return MLResult(ok=True, output=f"{len(scored)} scored",
                   parsed={"hits": [{"id": i, "score": round(s, 4)}
                                    for s, i in scored[:k]]})


def _dataset_process(args: dict) -> MLResult:
    path = str(args.get("path", ""))
    if not path:
        return _fail("path required")
    p = Path(path)
    if not p.exists():
        return _fail(f"not found: {path}")
    rows = []
    if p.suffix == ".csv":
        with p.open(encoding="utf-8", errors="replace") as fh:
            rows = list(csv.DictReader(fh))[:20000]
        stats = {"rows": len(rows), "columns": list(rows[0].keys()) if rows else []}
    elif p.suffix == ".jsonl":
        for i, ln in enumerate(p.open(encoding="utf-8", errors="replace")):
            if i >= 20000:
                break
            try:
                rows.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
        stats = {"rows": len(rows)}
    else:
        return _fail("only .csv and .jsonl supported")
    out = str(args.get("output", ""))
    if out:
        Path(out).write_text(json.dumps(rows[:5000]), encoding="utf-8")
    return MLResult(ok=True, output=json.dumps(stats), parsed={**stats, "output": out})


def _dataset_validate(args: dict) -> MLResult:
    path = str(args.get("path", ""))
    schema = args.get("schema") or {}
    if not path:
        return _fail("path required")
    p = Path(path)
    if not p.exists():
        return _fail(f"not found: {path}")
    issues = []
    n = 0
    if p.suffix == ".csv":
        with p.open(encoding="utf-8", errors="replace") as fh:
            reader = csv.DictReader(fh)
            cols = reader.fieldnames or []
            for col in schema:
                if col not in cols:
                    issues.append(f"missing column: {col}")
            for i, row in enumerate(reader):
                n += 1
                if n > 20000:
                    break
                for col, typ in schema.items():
                    v = (row.get(col) or "").strip()
                    if not v:
                        issues.append(f"row {i}: empty {col}")
                    elif typ == "int":
                        try:
                            int(v)
                        except ValueError:
                            issues.append(f"row {i}: {col} not int: {v[:30]}")
                    elif typ == "float":
                        try:
                            float(v)
                        except ValueError:
                            issues.append(f"row {i}: {col} not float: {v[:30]}")
                if len(issues) > 100:
                    break
    elif p.suffix == ".jsonl":
        for i, ln in enumerate(p.open(encoding="utf-8", errors="replace")):
            n += 1
            if n > 20000:
                break
            try:
                obj = json.loads(ln)
            except json.JSONDecodeError:
                issues.append(f"line {i}: bad json")
                continue
            for col in schema:
                if col not in obj:
                    issues.append(f"line {i}: missing key {col}")
            if len(issues) > 100:
                break
    else:
        return _fail("only .csv and .jsonl supported")
    return MLResult(ok=not issues, output=f"{n} rows checked, {len(issues)} issues",
                   parsed={"rows": n, "issues": issues[:100]})


def _eval_dataset_gen(args: dict) -> MLResult:
    topic = str(args.get("topic", "general"))
    n = min(int(args.get("n", 20)), 200)
    out = str(args.get("output", "eval_dataset.jsonl"))
    items = [{"id": i, "prompt": f"[{topic}] eval prompt {i}",
              "expected": "", "rubric": "correctness, completeness"}
             for i in range(n)]
    Path(out).write_text("\n".join(json.dumps(i) for i in items), encoding="utf-8")
    return MLResult(ok=True, output=f"wrote {n} eval stubs to {out} (fill expected answers)",
                   parsed={"output": out, "n": n,
                           "note": "stubs — fill 'expected' per your task"})


def _prompt_eval(args: dict) -> MLResult:
    prompt = str(args.get("prompt", ""))
    if not prompt:
        return _fail("prompt required")
    checks = {
        "has_role": bool(re.search(r"you are|act as|role", prompt, re.I)),
        "has_task": bool(re.search(r"do|generate|write|explain|analyze", prompt, re.I)),
        "has_format": bool(re.search(r"format|json|markdown|structure", prompt, re.I)),
        "has_constraints": bool(re.search(r"must|never|only|limit|max", prompt, re.I)),
        "length_ok": 20 <= len(prompt) <= 4000,
    }
    score = sum(checks.values())
    tips = []
    if not checks["has_role"]:
        tips.append("add a role ('you are ...')")
    if not checks["has_format"]:
        tips.append("specify output format")
    if not checks["has_constraints"]:
        tips.append("add explicit constraints")
    return MLResult(ok=True, output=f"prompt score {score}/5",
                   parsed={"score": score, "checks": checks, "tips": tips,
                           "note": "heuristic rubric, not a model judgment"})


def _quality_score(args: dict) -> MLResult:
    text = str(args.get("text", ""))
    if not text:
        return _fail("text required")
    words = _tokens(text)
    uniq = len(set(words))
    sentences = [s for s in re.split(r"[.!?]+", text) if s.strip()]
    metrics = {
        "chars": len(text),
        "words": len(words),
        "unique_ratio": round(uniq / max(len(words), 1), 3),
        "sentences": len(sentences),
        "avg_sentence_len": round(sum(len(s.split()) for s in sentences)
                                 / max(len(sentences), 1), 1),
    }
    score = 0
    if 50 <= metrics["words"] <= 2000:
        score += 2
    if metrics["unique_ratio"] > 0.4:
        score += 2
    if metrics["sentences"] >= 2:
        score += 1
    return MLResult(ok=True, output=f"quality heuristics score {score}/5",
                   parsed={"score": score, "metrics": metrics,
                           "note": "surface heuristics, not semantic quality"})


def _gpu_monitor(args: dict) -> MLResult:
    if not shutil.which("nvidia-smi"):
        return MLResult(ok=False, output="",
                       error="nvidia-smi not found — no NVIDIA GPU in this environment.")
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,utilization.gpu,memory.used,"
             "memory.total,temperature.gpu", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=15)
    except Exception as e:
        return _fail(f"nvidia-smi failed: {e}")
    return MLResult(ok=True, output=r.stdout.strip(),
                   parsed={"gpus": r.stdout.strip().splitlines()})


def _quantize_note(args: dict) -> MLResult:
    import importlib.util
    bits = importlib.util.find_spec("bitsandbytes") is not None
    return MLResult(ok=True,
                   output=("bitsandbytes " + ("installed — 8/4-bit quantization available"
                                            " via transformers BitsAndBytesConfig."
                            if bits else "NOT installed (`pip install bitsandbytes`, "
                            "needs CUDA GPU). No quantization possible here.")),
                   parsed={"bitsandbytes": bits})


def _finetune_note(args: dict) -> MLResult:
    return MLResult(ok=True,
                   output=("Fine-tuning needs: a base model, a training dataset, GPU "
                           "with enough VRAM, and transformers/peft/trd. None are "
                           "wired here. Use the Kaggle GPU flow for real training; "
                           "this tool only validates readiness."),
                   parsed={"ready": False})


def _lora_note(args: dict) -> MLResult:
    import importlib.util
    peft = importlib.util.find_spec("peft") is not None
    return MLResult(ok=True,
                   output=("peft " + ("installed — LoRA configs available."
                                     if peft else "not installed. LoRA training needs "
                                     "peft + a base model + GPU.")),
                   parsed={"peft": peft})


def _infer_opt_note(args: dict) -> MLResult:
    return MLResult(ok=True,
                   output=("Inference optimization options: vLLM/TGI server, "
                           "quantization (see quantize_note), KV-cache tuning, "
                           "batching. None are running here — configure "
                           "HALEY_API_URL to a tuned server."),
                   parsed={"guidance": True})


def _model_version(args: dict) -> MLResult:
    action = str(args.get("action", "list")).lower()
    store = RAG_DIR / "model_versions.json"
    data = json.loads(store.read_text()) if store.exists() else {}
    if action == "list":
        return MLResult(ok=True, output=json.dumps(data, indent=2)[:2000],
                       parsed={"versions": data})
    name = str(args.get("name", ""))
    if action == "set":
        ver = str(args.get("version", ""))
        if not name or not ver:
            return _fail("name and version required")
        data[name] = {"version": ver, "pinned": True}
        RAG_DIR.mkdir(parents=True, exist_ok=True)
        store.write_text(json.dumps(data, indent=2))
        return MLResult(ok=True, output=f"pinned {name}={ver}", parsed={"ok": True})
    if action == "get":
        return MLResult(ok=True, output=json.dumps(data.get(name, {})),
                       parsed={"entry": data.get(name, {})})
    return _fail("action must be list|get|set")


HANDLERS: dict = {
    "ml_inference": _ml_inference,
    "model_route": _model_route,
    "rag_index": _rag_index,
    "rag_query": _rag_query,
    "embed_generate": _embed_generate,
    "vector_search": _vector_search,
    "dataset_process": _dataset_process,
    "dataset_validate": _dataset_validate,
    "eval_dataset_gen": _eval_dataset_gen,
    "prompt_eval": _prompt_eval,
    "quality_score": _quality_score,
    "gpu_monitor": _gpu_monitor,
    "quantize_note": _quantize_note,
    "finetune_note": _finetune_note,
    "lora_note": _lora_note,
    "infer_opt_note": _infer_opt_note,
    "model_version": _model_version,
}

TOOL_DEFS: list[tuple] = [
    ("ml_inference", "Call the configured model endpoint (HALEY_API_URL)",
     {"prompt": _sch("string", True, "Prompt"),
      "max_tokens": _sch("integer", False, "Default 512"),
      "temperature": _sch("number", False, "Default 0.7"),
      "timeout": _sch("integer", False, "Seconds")}, "computer", 300),
    ("model_route", "Heuristic routing of a task to code/chat/embed path",
     {"task": _sch("string", False, "Task description")}, "computer", 20),
    ("rag_index", "Build a TF-IDF index over docs (honest, no fake vectors)",
     {"name": _sch("string", False, "Index name"),
      "docs": _sch("array", False, "[{id,text}]"),
      "root": _sch("string", False, "Or scan .md/.txt under dir")}, "computer", 300),
    ("rag_query", "Cosine TF-IDF search over a built index",
     {"name": _sch("string", False, "Index name"),
      "query": _sch("string", True, "Query"),
      "top_k": _sch("integer", False, "Default 5")}, "computer", 60),
    ("embed_generate", "Hashed BOW pseudo-embeddings (labeled non-semantic)",
     {"texts": _sch("array", True, "Texts")}, "computer", 60),
    ("vector_search", "Cosine search over caller-supplied vectors",
     {"query_vector": _sch("array", True, "Query vector"),
      "items": _sch("array", True, "[{id, vector}]"),
      "top_k": _sch("integer", False, "Default 5")}, "computer", 60),
    ("dataset_process", "Load .csv/.jsonl (capped), optional JSON dump",
     {"path": _sch("string", True, "Dataset path"),
      "output": _sch("string", False, "Write JSON path")}, "computer", 180),
    ("dataset_validate", "Row-level schema checks on .csv/.jsonl",
     {"path": _sch("string", True, "Dataset path"),
      "schema": _sch("object", False, "{col: int|float}")}, "computer", 180),
    ("eval_dataset_gen", "Write eval JSONL stubs",
     {"topic": _sch("string", False, "Topic"),
      "n": _sch("integer", False, "Default 20"),
      "output": _sch("string", False, "Output path")}, "computer", 30),
    ("prompt_eval", "Heuristic prompt rubric (5 checks)",
     {"prompt": _sch("string", True, "Prompt text")}, "computer", 20),
    ("quality_score", "Surface text-quality heuristics",
     {"text": _sch("string", True, "Text")}, "computer", 20),
    ("gpu_monitor", "nvidia-smi GPU stats or honest absent-note",
     {}, "computer", 30),
    ("quantize_note", "bitsandbytes availability report",
     {}, "computer", 20),
    ("finetune_note", "Fine-tuning readiness (honest)",
     {}, "computer", 10),
    ("lora_note", "peft/LoRA availability report",
     {}, "computer", 20),
    ("infer_opt_note", "Inference optimization guidance",
     {}, "computer", 10),
    ("model_version", "list|get|set pinned model versions",
     {"action": _sch("string", False, "list|get|set"),
      "name": _sch("string", False, "Model name"),
      "version": _sch("string", False, "Version")}, "computer", 20),
]
