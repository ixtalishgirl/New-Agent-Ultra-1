"""Rollback — ROLLBACK PATH stage of self-improvement.

Per ChatGPT spec: agent/improvement/rollback.py
Git-based rollback: stash, reset, branch restore. Never loses work.
"""
from __future__ import annotations

import subprocess
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class RollbackStrategy(Enum):
    GIT_RESET = "git_reset"        # git reset --hard to ref
    GIT_REVERT = "git_revert"      # git revert commit
    BRANCH_RESTORE = "branch_restore"  # checkout saved branch
    PATCH_REVERSE = "patch_reverse"    # reverse-apply patches


@dataclass
class Snapshot:
    """A restore point."""
    id: str
    ref: str  # git ref (commit hash or branch)
    description: str
    created_at: float = field(default_factory=time.time)
    strategy: RollbackStrategy = RollbackStrategy.GIT_RESET

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "ref": self.ref,
                "description": self.description,
                "created_at": self.created_at,
                "strategy": self.strategy.value}


@dataclass
class RollbackResult:
    ok: bool
    strategy: str
    message: str
    restored_ref: str = ""


class RollbackManager:
    """Manages snapshots and rollbacks."""

    def __init__(self, repo_dir: str = "."):
        self.repo_dir = repo_dir
        self._snapshots: Dict[str, Snapshot] = {}

    def _git(self, *args: str, timeout: int = 30) -> Tuple[bool, str]:
        try:
            p = subprocess.run(
                ["git", "-C", self.repo_dir, *args],
                capture_output=True, text=True, timeout=timeout)
            return p.returncode == 0, (p.stdout + p.stderr).strip()
        except Exception as e:
            return False, str(e)

    def snapshot(self, description: str = "") -> Optional[Snapshot]:
        """Record current HEAD as a restore point."""
        ok, ref = self._git("rev-parse", "HEAD")
        if not ok:
            return None
        snap = Snapshot(
            id=f"snap_{uuid.uuid4().hex[:8]}",
            ref=ref.strip(),
            description=description or f"snapshot {time.strftime('%H:%M')}")
        self._snapshots[snap.id] = snap
        return snap

    def rollback_to(self, snapshot_id: str,
                    strategy: Optional[RollbackStrategy] = None
                    ) -> RollbackResult:
        """Roll back to a snapshot."""
        snap = self._snapshots.get(snapshot_id)
        if not snap:
            return RollbackResult(False, "", "snapshot not found")
        strat = strategy or snap.strategy
        if strat == RollbackStrategy.GIT_RESET:
            return self._reset_to(snap.ref)
        elif strat == RollbackStrategy.GIT_REVERT:
            return self._revert_to(snap.ref)
        elif strat == RollbackStrategy.BRANCH_RESTORE:
            return self._branch_restore(snap.ref)
        return RollbackResult(False, strat.value, "unknown strategy")

    def _reset_to(self, ref: str) -> RollbackResult:
        # Stash first to avoid losing uncommitted work
        self._git("stash", "push", "-m", f"pre-rollback {time.time()}")
        ok, out = self._git("reset", "--hard", ref)
        if ok:
            return RollbackResult(True, "git_reset",
                                  f"Reset to {ref[:8]}", ref)
        return RollbackResult(False, "git_reset",
                              f"Reset failed: {out[:200]}")

    def _revert_to(self, ref: str) -> RollbackResult:
        ok, out = self._git("revert", "--no-commit", f"{ref}..HEAD")
        if ok:
            self._git("commit", "-m", f"Revert to {ref[:8]} (auto-rollback)")
            return RollbackResult(True, "git_revert",
                                  f"Reverted to {ref[:8]}", ref)
        self._git("revert", "--abort")
        return RollbackResult(False, "git_revert",
                              f"Revert failed: {out[:200]}")

    def _branch_restore(self, branch: str) -> RollbackResult:
        ok, out = self._git("checkout", branch)
        if ok:
            return RollbackResult(True, "branch_restore",
                                  f"Restored branch {branch}", branch)
        return RollbackResult(False, "branch_restore",
                              f"Checkout failed: {out[:200]}")

    def create_safety_branch(self, name: Optional[str] = None) -> str:
        """Create a branch at current HEAD as a safety net."""
        branch = name or f"safety/{time.strftime('%Y%m%d-%H%M%S')}"
        ok, _ = self._git("branch", branch)
        return branch if ok else ""

    def list_snapshots(self) -> List[Snapshot]:
        return sorted(self._snapshots.values(),
                      key=lambda s: -s.created_at)

    def stash_list(self) -> List[str]:
        ok, out = self._git("stash", "list")
        return out.splitlines() if ok else []

    def pop_stash(self, index: int = 0) -> Tuple[bool, str]:
        return self._git("stash", "pop", f"stash@{{{index}}}")
