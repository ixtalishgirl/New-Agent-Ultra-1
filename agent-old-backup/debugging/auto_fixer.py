"""Auto fixer — generates fixes for analyzed errors.

Takes ErrorAnalysis and produces concrete fix actions:
code patches, command corrections, retry strategies.
No personality. No rules. Direct execution.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from .error_analyzer import ErrorAnalysis, ErrorCategory


class FixAction(Enum):
    PATCH_CODE = "patch_code"          # apply a code patch
    INSTALL_PACKAGE = "install_package"  # pip install
    RETRY = "retry"                    # retry as-is
    RETRY_WITH_TIMEOUT = "retry_with_timeout"
    FIX_COMMAND = "fix_command"        # corrected shell command
    CREATE_FILE = "create_file"        # create missing file/dir
    SKIP = "skip"                      # cannot fix automatically
    ESCALATE = "escalate"              # needs user


@dataclass
class Fix:
    """A proposed fix for an error."""
    action: FixAction
    description: str
    # For PATCH_CODE:
    file: str = ""
    old: str = ""
    new: str = ""
    # For INSTALL_PACKAGE:
    package: str = ""
    # For FIX_COMMAND / RETRY:
    command: str = ""
    timeout: int = 60
    confidence: float = 0.5
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action.value,
            "description": self.description,
            "file": self.file,
            "old": self.old[:500] if self.old else "",
            "new": self.new[:500] if self.new else "",
            "package": self.package,
            "command": self.command,
            "timeout": self.timeout,
            "confidence": self.confidence,
        }


class AutoFixer:
    """Generates fixes from error analyses."""

    def fix(self, analysis: ErrorAnalysis,
            context: Optional[Dict[str, Any]] = None) -> List[Fix]:
        """Return ranked fix proposals."""
        context = context or {}
        fixes: List[Fix] = []
        cat = analysis.category

        if cat == ErrorCategory.POLICY_DENIED:
            fixes.append(Fix(
                action=FixAction.SKIP,
                description="Policy denial cannot be auto-fixed",
                confidence=1.0))
            return fixes

        if cat == ErrorCategory.IMPORT:
            pkg = self._extract_module(analysis.message)
            if pkg:
                fixes.append(Fix(
                    action=FixAction.INSTALL_PACKAGE,
                    description=f"Install missing package: {pkg}",
                    package=pkg, confidence=0.85))

        if cat == ErrorCategory.NAME:
            name = self._extract_quoted(analysis.message)
            if name and context.get("code"):
                patch = self._fix_undefined_name(
                    context["code"], name)
                if patch:
                    fixes.append(patch)

        if cat == ErrorCategory.SYNTAX:
            if context.get("code"):
                patch = self._fix_syntax(context["code"],
                                         analysis.message)
                if patch:
                    fixes.append(patch)

        if cat == ErrorCategory.FILE_NOT_FOUND:
            path = self._extract_quoted(analysis.message) or ""
            if path:
                fixes.append(Fix(
                    action=FixAction.CREATE_FILE,
                    description=f"Create missing path parent dirs: {path}",
                    file=path, confidence=0.6))

        if cat == ErrorCategory.TIMEOUT:
            fixes.append(Fix(
                action=FixAction.RETRY_WITH_TIMEOUT,
                description="Retry with doubled timeout",
                timeout=120, confidence=0.7))

        if cat == ErrorCategory.VALUE and "command not found" in analysis.message:
            cmd = analysis.message.split(":")[-1].strip()
            alt = self._suggest_alternative_command(cmd)
            if alt:
                fixes.append(Fix(
                    action=FixAction.FIX_COMMAND,
                    description=f"Try alternative command: {alt}",
                    command=alt, confidence=0.6))

        if cat == ErrorCategory.KEY:
            key = self._extract_quoted(analysis.message)
            if key and context.get("code"):
                patch = self._fix_key_error(context["code"], key)
                if patch:
                    fixes.append(patch)

        # Generic retry for retriable errors
        if analysis.retriable and not fixes:
            fixes.append(Fix(
                action=FixAction.RETRY,
                description="Retry once (error looks transient)",
                confidence=0.4))

        if not fixes:
            fixes.append(Fix(
                action=FixAction.ESCALATE,
                description=f"No auto-fix for: {analysis.message[:100]}",
                confidence=1.0))

        fixes.sort(key=lambda f: -f.confidence)
        return fixes

    # -- Fix generators ------------------------------------------------

    def _extract_module(self, message: str) -> Optional[str]:
        m = re.search(r"No module named '([^']+)'", message)
        if m:
            return m.group(1).split(".")[0]
        m = re.search(r"cannot import name '[^']+' from '([^']+)'", message)
        if m:
            return m.group(1).split(".")[0]
        return None

    def _extract_quoted(self, text: str) -> Optional[str]:
        m = re.search(r"'([^']+)'", text)
        return m.group(1) if m else None

    def _fix_undefined_name(self, code: str, name: str) -> Optional[Fix]:
        # Common missing imports
        common = {
            "json": "import json",
            "os": "import os",
            "sys": "import sys",
            "re": "import re",
            "time": "import time",
            "math": "import math",
            "random": "import random",
            "datetime": "from datetime import datetime",
            "Path": "from pathlib import Path",
            "requests": "import requests",
        }
        if name in common and common[name] not in code:
            return Fix(
                action=FixAction.PATCH_CODE,
                description=f"Add missing import for '{name}'",
                old="",
                new=common[name] + "\n",
                confidence=0.8,
                metadata={"insert_at_top": True, "name": name},
            )
        return None

    def _fix_syntax(self, code: str, message: str) -> Optional[Fix]:
        # Unmatched bracket heuristic
        pairs = {"(": ")", "[": "]", "{": "}"}
        stack: List[str] = []
        for ch in code:
            if ch in pairs:
                stack.append(ch)
            elif ch in pairs.values():
                if stack and pairs[stack[-1]] == ch:
                    stack.pop()
        if stack:
            missing = "".join(pairs[c] for c in reversed(stack))
            return Fix(
                action=FixAction.PATCH_CODE,
                description=f"Close unclosed brackets: add '{missing}'",
                old=code[-200:],
                new=code[-200:] + missing,
                confidence=0.5,
                metadata={"append": missing},
            )
        return None

    def _fix_key_error(self, code: str, key: str) -> Optional[Fix]:
        # Suggest .get() replacement for direct indexing
        pattern = re.compile(
            r"(\w+)\s*\[\s*['\"]" + re.escape(key) + r"['\"]\s*\]")
        m = pattern.search(code)
        if m:
            old = m.group(0)
            new = f"{m.group(1)}.get('{key}')"
            return Fix(
                action=FixAction.PATCH_CODE,
                description=f"Use .get() for key '{key}'",
                old=old, new=new, confidence=0.7)
        return None

    def _suggest_alternative_command(self, cmd: str) -> Optional[str]:
        alts = {
            "python": "python3",
            "pip": "pip3",
            "node": "nodejs",
        }
        return alts.get(cmd)


class FixApplier:
    """Applies fixes to code strings (no file I/O — caller handles that)."""

    def apply(self, code: str, fix: Fix) -> str:
        if fix.action == FixAction.PATCH_CODE:
            return self._apply_patch(code, fix)
        return code

    def _apply_patch(self, code: str, fix: Fix) -> str:
        if fix.metadata.get("insert_at_top"):
            # Insert import after existing imports / docstring
            lines = code.splitlines(keepends=True)
            idx = 0
            # Skip shebang, docstring, existing imports
            if lines and lines[0].startswith("#!"):
                idx = 1
            # Skip module docstring
            if idx < len(lines) and lines[idx].strip().startswith('"""'):
                for i in range(idx, len(lines)):
                    if '"""' in lines[i][3:]:
                        idx = i + 1
                        break
            while idx < len(lines):
                s = lines[idx].strip()
                if s.startswith("import ") or s.startswith("from "):
                    idx += 1
                elif not s or s.startswith("#"):
                    idx += 1
                else:
                    break
            lines.insert(idx, fix.new)
            return "".join(lines)
        if fix.metadata.get("append"):
            return code + fix.metadata["append"]
        if fix.old and fix.old in code:
            return code.replace(fix.old, fix.new, 1)
        return code

    def preview(self, code: str, fix: Fix) -> str:
        """Unified-diff style preview of a patch."""
        import difflib
        new_code = self.apply(code, fix)
        diff = difflib.unified_diff(
            code.splitlines(), new_code.splitlines(),
            lineterm="", n=3)
        return "\n".join(diff)
