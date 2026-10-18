#!/usr/bin/env python3
"""Benchmark: Rust scheduler vs stdlib heapq baseline.

Compares haley_rust_core.PyScheduler against a minimal heapq-based
priority queue defined inline here (benchmark-only; not a shipped
duplicate).

Usage: python3 agent/perf/benchmark.py [--n 50000] [--rounds 3]
"""

import argparse
import heapq
import itertools
import sys
import time

sys.path.insert(0, ".")

from agent.perf import rust_bridge


class HeapqBaseline:
    """Minimal priority queue on stdlib heapq — baseline only."""

    def __init__(self):
        self._heap = []
        self._seq = itertools.count()

    def push(self, id, priority, deadline_ms=None, payload=""):
        heapq.heappush(self._heap, (-priority, next(self._seq), id, payload))

    def pop(self):
        if not self._heap:
            return None
        negp, seq, tid, payload = heapq.heappop(self._heap)
        return {"id": tid, "priority": -negp, "seq": seq, "payload": payload}


def bench_push_pop(make_sched, n):
    s = make_sched()
    t0 = time.perf_counter()
    for i in range(n):
        s.push(f"task-{i}", priority=i % 16, deadline_ms=None, payload='{"x":1}')
    t_push = time.perf_counter() - t0

    t0 = time.perf_counter()
    c = 0
    while s.pop() is not None:
        c += 1
    t_pop = time.perf_counter() - t0
    assert c == n, f"drained {c}, expected {n}"
    return t_push, t_pop


def bench_mixed(make_sched, n):
    s = make_sched()
    t0 = time.perf_counter()
    for i in range(n):
        s.push(f"task-{i}", priority=i % 8, deadline_ms=None, payload="{}")
        if i % 3 == 0:
            s.pop()
    return time.perf_counter() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=50000)
    ap.add_argument("--rounds", type=int, default=3)
    args = ap.parse_args()

    print(f"Rust extension available: {rust_bridge.is_rust_available()}")
    print(f"n={args.n}, rounds={args.rounds}\n")

    import haley_rust_core

    impls = [
        ("heapq-baseline", HeapqBaseline),
        ("rust", haley_rust_core.PyScheduler),
    ]

    results = {}
    for name, cls in impls:
        pushes, pops, mixed = [], [], []
        for _ in range(args.rounds):
            tp, td = bench_push_pop(cls, args.n)
            pushes.append(tp)
            pops.append(td)
            mixed.append(bench_mixed(cls, args.n))
        results[name] = {
            "push_s": min(pushes),
            "pop_s": min(pops),
            "mixed_s": min(mixed),
        }
        r = results[name]
        print(
            f"{name:16s} push {r['push_s']:.3f}s ({args.n / r['push_s']:,.0f}/s) | "
            f"pop {r['pop_s']:.3f}s ({args.n / r['pop_s']:,.0f}/s) | "
            f"mixed {r['mixed_s']:.3f}s"
        )

    base, rs = results["heapq-baseline"], results["rust"]
    print("\nSpeedup (rust vs heapq baseline):")
    for k in ("push_s", "pop_s", "mixed_s"):
        print(f"  {k}: {base[k] / rs[k]:.2f}x")


if __name__ == "__main__":
    main()
