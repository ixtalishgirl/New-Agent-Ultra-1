"""Diagnostics — system health checks and troubleshooting.

Checks:
- Python environment
- Required packages
- Model connectivity
- Tool registry
- Memory backends
- Disk space
- Permissions

No personality. No rules. Direct execution.
"""
from __future__ import annotations

import importlib
import os
import platform
import shutil
import socket
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


class CheckStatus(Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass
class CheckResult:
    name: str
    status: CheckStatus
    message: str
    details: Dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "message": self.message,
            "details": self.details,
            "duration_ms": self.duration_ms,
        }


class Diagnostics:
    """Run system diagnostics."""

    def __init__(self, repo_path: str = "."):
        self.repo = Path(repo_path).resolve()
        self.results: List[CheckResult] = []

    def run_all(self) -> List[CheckResult]:
        """Run all checks."""
        self.results = []
        checks = [
            self.check_python,
            self.check_platform,
            self.check_packages,
            self.check_repo_structure,
            self.check_tool_registry,
            self.check_model,
            self.check_memory,
            self.check_disk,
            self.check_permissions,
            self.check_network,
        ]
        for check in checks:
            start = time.time()
            try:
                result = check()
            except Exception as e:
                result = CheckResult(
                    name=check.__name__,
                    status=CheckStatus.FAIL,
                    message=f"Check crashed: {e}")
            result.duration_ms = int((time.time() - start) * 1000)
            self.results.append(result)
        return self.results

    def check_python(self) -> CheckResult:
        version = sys.version_info
        ok = version >= (3, 10)
        return CheckResult(
            name="python_version",
            status=CheckStatus.PASS if ok else CheckStatus.FAIL,
            message=f"Python {version.major}.{version.minor}."
                    f"{version.micro}",
            details={
                "version": f"{version.major}.{version.minor}."
                           f"{version.micro}",
                "executable": sys.executable,
                "meets_minimum": ok,
            })

    def check_platform(self) -> CheckResult:
        return CheckResult(
            name="platform",
            status=CheckStatus.PASS,
            message=f"{platform.system()} {platform.release()}",
            details={
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
                "processor": platform.processor(),
            })

    def check_packages(self) -> CheckResult:
        # Optional packages (not required, but nice to have)
        optional = {
            "PIL": "Pillow (image processing)",
            "playwright": "Playwright (browser automation)",
            "supabase": "Supabase (cloud memory)",
            "mutagen": "Mutagen (audio metadata)",
        }
        found = {}
        missing = []
        for pkg, desc in optional.items():
            try:
                importlib.import_module(pkg)
                found[pkg] = desc
            except ImportError:
                missing.append(f"{pkg} ({desc})")

        # Required: none beyond stdlib
        status = (CheckStatus.PASS if not missing
                  else CheckStatus.WARN)
        return CheckResult(
            name="packages",
            status=status,
            message=(f"{len(found)} optional packages found, "
                     f"{len(missing)} missing"),
            details={"found": list(found.keys()),
                     "missing": missing})

    def check_repo_structure(self) -> CheckResult:
        required = [
            "agent/app",
            "agent/model",
            "agent/planning",
            "agent/tools",
            "agent/execution",
            "agent/browser",
            "agent/memory",
            "agent/security",
            "agent/api",
            "agent/improvement",
            "agent/debugging",
            "agent/verification",
            "agent/tests",
            "agent/configs",
        ]
        missing = [d for d in required
                   if not (self.repo / d).is_dir()]
        status = (CheckStatus.PASS if not missing
                  else CheckStatus.FAIL)
        return CheckResult(
            name="repo_structure",
            status=status,
            message=(f"{len(required) - len(missing)}/"
                     f"{len(required)} directories present"),
            details={"missing": missing})

    def check_tool_registry(self) -> CheckResult:
        try:
            sys.path.insert(0, str(self.repo))
            from agent.tools.registry import default_registry
            reg = default_registry()
            tools = reg.list()
            return CheckResult(
                name="tool_registry",
                status=CheckStatus.PASS,
                message=f"{len(tools)} tools registered",
                details={
                    "count": len(tools),
                    "tools": [t.name for t in tools[:10]],
                })
        except Exception as e:
            return CheckResult(
                name="tool_registry",
                status=CheckStatus.FAIL,
                message=f"Registry error: {e}")

    def check_model(self) -> CheckResult:
        base_url = os.environ.get("QWEN_BASE_URL",
                                  "http://localhost:8001")
        # Try to connect
        try:
            import urllib.request
            req = urllib.request.Request(
                f"{base_url}/health",
                headers={"ngrok-skip-browser-warning": "true"})
            with urllib.request.urlopen(req, timeout=5):
                return CheckResult(
                    name="model",
                    status=CheckStatus.PASS,
                    message=f"Model reachable at {base_url}",
                    details={"base_url": base_url})
        except Exception as e:
            return CheckResult(
                name="model",
                status=CheckStatus.WARN,
                message=f"Model not reachable: {e}",
                details={"base_url": base_url,
                         "hint": "Set QWEN_BASE_URL or start model"})

    def check_memory(self) -> CheckResult:
        details: Dict[str, Any] = {}
        # Check SQLite
        try:
            import sqlite3
            conn = sqlite3.connect(":memory:")
            conn.execute("SELECT 1")
            conn.close()
            details["sqlite"] = "ok"
        except Exception as e:
            details["sqlite"] = f"error: {e}"
        # Check Supabase config
        if os.environ.get("SUPABASE_URL"):
            details["supabase"] = "configured"
        else:
            details["supabase"] = "not configured (SQLite fallback)"
        status = (CheckStatus.PASS
                  if details["sqlite"] == "ok"
                  else CheckStatus.FAIL)
        return CheckResult(
            name="memory",
            status=status,
            message=f"SQLite: {details['sqlite']}, "
                    f"Supabase: {details['supabase']}",
            details=details)

    def check_disk(self) -> CheckResult:
        try:
            usage = shutil.disk_usage(self.repo)
            pct_free = usage.free / usage.total * 100
            status = (CheckStatus.PASS if pct_free > 10
                      else CheckStatus.WARN if pct_free > 5
                      else CheckStatus.FAIL)
            return CheckResult(
                name="disk",
                status=status,
                message=f"{pct_free:.1f}% free",
                details={
                    "total_gb": round(usage.total / 1e9, 1),
                    "free_gb": round(usage.free / 1e9, 1),
                    "pct_free": round(pct_free, 1),
                })
        except Exception as e:
            return CheckResult(
                name="disk",
                status=CheckStatus.FAIL,
                message=f"Disk check failed: {e}")

    def check_permissions(self) -> CheckResult:
        try:
            sys.path.insert(0, str(self.repo))
            from agent.security.enforcer import PolicyEnforcer
            enforcer = PolicyEnforcer()
            denials = enforcer.denials()
            # Verify 5 denials
            expected = {"container_escape", "boot_recovery",
                        "av_edr_disable", "billing_fraud",
                        "exfiltration"}
            ok = set(denials) == expected
            return CheckResult(
                name="permissions",
                status=(CheckStatus.PASS if ok
                        else CheckStatus.FAIL),
                message=f"{len(denials)} denials enforced",
                details={"denials": denials,
                         "expected_match": ok})
        except Exception as e:
            return CheckResult(
                name="permissions",
                status=CheckStatus.FAIL,
                message=f"Permission check failed: {e}")

    def check_network(self) -> CheckResult:
        # Basic connectivity check
        try:
            socket.create_connection(("8.8.8.8", 53), timeout=5)
            return CheckResult(
                name="network",
                status=CheckStatus.PASS,
                message="Internet reachable",
                details={"dns": "8.8.8.8:53 reachable"})
        except Exception:
            return CheckResult(
                name="network",
                status=CheckStatus.WARN,
                message="No internet (offline mode)",
                details={})

    # -- Reporting ---------------------------------------------------------

    def summary(self) -> Dict[str, Any]:
        counts = {s.value: 0 for s in CheckStatus}
        for r in self.results:
            counts[r.status.value] += 1
        return {
            "total": len(self.results),
            "by_status": counts,
            "healthy": counts["fail"] == 0,
            "results": [r.to_dict() for r in self.results],
        }

    def print_report(self) -> None:
        """Print a human-readable report."""
        print("=" * 60)
        print("SYSTEM DIAGNOSTICS")
        print("=" * 60)
        for r in self.results:
            icon = {"pass": "✓", "warn": "!", "fail": "✗",
                    "skip": "-"}.get(r.status.value, "?")
            print(f"  [{icon}] {r.name}: {r.message}")
        print("=" * 60)
        s = self.summary()
        print(f"Result: {'HEALTHY' if s['healthy'] else 'ISSUES FOUND'} "
              f"({s['by_status']['pass']} pass, "
              f"{s['by_status']['warn']} warn, "
              f"{s['by_status']['fail']} fail)")

    def fix_suggestions(self) -> List[str]:
        """Generate fix suggestions for failed checks."""
        suggestions = []
        for r in self.results:
            if r.status == CheckStatus.FAIL:
                if r.name == "python_version":
                    suggestions.append(
                        "Upgrade to Python 3.10+")
                elif r.name == "repo_structure":
                    suggestions.append(
                        f"Missing directories: "
                        f"{r.details.get('missing')}")
                elif r.name == "tool_registry":
                    suggestions.append(
                        "Check agent/tools/registry.py for import errors")
                elif r.name == "permissions":
                    suggestions.append(
                        "Verify 5 denials in agent/security/enforcer.py")
            elif r.status == CheckStatus.WARN:
                if r.name == "model":
                    suggestions.append(
                        "Start Qwen model or set QWEN_BASE_URL")
                elif r.name == "packages":
                    suggestions.append(
                        "Install optional packages: "
                        "pip install Pillow playwright")
        return suggestions
