"""Rust bridge for the Haley agent performance core.

Thin Python interface over the compiled ``haley_rust_core`` extension
(built from ``rust-core/`` via ``rust-core/build.sh``).

There is exactly ONE implementation of each primitive — the Rust one.
No pure-Python duplicates are kept here (per ahsan's order: no duplicate
code across languages).

If the extension is not built, importing this module raises an
ImportError telling you how to build it.
"""

from __future__ import annotations

try:
    import haley_rust_core as _rust
except ImportError as e:
    raise ImportError(
        "haley_rust_core extension is not built. "
        "Build it with: cd rust-core && ./build.sh"
    ) from e


def is_rust_available() -> bool:
    """Always True — this bridge requires the compiled extension."""
    return True


def rust_version() -> str:
    return _rust.__version__


# ---------------------------------------------------------------------------
# Scheduler — priority task queue (Rust: rust-core/src/scheduler.rs)
# ---------------------------------------------------------------------------

def get_scheduler():
    """Thread-safe priority scheduler (priority + deadline + FIFO)."""
    return _rust.PyScheduler()


# ---------------------------------------------------------------------------
# Message queue — MPMC channel (Rust: rust-core/src/queue.rs)
# ---------------------------------------------------------------------------

def get_queue(capacity=None):
    """High-throughput multi-producer multi-consumer message queue."""
    if capacity:
        return _rust.PyMessageQueue.bounded(capacity)
    return _rust.PyMessageQueue.unbounded()


# ---------------------------------------------------------------------------
# Resource monitor (Rust: rust-core/src/monitor.rs)
# ---------------------------------------------------------------------------

def get_monitor():
    """CPU/memory sampler reading /proc directly."""
    return _rust.PyResourceMonitor()


def quick_cpu_count() -> int:
    return _rust.quick_cpu_count()


def quick_mem_used_percent() -> float:
    return _rust.quick_mem_used_percent()


def quick_self_rss_kb() -> int:
    return _rust.quick_self_rss_kb()


# ---------------------------------------------------------------------------
# Event log — structured ring buffer (Rust: rust-core/src/events.rs)
# ---------------------------------------------------------------------------

def get_event_log(capacity=10000):
    """Bounded structured event log with kind/level filtering."""
    return _rust.PyEventLog(capacity)


# ---------------------------------------------------------------------------
# Rate limiting (Rust: rust-core/src/ratelimit.rs)
# ---------------------------------------------------------------------------

def get_rate_limiter(capacity=100.0, refill_per_sec=10.0):
    """Token-bucket rate limiter (per-key buckets)."""
    return _rust.PyRateLimiter(capacity, refill_per_sec)


def get_sliding_window(window_secs=60):
    """Sliding-window request counter."""
    return _rust.PySlidingWindow(window_secs)


# ---------------------------------------------------------------------------
# Tool execution metadata (Rust: rust-core/src/serde_fast.rs)
# ---------------------------------------------------------------------------

def get_tool_meta(capacity=5000):
    """Recorder of tool execution metadata + per-tool aggregates."""
    return _rust.PyToolMeta(capacity)


def json_validate(s: str):
    """(valid: bool, error: str)"""
    return _rust.json_validate(s)


def json_get_field(obj_json: str, field: str):
    return _rust.json_get_field(obj_json, field)


def json_merge(a: str, b: str) -> str:
    return _rust.json_merge(a, b)


def json_encode_str_list(items) -> str:
    return _rust.json_encode_str_list(list(items))


def json_decode_str_list(s: str):
    return _rust.json_decode_str_list(s)


# ---------------------------------------------------------------------------
# Worker pool (Rust: rust-core/src/worker.rs)
# ---------------------------------------------------------------------------

def get_worker_pool(workers=4, queue_capacity=256, handler=None):
    """Fixed-size worker pool with panic isolation and backpressure.

    handler(job_id, kind, payload) -> (ok: bool, output: str)
    """
    if handler is None:
        handler = lambda jid, kind, payload: (True, payload)
    return _rust.PyWorkerPool(workers, queue_capacity, handler)


# ---------------------------------------------------------------------------
# Log processing (Rust: rust-core/src/logproc.rs)
# ---------------------------------------------------------------------------

def log_count_lines(path: str) -> int:
    return _rust.log_count_lines(path)


def log_grep_count(path: str, needle: str) -> int:
    return _rust.log_grep_count(path, needle)


def log_tail_lines(path: str, n: int):
    return _rust.log_tail_lines(path, n)


def log_find_offsets(path: str, needle: str, limit: int = 100):
    return _rust.log_find_offsets(path, needle, limit)


# ---------------------------------------------------------------------------
# File indexing (Rust: rust-core/src/index.rs)
# ---------------------------------------------------------------------------

def index_directory(root, max_depth=12, skip_hidden=True, include_exts=None):
    return _rust.index_directory(root, max_depth, skip_hidden, include_exts or [])


def index_search(entries, query, limit=50):
    return _rust.index_search(entries, query, limit)
