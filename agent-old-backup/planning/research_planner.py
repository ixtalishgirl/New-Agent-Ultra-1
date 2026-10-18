"""Research planner — RESEARCH phase of the control loop.

Plans web research: generates search queries, prioritizes sources,
triangulates facts across sources, and synthesizes findings.

No personality. No rules. Powers hard-coded via tool registry.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple


class SourceType(Enum):
    OFFICIAL_DOCS = "official_docs"
    API_REFERENCE = "api_reference"
    TUTORIAL = "tutorial"
    FORUM = "forum"
    NEWS = "news"
    CODE_REPO = "code_repo"
    ACADEMIC = "academic"
    UNKNOWN = "unknown"


class SourceReliability(Enum):
    HIGH = 3
    MEDIUM = 2
    LOW = 1


@dataclass
class SearchQuery:
    """A single planned search query."""
    query: str
    intent: str  # what we hope to learn
    priority: int = 2  # 1-3
    expected_source_types: List[str] = field(default_factory=list)
    executed: bool = False
    result_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Source:
    """A discovered information source."""
    url: str
    title: str = ""
    snippet: str = ""
    source_type: str = SourceType.UNKNOWN.value
    reliability: int = SourceReliability.MEDIUM.value
    fetched: bool = False
    fetch_error: str = ""
    content_hash: str = ""
    fetched_at: Optional[float] = None

    def domain(self) -> str:
        try:
            return urllib.parse.urlparse(self.url).netloc.lower()
        except Exception:
            return ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Fact:
    """An atomic fact extracted during research."""
    statement: str
    sources: List[str] = field(default_factory=list)  # urls
    confidence: float = 0.5
    category: str = "general"
    extracted_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ResearchPlan:
    """Complete research plan for a question."""
    id: str
    question: str
    queries: List[SearchQuery] = field(default_factory=list)
    sources: List[Source] = field(default_factory=list)
    facts: List[Fact] = field(default_factory=list)
    status: str = "planned"
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "queries": [q.to_dict() for q in self.queries],
            "sources": [s.to_dict() for s in self.sources],
            "facts": [f.to_dict() for f in self.facts],
            "status": self.status,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }


# ---------------------------------------------------------------------------
# Query generation
# ---------------------------------------------------------------------------

# High-reliability domains (official docs, etc.)
_TRUSTED_DOMAINS = {
    "docs.python.org": (SourceType.OFFICIAL_DOCS, SourceReliability.HIGH),
    "developer.mozilla.org": (SourceType.OFFICIAL_DOCS, SourceReliability.HIGH),
    "docs.github.com": (SourceType.OFFICIAL_DOCS, SourceReliability.HIGH),
    "kubernetes.io": (SourceType.OFFICIAL_DOCS, SourceReliability.HIGH),
    "docs.docker.com": (SourceType.OFFICIAL_DOCS, SourceReliability.HIGH),
    "stackoverflow.com": (SourceType.FORUM, SourceReliability.MEDIUM),
    "github.com": (SourceType.CODE_REPO, SourceReliability.MEDIUM),
    "arxiv.org": (SourceType.ACADEMIC, SourceReliability.HIGH),
    "wikipedia.org": (SourceType.UNKNOWN, SourceReliability.MEDIUM),
}

# Query expansion templates per intent
_QUERY_TEMPLATES = {
    "howto": [
        "{topic} how to",
        "{topic} tutorial",
        "{topic} step by step guide",
        "{topic} example",
    ],
    "docs": [
        "{topic} documentation",
        "{topic} official docs",
        "{topic} API reference",
    ],
    "error": [
        "{topic} error fix",
        "{topic} troubleshooting",
        "{topic} common issues",
    ],
    "compare": [
        "{topic} vs",
        "{topic} comparison",
        "{topic} pros cons",
        "best {topic}",
    ],
    "news": [
        "{topic} latest news",
        "{topic} update 2026",
        "{topic} changelog",
    ],
}


class QueryGenerator:
    """Generates diverse search queries from a research question."""

    def __init__(self, templates: Optional[Dict[str, List[str]]] = None):
        self.templates = templates or _QUERY_TEMPLATES

    def generate(self, question: str,
                 max_queries: int = 8) -> List[SearchQuery]:
        """Generate search queries covering different angles."""
        topic = self._extract_topic(question)
        intent = self._classify_intent(question)
        queries: List[SearchQuery] = []

        # 1. Direct question as query (highest priority)
        queries.append(SearchQuery(
            query=question[:200],
            intent="direct answer",
            priority=3,
        ))

        # 2. Template expansions for detected intent
        for tmpl in self.templates.get(intent, self.templates["howto"]):
            q = tmpl.format(topic=topic)
            if q.lower() not in {x.query.lower() for x in queries}:
                queries.append(SearchQuery(
                    query=q, intent=intent, priority=2))

        # 3. Entity-specific queries
        for entity in self._extract_entities(question):
            q = f"{entity} {topic}"
            if q.lower() not in {x.query.lower() for x in queries}:
                queries.append(SearchQuery(
                    query=q, intent="entity detail", priority=1))

        # 4. Question variants (what/why/how)
        for wh in ["what is", "how does", "why"]:
            if wh not in question.lower():
                q = f"{wh} {topic}"
                if q.lower() not in {x.query.lower() for x in queries}:
                    queries.append(SearchQuery(
                        query=q, intent="background", priority=1))

        # Sort by priority desc, dedupe, cap
        seen: Set[str] = set()
        out: List[SearchQuery] = []
        for q in sorted(queries, key=lambda x: -x.priority):
            key = q.query.lower().strip()
            if key not in seen:
                seen.add(key)
                out.append(q)
            if len(out) >= max_queries:
                break
        return out

    def _extract_topic(self, question: str) -> str:
        # Remove question words and keep the core noun phrase
        q = re.sub(r"^(what|how|why|when|where|who|which|is|are|do|does|can)\b",
                   "", question.lower().strip())
        q = re.sub(r"\?$", "", q).strip()
        # Take up to 6 significant words
        words = [w for w in q.split()
                 if w not in {"the", "a", "an", "of", "for", "to", "in",
                              "on", "with", "about", "me", "please"}]
        return " ".join(words[:6]) or question[:60]

    def _classify_intent(self, question: str) -> str:
        q = question.lower()
        if any(w in q for w in ["error", "fix", "broken", "fail", "issue",
                                "problem", "troubleshoot"]):
            return "error"
        if any(w in q for w in [" vs ", "versus", "compare", "better",
                                "best", "alternative"]):
            return "compare"
        if any(w in q for w in ["news", "update", "latest", "new",
                                "release", "changelog"]):
            return "news"
        if any(w in q for w in ["doc", "api", "reference", "spec"]):
            return "docs"
        return "howto"

    def _extract_entities(self, question: str) -> List[str]:
        # Capitalized words and quoted strings are likely entities
        entities = re.findall(r'"([^"]+)"', question)
        entities += re.findall(r"\b([A-Z][a-zA-Z0-9]+(?:\s+[A-Z][a-zA-Z0-9]+)?)\b",
                               question)
        seen: Set[str] = set()
        out = []
        for e in entities:
            if e.lower() not in seen and len(e) > 1:
                seen.add(e.lower())
                out.append(e)
        return out[:5]


# ---------------------------------------------------------------------------
# Source scoring and selection
# ---------------------------------------------------------------------------

class SourceScorer:
    """Scores sources by reliability and relevance."""

    def __init__(self, trusted: Optional[Dict[str, Tuple]] = None):
        self.trusted = trusted or _TRUSTED_DOMAINS

    def score(self, source: Source, question: str) -> float:
        """0.0-1.0 composite score."""
        rel = self._reliability_score(source)
        rev = self._relevance_score(source, question)
        fresh = self._freshness_score(source)
        # Weighted: reliability 40%, relevance 40%, freshness 20%
        return 0.4 * rel + 0.4 * rev + 0.2 * fresh

    def classify(self, source: Source) -> None:
        """Fill in source_type and reliability from domain."""
        domain = source.domain()
        for trusted_domain, (st, rel) in self.trusted.items():
            if domain.endswith(trusted_domain):
                source.source_type = st.value
                source.reliability = rel.value
                return
        # Heuristics for unknown domains
        if any(x in domain for x in ["docs.", "documentation"]):
            source.source_type = SourceType.OFFICIAL_DOCS.value
        elif "blog" in domain:
            source.source_type = SourceType.TUTORIAL.value
            source.reliability = SourceReliability.LOW.value
        elif any(x in domain for x in ["forum", "reddit", "stack"]):
            source.source_type = SourceType.FORUM.value

    def _reliability_score(self, source: Source) -> float:
        return {3: 1.0, 2: 0.6, 1: 0.3}.get(source.reliability, 0.5)

    def _relevance_score(self, source: Source, question: str) -> float:
        q_words = set(re.findall(r"\w+", question.lower()))
        t_words = set(re.findall(r"\w+",
                                 (source.title + " " + source.snippet).lower()))
        if not q_words:
            return 0.5
        overlap = len(q_words & t_words) / len(q_words)
        return min(1.0, overlap * 2)

    def _freshness_score(self, source: Source) -> float:
        # Prefer recently fetched; unknown age = neutral
        if not source.fetched_at:
            return 0.5
        age_days = (time.time() - source.fetched_at) / 86400
        if age_days < 7:
            return 1.0
        if age_days < 30:
            return 0.8
        if age_days < 365:
            return 0.5
        return 0.3

    def select_top(self, sources: List[Source], question: str,
                   k: int = 5) -> List[Source]:
        """Select top-k sources by score, ensuring type diversity."""
        scored = [(self.score(s, question), s) for s in sources]
        scored.sort(key=lambda x: -x[0])
        # Diversity: prefer different domains
        out: List[Source] = []
        seen_domains: Set[str] = set()
        for _, s in scored:
            if s.domain() not in seen_domains or len(out) >= k * 2:
                out.append(s)
                seen_domains.add(s.domain())
            if len(out) >= k:
                break
        # Fill remaining slots if diversity left gaps
        for _, s in scored:
            if s not in out and len(out) < k:
                out.append(s)
        return out[:k]


# ---------------------------------------------------------------------------
# Fact extraction and triangulation
# ---------------------------------------------------------------------------

class FactExtractor:
    """Extracts atomic facts from source content."""

    # Patterns for factual statements
    _PATTERNS = [
        # "X is Y" definitions
        re.compile(r"([A-Z][^.!?]{5,80}?)\s+is\s+([^.!?]{10,200})[.!?]"),
        # Version numbers
        re.compile(r"(version\s+\d+(?:\.\d+)+)", re.IGNORECASE),
        # Dates
        re.compile(r"(\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}\b)"),
        # "released in YEAR"
        re.compile(r"released\s+(?:in\s+)?(\d{4})", re.IGNORECASE),
        # Numbers with units
        re.compile(r"(\d+(?:\.\d+)?\s*(?:ms|seconds?|minutes?|hours?|days?|GB|MB|KB|%))", re.IGNORECASE),
    ]

    def extract(self, content: str, source_url: str,
                max_facts: int = 20) -> List[Fact]:
        """Extract candidate facts from text content."""
        facts: List[Fact] = []
        # Sentence-level extraction
        sentences = re.split(r"(?<=[.!?])\s+", content)
        for sent in sentences:
            sent = sent.strip()
            if len(sent) < 20 or len(sent) > 400:
                continue
            if self._is_factual_sentence(sent):
                facts.append(Fact(
                    statement=sent[:300],
                    sources=[source_url],
                    confidence=0.5,
                    category=self._categorize(sent),
                ))
            if len(facts) >= max_facts:
                break
        return facts

    def _is_factual_sentence(self, sent: str) -> bool:
        low = sent.lower()
        # Skip opinions, questions, imperatives
        if sent.endswith("?"):
            return False
        if any(low.startswith(w) for w in
               ["i think", "in my opinion", "maybe", "probably",
                "click", "try", "please", "note:"]):
            return False
        # Factual indicators
        indicators = ["is", "are", "was", "were", "has", "have",
                      "released", "version", "supports", "requires",
                      "defined", "used for", "provides"]
        return any(ind in low for ind in indicators)

    def _categorize(self, sent: str) -> str:
        low = sent.lower()
        if any(w in low for w in ["version", "release", "update"]):
            return "version"
        if any(w in low for w in ["error", "fail", "bug", "fix"]):
            return "troubleshooting"
        if any(w in low for w in ["install", "setup", "configure"]):
            return "setup"
        if any(w in low for w in ["api", "function", "method", "class"]):
            return "api"
        return "general"


class FactTriangulator:
    """Cross-checks facts across multiple sources."""

    def __init__(self, similarity_threshold: float = 0.6):
        self.similarity_threshold = similarity_threshold

    def triangulate(self, facts: List[Fact]) -> List[Fact]:
        """Merge duplicate facts, boost confidence for multi-source facts."""
        groups: List[List[Fact]] = []
        for fact in facts:
            placed = False
            for group in groups:
                if self._similar(fact.statement, group[0].statement):
                    group.append(fact)
                    placed = True
                    break
            if not placed:
                groups.append([fact])
        merged: List[Fact] = []
        for group in groups:
            sources: List[str] = []
            for f in group:
                for s in f.sources:
                    if s not in sources:
                        sources.append(s)
            # Confidence: base 0.5, +0.15 per additional independent source
            # (cap 0.95). Independent = different domain.
            domains = {urllib.parse.urlparse(s).netloc for s in sources}
            conf = min(0.95, 0.5 + 0.15 * (len(domains) - 1))
            # Pick the longest statement as canonical
            canonical = max(group, key=lambda f: len(f.statement))
            merged.append(Fact(
                statement=canonical.statement,
                sources=sources,
                confidence=round(conf, 2),
                category=canonical.category,
            ))
        # Sort by confidence desc
        merged.sort(key=lambda f: -f.confidence)
        return merged

    def _similar(self, a: str, b: str) -> bool:
        return self._jaccard(a.lower(), b.lower()) >= self.similarity_threshold

    def _jaccard(self, a: str, b: str) -> float:
        wa = set(re.findall(r"\w+", a))
        wb = set(re.findall(r"\w+", b))
        if not wa or not wb:
            return 0.0
        return len(wa & wb) / len(wa | wb)

    def find_conflicts(self, facts: List[Fact]) -> List[Dict[str, Any]]:
        """Find facts that contradict each other (same topic, low similarity
        but overlapping keywords with negation mismatch)."""
        conflicts = []
        for i, fa in enumerate(facts):
            for fb in facts[i + 1:]:
                if self._contradicts(fa.statement, fb.statement):
                    conflicts.append({
                        "fact_a": fa.statement,
                        "fact_b": fb.statement,
                        "sources_a": fa.sources,
                        "sources_b": fb.sources,
                    })
        return conflicts

    def _contradicts(self, a: str, b: str) -> bool:
        # Heuristic: high keyword overlap + one has negation, other doesn't
        wa = set(re.findall(r"\w+", a.lower()))
        wb = set(re.findall(r"\w+", b.lower()))
        if not wa or not wb:
            return False
        overlap = len(wa & wb) / max(len(wa), len(wb))
        if overlap < 0.5:
            return False
        negations = {"not", "no", "never", "n't", "cannot", "won't",
                     "doesn't", "isn't", "aren't"}
        a_neg = bool(wa & negations)
        b_neg = bool(wb & negations)
        return a_neg != b_neg


# ---------------------------------------------------------------------------
# Research synthesis — turn facts into an answer
# ---------------------------------------------------------------------------

@dataclass
class ResearchSummary:
    question: str
    answer: str
    key_facts: List[Fact]
    sources_used: List[str]
    confidence: float
    conflicts: List[Dict[str, Any]]
    generated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "key_facts": [f.to_dict() for f in self.key_facts],
            "sources_used": self.sources_used,
            "confidence": self.confidence,
            "conflicts": self.conflicts,
            "generated_at": self.generated_at,
        }


class ResearchSynthesizer:
    """Synthesizes triangulated facts into a coherent answer."""

    def synthesize(self, plan: ResearchPlan) -> ResearchSummary:
        """Build an answer from the plan's facts."""
        triangulator = FactTriangulator()
        merged = triangulator.triangulate(plan.facts)
        conflicts = triangulator.find_conflicts(merged)

        # Group facts by category
        by_cat: Dict[str, List[Fact]] = defaultdict(list)
        for f in merged:
            by_cat[f.category].append(f)

        # Build answer sections
        sections: List[str] = []
        for cat in ["general", "api", "setup", "version", "troubleshooting"]:
            facts = by_cat.get(cat, [])
            if not facts:
                continue
            top = facts[:3]
            lines = [f"- {f.statement} "
                     f"(confidence {f.confidence:.0%}, "
                     f"{len(f.sources)} sources)" for f in top]
            sections.append(f"**{cat.title()}**\n" + "\n".join(lines))

        answer = (f"Research on: {plan.question}\n\n" +
                  "\n\n".join(sections))
        if conflicts:
            answer += "\n\n**Conflicting information found:**\n"
            for c in conflicts[:3]:
                answer += f"- {c['fact_a'][:100]} vs {c['fact_b'][:100]}\n"

        # Overall confidence: mean of top-5 fact confidences
        top_conf = [f.confidence for f in merged[:5]]
        overall = sum(top_conf) / len(top_conf) if top_conf else 0.0
        # Penalize for conflicts
        overall = max(0.0, overall - 0.1 * len(conflicts))

        sources = []
        for f in merged[:10]:
            sources.extend(f.sources)
        sources = list(dict.fromkeys(sources))  # dedupe, keep order

        return ResearchSummary(
            question=plan.question,
            answer=answer,
            key_facts=merged[:10],
            sources_used=sources,
            confidence=round(overall, 2),
            conflicts=conflicts,
        )


# ---------------------------------------------------------------------------
# Research orchestrator — full RESEARCH phase
# ---------------------------------------------------------------------------

class ResearchOrchestrator:
    """Runs the complete research phase: plan → queries → sources → facts."""

    def __init__(self,
                 query_gen: Optional[QueryGenerator] = None,
                 scorer: Optional[SourceScorer] = None,
                 extractor: Optional[FactExtractor] = None):
        self.query_gen = query_gen or QueryGenerator()
        self.scorer = scorer or SourceScorer()
        self.extractor = extractor or FactExtractor()
        self.synthesizer = ResearchSynthesizer()
        self.triangulator = FactTriangulator()

    def plan_research(self, question: str,
                      max_queries: int = 8) -> ResearchPlan:
        """Create a research plan without executing it."""
        import uuid as _uuid
        plan = ResearchPlan(
            id=f"res_{_uuid.uuid4().hex[:10]}",
            question=question,
        )
        plan.queries = self.query_gen.generate(question, max_queries)
        return plan

    def register_search_results(
            self, plan: ResearchPlan,
            query: str,
            results: List[Dict[str, str]]) -> None:
        """Register raw search results (title/url/snippet) for a query."""
        for r in results:
            url = r.get("url", "")
            if not url:
                continue
            if any(s.url == url for s in plan.sources):
                continue
            src = Source(url=url, title=r.get("title", ""),
                         snippet=r.get("snippet", ""))
            self.scorer.classify(src)
            plan.sources.append(src)
        # Mark query executed
        for q in plan.queries:
            if q.query == query:
                q.executed = True
                q.result_count = len(results)

    def register_fetched_content(self, plan: ResearchPlan,
                                 url: str, content: str) -> int:
        """Register fetched page content; extract facts. Returns fact count."""
        src = next((s for s in plan.sources if s.url == url), None)
        if src is None:
            src = Source(url=url)
            self.scorer.classify(src)
            plan.sources.append(src)
        src.fetched = True
        src.fetched_at = time.time()
        src.content_hash = hashlib.sha256(
            content.encode(errors="ignore")).hexdigest()
        facts = self.extractor.extract(content, url)
        plan.facts.extend(facts)
        return len(facts)

    def finalize(self, plan: ResearchPlan) -> ResearchSummary:
        """Triangulate facts and synthesize the final summary."""
        plan.status = "completed"
        plan.completed_at = time.time()
        return self.synthesizer.synthesize(plan)

    def quick_research(self, question: str,
                       search_fn, fetch_fn,
                       max_queries: int = 4,
                       max_sources: int = 5) -> ResearchSummary:
        """End-to-end research using injected search/fetch functions.

        search_fn(query) -> List[Dict[title,url,snippet]]
        fetch_fn(url) -> str (page text)
        """
        plan = self.plan_research(question, max_queries)
        for q in plan.queries[:max_queries]:
            try:
                results = search_fn(q.query) or []
            except Exception:
                results = []
            self.register_search_results(plan, q.query, results)
        top = self.scorer.select_top(plan.sources, question, max_sources)
        for src in top:
            try:
                content = fetch_fn(src.url) or ""
            except Exception as e:
                src.fetch_error = str(e)[:200]
                continue
            if content:
                self.register_fetched_content(plan, src.url, content)
        return self.finalize(plan)


# ---------------------------------------------------------------------------
# Citation formatting
# ---------------------------------------------------------------------------

class CitationFormatter:
    """Formats sources as citations."""

    @staticmethod
    def format_markdown(sources: List[Source]) -> str:
        lines = []
        for i, s in enumerate(sources, 1):
            title = s.title or s.url
            lines.append(f"[{i}] [{title}]({s.url})")
        return "\n".join(lines)

    @staticmethod
    def format_inline(fact: Fact) -> str:
        if not fact.sources:
            return fact.statement
        cites = ", ".join(fact.sources[:2])
        return f"{fact.statement} [sources: {cites}]"

    @staticmethod
    def bibliography(summary: ResearchSummary) -> str:
        lines = ["## Sources"]
        for i, url in enumerate(summary.sources_used, 1):
            domain = urllib.parse.urlparse(url).netloc
            lines.append(f"{i}. {url} ({domain})")
        return "\n".join(lines)
