"""
Self-improvement pipeline — controlled, measurable, reversible.
OBSERVE → HYPOTHESIZE → BASELINE → BRANCH → PATCH → TEST → BENCHMARK → REVIEW → MERGE/REJECT → ROLLBACK

Rules:
- NEVER modify running production code directly (always a Git branch).
- NEVER edit security policy, permission gate, or secrets handling without owner approval.
- Auto-reject patches that fail tests.
- Keep rollback path to last known-good.
"""
from __future__ import annotations
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Experiment:
    id: str
    hypothesis: str
    branch: str
    target_file: str
    status: str = "PLANNED"  # PLANNED, RUNNING, TESTING, PASSED, FAILED, MERGED, REJECTED, ROLLED_BACK
    test_results: list = field(default_factory=list)
    created_at: float = field(default_factory=time.time)


class ExperimentManager:
    """Manages isolated self-improvement experiments via Git branches."""

    # Files the agent may NEVER self-modify without explicit owner approval
    PROTECTED = {
        "agent/security/policy.py",
        "agent/tools/permission_gate.py",
        "agent/security/secrets.py",
    }

    def __init__(self, repo_root: Path):
        self.repo_root = Path(repo_root)
        self.experiments: dict[str, Experiment] = {}

    def _git(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=self.repo_root,
            capture_output=True, text=True, timeout=30,
        )

    def start(self, exp_id: str, hypothesis: str, target_file: str) -> Experiment:
        """Create an isolated branch for the experiment."""
        # Protected-file check
        if any(target_file.endswith(p) for p in self.PROTECTED):
            raise PermissionError(
                f"SELF-MODIFY BLOCKED: {target_file} is protected. "
                "Owner approval required."
            )
        branch = f"exp/{exp_id}"
        self._git("checkout", "-b", branch)
        exp = Experiment(id=exp_id, hypothesis=hypothesis,
                         branch=branch, target_file=target_file,
                         status="RUNNING")
        self.experiments[exp_id] = exp
        return exp

    def apply_patch(self, exp_id: str, old_text: str, new_text: str) -> bool:
        """Apply a patch to the target file within the experiment branch."""
        exp = self.experiments.get(exp_id)
        if not exp or exp.status != "RUNNING":
            return False
        path = self.repo_root / exp.target_file
        content = path.read_text()
        if old_text not in content:
            exp.status = "FAILED"
            return False
        path.write_text(content.replace(old_text, new_text, 1))
        return True

    def run_tests(self, exp_id: str, test_cmd: list[str] | None = None) -> dict:
        """Run tests for the experiment. Auto-reject on failure."""
        exp = self.experiments.get(exp_id)
        if not exp:
            return {"ok": False, "error": "unknown experiment"}
        exp.status = "TESTING"
        cmd = test_cmd or ["python3", "-m", "pytest", "agent/tests/", "-x", "-q"]
        try:
            proc = subprocess.run(cmd, cwd=self.repo_root,
                                  capture_output=True, text=True, timeout=300)
            ok = proc.returncode == 0
            exp.test_results.append({
                "cmd": " ".join(cmd),
                "ok": ok,
                "output": (proc.stdout + proc.stderr)[-2000:],
            })
            exp.status = "PASSED" if ok else "FAILED"
            return {"ok": ok, "output": exp.test_results[-1]["output"][:500]}
        except Exception as e:
            exp.status = "FAILED"
            return {"ok": False, "error": str(e)}

    def merge(self, exp_id: str) -> bool:
        """Merge a PASSED experiment to main."""
        exp = self.experiments.get(exp_id)
        if not exp or exp.status != "PASSED":
            return False
        self._git("checkout", "main")
        r = self._git("merge", "--no-ff", exp.branch, "-m", f"Self-improve: {exp.hypothesis[:60]}")
        if r.returncode == 0:
            exp.status = "MERGED"
            self._git("branch", "-d", exp.branch)
            return True
        exp.status = "FAILED"
        return False

    def reject(self, exp_id: str) -> None:
        """Reject and clean up a failed experiment."""
        exp = self.experiments.get(exp_id)
        if not exp:
            return
        self._git("checkout", "main")
        self._git("branch", "-D", exp.branch)
        exp.status = "REJECTED"

    def rollback(self, exp_id: str) -> bool:
        """Roll back a merged experiment."""
        exp = self.experiments.get(exp_id)
        if not exp or exp.status != "MERGED":
            return False
        r = self._git("revert", "--no-commit", "HEAD")
        if r.returncode == 0:
            self._git("commit", "-m", f"Rollback self-improve {exp_id}")
            exp.status = "ROLLED_BACK"
            return True
        self._git("revert", "--abort")
        return False
