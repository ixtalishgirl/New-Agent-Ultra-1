"""Consistency checking for the agent control loop.

Catches contradictions the single-result verifier cannot see:
  - cross-run consistency: same tool, same inputs -> wildly different outputs
  - temporal consistency: a new result contradicts an earlier VERIFIED fact
  - cross-tool consistency: web result vs shell result for the same fact
  - internal consistency: a single result contradicting itself

The agent MUST use tools and cannot fabricate output. Consistency checking
is the second line of defense: even a well-formed single result is rejected
if it disagrees with what the agent already verified.

Key classes:
    Contradiction          a detected conflict with severity + resolution
    TemporalFactStore      append-only store of verified facts (with TTL)
    ConsistencyChecker     runs all consistency analyses
    ConflictResolutionStrategy  policies for resolving conflicts
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class ContradictionSeverity(str, Enum):
    LOW = "low"          # cosmetic / rounding-level disagreement
    MEDIUM = "medium"    # material disagreement, needs a re-check
    HIGH = "high"        # direct factual contradiction on an important claim
    CRITICAL = "critical"  # verified fact contradicted: trust is broken


@dataclass
class Contradiction:
    """A single detected contradiction between two sources of truth."""
    description: str
    sources: List[str]
    severity: ContradictionSeverity
    resolution: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    detected_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "description": self.description,
            "sources": self.sources,
            "severity": self.severity.value,
            "resolution": self.resolution,
            "evidence": self.evidence,
            "detected_at": self.detected_at,
        }


@dataclass
class ConsistencyReport:
    """Outcome of a consistency analysis."""
    consistent: bool
    score: float  # 0.0..1.0, 1.0 = fully consistent
    contradictions: List[Contradiction] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "consistent": self.consistent,
            "score": self.score,
            "contradictions": [c.to_dict() for c in self.contradictions],
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Similarity primitives
# ---------------------------------------------------------------------------

def fuzzy_ratio(a: str, b: str) -> float:
    """0..1 string similarity (SequenceMatcher ratio on normalized text)."""
    a_n = re.sub(r"\s+", " ", (a or "").strip().lower())
    b_n = re.sub(r"\s+", " ", (b or "").strip().lower())
    if not a_n and not b_n:
        return 1.0
    if not a_n or not b_n:
        return 0.0
    return SequenceMatcher(None, a_n, b_n).ratio()


def token_jaccard(a: str, b: str) -> float:
    """Jaccard similarity over word tokens."""
    ta = set(re.findall(r"\w+", (a or "").lower()))
    tb = set(re.findall(r"\w+", (b or "").lower()))
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def numeric_close(a: float, b: float, rel_tol: float = 0.02,
                  abs_tol: float = 1e-9) -> Tuple[bool, float]:
    """Numeric comparison with tolerance. Returns (close, relative_diff)."""
    if a == b:
        return True, 0.0
    denom = max(abs(a), abs(b))
    rel = abs(a - b) / denom if denom else abs(a - b)
    return (rel <= rel_tol or abs(a - b) <= abs_tol), rel


def extract_numbers(text: str) -> List[float]:
    """Extract all numbers from text (ints, floats, with commas)."""
    out: List[float] = []
    for m in re.finditer(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", text or ""):
        try:
            out.append(float(m.group(0).replace(",", "")))
        except ValueError:
            continue
    return out


def extract_key_values(text: str) -> Dict[str, str]:
    """Extract `key: value` / `key = value` pairs from text."""
    pairs: Dict[str, str] = {}
    for m in re.finditer(r"(?m)^\s*([A-Za-z_][\w .\-]{0,60}?)\s*[:=]\s*(.+?)\s*$", text or ""):
        key = m.group(1).strip().lower()
        val = m.group(2).strip()
        if key and val and len(val) < 500:
            pairs[key] = val
    return pairs


def semantic_similarity(a: str, b: str) -> float:
    """Heuristic semantic similarity: blend of fuzzy ratio, token jaccard,
    number agreement and key-value agreement. 0..1."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    fr = fuzzy_ratio(a, b)
    tj = token_jaccard(a, b)
    nums_a, nums_b = extract_numbers(a), extract_numbers(b)
    if nums_a or nums_b:
        # numbers must line up in count and value for high similarity
        if len(nums_a) != len(nums_b):
            num_score = 0.2
        else:
            close = [numeric_close(x, y)[0] for x, y in zip(sorted(nums_a), sorted(nums_b))]
            num_score = sum(close) / len(close) if close else 0.5
    else:
        num_score = 0.75  # neutral when no numbers present
    kv_a, kv_b = extract_key_values(a), extract_key_values(b)
    shared = set(kv_a) & set(kv_b)
    if shared:
        kv_score = sum(1 for k in shared
                       if fuzzy_ratio(kv_a[k], kv_b[k]) > 0.85) / len(shared)
    else:
        kv_score = 0.75
    return round(0.4 * fr + 0.3 * tj + 0.2 * num_score + 0.1 * kv_score, 4)


# ---------------------------------------------------------------------------
# Temporal fact store
# ---------------------------------------------------------------------------

@dataclass
class VerifiedFact:
    """A fact the agent verified at some point in the past."""
    key: str
    value: Any
    source: str                    # e.g. "shell:df -h", "web_fetch:example.com"
    verified_at: float             # epoch seconds
    confidence: float
    ttl_seconds: Optional[float] = None  # None = never expires
    tags: List[str] = field(default_factory=list)

    def is_expired(self, now: Optional[float] = None) -> bool:
        if self.ttl_seconds is None:
            return False
        return (now or time.time()) - self.verified_at > self.ttl_seconds

    def age_seconds(self, now: Optional[float] = None) -> float:
        return (now or time.time()) - self.verified_at

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key, "value": self.value, "source": self.source,
            "verified_at": self.verified_at, "confidence": self.confidence,
            "ttl_seconds": self.ttl_seconds, "tags": self.tags,
        }


class TemporalFactStore:
    """Append-only store of verified facts with TTL and contradiction lookup.

    Facts are keyed by a normalized fact key (e.g. "disk_free:/" or
    "http_status:https://example.com"). When a new result arrives, the
    checker asks the store for the latest non-expired fact with the same
    key and compares.
    """

    def __init__(self) -> None:
        self._facts: Dict[str, List[VerifiedFact]] = {}

    @staticmethod
    def normalize_key(key: str) -> str:
        return re.sub(r"\s+", " ", key.strip().lower())

    def record(self, key: str, value: Any, source: str,
               confidence: float = 1.0,
               ttl_seconds: Optional[float] = None,
               tags: Optional[List[str]] = None) -> VerifiedFact:
        fact = VerifiedFact(
            key=self.normalize_key(key), value=value, source=source,
            verified_at=time.time(), confidence=float(confidence),
            ttl_seconds=ttl_seconds, tags=tags or [])
        self._facts.setdefault(fact.key, []).append(fact)
        return fact

    def latest(self, key: str, include_expired: bool = False) -> Optional[VerifiedFact]:
        facts = self._facts.get(self.normalize_key(key), [])
        now = time.time()
        for fact in reversed(facts):
            if include_expired or not fact.is_expired(now):
                return fact
        return None

    def history(self, key: str) -> List[VerifiedFact]:
        return list(self._facts.get(self.normalize_key(key), []))

    def keys(self) -> List[str]:
        return sorted(self._facts.keys())

    def prune_expired(self) -> int:
        """Remove expired facts. Returns number removed."""
        now = time.time()
        removed = 0
        for key in list(self._facts.keys()):
            kept = [f for f in self._facts[key] if not f.is_expired(now)]
            removed += len(self._facts[key]) - len(kept)
            if kept:
                self._facts[key] = kept
            else:
                del self._facts[key]
        return removed

    def to_dict(self) -> Dict[str, List[Dict[str, Any]]]:
        return {k: [f.to_dict() for f in v] for k, v in self._facts.items()}

    @classmethod
    def from_dict(cls, data: Dict[str, List[Dict[str, Any]]]) -> "TemporalFactStore":
        store = cls()
        for key, facts in data.items():
            for f in facts:
                store._facts.setdefault(key, []).append(VerifiedFact(**f))
        return store

# ---------------------------------------------------------------------------
# Conflict resolution strategies
# ---------------------------------------------------------------------------

class ConflictResolutionStrategy:
    """Policy for resolving a detected contradiction.

    Strategies:
        newest_wins      trust the most recent source
        highest_confidence_wins  trust the source with the best verification score
        majority_wins    trust the value most sources agree on
        verified_fact_wins  a TemporalFactStore fact always beats a new claim
        escalate         do not resolve automatically; flag for the agent/user
    """

    def __init__(self, strategy: str = "highest_confidence_wins") -> None:
        valid = {"newest_wins", "highest_confidence_wins", "majority_wins",
                 "verified_fact_wins", "escalate"}
        if strategy not in valid:
            raise ValueError(f"unknown strategy {strategy!r}; choose from {sorted(valid)}")
        self.strategy = strategy

    def resolve(self, contradiction: Contradiction,
                candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Resolve a contradiction given candidate values.

        Each candidate: {"value": ..., "source": ..., "confidence": ...,
        "timestamp": ...}. Returns {"winner": candidate, "reason": str}.
        """
        if not candidates:
            return {"winner": None, "reason": "no candidates to resolve between"}
        if self.strategy == "escalate":
            return {"winner": None,
                    "reason": "escalated: manual review required"}
        if self.strategy == "newest_wins":
            winner = max(candidates, key=lambda c: c.get("timestamp", 0))
            return {"winner": winner, "reason": "newest source wins"}
        if self.strategy == "highest_confidence_wins":
            winner = max(candidates, key=lambda c: c.get("confidence", 0))
            return {"winner": winner,
                    "reason": f"highest verification confidence ({winner.get('confidence')}) wins"}
        if self.strategy == "verified_fact_wins":
            for c in candidates:
                if c.get("is_verified_fact"):
                    return {"winner": c, "reason": "previously verified fact wins over new claim"}
            winner = max(candidates, key=lambda c: c.get("confidence", 0))
            return {"winner": winner, "reason": "no verified fact present; fell back to confidence"}
        # majority_wins
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for c in candidates:
            key = json.dumps(c.get("value"), sort_keys=True, default=str)
            groups.setdefault(key, []).append(c)
        biggest = max(groups.values(), key=len)
        if len(biggest) > len(candidates) / 2:
            winner = max(biggest, key=lambda c: c.get("confidence", 0))
            return {"winner": winner,
                    "reason": f"majority ({len(biggest)}/{len(candidates)}) agrees"}
        winner = max(candidates, key=lambda c: c.get("confidence", 0))
        return {"winner": winner,
                "reason": "no majority; fell back to highest confidence"}


# ---------------------------------------------------------------------------
# ConsistencyChecker
# ---------------------------------------------------------------------------

class ConsistencyChecker:
    """Runs consistency analyses over tool results.

    Typical wiring in the control loop:
        checker = ConsistencyChecker(fact_store)
        report = checker.analyze(tool_name, result, run_history)
        if not report.consistent: <retry / escalate>
        else: fact_store.record(new verified facts)
    """

    # below this semantic similarity, two texts are "contradicting"
    CONTRADICTION_THRESHOLD = 0.45
    # below this but above the threshold: suspicious, medium severity
    SUSPICIOUS_THRESHOLD = 0.65

    def __init__(self,
                 fact_store: Optional[TemporalFactStore] = None,
                 resolution: Optional[ConflictResolutionStrategy] = None) -> None:
        self.fact_store = fact_store or TemporalFactStore()
        self.resolution = resolution or ConflictResolutionStrategy("highest_confidence_wins")
        self._run_history: List[Dict[str, Any]] = []

    # -- run history --------------------------------------------------------
    def record_run(self, tool_name: str, inputs: Dict[str, Any],
                   output_text: str, confidence: float = 1.0) -> None:
        """Record a tool run for cross-run consistency analysis."""
        self._run_history.append({
            "tool_name": tool_name,
            "inputs_hash": self._hash_inputs(inputs),
            "inputs": inputs,
            "output_text": output_text,
            "confidence": float(confidence),
            "timestamp": time.time(),
        })
        if len(self._run_history) > 500:
            self._run_history = self._run_history[-500:]

    @staticmethod
    def _hash_inputs(inputs: Dict[str, Any]) -> str:
        try:
            canonical = json.dumps(inputs, sort_keys=True, default=str)
        except Exception:
            canonical = repr(inputs)
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    def runs_for(self, tool_name: str, inputs: Dict[str, Any],
                 limit: int = 10) -> List[Dict[str, Any]]:
        h = self._hash_inputs(inputs)
        return [r for r in reversed(self._run_history)
                if r["tool_name"] == tool_name and r["inputs_hash"] == h][:limit]

    # -- individual analyses ------------------------------------------------
    def check_cross_run(self, tool_name: str, inputs: Dict[str, Any],
                        output_text: str) -> ConsistencyReport:
        """Same tool + same inputs should produce similar outputs."""
        contradictions: List[Contradiction] = []
        notes: List[str] = []
        prior = self.runs_for(tool_name, inputs, limit=5)
        if not prior:
            return ConsistencyReport(True, 1.0, [],
                                     ["no prior runs with identical inputs"])
        sims = []
        for run in prior:
            sim = semantic_similarity(output_text, run["output_text"])
            sims.append(sim)
            if sim < self.CONTRADICTION_THRESHOLD:
                contradictions.append(Contradiction(
                    description=(
                        f"same tool+inputs produced a very different output "
                        f"(similarity {sim:.2f})"),
                    sources=[f"current run", f"prior run @ {datetime.fromtimestamp(run['timestamp']).isoformat()}"],
                    severity=ContradictionSeverity.HIGH,
                    resolution=("Re-run the tool once more. If 2 of 3 agree, trust the "
                                "majority; otherwise treat the tool as non-deterministic "
                                "and widen tolerance or pick a deterministic tool."),
                    evidence={"similarity": sim, "prior_confidence": run["confidence"]},
                ))
            elif sim < self.SUSPICIOUS_THRESHOLD:
                notes.append(f"prior run output only {sim:.2f} similar (suspicious drift)")
        score = round(sum(sims) / len(sims), 4) if sims else 1.0
        return ConsistencyReport(
            consistent=len(contradictions) == 0,
            score=score, contradictions=contradictions, notes=notes)

    def check_temporal(self, fact_key: str, new_value: Any,
                       new_source: str,
                       numeric_tolerance: float = 0.02) -> ConsistencyReport:
        """New value must not contradict the stored verified fact."""
        prior = self.fact_store.latest(fact_key)
        if prior is None:
            return ConsistencyReport(True, 1.0, [],
                                     [f"no prior verified fact for {fact_key!r}"])
        old, new = prior.value, new_value
        contradiction: Optional[Contradiction] = None
        score = 1.0

        if isinstance(old, bool) or isinstance(new, bool):
            agree = old is new
            score = 1.0 if agree else 0.0
            detail = "boolean flip"
        elif isinstance(old, (int, float)) and isinstance(new, (int, float)):
            close, rel = numeric_close(float(old), float(new), rel_tol=numeric_tolerance)
            agree = close
            score = round(max(0.0, 1.0 - rel * 5), 4) if not close else 1.0
            detail = f"numeric rel-diff {rel:.4f}"
        elif isinstance(old, dict) and isinstance(new, dict):
            shared = set(old) & set(new)
            if not shared:
                agree, score, detail = False, 0.3, "no shared keys"
            else:
                per_key = []
                for k in shared:
                    ok, _ = self._values_agree_loose(old[k], new[k], numeric_tolerance)
                    per_key.append(ok)
                agree = all(per_key)
                score = round(sum(per_key) / len(per_key), 4)
                detail = f"{sum(per_key)}/{len(per_key)} shared keys agree"
        else:
            sim = semantic_similarity(str(old), str(new))
            agree = sim >= self.SUSPICIOUS_THRESHOLD
            score = sim
            detail = f"text similarity {sim:.2f}"

        if not agree:
            severity = (ContradictionSeverity.CRITICAL
                        if prior.confidence >= 0.85 else ContradictionSeverity.HIGH)
            contradiction = Contradiction(
                description=(f"new value for {fact_key!r} contradicts verified fact "
                             f"({detail})"),
                sources=[f"verified fact from {prior.source}",
                         f"new claim from {new_source}"],
                severity=severity,
                resolution=self._temporal_resolution(prior, new_source),
                evidence={"old_value": str(old)[:300], "new_value": str(new)[:300],
                          "fact_age_seconds": round(prior.age_seconds(), 1),
                          "fact_confidence": prior.confidence},
            )
        return ConsistencyReport(
            consistent=contradiction is None, score=score,
            contradictions=[contradiction] if contradiction else [],
            notes=[] if contradiction else [f"consistent with verified fact ({detail})"])

    @staticmethod
    def _temporal_resolution(prior: VerifiedFact, new_source: str) -> str:
        if prior.age_seconds() < 60 and prior.confidence >= 0.9:
            return ("The verified fact is fresh (<60s) and high-confidence: the new "
                    "claim is almost certainly wrong. Re-run the tool that produced "
                    "the new claim.")
        return ("Re-verify with an independent tool. If the new claim reproduces, "
                "update the fact (the world may have changed); otherwise discard "
                "the new claim.")

    def check_cross_tool(self, claims: List[Dict[str, Any]]) -> ConsistencyReport:
        """Multiple tools claiming the same fact must agree.

        claims: [{"tool": ..., "value": ..., "confidence": ...}, ...]
        """
        contradictions: List[Contradiction] = []
        notes: List[str] = []
        if len(claims) < 2:
            return ConsistencyReport(True, 1.0, [], ["need >= 2 claims to compare"])
        # pairwise agreement
        sims: List[float] = []
        for i in range(len(claims)):
            for j in range(i + 1, len(claims)):
                a, b = claims[i], claims[j]
                ok, detail = self._values_agree_loose(a["value"], b["value"])
                sim = 1.0 if ok else semantic_similarity(str(a["value"]), str(b["value"]))
                sims.append(sim)
                if not ok and sim < self.CONTRADICTION_THRESHOLD:
                    contradictions.append(Contradiction(
                        description=(f"{a['tool']} and {b['tool']} disagree on the same "
                                     f"fact ({detail})"),
                        sources=[a["tool"], b["tool"]],
                        severity=ContradictionSeverity.MEDIUM,
                        resolution=("Add a third independent source and apply "
                                    "majority_wins, or trust the higher-confidence tool."),
                        evidence={"value_a": str(a["value"])[:300],
                                  "value_b": str(b["value"])[:300],
                                  "similarity": round(sim, 4)},
                    ))
                elif not ok:
                    notes.append(f"{a['tool']} vs {b['tool']}: partial disagreement ({detail})")
        score = round(sum(sims) / len(sims), 4) if sims else 1.0
        return ConsistencyReport(len(contradictions) == 0, score,
                                 contradictions, notes)

    def check_internal(self, text: str) -> ConsistencyReport:
        """Detect self-contradictions inside a single result text.

        Looks for: numbers that disagree about the same quantity, direct
        negation pairs ("X is true" / "X is false"), and key:value pairs
        stated twice with different values.
        """
        contradictions: List[Contradiction] = []
        notes: List[str] = []
        if not text or not text.strip():
            return ConsistencyReport(True, 1.0, [], ["empty text: nothing to check"])

        # 1. repeated key:value with different values
        kv = extract_key_values(text)
        # extract_key_values keeps the last occurrence; re-scan manually for repeats
        seen: Dict[str, List[str]] = {}
        for m in re.finditer(r"(?m)^\s*([A-Za-z_][\w .\-]{0,60}?)\s*[:=]\s*(.+?)\s*$", text):
            key = m.group(1).strip().lower()
            val = m.group(2).strip()
            if key and val and len(val) < 500:
                seen.setdefault(key, []).append(val)
        for key, vals in seen.items():
            uniq = {v.lower() for v in vals}
            if len(uniq) > 1:
                # allow numeric tolerance
                try:
                    nums = [float(v.replace(",", "")) for v in vals]
                    if all(numeric_close(nums[0], n)[0] for n in nums[1:]):
                        continue
                except ValueError:
                    pass
                contradictions.append(Contradiction(
                    description=f"result states {key!r} with conflicting values: {vals[:3]}",
                    sources=["same result, different lines"],
                    severity=ContradictionSeverity.MEDIUM,
                    resolution="Prefer the value from the most specific/structured part of the output; re-run to disambiguate.",
                    evidence={"key": key, "values": vals[:5]},
                ))

        # 2. negation pairs: "X is enabled" vs "X is not enabled"
        sentences = re.split(r"[.\n;]+", text)
        norm = [re.sub(r"\s+", " ", s.strip().lower()) for s in sentences if s.strip()]
        for i, s1 in enumerate(norm):
            m1 = re.match(r"(.+?)\s+is\s+(not\s+)?(true|false|enabled|disabled|available|unavailable|up|down|running|stopped|open|closed)\s*$", s1)
            if not m1:
                continue
            subject, neg1, state1 = m1.group(1), bool(m1.group(2)), m1.group(3)
            for s2 in norm[i + 1:]:
                m2 = re.match(r"(.+?)\s+is\s+(not\s+)?(true|false|enabled|disabled|available|unavailable|up|down|running|stopped|open|closed)\s*$", s2)
                if not m2 or m2.group(1) != subject or m2.group(3) != state1:
                    continue
                if bool(m2.group(2)) != neg1:
                    contradictions.append(Contradiction(
                        description=f"result both asserts and denies: {subject!r} is {state1}",
                        sources=["same result, different sentences"],
                        severity=ContradictionSeverity.HIGH,
                        resolution="The output is self-contradictory; re-run the tool and inspect raw output.",
                        evidence={"sentence_a": s1[:200], "sentence_b": s2[:200]},
                    ))
                    break

        # 3. total/summary lines disagreeing with itemized numbers
        numbers = extract_numbers(text)
        total_m = re.search(r"(?im)^\s*total\s*[:=]\s*([0-9,]+(?:\.\d+)?)", text)
        if total_m and len(numbers) >= 3:
            try:
                claimed_total = float(total_m.group(1).replace(",", ""))
                # sum all numbers except the claimed total itself
                rest = [n for n in numbers if n != claimed_total]
                s = sum(rest[:50])
                if rest and not numeric_close(s, claimed_total, rel_tol=0.05)[0] and s != 0:
                    notes.append(
                        f"claimed total {claimed_total} differs from sum of parts {s:.2f} (may be fine if parts are not additive)")
            except ValueError:
                pass

        score = 1.0 if not contradictions else round(1.0 - 0.4 * len(contradictions), 2)
        score = max(0.0, score)
        return ConsistencyReport(len(contradictions) == 0, score, contradictions, notes)

    # -- combined analysis --------------------------------------------------
    def analyze(self,
                tool_name: str,
                result_text: str,
                inputs: Optional[Dict[str, Any]] = None,
                fact_key: Optional[str] = None,
                fact_value: Any = None,
                cross_tool_claims: Optional[List[Dict[str, Any]]] = None,
                record: bool = True) -> ConsistencyReport:
        """Run all applicable consistency analyses and merge the reports."""
        sub: List[ConsistencyReport] = []
        sub.append(self.check_internal(result_text))
        if inputs is not None:
            sub.append(self.check_cross_run(tool_name, inputs, result_text))
        if fact_key is not None:
            sub.append(self.check_temporal(fact_key, fact_value, tool_name))
        if cross_tool_claims:
            sub.append(self.check_cross_tool(cross_tool_claims))

        contradictions = [c for r in sub for c in r.contradictions]
        notes = [n for r in sub for n in r.notes]
        score = round(sum(r.score for r in sub) / len(sub), 4) if sub else 1.0
        merged = ConsistencyReport(len(contradictions) == 0, score,
                                   contradictions, notes)
        if record:
            self.record_run(tool_name, inputs or {}, result_text, score)
        return merged

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _values_agree_loose(a: Any, b: Any,
                            numeric_tolerance: float = 0.02) -> Tuple[bool, str]:
        if a is None or b is None:
            return False, "one side missing"
        if isinstance(a, bool) or isinstance(b, bool):
            return (a is b, "booleans match" if a is b else "booleans differ")
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            close, rel = numeric_close(float(a), float(b), rel_tol=numeric_tolerance)
            return close, f"numeric rel-diff {rel:.4f}"
        sa = re.sub(r"\s+", " ", str(a).strip().lower())
        sb = re.sub(r"\s+", " ", str(b).strip().lower())
        if sa == sb:
            return True, "strings equal after normalization"
        sim = semantic_similarity(sa, sb)
        return sim >= 0.8, f"text similarity {sim:.2f}"

    def resolve_contradiction(self, contradiction: Contradiction,
                              candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Apply the configured resolution strategy."""
        return self.resolution.resolve(contradiction, candidates)


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------

_default_checker: Optional[ConsistencyChecker] = None


def get_default_checker() -> ConsistencyChecker:
    global _default_checker
    if _default_checker is None:
        _default_checker = ConsistencyChecker()
    return _default_checker

# ---------------------------------------------------------------------------
# Drift detection: numeric metrics tracked over time
# ---------------------------------------------------------------------------

@dataclass
class MetricSample:
    name: str
    value: float
    timestamp: float = field(default_factory=time.time)
    source: str = ""


class DriftDetector:
    """Tracks a numeric metric across runs and flags sudden drift.

    Useful for metrics the agent monitors repeatedly (disk usage, latency,
    queue length): a sudden jump that contradicts the recent trend is
    either a real incident or a broken measurement, and both deserve a
    contradiction report rather than silent acceptance.
    """

    def __init__(self, window: int = 20) -> None:
        self.window = max(3, window)
        self._series: Dict[str, List[MetricSample]] = {}

    def add(self, name: str, value: float, source: str = "") -> MetricSample:
        sample = MetricSample(name=name, value=float(value), source=source)
        series = self._series.setdefault(name, [])
        series.append(sample)
        if len(series) > self.window:
            del series[:len(series) - self.window]
        return sample

    def stats(self, name: str) -> Optional[Dict[str, float]]:
        series = self._series.get(name, [])
        if len(series) < 3:
            return None
        vals = [s.value for s in series]
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        return {
            "n": float(len(vals)),
            "mean": mean,
            "stdev": math.sqrt(var),
            "min": min(vals),
            "max": max(vals),
            "last": vals[-1],
        }

    def check_drift(self, name: str, value: float,
                    sigma_threshold: float = 3.0,
                    rel_jump_threshold: float = 0.5) -> ConsistencyReport:
        """Check a new sample against history; flag drift as contradiction."""
        value = float(value)
        stats = self.stats(name)
        if stats is None or stats["n"] < 3:
            self.add(name, value)
            return ConsistencyReport(True, 1.0, [],
                                     [f"not enough history for {name!r}; recorded"])
        mean, stdev = stats["mean"], stats["stdev"]
        z = abs(value - mean) / stdev if stdev > 1e-12 else 0.0
        rel_jump = abs(value - stats["last"]) / max(abs(stats["last"]), 1e-12)
        drifted = z > sigma_threshold or rel_jump > rel_jump_threshold
        self.add(name, value)
        if not drifted:
            return ConsistencyReport(True, 1.0, [],
                                     [f"{name}={value} within normal band "
                                      f"(mean {mean:.3f}, stdev {stdev:.3f})"])
        severity = (ContradictionSeverity.HIGH if z > sigma_threshold * 2
                    else ContradictionSeverity.MEDIUM)
        contradiction = Contradiction(
            description=(f"metric {name!r} drifted: {value} vs recent "
                         f"mean {mean:.4f} (z={z:.2f}, jump {rel_jump:.1%})"),
            sources=["metric history", "current sample"],
            severity=severity,
            resolution=("Confirm the measurement is real (re-run the probe). If real, "
                        "treat as an incident; if the probe changed, reset the baseline."),
            evidence={"value": value, "mean": mean, "stdev": stdev,
                      "z_score": round(z, 3), "rel_jump": round(rel_jump, 4),
                      "n_history": int(stats["n"])},
        )
        score = max(0.0, round(1.0 - min(z / 10.0, 0.9), 3))
        return ConsistencyReport(False, score, [contradiction], [])

    def reset(self, name: str) -> None:
        self._series.pop(name, None)


# ---------------------------------------------------------------------------
# Claim extraction: turn result text into atomic checkable claims
# ---------------------------------------------------------------------------

@dataclass
class AtomicClaim:
    """One atomic factual claim extracted from a result."""
    text: str
    kind: str  # "equality" | "inequality" | "existence" | "quantity" | "status" | "other"
    subject: str = ""
    value: str = ""
    confidence: float = 0.5

    def to_dict(self) -> Dict[str, Any]:
        return {"text": self.text, "kind": self.kind, "subject": self.subject,
                "value": self.value, "confidence": self.confidence}


class ClaimExtractor:
    """Extracts atomic claims from tool output text.

    This is heuristic NLP-lite: sentence splitting plus pattern matching for
    common claim shapes. It is intentionally conservative: it would rather
    miss a claim than invent one.
    """

    _SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])|\n+")

    _PATTERNS: List[Tuple[str, re.Pattern, float]] = [
        ("equality", re.compile(
            r"^(.{2,80}?)\s+(?:is|are|was|were|equals?)\s+(.+?)[.!]?\s*$", re.I), 0.7),
        ("inequality", re.compile(
            r"^(.{2,80}?)\s+(?:is\s+not|are\s+not|was\s+not|were\s+not|isn't|aren't|wasn't|weren't)\s+(.+?)[.!]?\s*$", re.I), 0.7),
        ("quantity", re.compile(
            r"(.{2,60}?)\s*[:=]\s*([+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:\s*(?:bytes|KB|MB|GB|ms|s|%|percent|x))?)",
            re.I), 0.8),
        ("status", re.compile(
            r"^(.{2,60}?)\s*[:\-]\s*(ok|okay|success|successful|failed|failure|error|up|down|running|stopped|enabled|disabled|active|inactive|true|false|yes|no)\s*$",
            re.I), 0.75),
        ("existence", re.compile(
            r"(?:found|exists|present|created|deleted|missing|not found)\b.{0,80}", re.I), 0.6),
    ]

    def extract(self, text: str, max_claims: int = 50) -> List[AtomicClaim]:
        claims: List[AtomicClaim] = []
        if not text or not text.strip():
            return claims
        sentences = [s.strip() for s in self._SENT_SPLIT.split(text) if s.strip()]
        for sent in sentences:
            if len(claims) >= max_claims:
                break
            if len(sent) > 400 or len(sent) < 4:
                continue
            claim = self._classify(sent)
            if claim is not None:
                claims.append(claim)
        # de-duplicate near-identical claims
        unique: List[AtomicClaim] = []
        for c in claims:
            if all(fuzzy_ratio(c.text, u.text) < 0.9 for u in unique):
                unique.append(c)
        return unique

    def _classify(self, sentence: str) -> Optional[AtomicClaim]:
        for kind, pattern, conf in self._PATTERNS:
            m = pattern.search(sentence)
            if not m:
                continue
            groups = m.groups()
            subject = groups[0].strip() if len(groups) > 0 else ""
            value = groups[1].strip() if len(groups) > 1 else ""
            # reject garbage subjects
            if len(subject) < 2 or len(subject.split()) > 12:
                if kind not in ("existence",):
                    continue
            return AtomicClaim(text=sentence[:300], kind=kind,
                               subject=subject[:120], value=value[:200],
                               confidence=conf)
        # fallback: long declarative sentences become "other" claims at low confidence
        if re.match(r"^[A-Z].{20,200}[.!?]$", sentence):
            return AtomicClaim(text=sentence[:300], kind="other",
                               confidence=0.35)
        return None

    def claims_about(self, claims: List[AtomicClaim], subject: str) -> List[AtomicClaim]:
        """Filter claims mentioning a subject (fuzzy)."""
        sub = subject.lower()
        return [c for c in claims
                if sub in c.text.lower() or sub in c.subject.lower()
                or fuzzy_ratio(sub, c.subject.lower()) > 0.7]


# ---------------------------------------------------------------------------
# Snapshot comparison: file/content snapshots across time
# ---------------------------------------------------------------------------

@dataclass
class Snapshot:
    """A captured snapshot of a file, directory listing, or text blob."""
    label: str
    content: str
    captured_at: float = field(default_factory=time.time)
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        self.sha256 = hashlib.sha256(self.content.encode("utf-8", "replace")).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return {"label": self.label, "captured_at": self.captured_at,
                "sha256": self.sha256, "size": len(self.content)}


def diff_snapshots(old: Snapshot, new: Snapshot,
                   context_lines: int = 3) -> Dict[str, Any]:
    """Line diff between two snapshots with a change summary."""
    import difflib as _difflib
    old_lines = old.content.splitlines()
    new_lines = new.content.splitlines()
    if old.sha256 == new.sha256:
        return {"identical": True, "added": 0, "removed": 0, "diff": ""}
    diff = list(_difflib.unified_diff(old_lines, new_lines,
                                      fromfile=f"a/{old.label}",
                                      tofile=f"b/{new.label}",
                                      n=context_lines, lineterm=""))
    added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
    # cap diff output: keep head + tail
    body = "\n".join(diff)
    if len(body) > 8000:
        body = body[:4000] + "\n... [truncated] ...\n" + body[-3000:]
    return {"identical": False, "added": added, "removed": removed, "diff": body,
            "old_sha": old.sha256[:16], "new_sha": new.sha256[:16]}


class SnapshotTracker:
    """Tracks snapshots of watched paths/contents and reports unexpected change."""

    def __init__(self) -> None:
        self._snapshots: Dict[str, Snapshot] = {}

    def capture(self, label: str, content: str) -> Snapshot:
        snap = Snapshot(label=label, content=content)
        self._snapshots[label] = snap
        return snap

    def check(self, label: str, content: str,
              expect_change: bool = False) -> ConsistencyReport:
        """Compare new content against the stored snapshot."""
        new = Snapshot(label=label, content=content)
        old = self._snapshots.get(label)
        if old is None:
            self._snapshots[label] = new
            return ConsistencyReport(True, 1.0, [],
                                     [f"baseline captured for {label!r}"])
        result = diff_snapshots(old, new)
        self._snapshots[label] = new
        if result["identical"]:
            return ConsistencyReport(True, 1.0, [],
                                     [f"{label!r} unchanged"])
        if expect_change:
            return ConsistencyReport(
                True, 0.95, [],
                [f"{label!r} changed as expected (+{result['added']}/-{result['removed']} lines)"])
        contradiction = Contradiction(
            description=(f"{label!r} changed unexpectedly "
                         f"(+{result['added']}/-{result['removed']} lines)"),
            sources=["previous snapshot", "current snapshot"],
            severity=ContradictionSeverity.MEDIUM,
            resolution=("If the agent itself made the change, this is expected: mark "
                        "expect_change=True. Otherwise investigate who/what changed it."),
            evidence={"diff_head": result["diff"][:2000],
                      "old_sha": result["old_sha"], "new_sha": result["new_sha"]},
        )
        return ConsistencyReport(False, 0.4, [contradiction], [])
