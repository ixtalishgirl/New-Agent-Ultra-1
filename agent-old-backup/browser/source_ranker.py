"""Source ranker — ranks web sources by reliability and relevance.

Per ChatGPT spec: agent/browser/source_ranker.py
Prefer official docs. Score by domain authority, freshness, relevance.
"""
from __future__ import annotations

import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class RankedSource:
    url: str
    title: str = ""
    snippet: str = ""
    score: float = 0.0
    reasons: List[str] = field(default_factory=list)
    domain: str = ""

    def __post_init__(self) -> None:
        if not self.domain:
            try:
                self.domain = urllib.parse.urlparse(
                    self.url).netloc.lower()
            except Exception:
                pass

    def to_dict(self) -> Dict[str, Any]:
        return {"url": self.url, "title": self.title,
                "snippet": self.snippet[:300],
                "score": round(self.score, 3),
                "reasons": self.reasons, "domain": self.domain}


# Domain authority tiers
_TIER_1 = {  # official docs — highest trust
    "docs.python.org", "developer.mozilla.org", "docs.github.com",
    "kubernetes.io", "docs.docker.com", "go.dev", "docs.rs",
    "learn.microsoft.com", "developer.apple.com",
    "docs.gitlab.com", "www.postgresql.org", "dev.mysql.com",
}
_TIER_2 = {  # reputable: Q&A, code, reference
    "stackoverflow.com", "github.com", "gitlab.com",
    "arxiv.org", "en.wikipedia.org", "medium.com",
    "dev.to", "realpython.com",
}
_TIER_3_PENALTY = {  # low trust signals
    "spam", "clickbait", "free-money", "crack", "warez",
}


class SourceRanker:
    """Ranks sources for research quality."""

    def __init__(self):
        self._tier1 = _TIER_1
        self._tier2 = _TIER_2

    def rank(self, sources: List[Dict[str, str]],
             query: str) -> List[RankedSource]:
        """Rank raw search results. Each dict: url, title, snippet."""
        ranked = []
        for s in sources:
            rs = RankedSource(
                url=s.get("url", ""), title=s.get("title", ""),
                snippet=s.get("snippet", ""))
            rs.score, rs.reasons = self._score(rs, query)
            ranked.append(rs)
        ranked.sort(key=lambda r: -r.score)
        return ranked

    def _score(self, src: RankedSource,
               query: str) -> Tuple[float, List[str]]:
        score = 0.5
        reasons: List[str] = []
        domain = src.domain

        # 1. Domain authority (0.0 - 0.4)
        if any(domain.endswith(t) for t in self._tier1):
            score += 0.35
            reasons.append("official documentation")
        elif any(domain.endswith(t) for t in self._tier2):
            score += 0.2
            reasons.append("reputable source")
        if any(bad in domain for bad in _TIER_3_PENALTY):
            score -= 0.3
            reasons.append("low-trust domain signals")

        # 2. HTTPS bonus
        if src.url.startswith("https://"):
            score += 0.05

        # 3. Relevance: query term overlap (0.0 - 0.3)
        q_terms = set(re.findall(r"\w+", query.lower()))
        q_terms -= {"the", "a", "an", "of", "for", "to", "in", "how",
                    "what", "why", "is", "are", "with"}
        text = (src.title + " " + src.snippet).lower()
        if q_terms:
            hits = sum(1 for t in q_terms if t in text)
            rel = hits / len(q_terms)
            score += 0.3 * rel
            if rel > 0.5:
                reasons.append(f"high query relevance ({hits}/{len(q_terms)})")

        # 4. Title quality: specific beats generic
        if src.title and len(src.title) > 10:
            score += 0.05
        if re.search(r"\b(official|documentation|guide|reference)\b",
                     src.title, re.I):
            score += 0.05
            reasons.append("documentation-like title")

        # 5. Snippet depth
        if len(src.snippet) > 200:
            score += 0.05

        return max(0.0, min(1.0, score)), reasons

    def top(self, sources: List[Dict[str, str]], query: str,
            k: int = 5, diverse: bool = True) -> List[RankedSource]:
        """Top-k with domain diversity."""
        ranked = self.rank(sources, query)
        if not diverse:
            return ranked[:k]
        out: List[RankedSource] = []
        seen: set = set()
        for r in ranked:
            if r.domain not in seen:
                out.append(r)
                seen.add(r.domain)
            if len(out) >= k:
                break
        # Fill if diversity left gaps
        for r in ranked:
            if r not in out and len(out) < k:
                out.append(r)
        return out

    def filter_trusted(self, sources: List[Dict[str, str]],
                       min_score: float = 0.6) -> List[RankedSource]:
        """Only sources above a trust threshold (query-agnostic)."""
        out = []
        for s in sources:
            rs = RankedSource(url=s.get("url", ""),
                              title=s.get("title", ""),
                              snippet=s.get("snippet", ""))
            score, reasons = self._score(rs, "")
            # Re-score without query: authority only
            rs.score, rs.reasons = score, reasons
            if score >= min_score:
                out.append(rs)
        out.sort(key=lambda r: -r.score)
        return out
