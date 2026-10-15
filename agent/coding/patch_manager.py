"""Patch manager — creates, validates, and applies code patches.

Per ChatGPT spec: agent/coding/patch_manager.py
Patches go through Git branches (never direct overwrite of running code).
No personality. No rules. Direct execution.
"""
from __future__ import annotations

import difflib
import hashlib
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class PatchStatus(Enum):
    DRAFT = "draft"
    VALIDATED = "validated"
    APPLIED = "applied"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"


@dataclass
class Patch:
    """A code patch with metadata."""
    id: str
    file: str
    old: str
    new: str
    reason: str
    status: PatchStatus = PatchStatus.DRAFT
    created_at: float = field(default_factory=time.time)
    applied_at: Optional[float] = None
    checksum: str = ""
    author: str = "agent"

    def __post_init__(self) -> None:
        if not self.checksum:
            self.checksum = self._compute_checksum()

    def _compute_checksum(self) -> str:
        h = hashlib.sha256()
        h.update(self.file.encode())
        h.update(b"\x00")
        h.update(self.old.encode())
        h.update(b"\x00")
        h.update(self.new.encode())
        return h.hexdigest()[:16]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "file": self.file,
            "old": self.old[:2000], "new": self.new[:2000],
            "reason": self.reason,
            "status": self.status.value,
            "created_at": self.created_at,
            "applied_at": self.applied_at,
            "checksum": self.checksum,
            "author": self.author,
        }

    @property
    def diff(self) -> str:
        return "\n".join(difflib.unified_diff(
            self.old.splitlines(), self.new.splitlines(),
            fromfile=f"a/{self.file}", tofile=f"b/{self.file}",
            lineterm="", n=3))


class PatchManager:
    """Manages patch lifecycle: create → validate → apply → rollback."""

    def __init__(self, repo_root: str = "."):
        self.repo_root = repo_root
        self._patches: Dict[str, Patch] = {}
        self._applied: List[str] = []  # patch ids in apply order

    def create(self, file: str, old: str, new: str,
               reason: str) -> Patch:
        """Create a new patch (DRAFT status)."""
        patch = Patch(
            id=f"patch_{uuid.uuid4().hex[:8]}",
            file=file, old=old, new=new, reason=reason)
        self._patches[patch.id] = patch
        return patch

    def create_from_diff(self, file: str, original: str, modified: str,
                         reason: str) -> Patch:
        """Create patch from full old/new file contents."""
        # Find the differing hunks and store minimal old/new
        sm = difflib.SequenceMatcher(None, original.splitlines(),
                                     modified.splitlines())
        old_lines: List[str] = []
        new_lines: List[str] = []
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag != "equal":
                old_lines.extend(original.splitlines()[i1:i2])
                new_lines.extend(modified.splitlines()[j1:j2])
        return self.create(
            file=file,
            old="\n".join(old_lines),
            new="\n".join(new_lines),
            reason=reason)

    def validate(self, patch: Patch) -> Dict[str, Any]:
        """Validate a patch: syntax check, old-text presence, safety."""
        import ast
        import os
        issues: List[str] = []
        path = os.path.join(self.repo_root, patch.file)
        # 1. File exists?
        if not os.path.exists(path):
            issues.append(f"file not found: {patch.file}")
            patch.status = PatchStatus.REJECTED
            return {"valid": False, "issues": issues}
        # 2. Old text present?
        with open(path, encoding="utf-8", errors="ignore") as f:
            content = f.read()
        if patch.old and patch.old not in content:
            issues.append("old text not found in file (stale patch?)")
        # 3. New code syntax (if Python file)
        if patch.file.endswith(".py"):
            new_content = content.replace(patch.old, patch.new, 1) \
                if patch.old else patch.new
            try:
                ast.parse(new_content)
            except SyntaxError as e:
                issues.append(f"syntax error in patched code: {e}")
        # 4. Safety: no denial patterns in new code
        from ..tools.permission_gate import check_tool_call
        gate = check_tool_call("file_write",
                               {"path": patch.file, "content": patch.new})
        if not gate.allowed:
            issues.append(f"patch blocked by policy: {gate.reason[:100]}")
        if issues:
            patch.status = PatchStatus.REJECTED
            return {"valid": False, "issues": issues}
        patch.status = PatchStatus.VALIDATED
        return {"valid": True, "issues": []}

    def apply(self, patch: Patch,
              validate_first: bool = True) -> Dict[str, Any]:
        """Apply a validated patch to the file."""
        import os
        if validate_first and patch.status != PatchStatus.VALIDATED:
            v = self.validate(patch)
            if not v["valid"]:
                return {"applied": False, **v}
        path = os.path.join(self.repo_root, patch.file)
        with open(path, encoding="utf-8", errors="ignore") as f:
            content = f.read()
        if patch.old:
            if patch.old not in content:
                return {"applied": False,
                        "issues": ["old text not found"]}
            new_content = content.replace(patch.old, patch.new, 1)
        else:
            new_content = patch.new
        # Atomic write
        tmp = path + f".patchtmp.{patch.id}"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(new_content)
        os.replace(tmp, path)
        patch.status = PatchStatus.APPLIED
        patch.applied_at = time.time()
        self._applied.append(patch.id)
        return {"applied": True, "patch_id": patch.id}

    def rollback(self, patch_id: str) -> Dict[str, Any]:
        """Reverse an applied patch."""
        import os
        patch = self._patches.get(patch_id)
        if not patch or patch.status != PatchStatus.APPLIED:
            return {"rolled_back": False,
                    "error": "patch not applied"}
        path = os.path.join(self.repo_root, patch.file)
        with open(path, encoding="utf-8", errors="ignore") as f:
            content = f.read()
        if patch.new not in content:
            return {"rolled_back": False,
                    "error": "patched text not found (file changed?)"}
        new_content = content.replace(patch.new, patch.old, 1)
        tmp = path + f".rollback.{patch.id}"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(new_content)
        os.replace(tmp, path)
        patch.status = PatchStatus.ROLLED_BACK
        if patch_id in self._applied:
            self._applied.remove(patch_id)
        return {"rolled_back": True, "patch_id": patch_id}

    def rollback_all(self) -> Dict[str, Any]:
        """Roll back all applied patches in reverse order."""
        results = []
        for pid in reversed(list(self._applied)):
            results.append(self.rollback(pid))
        return {"rolled_back": len([r for r in results
                                    if r.get("rolled_back")]),
                "details": results}

    def get(self, patch_id: str) -> Optional[Patch]:
        return self._patches.get(patch_id)

    def list(self, status: Optional[PatchStatus] = None) -> List[Patch]:
        patches = list(self._patches.values())
        if status:
            patches = [p for p in patches if p.status == status]
        return sorted(patches, key=lambda p: -p.created_at)
