"""Proof collection for the agent control loop.

Every claim the agent makes about the world should be backed by evidence.
The ProofCollector gathers that evidence (command outputs, file snapshots,
timestamps, hashes), chains each item to the previous one with a SHA256
chain hash (tamper-evident chain of custody), and exports verifiable proof
bundles as JSON.

Chain of custody model:
    evidence[i].chain_hash = sha256(evidence[i-1].chain_hash + evidence[i].hash)

Any modification, deletion, or reordering of an item breaks the chain and
is detected by verify_chain().

Classes:
    EvidenceType   kinds of evidence
    Evidence       one item of evidence (id, type, content, hash, timestamp, source)
    ProofBundle    an exportable, self-verifying set of evidence for one claim/task
    ProofCollector orchestrator: collect, chain, export, import, verify
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


class EvidenceType(str, Enum):
    COMMAND_OUTPUT = "command_output"   # stdout/stderr/exit code of a tool call
    FILE_SNAPSHOT = "file_snapshot"     # content snapshot of a file
    FILE_HASH = "file_hash"             # hash of a file on disk (content not stored)
    SCREENSHOT = "screenshot"           # base64 image bytes
    TIMESTAMP = "timestamp"             # a signed time marker
    NETWORK_RESPONSE = "network_response"  # HTTP response (status/headers/body excerpt)
    TEXT_NOTE = "text_note"             # analyst/agent note
    DIFF = "diff"                       # diff between two states
    TEST_RESULT = "test_result"         # structured test outcome


# Binary-ish evidence types whose content is stored base64-encoded.
_BINARY_TYPES = {EvidenceType.SCREENSHOT}

MAX_INLINE_BYTES = 1_000_000  # evidence content larger than this is hashed, not stored


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8", "replace"))


def utc_now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


@dataclass
class Evidence:
    """One item of evidence in the chain of custody."""
    id: str
    type: EvidenceType
    content: str                      # text, or base64 for binary types
    hash: str                         # sha256 of the canonical content bytes
    timestamp: str                    # ISO-8601 UTC
    source: str                       # what produced it, e.g. "shell:ls -la"
    chain_hash: str = ""              # sha256(prev_chain_hash + hash); "" for genesis
    label: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    content_truncated: bool = False   # True if content was too large to store inline

    def to_dict(self, include_content: bool = True) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "id": self.id,
            "type": self.type.value,
            "hash": self.hash,
            "timestamp": self.timestamp,
            "source": self.source,
            "chain_hash": self.chain_hash,
            "label": self.label,
            "metadata": self.metadata,
            "content_truncated": self.content_truncated,
        }
        if include_content:
            d["content"] = self.content
        else:
            d["content_bytes"] = len(self.content.encode("utf-8", "replace"))
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Evidence":
        return cls(
            id=data["id"],
            type=EvidenceType(data["type"]),
            content=data.get("content", ""),
            hash=data["hash"],
            timestamp=data["timestamp"],
            source=data["source"],
            chain_hash=data.get("chain_hash", ""),
            label=data.get("label", ""),
            metadata=data.get("metadata", {}),
            content_truncated=data.get("content_truncated", False),
        )

    def content_bytes(self) -> bytes:
        """Canonical bytes the hash was computed over."""
        if self.type in _BINARY_TYPES:
            try:
                return base64.b64decode(self.content, validate=True)
            except Exception:
                return self.content.encode("utf-8", "replace")
        return self.content.encode("utf-8", "replace")

    def verify_hash(self) -> bool:
        """Recompute the content hash; False means tampered/truncated."""
        if self.content_truncated:
            return False  # cannot verify without the bytes; hash is a commitment only
        return sha256_bytes(self.content_bytes()) == self.hash


@dataclass
class ProofBundle:
    """An exportable, self-verifying bundle of evidence for one claim/task."""
    bundle_id: str
    claim: str
    evidence: List[Evidence] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)
    created_by: str = "agent"
    bundle_hash: str = ""  # sha256 over the ordered evidence chain hashes

    def __post_init__(self) -> None:
        if not self.bundle_hash and self.evidence:
            self.bundle_hash = self.compute_bundle_hash()

    def compute_bundle_hash(self) -> str:
        joined = "|".join(e.chain_hash or e.hash for e in self.evidence)
        return sha256_text(f"{self.bundle_id}:{self.claim}:{joined}")

    def seal(self) -> "ProofBundle":
        """Finalize the bundle: recompute the bundle hash."""
        self.bundle_hash = self.compute_bundle_hash()
        return self

    def to_dict(self, include_content: bool = True) -> Dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "claim": self.claim,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "bundle_hash": self.bundle_hash,
            "evidence_count": len(self.evidence),
            "evidence": [e.to_dict(include_content=include_content) for e in self.evidence],
        }

    def to_json(self, indent: int = 2, include_content: bool = True) -> str:
        return json.dumps(self.to_dict(include_content=include_content),
                          indent=indent, default=str)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProofBundle":
        return cls(
            bundle_id=data["bundle_id"],
            claim=data["claim"],
            evidence=[Evidence.from_dict(e) for e in data.get("evidence", [])],
            created_at=data.get("created_at", utc_now_iso()),
            created_by=data.get("created_by", "agent"),
            bundle_hash=data.get("bundle_hash", ""),
        )

    @classmethod
    def from_json(cls, text: str) -> "ProofBundle":
        return cls.from_dict(json.loads(text))

    def verify(self) -> Dict[str, Any]:
        """Full self-verification: chain integrity + content hashes + bundle hash.

        Returns {"ok": bool, "checks": [...], "failed": [...]}.
        """
        checks: List[Dict[str, Any]] = []
        failed: List[str] = []

        def record(name: str, ok: bool, detail: str = "") -> None:
            checks.append({"check": name, "ok": ok, "detail": detail})
            if not ok:
                failed.append(name)

        # 1. chain linkage
        prev = ""
        for i, e in enumerate(self.evidence):
            expected = sha256_text(prev + e.hash)
            if i == 0:
                # genesis item: chain_hash must equal sha256("" + hash)
                ok = e.chain_hash == expected
                record(f"chain_link[{i}]", ok,
                       "" if ok else f"genesis chain hash mismatch for {e.id}")
            else:
                ok = e.chain_hash == expected
                record(f"chain_link[{i}]", ok,
                       "" if ok else f"chain broken at {e.id}: expected {expected[:16]}...")
            prev = e.chain_hash or e.hash

        # 2. content hashes
        for e in self.evidence:
            if e.content_truncated:
                record(f"content_hash[{e.id}]", True,
                       "truncated content: hash is a commitment, not verifiable inline")
                continue
            ok = e.verify_hash()
            record(f"content_hash[{e.id}]", ok,
                   "" if ok else f"content hash mismatch for {e.id}")

        # 3. bundle hash
        if self.bundle_hash:
            expected_bundle = self.compute_bundle_hash()
            record("bundle_hash", expected_bundle == self.bundle_hash,
                   "" if expected_bundle == self.bundle_hash
                   else "bundle hash mismatch: evidence set was modified")
        else:
            record("bundle_hash", False, "bundle not sealed (no bundle hash)")

        # 4. timestamp ordering (non-decreasing)
        try:
            stamps = [datetime.fromisoformat(e.timestamp.replace("Z", "+00:00"))
                      for e in self.evidence]
            ordered = all(b >= a for a, b in zip(stamps, stamps[1:]))
            record("timestamp_order", ordered,
                   "" if ordered else "timestamps out of order: possible backdating")
        except Exception as exc:
            record("timestamp_order", False, f"unparseable timestamps: {exc}")

        return {"ok": len(failed) == 0, "checks": checks, "failed": failed,
                "evidence_count": len(self.evidence)}

# ---------------------------------------------------------------------------
# ProofCollector
# ---------------------------------------------------------------------------

class ProofCollector:
    """Collects evidence with a tamper-evident hash chain.

    Usage:
        pc = ProofCollector()
        pc.collect_command_output("shell", "ls -la", stdout, stderr, 0)
        pc.collect_file_snapshot("/path/to/file.py")
        bundle = pc.bundle("claim: the file exists and is valid python")
        bundle.seal()
        report = bundle.verify()   # {"ok": True, ...}
        pc.export_bundle(bundle, "/path/to/proof.json")
    """

    GENESIS_CHAIN = ""  # chain_hash input for the first item

    def __init__(self, store_dir: Optional[Union[str, Path]] = None) -> None:
        self._evidence: List[Evidence] = []
        self._last_chain_hash = self.GENESIS_CHAIN
        self.store_dir = Path(store_dir) if store_dir else None
        if self.store_dir:
            self.store_dir.mkdir(parents=True, exist_ok=True)

    # -- core ----------------------------------------------------------------
    def _chain(self, content_hash: str) -> str:
        chained = sha256_text(self._last_chain_hash + content_hash)
        self._last_chain_hash = chained
        return chained

    def add(self, type: EvidenceType, content: Union[str, bytes],
            source: str, label: str = "",
            metadata: Optional[Dict[str, Any]] = None) -> Evidence:
        """Add arbitrary evidence; returns the chained Evidence item."""
        if isinstance(content, bytes):
            if type in _BINARY_TYPES:
                stored = base64.b64encode(content).decode("ascii")
                raw = content
            else:
                stored = content.decode("utf-8", "replace")
                raw = stored.encode("utf-8", "replace")
        else:
            stored = content
            raw = content.encode("utf-8", "replace")

        truncated = False
        if len(raw) > MAX_INLINE_BYTES:
            # store only the hash commitment, not the bytes
            stored = ""
            truncated = True

        content_hash = sha256_bytes(raw)
        evidence = Evidence(
            id=f"ev-{uuid.uuid4().hex[:12]}",
            type=type,
            content=stored,
            hash=content_hash,
            timestamp=utc_now_iso(),
            source=source,
            chain_hash=self._chain(content_hash),
            label=label,
            metadata=metadata or {},
            content_truncated=truncated,
        )
        self._evidence.append(evidence)
        return evidence

    # -- typed collectors ------------------------------------------------------
    def collect_command_output(self, tool_name: str, command: str,
                               stdout: str, stderr: str = "",
                               exit_code: int = 0,
                               label: str = "") -> Evidence:
        body = (f"$ {command}\n--- exit: {exit_code} ---\n"
                f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}")
        return self.add(EvidenceType.COMMAND_OUTPUT, body,
                        source=f"{tool_name}:{command[:120]}",
                        label=label or f"output of {command[:60]}",
                        metadata={"tool": tool_name, "command": command,
                                  "exit_code": exit_code,
                                  "stdout_chars": len(stdout),
                                  "stderr_chars": len(stderr)})

    def collect_file_snapshot(self, path: Union[str, Path],
                              label: str = "") -> Evidence:
        p = Path(path)
        try:
            data = p.read_bytes()
            is_text = True
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("utf-8", "replace")
                is_text = False
        except OSError as exc:
            return self.add(EvidenceType.TEXT_NOTE,
                            f"file snapshot FAILED for {p}: {exc}",
                            source=f"file_snapshot:{p}",
                            label=label or f"missing file {p}",
                            metadata={"path": str(p), "error": str(exc)})
        stat = p.stat()
        return self.add(EvidenceType.FILE_SNAPSHOT, text,
                        source=f"file_snapshot:{p}",
                        label=label or f"snapshot of {p}",
                        metadata={"path": str(p), "size": stat.st_size,
                                  "mtime": stat.st_mtime, "is_text": is_text})

    def collect_file_hash(self, path: Union[str, Path],
                          label: str = "") -> Evidence:
        """Hash a file without storing its content (for large/secrets files)."""
        p = Path(path)
        try:
            h = hashlib.sha256()
            size = 0
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
                    size += len(chunk)
            digest = h.hexdigest()
            body = f"sha256({p}) = {digest}\nsize = {size} bytes"
            meta = {"path": str(p), "sha256": digest, "size": size}
        except OSError as exc:
            body = f"hash FAILED for {p}: {exc}"
            meta = {"path": str(p), "error": str(exc)}
        return self.add(EvidenceType.FILE_HASH, body,
                        source=f"file_hash:{p}",
                        label=label or f"hash of {p}",
                        metadata=meta)

    def collect_screenshot(self, image_bytes: bytes, source: str = "browser",
                           label: str = "") -> Evidence:
        return self.add(EvidenceType.SCREENSHOT, image_bytes, source=source,
                        label=label or "screenshot",
                        metadata={"bytes": len(image_bytes)})

    def collect_timestamp(self, label: str = "time marker",
                          note: str = "") -> Evidence:
        now = time.time()
        body = f"{utc_now_iso()}\nepoch: {now}\nnote: {note}"
        return self.add(EvidenceType.TIMESTAMP, body, source="clock",
                        label=label, metadata={"epoch": now})

    def collect_network_response(self, url: str, status_code: int,
                                 headers: Optional[Dict[str, str]] = None,
                                 body_excerpt: str = "",
                                 label: str = "") -> Evidence:
        header_block = "\n".join(f"{k}: {v}" for k, v in (headers or {}).items())
        content = (f"URL: {url}\nstatus: {status_code}\n"
                   f"--- headers ---\n{header_block}\n"
                   f"--- body excerpt ---\n{body_excerpt[:4000]}")
        return self.add(EvidenceType.NETWORK_RESPONSE, content,
                        source=f"http:{url[:120]}",
                        label=label or f"HTTP {status_code} {url[:60]}",
                        metadata={"url": url, "status_code": status_code})

    def collect_note(self, note: str, source: str = "agent",
                     label: str = "") -> Evidence:
        return self.add(EvidenceType.TEXT_NOTE, note, source=source,
                        label=label or "note")

    def collect_diff(self, label_a: str, content_a: str,
                     label_b: str, content_b: str,
                     label: str = "") -> Evidence:
        import difflib as _difflib
        diff = "\n".join(_difflib.unified_diff(
            content_a.splitlines(), content_b.splitlines(),
            fromfile=f"a/{label_a}", tofile=f"b/{label_b}", lineterm=""))
        return self.add(EvidenceType.DIFF, diff or "(no differences)",
                        source=f"diff:{label_a}->{label_b}",
                        label=label or f"diff {label_a} -> {label_b}")

    def collect_test_result(self, suite: str, passed: bool,
                            details: Dict[str, Any],
                            label: str = "") -> Evidence:
        body = json.dumps({"suite": suite, "passed": passed,
                           "details": details}, indent=2, default=str)
        return self.add(EvidenceType.TEST_RESULT, body,
                        source=f"tests:{suite}",
                        label=label or f"test result: {suite}",
                        metadata={"suite": suite, "passed": passed})

    # -- bundling --------------------------------------------------------------
    def bundle(self, claim: str, evidence: Optional[List[Evidence]] = None,
               created_by: str = "agent") -> ProofBundle:
        """Create a sealed bundle from collected (or supplied) evidence."""
        items = list(evidence) if evidence is not None else list(self._evidence)
        bundle = ProofBundle(bundle_id=f"pb-{uuid.uuid4().hex[:12]}",
                             claim=claim, evidence=items,
                             created_by=created_by)
        return bundle.seal()

    def bundle_since(self, evidence_id: str, claim: str,
                     created_by: str = "agent") -> ProofBundle:
        """Bundle all evidence collected after a given evidence id."""
        items: List[Evidence] = []
        collecting = False
        for e in self._evidence:
            if collecting:
                items.append(e)
            if e.id == evidence_id:
                collecting = True
        return ProofBundle(bundle_id=f"pb-{uuid.uuid4().hex[:12]}",
                           claim=claim, evidence=items,
                           created_by=created_by).seal()

    def clear(self) -> None:
        """Reset the collector (evidence already bundled is unaffected)."""
        self._evidence = []
        self._last_chain_hash = self.GENESIS_CHAIN

    @property
    def evidence(self) -> List[Evidence]:
        return list(self._evidence)

    def __len__(self) -> int:
        return len(self._evidence)

    # -- persistence -----------------------------------------------------------
    def export_bundle(self, bundle: ProofBundle,
                      path: Union[str, Path],
                      include_content: bool = True) -> Path:
        """Write a bundle to disk as JSON. Returns the path."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(bundle.to_json(include_content=include_content),
                     encoding="utf-8")
        return p

    def import_bundle(self, path: Union[str, Path]) -> ProofBundle:
        """Load a bundle from disk."""
        return ProofBundle.from_json(Path(path).read_text(encoding="utf-8"))

    def export_chain(self, path: Union[str, Path]) -> Path:
        """Export the raw evidence chain (all collected evidence)."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "exported_at": utc_now_iso(),
            "evidence_count": len(self._evidence),
            "tip_chain_hash": self._last_chain_hash,
            "evidence": [e.to_dict() for e in self._evidence],
        }
        p.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        return p

    def verify_chain(self) -> Dict[str, Any]:
        """Verify the in-progress chain without bundling."""
        bundle = ProofBundle(bundle_id="chain-check", claim="chain check",
                             evidence=list(self._evidence))
        # don't require a sealed bundle hash for a raw chain check
        result = bundle.verify()
        result["checks"] = [c for c in result["checks"]
                            if c["check"] != "bundle_hash"]
        result["failed"] = [f for f in result["failed"] if f != "bundle_hash"]
        result["ok"] = len(result["failed"]) == 0
        return result

    # -- tamper detection demo helper -------------------------------------------
    @staticmethod
    def tamper_report(bundle: ProofBundle) -> Dict[str, Any]:
        """Human-readable tamper report for a bundle."""
        result = bundle.verify()
        if result["ok"]:
            return {"tampered": False,
                    "summary": f"bundle {bundle.bundle_id}: all {result['evidence_count']} "
                               f"evidence items verified, chain intact"}
        lines = []
        for check in result["checks"]:
            if not check["ok"]:
                lines.append(f"- {check['check']}: {check['detail']}")
        return {"tampered": True,
                "summary": f"bundle {bundle.bundle_id}: TAMPER DETECTED",
                "failures": lines}

# ---------------------------------------------------------------------------
# Merkle tree over evidence hashes (for large bundles)
# ---------------------------------------------------------------------------

def merkle_root(hashes: List[str]) -> str:
    """Compute a Merkle root over hex hash strings."""
    if not hashes:
        return sha256_text("empty")
    level = [h.lower() for h in hashes]
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        nxt = []
        for i in range(0, len(level), 2):
            nxt.append(sha256_text(level[i] + level[i + 1]))
        level = nxt
    return level[0]


def merkle_proof(hashes: List[str], index: int) -> Dict[str, Any]:
    """Return a Merkle inclusion proof for hashes[index]."""
    if not hashes or not (0 <= index < len(hashes)):
        raise ValueError("index out of range")
    level = [h.lower() for h in hashes]
    idx = index
    proof: List[Dict[str, str]] = []
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        sibling = idx + 1 if idx % 2 == 0 else idx - 1
        proof.append({"sibling": level[sibling],
                      "position": "right" if idx % 2 == 0 else "left"})
        nxt = []
        for i in range(0, len(level), 2):
            nxt.append(sha256_text(level[i] + level[i + 1]))
        level = nxt
        idx //= 2
    return {"leaf": hashes[index].lower(), "proof": proof, "root": level[0]}


def verify_merkle_proof(leaf: str, proof: List[Dict[str, str]], root: str) -> bool:
    """Verify a Merkle inclusion proof."""
    current = leaf.lower()
    for step in proof:
        sib = step["sibling"]
        if step["position"] == "right":
            current = sha256_text(current + sib)
        else:
            current = sha256_text(sib + current)
    return current == root.lower()


# ---------------------------------------------------------------------------
# Evidence query helpers
# ---------------------------------------------------------------------------

class EvidenceQuery:
    """Filter/search helpers over a list of Evidence items."""

    def __init__(self, evidence: List[Evidence]) -> None:
        self._evidence = list(evidence)

    def by_type(self, type: EvidenceType) -> List[Evidence]:
        return [e for e in self._evidence if e.type == type]

    def by_source(self, substring: str) -> List[Evidence]:
        sub = substring.lower()
        return [e for e in self._evidence if sub in e.source.lower()]

    def by_label(self, substring: str) -> List[Evidence]:
        sub = substring.lower()
        return [e for e in self._evidence if sub in e.label.lower()]

    def since(self, timestamp_iso: str) -> List[Evidence]:
        cutoff = datetime.fromisoformat(timestamp_iso.replace("Z", "+00:00"))
        out = []
        for e in self._evidence:
            try:
                ts = datetime.fromisoformat(e.timestamp.replace("Z", "+00:00"))
                if ts >= cutoff:
                    out.append(e)
            except ValueError:
                continue
        return out

    def search_content(self, pattern: str, case_insensitive: bool = True,
                       max_hits: int = 50) -> List[Evidence]:
        import re as _re
        flags = _re.IGNORECASE if case_insensitive else 0
        rx = _re.compile(pattern, flags)
        hits = []
        for e in self._evidence:
            if e.content_truncated:
                continue
            if rx.search(e.content):
                hits.append(e)
                if len(hits) >= max_hits:
                    break
        return hits

    def timeline(self) -> List[Dict[str, Any]]:
        """Chronological summary of the evidence."""
        return [{"id": e.id, "timestamp": e.timestamp,
                 "type": e.type.value, "label": e.label,
                 "source": e.source} for e in self._evidence]

    def stats(self) -> Dict[str, Any]:
        by_type: Dict[str, int] = {}
        for e in self._evidence:
            by_type[e.type.value] = by_type.get(e.type.value, 0) + 1
        total_bytes = sum(len(e.content.encode("utf-8", "replace"))
                          for e in self._evidence)
        return {"count": len(self._evidence), "by_type": by_type,
                "total_content_bytes": total_bytes,
                "chain_tip": self._evidence[-1].chain_hash if self._evidence else None}


# ---------------------------------------------------------------------------
# Redaction: keep the hash commitment, drop sensitive bytes
# ---------------------------------------------------------------------------

def redact_evidence(evidence: Evidence,
                    patterns: Optional[List[str]] = None) -> Evidence:
    """Return a redacted copy: sensitive spans replaced, hash preserved.

    The original hash is kept as a commitment; the redacted copy is marked
    so verify_hash() semantics stay honest (content no longer matches hash).
    """
    import re as _re
    text = evidence.content
    default_patterns = [
        r"(?i)(api[_-]?key\s*[:=]\s*)([^\s'\"]+)",
        r"(?i)(password\s*[:=]\s*)([^\s'\"]+)",
        r"(?i)(token\s*[:=]\s*)([^\s'\"]+)",
        r"(?i)(secret\s*[:=]\s*)([^\s'\"]+)",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    ]
    for pat in (patterns or default_patterns):
        text = _re.sub(pat, lambda m: m.group(0)[:0] + "[REDACTED]"
                       if m.lastindex in (None, 0) else m.group(1) + "[REDACTED]",
                       text)
    # simpler robust pass: replace any remaining long secret-looking tokens
    return Evidence(
        id=evidence.id, type=evidence.type, content=text,
        hash=evidence.hash, timestamp=evidence.timestamp,
        source=evidence.source, chain_hash=evidence.chain_hash,
        label=evidence.label,
        metadata={**evidence.metadata, "redacted": True,
                  "original_hash_commitment": evidence.hash},
        content_truncated=True,  # hash no longer verifiable inline by design
    )


# ---------------------------------------------------------------------------
# Persistent chain-of-custody log (append-only, JSONL)
# ---------------------------------------------------------------------------

class ChainOfCustodyLog:
    """Append-only JSONL log of evidence with per-line chain hashes.

    Each line: {"seq": n, "evidence": {...}, "prev": <prev line hash>,
                "line_hash": sha256(prev + canonical(evidence))}.
    verify() replays the file and reports the first broken line.
    """

    def __init__(self, path: Union[str, Path]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = self._count_lines()

    def _count_lines(self) -> int:
        if not self.path.exists():
            return 0
        n = 0
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    n += 1
        return n

    def _tip_hash(self) -> str:
        if self._seq == 0:
            return ""
        with open(self.path, "r", encoding="utf-8") as fh:
            last = None
            for line in fh:
                if line.strip():
                    last = line
        if last is None:
            return ""
        return json.loads(last).get("line_hash", "")

    def append(self, evidence: Evidence) -> Dict[str, Any]:
        prev = self._tip_hash()
        canonical = json.dumps(evidence.to_dict(), sort_keys=True, default=str)
        line_hash = sha256_text(prev + canonical)
        record = {"seq": self._seq, "prev": prev,
                  "evidence": evidence.to_dict(), "line_hash": line_hash}
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        self._seq += 1
        return record

    def verify(self) -> Dict[str, Any]:
        prev = ""
        checked = 0
        if not self.path.exists():
            return {"ok": True, "checked": 0, "failed_at": None}
        with open(self.path, "r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    return {"ok": False, "checked": checked,
                            "failed_at": lineno, "reason": "unparseable line"}
                if record.get("prev") != prev:
                    return {"ok": False, "checked": checked,
                            "failed_at": lineno, "reason": "prev-hash mismatch"}
                canonical = json.dumps(record["evidence"], sort_keys=True, default=str)
                if sha256_text(prev + canonical) != record.get("line_hash"):
                    return {"ok": False, "checked": checked,
                            "failed_at": lineno, "reason": "line hash mismatch"}
                prev = record["line_hash"]
                checked += 1
        return {"ok": True, "checked": checked, "failed_at": None}

    def read_all(self) -> List[Evidence]:
        out = []
        if not self.path.exists():
            return out
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    out.append(Evidence.from_dict(json.loads(line)["evidence"]))
        return out


# ---------------------------------------------------------------------------
# Extra typed collectors (environment / process / directory state)
# ---------------------------------------------------------------------------

def collect_environment_snapshot(collector: "ProofCollector",
                                 redact: bool = True) -> Evidence:
    """Snapshot relevant environment facts (NOT secret values)."""
    safe_vars = {k: v for k, v in os.environ.items()
                 if not any(s in k.upper() for s in
                            ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH"))}
    body = json.dumps({
        "captured_at": utc_now_iso(),
        "env_var_names": sorted(safe_vars.keys()),
        "env_var_count": len(safe_vars),
        "path": safe_vars.get("PATH", "")[:500],
        "cwd": os.getcwd(),
        "pid": os.getpid(),
    }, indent=2)
    note = "secret-valued vars listed by name only" if redact else ""
    return collector.add(EvidenceType.TEXT_NOTE, body + ("\n" + note if note else ""),
                         source="environment",
                         label="environment snapshot",
                         metadata={"redacted": redact})


def collect_directory_listing(collector: "ProofCollector",
                              path: Union[str, Path],
                              label: str = "") -> Evidence:
    """Snapshot a directory listing (names + sizes + mtimes, no contents)."""
    p = Path(path)
    entries = []
    try:
        for child in sorted(p.iterdir(), key=lambda c: c.name):
            try:
                st = child.stat()
                entries.append({"name": child.name,
                                "is_dir": child.is_dir(),
                                "size": st.st_size,
                                "mtime": st.st_mtime})
            except OSError as exc:
                entries.append({"name": child.name, "error": str(exc)})
        body = json.dumps({"path": str(p), "entries": entries}, indent=2, default=str)
        meta: Dict[str, Any] = {"path": str(p), "count": len(entries)}
    except OSError as exc:
        body = f"listing FAILED for {p}: {exc}"
        meta = {"path": str(p), "error": str(exc)}
    return collector.add(EvidenceType.FILE_SNAPSHOT, body,
                         source=f"dir_listing:{p}",
                         label=label or f"listing of {p}",
                         metadata=meta)

# ---------------------------------------------------------------------------
# Witness co-signing: independent parties attest to a bundle hash
# ---------------------------------------------------------------------------

@dataclass
class WitnessSignature:
    """A third party's attestation that they saw a bundle hash at a time."""
    witness: str
    bundle_id: str
    bundle_hash: str
    signed_at: str = field(default_factory=utc_now_iso)
    statement: str = ""
    signature: str = ""  # sha256(witness + bundle_hash + signed_at + statement)

    def __post_init__(self) -> None:
        if not self.signature:
            self.signature = sha256_text(
                f"{self.witness}|{self.bundle_hash}|{self.signed_at}|{self.statement}")

    def verify(self, bundle: "ProofBundle") -> bool:
        expected = sha256_text(
            f"{self.witness}|{self.bundle_hash}|{self.signed_at}|{self.statement}")
        return (self.signature == expected
                and self.bundle_hash == bundle.bundle_hash
                and self.bundle_id == bundle.bundle_id)

    def to_dict(self) -> Dict[str, Any]:
        return {"witness": self.witness, "bundle_id": self.bundle_id,
                "bundle_hash": self.bundle_hash, "signed_at": self.signed_at,
                "statement": self.statement, "signature": self.signature}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WitnessSignature":
        sig = cls(witness=data["witness"], bundle_id=data["bundle_id"],
                  bundle_hash=data["bundle_hash"],
                  signed_at=data.get("signed_at", utc_now_iso()),
                  statement=data.get("statement", ""))
        sig.signature = data.get("signature", sig.signature)
        return sig


def cosign_bundle(bundle: ProofBundle, witness: str,
                  statement: str = "") -> WitnessSignature:
    """Create a witness signature for a sealed bundle."""
    if not bundle.bundle_hash:
        raise ValueError("bundle must be sealed before co-signing")
    return WitnessSignature(witness=witness, bundle_id=bundle.bundle_id,
                            bundle_hash=bundle.bundle_hash, statement=statement)


def verify_cosignatures(bundle: ProofBundle,
                        signatures: List[WitnessSignature]) -> Dict[str, Any]:
    results = [{"witness": s.witness, "ok": s.verify(bundle),
                "signed_at": s.signed_at} for s in signatures]
    return {"all_valid": all(r["ok"] for r in results),
            "count": len(results),
            "valid_count": sum(1 for r in results if r["ok"]),
            "results": results}


# ---------------------------------------------------------------------------
# Bundle comparison: what changed between two bundles of the same claim
# ---------------------------------------------------------------------------

def compare_bundles(old: ProofBundle, new: ProofBundle) -> Dict[str, Any]:
    """Compare two bundles (e.g. before/after a fix) by evidence id."""
    old_ids = {e.id for e in old.evidence}
    new_ids = {e.id for e in new.evidence}
    added = [e.to_dict(include_content=False) for e in new.evidence if e.id not in old_ids]
    removed = [e.to_dict(include_content=False) for e in old.evidence if e.id not in new_ids]
    changed = []
    for e in new.evidence:
        if e.id in old_ids:
            prev = next(x for x in old.evidence if x.id == e.id)
            if prev.hash != e.hash:
                changed.append({"id": e.id, "label": e.label,
                                "old_hash": prev.hash[:16], "new_hash": e.hash[:16]})
    return {
        "old_bundle": old.bundle_id, "new_bundle": new.bundle_id,
        "same_claim": old.claim == new.claim,
        "added": added, "removed": removed, "changed": changed,
        "old_count": len(old.evidence), "new_count": len(new.evidence),
    }
