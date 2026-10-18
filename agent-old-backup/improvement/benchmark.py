"""Benchmark — BASELINE and BENCHMARK stages of self-improvement.

Per ChatGPT spec: agent/improvement/benchmark.py
Captures performance baselines, compares after changes.
"""
from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional


@dataclass
class BenchmarkResult:
    """Result of benchmarking one operation."""
    name: str
    runs: int
    durations_ms: List[float] = field(default_factory=list)
    success_count: int = 0
    timestamp: float = field(default_factory=time.time)

    @property
    def mean_ms(self) -> float:
        return statistics.mean(self.durations_ms) if self.durations_ms else 0

    @property
    def median_ms(self) -> float:
        return statistics.median(self.durations_ms) if self.durations_ms else 0

    @property
    def p95_ms(self) -> float:
        if not self.durations_ms:
            return 0
        s = sorted(self.durations_ms)
        return s[min(len(s) - 1, int(len(s) * 0.95))]

    @property
    def min_ms(self) -> float:
        return min(self.durations_ms) if self.durations_ms else 0

    @property
    def max_ms(self) -> float:
        return max(self.durations_ms) if self.durations_ms else 0

    @property
    def success_rate(self) -> float:
        return self.success_count / self.runs if self.runs else 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "runs": self.runs,
            "mean_ms": round(self.mean_ms, 1),
            "median_ms": round(self.median_ms, 1),
            "p95_ms": round(self.p95_ms, 1),
            "min_ms": round(self.min_ms, 1),
            "max_ms": round(self.max_ms, 1),
            "success_rate": round(self.success_rate, 3),
            "timestamp": self.timestamp,
        }


@dataclass
class Comparison:
    """Baseline vs current comparison."""
    name: str
    baseline: BenchmarkResult
    current: BenchmarkResult
    speedup: float  # >1 = faster
    success_delta: float
    verdict: str  # "improved", "regressed", "same"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "baseline_mean_ms": round(self.baseline.mean_ms, 1),
            "current_mean_ms": round(self.current.mean_ms, 1),
            "speedup": round(self.speedup, 2),
            "success_delta": round(self.success_delta, 3),
            "verdict": self.verdict,
        }


class Benchmark:
    """Runs benchmarks and compares against baselines."""

    def __init__(self):
        self.baselines: Dict[str, BenchmarkResult] = {}

    def run(self, name: str, fn: Callable[[], Any],
            runs: int = 5, warmup: int = 1) -> BenchmarkResult:
        """Benchmark a callable."""
        result = BenchmarkResult(name=name, runs=runs)
        for _ in range(warmup):
            try:
                fn()
            except Exception:
                pass
        for _ in range(runs):
            start = time.time()
            try:
                fn()
                result.success_count += 1
            except Exception:
                pass
            result.durations_ms.append((time.time() - start) * 1000)
        return result

    def set_baseline(self, result: BenchmarkResult) -> None:
        self.baselines[result.name] = result

    def compare(self, current: BenchmarkResult) -> Optional[Comparison]:
        baseline = self.baselines.get(current.name)
        if not baseline:
            return None
        speedup = (baseline.mean_ms / current.mean_ms
                   if current.mean_ms > 0 else 1.0)
        success_delta = current.success_rate - baseline.success_rate
        if speedup >= 1.1 and success_delta >= 0:
            verdict = "improved"
        elif speedup < 0.9 or success_delta < -0.05:
            verdict = "regressed"
        else:
            verdict = "same"
        return Comparison(
            name=current.name, baseline=baseline, current=current,
            speedup=speedup, success_delta=success_delta,
            verdict=verdict)

    def benchmark_suite(self, fns: Dict[str, Callable[[], Any]],
                        runs: int = 5) -> Dict[str, BenchmarkResult]:
        return {name: self.run(name, fn, runs)
                for name, fn in fns.items()}

    def save_baselines(self, path: str) -> None:
        with open(path, "w") as f:
            json.dump({k: v.to_dict() for k, v in
                       self.baselines.items()}, f, indent=2)

    def load_baselines(self, path: str) -> None:
        with open(path) as f:
            data = json.load(f)
        for name, d in data.items():
            r = BenchmarkResult(name=name, runs=d["runs"])
            # Reconstruct approximate durations from stats
            mean = d["mean_ms"]
            r.durations_ms = [mean] * d["runs"]
            r.success_count = int(d["success_rate"] * d["runs"])
            r.timestamp = d.get("timestamp", time.time())
            self.baselines[name] = r

    def report(self, results: Dict[str, BenchmarkResult]) -> str:
        lines = ["Benchmark Report", "=" * 40]
        for name, r in results.items():
            comp = self.compare(r)
            line = (f"{name}: mean={r.mean_ms:.0f}ms "
                    f"p95={r.p95_ms:.0f}ms "
                    f"success={r.success_rate:.0%}")
            if comp:
                line += f" [{comp.verdict} vs baseline, {comp.speedup:.2f}x]"
            lines.append(line)
        return "\n".join(lines)
