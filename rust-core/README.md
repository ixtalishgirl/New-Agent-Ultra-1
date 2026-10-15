# Haley Rust Core

High-performance Rust layer for the Haley autonomous coding agent
(`New-Agent-Ultra-1`).

**No-duplication policy (ahsan's order):** each primitive exists exactly
ONCE — in Rust. The Python side (`agent/perf/rust_bridge.py`) is a thin
wrapper over the compiled extension; there are no pure-Python fallback
reimplementations. The existing `agent/app/` modules (cron scheduler,
pub/sub event bus, persistent task queue) are NOT duplicates: they
provide features this core intentionally does not (cron parsing, live
handler dispatch, queue persistence).

## Modules

| Module | Purpose |
|---|---|
| `scheduler` | Thread-safe priority task scheduler (priority + deadline + FIFO) |
| `queue` | High-throughput MPMC message queue + broadcaster fan-out |
| `monitor` | CPU/memory sampling via `/proc` (no subprocess overhead) |
| `events` | Structured event log: ring buffer, kind/level filtering, JSON export |
| `ratelimit` | Token-bucket rate limiter + sliding-window request counter |
| `serde_fast` | JSON helpers + tool-execution metadata recorder |
| `worker` | Fixed-size worker pool with panic isolation and backpressure |
| `logproc` | Fast log processing: line count, grep count, tail, byte offsets |
| `index` | File indexing: recursive walk with skip rules, search, totals |

## Build

```bash
cd rust-core
./build.sh            # release build + install + cargo test
./build.sh --debug    # debug build (faster compile)
```

Requires: Rust stable, Python 3.8+, `maturin` (installed by the script).

## Python usage

```python
from agent.perf.rust_bridge import get_scheduler, is_rust_available

print(is_rust_available())  # True when the compiled extension is importable
sched = get_scheduler()     # Rust Scheduler, or pure-Python fallback
sched.push("task-1", priority=10, deadline_ms=None, payload="{}")
print(sched.pop())
```

## Tests

```bash
cargo test --release        # Rust unit tests
python3 agent/perf/benchmark.py  # Rust vs Python scheduler benchmark
```

## Benchmarks

`benches/scheduler_bench.rs` (criterion): push/pop throughput of the
priority scheduler. `agent/perf/benchmark.py` compares the Rust extension
against the pure-Python fallback end to end.
